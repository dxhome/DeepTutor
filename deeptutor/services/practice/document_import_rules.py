"""Deterministic, local parsing for exam documents.

This first-stage importer extracts text and uses explicit numbering/section
rules. It never sends document content to an inference model.
"""

from __future__ import annotations

import io
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections.abc import Awaitable, Callable

from .importing import MAX_ROWS, normalize_question

DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".md", ".txt"}
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_DOCUMENT_PAGES = 30
MAX_DOCUMENT_CHARS = 120_000

_ANSWER_SECTION = re.compile(
    r"(?:参考答案(?:与解析|及解析|及评分标准)?|答案与解析|答案及解析|答案解析|标准答案)"
)
_SECTION_TYPE = re.compile(
    r"^[一二三四五六七八九十百]+\s*[、.．]\s*([^：:。]+)"
)
_QUESTION_START = re.compile(r"^\s*(\d{1,3})\s*[.．、]\s*(.*)$")
_ANSWER_START = re.compile(
    r"^\s*(\d{1,3})(?:\s*[.．、]\s*(?=(?:（\s*\d+\s*分|\(\s*\d+\s*分|【\s*(?:答\s*案|解\s*析|小\s*问)|$))"
    r"|(?=\s*【\s*(?:答\s*案|解\s*析|小\s*问)))"
)
_ANSWER_LABEL = re.compile(r"【\s*答\s*案\s*】")
_EXPLANATION_LABEL = re.compile(r"【\s*解\s*析\s*】|【\s*点\s*评\s*】")
_INLINE_ANSWER = re.compile(r"(?:故\s*答案为|答案为|正确答案是?)\s*[：:]\s*(.+)")
_OPTION_START = re.compile(r"(?<![A-Za-z])([A-H])\s*[.．、:：)）]\s*")
_BLANK = re.compile(r"_{2,}|（\s*）|\(\s*\)|□{1,}|\.{3,}|…{2,}")


def _clean_extracted_text(text: str) -> str:
    cleaned = []
    for line in text.splitlines():
        if any(ord(char) < 32 and char not in "\t" for char in line):
            continue
        if (
            line.strip() in {"/g0", "/×0", "/x0"}
            or re.match(r"^\s*数学试题\s*第\s*\d+页", line)
        ):
            continue
        cleaned.append(line.rstrip())
    return "\n".join(cleaned)


def _read_pdf_pages(data: bytes) -> list[str]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    if not data.startswith(b"%PDF-"):
        raise ValueError("This file is not a valid PDF")
    try:
        reader = PdfReader(io.BytesIO(data))
    except PdfReadError as exc:
        raise ValueError("Could not read this PDF") from exc
    if reader.is_encrypted:
        raise ValueError("Encrypted PDFs cannot be imported")
    if len(reader.pages) > MAX_DOCUMENT_PAGES:
        raise ValueError(f"Import at most {MAX_DOCUMENT_PAGES} pages at a time")
    return [_clean_extracted_text(page.extract_text() or "") for page in reader.pages]


