"""Practice workflow. Every operation resolves the current user's scoped store."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import time
import traceback
from typing import Any, Literal
import zipfile
from pathlib import Path

from defusedxml.ElementTree import ParseError
from fastapi import APIRouter, File, Form, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field

from deeptutor.services.practice.answers import check_answer
from deeptutor.services.practice.importing import MAX_BYTES, preview
from deeptutor.services.practice.document_import import (
    DOCUMENT_EXTENSIONS, MAX_DOCUMENT_BYTES, parse_document,
)
from deeptutor.services.practice.scheduler import Rating, day_bounds
from deeptutor.services.practice.storage import PracticeStore, ReviewConflict
from deeptutor.services.session import get_sqlite_session_store

from .question_notebook import NotebookEntryItem, _course_session_ids

router = APIRouter()
logger = logging.getLogger(__name__)
_document_tasks: dict[str, asyncio.Task[None]] = {}
DOCUMENT_JOB_TIMEOUT = 300
_document_slots = asyncio.Semaphore(2)


class PracticeSummary(BaseModel):
    total: int
    mistakes: int
    due: int
    overdue: int
    reviewed_today: int
    next_due_at: float | None
    day_end: float
    timezone: str
    unavailable_workspaces: list[str] = Field(default_factory=list)


class PracticeQuestion(BaseModel):
    entry: NotebookEntryItem
    state: dict
    content_workspace_id: str = ""
    content_workspace_name: str = ""


class PracticeCounts(BaseModel):
    questions: int
    mistakes: int
    reviews: int


class PracticeDailyCounts(PracticeCounts):
    date: str


class PracticeSourceCounts(PracticeCounts):
    source: str


class PracticeAnalytics(BaseModel):
    timezone: str
    days: int
    start_date: str
    end_date: str
    updated_at: float
    daily: list[PracticeDailyCounts]
    sources: list[PracticeSourceCounts]
    totals: PracticeCounts


class AnswerRequest(BaseModel):
    answer: str = Field(default="", max_length=20000)


class ReviewRequest(AnswerRequest):
    self_report: bool = False
    request_id: str = Field(min_length=16, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    version: int = Field(ge=0)
    rating: Rating


class CommitRequest(BaseModel):
    token: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]+$")


class DocumentDraftUpdate(BaseModel):
    revision: int = Field(ge=0)
    items: list[dict[str, Any]] = Field(max_length=500)
    target: Literal["bank", "mistakes"]


class DocumentImportMode(BaseModel):
    mode: Literal["local_script"] = "local_script"


def _validate_timezone(timezone: str) -> tuple[float, float]:
    try:
        return day_bounds(timezone, time.time())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


async def _entry(entry_id: int) -> dict:
    store = get_sqlite_session_store()
    if not await asyncio.to_thread(PracticeStore(store.db_path).is_visible, entry_id):
        raise HTTPException(404, "Question not found")
    entry = await store.get_notebook_entry(entry_id)
    if entry is None:
        raise HTTPException(404, "Question not found")
    return entry


@router.get("/summary", response_model=PracticeSummary)
async def summary(timezone: str = "UTC", course_id: str = "", all_workspaces: bool = False):
    if all_workspaces and not course_id:
        from deeptutor.services.workspace.navigation import read_workspace_indexes

        _, end = _validate_timezone(timezone)

        async def read():
            return [await summary(timezone, course_id)]

        rows, unavailable = await read_workspace_indexes(read)
        return {
            **{
                key: sum(row[key] for row in rows)
                for key in ("total", "mistakes", "due", "overdue", "reviewed_today")
            },
            "next_due_at": min(
                (row["next_due_at"] for row in rows if row["next_due_at"]), default=None
            ),
            "day_end": end,
            "timezone": timezone,
            "unavailable_workspaces": unavailable,
        }

    store = get_sqlite_session_store()
    sessions = await _course_session_ids(store, course_id)
    try:
        return await asyncio.to_thread(PracticeStore(store.db_path).overview, timezone, sessions)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/queue", response_model=list[PracticeQuestion])
async def queue(
    timezone: str = "UTC",
    course_id: str = "",
    category_id: int | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    all_workspaces: bool = False,
):
    if all_workspaces and not course_id:
        from deeptutor.services.workspace.navigation import read_workspace_indexes

        _validate_timezone(timezone)

        async def read():
            return await queue(timezone, course_id, category_id, limit)

        rows, _ = await read_workspace_indexes(read)
        rows.sort(
            key=lambda row: (
                row["state"].get("due_at") or row["entry"].get("created_at", 0),
                row["content_workspace_id"],
                row["entry"]["id"],
            )
        )
        return rows[:limit]

    store = get_sqlite_session_store()
    practice = PracticeStore(store.db_path)
    sessions = await _course_session_ids(store, course_id)
    try:
        ids = await asyncio.to_thread(practice.queue, timezone, sessions, category_id, limit)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    from deeptutor.services.workspace.context import current_workspace_id

    questions = []
    for entry_id in ids:
        entry = await store.get_notebook_entry(entry_id)
        if entry:
            questions.append(
                {
                    "entry": entry,
                    "state": await asyncio.to_thread(practice.state, entry_id),
                    "content_workspace_id": current_workspace_id(),
                }
            )
    return questions


@router.get("/analytics", response_model=PracticeAnalytics)
async def practice_analytics(
    timezone: str = "UTC", days: int = 30, course_id: str = "", all_workspaces: bool = False
):
    _validate_timezone(timezone)
    if days not in {7, 30, 90}:
        raise HTTPException(422, "Days must be 7, 30, or 90")
    if all_workspaces and not course_id:
        from deeptutor.services.workspace.navigation import read_workspace_indexes

        async def read():
            return [await practice_analytics(timezone, days, course_id)]

        rows, _ = await read_workspace_indexes(read)
        if not rows:
            return await practice_analytics(timezone, days, course_id)
        result = dict(rows[0])
        for bucket, identity in (("daily", "date"), ("sources", "source")):
            combined = {}
            for row in rows:
                for item in row[bucket]:
                    target = combined.setdefault(
                        item[identity],
                        {identity: item[identity], "questions": 0, "mistakes": 0, "reviews": 0},
                    )
                    for metric in ("questions", "mistakes", "reviews"):
                        target[metric] += item[metric]
            result[bucket] = list(combined.values())
        result["totals"] = {
            metric: sum(row["totals"][metric] for row in rows)
            for metric in ("questions", "mistakes", "reviews")
        }
        return result

    store = get_sqlite_session_store()
    sessions = await _course_session_ids(store, course_id)
    try:
        return await asyncio.to_thread(
            PracticeStore(store.db_path).analytics, timezone, days, sessions
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/questions/{entry_id}", response_model=PracticeQuestion)
async def practice_question(entry_id: int):
    entry = await _entry(entry_id)
    state = await asyncio.to_thread(
        PracticeStore(get_sqlite_session_store().db_path).state, entry_id
    )
    return {"entry": entry, "state": state}


@router.post("/questions/{entry_id}/check")
async def check(entry_id: int, payload: AnswerRequest):
    entry = await _entry(entry_id)
    return {"correct": check_answer(entry, payload.answer)}


@router.post("/questions/{entry_id}/review")
async def review(entry_id: int, payload: ReviewRequest):
    try:
        return await asyncio.to_thread(
            PracticeStore(get_sqlite_session_store().db_path).review,
            entry_id,
            payload.request_id,
            payload.version,
            payload.rating,
            payload.answer,
            self_report=payload.self_report,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ReviewConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/import/template")
async def import_template(format: Literal["csv", "xlsx"] = "csv"):
    rows = [
        [
            "question",
            "question_type",
            "A",
            "B",
            "C",
            "D",
            "correct_answer",
            "explanation",
            "tags",
            "user_answer",
        ],
        ["2 + 2 = ?", "single_choice", "3", "4", "5", "6", "B", "2 + 2 = 4", "Math,Addition", ""],
        [
            "Select the prime numbers",
            "multi_choice",
            "2",
            "3",
            "4",
            "6",
            "A,B",
            "2 and 3 are prime",
            "Math",
            "",
        ],
        ["The Earth orbits the Sun", "true_false", "", "", "", "", "true", "", "Science", ""],
        [
            "Explain photosynthesis",
            "short_answer",
            "",
            "",
            "",
            "",
            "Plants convert light into chemical energy.",
            "",
            "Biology",
            "",
        ],
    ]
    if format == "xlsx":
        from openpyxl import Workbook

        book = Workbook()
        for row in rows:
            book.active.append(row)
        output = io.BytesIO()
        book.save(output)
        book.close()
        data = output.getvalue()
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        output = io.StringIO(newline="")
        csv.writer(output).writerows(rows)
        data, media = output.getvalue().encode("utf-8-sig"), "text/csv"
    return Response(
        data,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="practice-template.{format}"'},
    )


@router.post("/import/preview")
async def import_preview(
    file: UploadFile = File(...),
    target: Literal["bank", "mistakes"] = Form("bank"),
    course_id: str = Form("", max_length=200),
):
    await _course_session_ids(get_sqlite_session_store(), course_id)
    try:
        data = await file.read(MAX_BYTES + 1)
        parsed = await asyncio.to_thread(preview, data, file.filename or "")
    except (ValueError, UnicodeError, OSError, zipfile.BadZipFile, KeyError, ParseError) as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        await file.close()
    token = None
    if not parsed["errors"]:
        token = await asyncio.to_thread(
            PracticeStore(get_sqlite_session_store().db_path).stage_import,
            file.filename or "",
            target,
            parsed["questions"],
            course_id,
        )
    return {
        "token": token,
        "total": len(parsed["questions"]) + len(parsed["errors"]),
        "valid": len(parsed["questions"]),
        "errors": parsed["errors"],
        "samples": parsed["questions"][:5],
    }


@router.get("/import/document/mode", response_model=DocumentImportMode)
async def get_document_import_mode():
    return DocumentImportMode()


@router.post("/import/document/preview")
async def import_document_preview(
    file: UploadFile = File(...),
    target: Literal["bank", "mistakes"] = Form("bank"),
    course_id: str = Form("", max_length=200),
):
    await _course_session_ids(get_sqlite_session_store(), course_id)
    filename = Path(file.filename or "").name
    if Path(filename).suffix.lower() not in DOCUMENT_EXTENSIONS:
        raise HTTPException(422, "Supported documents: .pdf, .doc, .docx, .md and .txt")
    try:
        data = await file.read(MAX_DOCUMENT_BYTES + 1)
    finally:
        await file.close()
    if not data or len(data) > MAX_DOCUMENT_BYTES:
        raise HTTPException(422, "Choose a non-empty document up to 10 MB")
    try:
        items = await parse_document(data, filename)
    except (ValueError, UnicodeError, OSError, zipfile.BadZipFile) as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        logger.exception("Local document question extraction failed")
        raise HTTPException(503, "Local document parsing failed; check the server log for details") from exc
    media_type = "application/pdf" if filename.lower().endswith(".pdf") else (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if filename.lower().endswith(".docx") else "application/octet-stream"
    )
    return await asyncio.to_thread(
        _stage_document_preview, filename, target, items, data, media_type, course_id,
    )


def _stage_document_preview(
    filename: str, target: str, items: list[dict], data: bytes,
    media_type: str, course_id: str,
) -> dict:
    repo = PracticeStore(get_sqlite_session_store().db_path)
    token = repo.stage_document(filename, target, items, data, media_type, course_id)
    return repo.get_document(token)


async def _run_document_import_job(
    repo: PracticeStore, token: str, data: bytes, filename: str,
) -> None:
    try:
        async def progress(event: dict) -> None:
            await asyncio.to_thread(repo.set_document_progress, token, event)

        async with _document_slots:
            if not await asyncio.to_thread(repo.start_document_job, token):
                return
            items = await asyncio.wait_for(
                parse_document(data, filename, progress=progress), timeout=DOCUMENT_JOB_TIMEOUT,
            )
            await asyncio.to_thread(repo.finish_document_job, token, items)
    except asyncio.CancelledError:
        await asyncio.to_thread(
            repo.finish_document_job, token, [], "Parsing interrupted; retry this draft",
        )
        raise
    except ValueError as exc:
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        await asyncio.to_thread(repo.finish_document_job, token, [], detail)
    except TimeoutError as exc:
        detail = (
            f"Document import timed out after exceeding the {DOCUMENT_JOB_TIMEOUT}s job timeout.\n"
            + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        )
        await asyncio.to_thread(
            repo.finish_document_job, token, [],
            detail,
        )
    except Exception as exc:
        logger.exception("Background document question extraction failed")
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        await asyncio.to_thread(
            repo.finish_document_job, token, [], detail,
        )
    finally:
        _document_tasks.pop(token, None)


@router.post("/import/document/start")
async def start_import_document(
    file: UploadFile = File(...),
    target: Literal["bank", "mistakes"] = Form("bank"),
    course_id: str = Form("", max_length=200),
):
    await _course_session_ids(get_sqlite_session_store(), course_id)
    filename = Path(file.filename or "").name
    if Path(filename).suffix.lower() not in DOCUMENT_EXTENSIONS:
        raise HTTPException(422, "Supported documents: .pdf, .doc, .docx, .md and .txt")
    try:
        data = await file.read(MAX_DOCUMENT_BYTES + 1)
    finally:
        await file.close()
    if not data or len(data) > MAX_DOCUMENT_BYTES:
        raise HTTPException(422, "Choose a non-empty document up to 10 MB")
    media_type = "application/pdf" if filename.lower().endswith(".pdf") else "application/octet-stream"
    repo = PracticeStore(get_sqlite_session_store().db_path)
    token = await asyncio.to_thread(
        repo.stage_document_job, filename, target, data, media_type, course_id,
    )
    task = asyncio.create_task(_run_document_import_job(repo, token, data, filename))
    _document_tasks[token] = task
    return await asyncio.to_thread(repo.get_document, token)


@router.get("/import/document/jobs")
async def list_import_document_jobs(course_id: str = Query("", max_length=200)):
    await _course_session_ids(get_sqlite_session_store(), course_id)
    return await asyncio.to_thread(
        PracticeStore(get_sqlite_session_store().db_path).list_document_jobs, course_id,
    )


@router.delete("/import/document/{token}")
async def cancel_import_document(token: str):
    repo = PracticeStore(get_sqlite_session_store().db_path)
    try:
        deleted_status = await asyncio.to_thread(repo.delete_document_job, token)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if deleted_status in {"queued", "processing"}:
        task = _document_tasks.get(token)
        if task:
            task.cancel()
    return {"status": "deleted"}


@router.post("/import/document/{token}/retry")
async def retry_import_document(token: str):
    repo = PracticeStore(get_sqlite_session_store().db_path)
    try:
        filename, data = await asyncio.to_thread(repo.retry_document_job, token)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(410, str(exc)) from exc
    task = asyncio.create_task(_run_document_import_job(repo, token, data, filename))
    _document_tasks[token] = task
    return await asyncio.to_thread(repo.get_document, token)


@router.get("/import/document/{token}")
async def get_import_document(token: str):
    try:
        return await asyncio.to_thread(
            PracticeStore(get_sqlite_session_store().db_path).get_document, token,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(410, str(exc)) from exc


@router.patch("/import/document/{token}")
async def update_import_document(token: str, payload: DocumentDraftUpdate):
    try:
        return await asyncio.to_thread(
            PracticeStore(get_sqlite_session_store().db_path).update_document,
            token, payload.revision, payload.items, payload.target,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ReviewConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/import/document/{token}/source")
async def get_import_document_source(token: str):
    try:
        media_type, source = await asyncio.to_thread(
            PracticeStore(get_sqlite_session_store().db_path).document_source, token,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(source, media_type=media_type, headers={
        "Content-Disposition": "inline", "X-Content-Type-Options": "nosniff",
    })


@router.post("/import/commit")
async def import_commit(payload: CommitRequest):
    try:
        return await asyncio.to_thread(
            PracticeStore(get_sqlite_session_store().db_path).commit_import, payload.token
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