def _read_document_pages(data: bytes, suffix: str) -> list[str]:
    if suffix == ".pdf":
        return _read_pdf_pages(data)
    if suffix == ".docx":
        from deeptutor.co_writer.docx_converter import DocxConversionError, docx_to_markdown

        try:
            markdown = docx_to_markdown(data)
            # The converter escapes Markdown punctuation; restore it for the
            # editor-facing question text while preserving LaTeX commands.
            markdown = re.sub(r"\\([*_{}\[\]()#+.!-])", r"\1", markdown)
            return [markdown]
        except DocxConversionError as exc:
            raise ValueError(str(exc)) from exc
    if suffix == ".doc":
        office = shutil.which("libreoffice") or shutil.which("soffice")
        if not office:
            raise ValueError("Legacy .doc conversion is unavailable; save as .docx and retry")
        with tempfile.TemporaryDirectory(prefix="deeptutor-import-") as directory:
            source = Path(directory) / "source.doc"
            source.write_bytes(data)
            profile = (Path(directory) / "profile").as_uri()
            try:
                completed = subprocess.run(
                    [office, f"-env:UserInstallation={profile}", "--headless", "--convert-to", "docx", "--outdir", directory, str(source)],
                    capture_output=True, timeout=30, check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ValueError("Word conversion timed out; save as .docx and retry") from exc
            converted = Path(directory) / "source.docx"
            if completed.returncode or not converted.exists():
                raise ValueError("Could not convert this .doc file; save as .docx and retry")
            return _read_document_pages(converted.read_bytes(), ".docx")
    try:
        return [data.decode("utf-8-sig")]
    except UnicodeDecodeError:
        return [data.decode("gb18030")]


def _section_kind(line: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", line).strip()
    normalized = re.sub(r"^#{1,6}\s*", "", normalized)
    match = _SECTION_TYPE.match(normalized)
    if not match:
        return None
    title = match.group(1)
    if "填空" in title:
        return "fill_blank"
    if "多选" in title:
        return "multi_choice"
    if "单选" in title or "选择" in title:
        return "single_choice"
    if "判断" in title:
        return "true_false"
    if "解答" in title or "证明" in title:
        return "short_answer"
    return None


def _split_document(pages: list[str]) -> tuple[list[tuple[int, str]], list[tuple[int, str]], bool]:
    """Split source lines at the first answer-section heading, retaining pages."""
    questions: list[tuple[int, str]] = []
    answers: list[tuple[int, str]] = []
    in_answers = False
    found_answer_section = False
    for page_number, page in enumerate(pages, 1):
        for line in page.splitlines():
            if not in_answers and _ANSWER_SECTION.search(line):
                in_answers = True
                found_answer_section = True
                # A page can contain the end of the last question immediately
                # before a standalone answer heading. Do not discard text on
                # the same line when the heading is embedded in a document title.
                before, _separator, after = line.partition(_ANSWER_SECTION.search(line).group(0))
                if before.strip():
                    questions.append((page_number, before.strip()))
                if after.strip():
                    answers.append((page_number, after.strip()))
                continue
            (answers if in_answers else questions).append((page_number, line.rstrip()))
    return questions, answers, found_answer_section


def _find_question_start(line: str) -> tuple[int, str] | None:
    line = re.sub(r"^\s*#{1,6}\s*", "", line)
    match = _QUESTION_START.match(line)
    if not match:
        return None
    number = int(match.group(1))
    if number < 1 or number > MAX_ROWS:
        return None
    return number, match.group(2)


def _question_blocks(lines: list[tuple[int, str]]) -> tuple[list[dict], set[int]]:
    # Numbered exam instructions often precede the actual test. Starting after
    # the first recognized section heading avoids importing those as questions.
    first_section = next((i for i, (_, line) in enumerate(lines) if _section_kind(line)), None)
    if first_section is not None:
        lines = lines[first_section:]

    blocks: list[dict] = []
    section_ranges: list[tuple[int, int, str]] = []
    next_section_number = 1
    for _page, line in lines:
        kind = _section_kind(line)
        if not kind:
            continue
        normalized = unicodedata.normalize("NFKC", line)
        count_match = re.search(r"(?:共|共有)\s*(\d+)\s*(?:个小题|小题|题|道)", normalized)
        if count_match:
            count = int(count_match.group(1))
            if 0 < count <= 100:
                section_ranges.append((next_section_number, next_section_number + count - 1, kind))
                next_section_number += count
    current: dict | None = None
    current_kind: str | None = None
    loose_option_lines: list[str] = []
    gaps: set[int] = set()
    last_number = 0
    for page, line in lines:
        kind = _section_kind(line)
        if kind:
            # Section headings close the preceding question. Keeping the
            # heading's instructions on that question is especially harmful
            # when two sections share one page.
            if current:
                blocks.append(current)
                current = None
            current_kind = kind
            continue
        start = _find_question_start(line)
        if start:
            number, remainder = start
            # Accept monotonically increasing numbers; gaps are retained and
            # surfaced to the user rather than silently dropping later items.
            if number == 1 or last_number < number <= last_number + 2:
                if current:
                    blocks.append(current)
                    if number > int(current["number"]) + 1:
                        gaps.update(range(int(current["number"]) + 1, number))
                current = {
                    "number": str(number), "page": page, "kind": current_kind,
                    "lines": [],
                }
                last_number = number
                if remainder:
                    current["lines"].append(remainder)
                continue
        if current:
            if line.strip() in {"–", "—", "-"} or re.match(r"^数学试题\s*第\s*\d+页", line.strip()):
                continue
            current["lines"].append(line)
        elif _extract_options(line)[1]:
            loose_option_lines.append(line)
    if current:
        blocks.append(current)
    if section_ranges:
        for block in blocks:
            number = int(block["number"])
            block["kind"] = next(
                (kind for start, end, kind in section_ranges if start <= number <= end),
                block.get("kind"),
            )
    option_blocks = [_extract_options(line)[1] for line in loose_option_lines]
    missing_option_blocks = [
        block for block in blocks
        if block.get("kind") in {"single_choice", "multi_choice"}
        and not _extract_options("\n".join(block["lines"]))[1]
    ]
    for block, options in zip(missing_option_blocks, option_blocks):
        block["loose_options"] = options
    return blocks, gaps


def _answer_blocks(
    lines: list[tuple[int, str]], question_numbers: set[str],
) -> dict[str, dict[str, object]]:
    found: dict[str, dict[str, object]] = {}
    current_number: str | None = None
    for page, line in lines:
        if _section_kind(line):
            # Compact answer tables are commonly separated from worked
            # solutions by the original exam's section headings. Do not
            # attach that table or the next section heading to the preceding
            # solution when its own answer marker is absent.
            current_number = None
            continue
        match = _ANSWER_START.match(line)
        number = str(int(match.group(1))) if match else None
        if current_number is None and _ANSWER_LABEL.match(line.strip()):
            first_question = min((int(value) for value in question_numbers), default=0)
            if first_question:
                current_number = str(first_question)
                found.setdefault(current_number, {"parts": [], "pages": set()})
                found[current_number]["parts"].append(line)
                found[current_number]["pages"].add(page)
            continue
        if number in question_numbers and (
            current_number is None
            or number == current_number
            or int(number) > int(current_number)
        ):
            current_number = number
            found.setdefault(number, {"parts": [], "pages": set()})
            remainder = line[match.end():].strip()
            remainder = re.sub(r"^[.．、]\s*", "", remainder)
            if remainder:
                found[number]["parts"].append(remainder)
                found[number]["pages"].add(page)
            continue
        if current_number:
            found[current_number]["parts"].append(line)
            found[current_number]["pages"].add(page)
    return {
        number: {
            "text": "\n".join(part for part in value["parts"] if part.strip()).strip(),
            "pages": sorted(value["pages"]),
        }
        for number, value in found.items()
    }


def _source_excerpt(
    block: dict,
    question: str,
    options: dict[str, str],
    answer: dict[str, object] | None,
    correct_answer: str,
    explanation: str,
) -> str:
    # Show the extracted candidate fields, rather than a whole source page
    # that may contain neighboring questions or out-of-order option columns.
    page_label = str(block["page"])
    question_text = question.strip()
    sections = [f"题目原文（第 {page_label} 页）:\n{question_text}"]
    if options:
        sections.append("选项:\n" + "\n".join(f"{key}. {value}" for key, value in options.items()))
    if correct_answer:
        answer_pages = ", ".join(map(str, answer.get("pages", []))) or "未知"
        sections.append(f"答案提取（答案区第 {answer_pages} 页）:\n{correct_answer}")
    if explanation:
        sections.append(f"解析提取:\n{explanation}")
    return "\n\n".join(sections)[:2000]


def _has_suspicious_math_linebreak(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    math_fragment = re.compile(r"[A-Za-z0-9][A-Za-z0-9(){}]*\s*[+−\-*/=^<>≤≥∈∩]")
    return any(
        math_fragment.search(left) and math_fragment.search(right)
        for left, right in zip(lines, lines[1:])
    )


def _all_option_groups(lines: list[tuple[int, str]]) -> list[dict[str, str]]:
    """Collect A/B/C… option groups even when PDF text order moves them."""
    groups: list[dict[str, str]] = []
    current: dict[str, str] = {}
    last_letter = ""
    for _page, line in lines:
        if not re.match(r"^\s*[A-H]\s*[.．、:：)）]\s*", line):
            continue
        matches = list(_OPTION_START.finditer(line))
        for index, match in enumerate(matches):
            letter = match.group(1)
            if letter == "A" and current:
                groups.append(current)
                current = {}
                last_letter = ""
            if current and letter <= last_letter:
                continue
            end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
            value = line[match.end():end].strip()
            if value:
                current[letter] = (current.get(letter, "") + " " + value).strip()
                last_letter = letter
    if current:
        groups.append(current)
    return [group for group in groups if len(group) >= 2 and "A" in group and "B" in group]


def _strip_option_tail(text: str) -> str:
    match = _OPTION_START.search(text)
    if not match or len(list(_OPTION_START.finditer(text))) < 2:
        return text.strip()
    return text[:match.start()].strip()


def _extract_answer(segment: str) -> tuple[str, str]:
    answer_match = _ANSWER_LABEL.search(segment)
    explanation_match = _EXPLANATION_LABEL.search(segment)
    if answer_match:
        start = answer_match.end()
        end = explanation_match.start() if explanation_match and explanation_match.start() > start else len(segment)
        answer_text = segment[start:end].strip()
    else:
        inline = _INLINE_ANSWER.search(segment)
        if inline:
            answer_text = inline.group(1).strip()
        elif re.search(r"【\s*小\s*问", segment):
            answer_text = segment.strip()
        else:
            answer_text = ""
    explanation = segment[explanation_match.end():].strip() if explanation_match else ""
    if answer_match:
        inline = re.search(
            r"(?:故\s*答案为|答案为|正确答案是?)\s*[：:]?\s*(.+?)\s*$",
            answer_text,
            re.DOTALL,
        )
        if inline:
            explanation = "\n".join(part for part in (answer_text, explanation) if part).strip()
            answer_text = inline.group(1).strip()
    # Keep the explicit worked answer if the source contains no dedicated
    # concise answer marker. Never derive a result from the explanation.
    return answer_text[:4000], explanation[:8000]


def _extract_options(text: str) -> tuple[str, dict[str, str]]:
    matches = list(_OPTION_START.finditer(text))
    if len(matches) < 2 or matches[0].group(1) != "A":
        return text.strip(), {}
    letters = [match.group(1) for match in matches]
    if letters[:2] != ["A", "B"] or any(a >= b for a, b in zip(letters, letters[1:])):
        return text.strip(), {}
    options: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[match.end():end].strip()
        if value:
            options[match.group(1)] = value
    if len(options) < 2:
        return text.strip(), {}
    return text[:matches[0].start()].strip(), options


def _guess_kind(block: dict, question: str, options: dict[str, str]) -> str:
    kind = block.get("kind")
    if kind:
        return kind
    if options:
        return "single_choice"
    if _BLANK.search(question):
        return "fill_blank"
    if re.search(r"判断正误|正确.*错误|对.*错", question):
        return "true_false"
    return "short_answer"


def _choice_answer(answer: str, options: dict[str, str], kind: str) -> str:
    if not options:
        return answer
    stripped = answer.strip()
    direct = re.match(r"^([A-H])(?=\s|[，,、。；;]|$)", stripped, re.IGNORECASE)
    if direct and direct.group(1).upper() in options:
        return direct.group(1).upper()
    letters = re.findall(r"(?:选|为|答案)?\s*([A-H])(?:项)?", stripped[:80], re.IGNORECASE)
    letters = [letter.upper() for letter in letters if letter.upper() in options]
    if kind == "multi_choice":
        return ",".join(dict.fromkeys(letters))
    return letters[0] if letters else answer


async def parse_document(
    data: bytes,
    filename: str,
    progress: Callable[[dict], Awaitable[None]] | None = None,
) -> list[dict]:
    """Convert a supported exam into editable candidates without model calls."""
    suffix = Path(filename).suffix.lower()
    if suffix not in DOCUMENT_EXTENSIONS:
        raise ValueError("Supported documents: .pdf, .doc, .docx, .md and .txt")
    if not data or len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError("Choose a non-empty document up to 10 MB")
    if progress:
        await progress({"stage": "reading", "percent": 4, "message": f"本地脚本解析：读取文件 {filename}"})
    pages = _read_document_pages(data, suffix)
    if len(pages) > MAX_DOCUMENT_PAGES:
        raise ValueError(f"Import at most {MAX_DOCUMENT_PAGES} pages at a time")
    empty_pages = {i for i, page in enumerate(pages, 1) if not page.strip()}
    if not any(page.strip() for page in pages):
        raise ValueError("No extractable text was found; scanned PDFs require OCR, which is not enabled in this phase")
    content_size = sum(len(page) for page in pages)
    if content_size > MAX_DOCUMENT_CHARS:
        raise ValueError("The extracted document is too long; split it into smaller files")
    if progress:
        await progress({"stage": "extracting_questions", "percent": 18,
                        "message": f"题目提取：完成全文文本提取，共 {len(pages)} 页，{content_size} 个字符"})

    question_lines, answer_lines, has_answer_section = _split_document(pages)
    if progress:
        answer_pages = sorted({page for page, _ in answer_lines})
        answer_page_text = ", ".join(map(str, answer_pages)) if answer_pages else "未发现独立答案区"
        await progress({"stage": "extracting_questions", "percent": 27,
                        "message": f"题目提取：按参考答案标题划分题目区和答案区；答案页：{answer_page_text}"})
    blocks, missing_numbers = _question_blocks(question_lines)
    if not blocks:
        raise ValueError("No numbered questions were recognized; check that the document contains a text-based exam")
    if len(blocks) > MAX_ROWS:
        raise ValueError(f"Import at most {MAX_ROWS} questions at a time")
    numbers = {block["number"] for block in blocks}
    answers = _answer_blocks(answer_lines, numbers) if has_answer_section else {}
    if progress:
        await progress({"stage": "matching_answers", "percent": 62,
                        "message": f"答案提取：识别 {len(answers)}/{len(blocks)} 道题的编号对应答案"})

    choice_blocks = [block for block in blocks if block.get("kind") in {"single_choice", "multi_choice"}]
    option_groups = _all_option_groups(question_lines)
    options_by_number: dict[str, dict[str, str]] = {}
    if len(option_groups) == len(choice_blocks):
        options_by_number = {
            block["number"]: options for block, options in zip(choice_blocks, option_groups)
        }

    results: list[dict] = []
    for index, block in enumerate(blocks, 1):
        number = block["number"]
        raw_text = "\n".join(block["lines"]).strip()
        raw_text = re.sub(r"^\s*[（(]\s*\d+\s*分[）)]\s*", "", raw_text)
        question, options = _extract_options(raw_text)
        if block.get("kind") in {"single_choice", "multi_choice"}:
            question = _strip_option_tail(raw_text)
            options = options_by_number.get(number, options or block.get("loose_options", {}))
        kind = _guess_kind(block, question, options)
        answer_block = answers.get(number)
        answer, explanation = _extract_answer(
            str(answer_block.get("text", "")) if answer_block else ""
        )
        answer = _choice_answer(answer, options, kind)
        risk_reasons: list[tuple[int, str]] = []
        if not answer:
            risk_reasons.append((55, "未找到可按题号匹配的参考答案，请核对并补充"))
        if re.search(r"如图|如右图|图象|茎叶图|频率分布直方图|表格如下", question):
            risk_reasons.append((40, "来源页包含图表或绘图，题干文字可能不完整，请对照原卷核验"))
        if suffix == ".pdf" and (
            re.search(r"(?m)^\s*(?:\d+|[A-Za-z])\s*$", question + "\n" + answer)
            or _has_suspicious_math_linebreak(question + "\n" + answer)
        ):
            risk_reasons.append((35, "PDF 公式或分数可能被拆成多行，请核对数学符号和分母"))
        if block["page"] in empty_pages:
            risk_reasons.append((55, "来源页未提取到文本，可能是扫描页，请对照原卷核验"))
        if number in missing_numbers:
            risk_reasons.append((25, "题号序列存在缺口，请检查是否漏识别了前序题目"))
        if not has_answer_section and not answer:
            risk_reasons.append((20, "未识别到独立参考答案区"))
        if not question:
            risk_reasons.append((70, "题干为空，请检查 PDF 文本顺序或页面内容"))
        confidence = max(0, 100 - min(100, sum(score for score, _ in risk_reasons)))
        # This is a deterministic extraction-quality indicator, not an AI probability.
        warnings = [reason for score, reason in risk_reasons if score >= 30]
        candidate = {
            "id": str(index),
            "number": number,
            "page": block["page"],
            "source_excerpt": _source_excerpt(
                block, question, options, answer_block, answer, explanation,
            ),
            "warnings": warnings,
            "confidence": confidence,
            "confidence_reasons": [reason for _, reason in risk_reasons],
            "selected": True,
            "confirmed": False,
            "question": question,
            "question_type": kind,
            "options": options,
            "correct_answer": answer,
            "explanation": explanation,
            "tags": [],
        }
        try:
            normalize_question(candidate)
            candidate["errors"] = []
        except (ValueError, TypeError, KeyError) as exc:
            candidate["errors"] = [str(exc)]
        results.append(candidate)
        if progress and (index == len(blocks) or index % 5 == 0):
            percent = 68 + round(index / len(blocks) * 20)
            await progress({"stage": "preview", "percent": percent,
                            "message": f"生成预览：已整理 {index}/{len(blocks)} 道候选题"})
    if progress:
        await progress({"stage": "preview", "percent": 96,
                        "message": f"生成预览完成：{len(results)} 道候选题；脚本解析未调用模型"})
    return results


def validate_candidate(candidate: dict) -> list[str]:
    try:
        normalize_question(candidate)
    except (ValueError, TypeError, KeyError) as exc:
        return [str(exc)]
    return []
