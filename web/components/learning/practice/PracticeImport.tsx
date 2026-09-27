"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Download, FileSpreadsheet, Loader2, Trash2, X } from "lucide-react";
import ProcessLogs from "@/components/common/ProcessLogs";
import {
  commitPracticeImport,
  downloadPracticeTemplate,
  previewPracticeImport,
  previewDocumentImport,
  getDocumentImportMode,
  getDocumentImport,
  getDocumentImportJobs,
  cancelDocumentImport,
  retryDocumentImport,
  updateDocumentImport,
  type DocumentImportDraft,
  type DocumentImportJob,
  type ImportPreview,
} from "@/lib/practice-api";
import { activeWorkspaceId } from "@/lib/workspace-scope";
import { DocumentImportReview } from "./DocumentImportReview";

export function PracticeImport({
  onClose,
  onImported,
  initialTarget,
  courseId = "",
}: {
  onClose?: () => void;
  onImported: (message: string) => void;
  initialTarget: "bank" | "mistakes";
  courseId?: string;
}) {
  const { t } = useTranslation();
  const [target, setTarget] = useState(initialTarget);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [documentDraft, setDocumentDraft] = useState<DocumentImportDraft | null>(null);
  const [documentJobs, setDocumentJobs] = useState<DocumentImportJob[]>([]);
  const [importMode, setImportMode] = useState<string | null>(null);
  const draftRef = useRef<DocumentImportDraft | null>(null);
  const dirtyRef = useRef(false);
  const sequenceRef = useRef(0);
  const savePromiseRef = useRef<Promise<void> | null>(null);
  const draftKey = `practice-import-draft:${activeWorkspaceId()}:${courseId}`;
  const stageLabels: Record<string, string> = {
    queued: t("Waiting to parse…"),
    reading: t("Extracting questions…"),
    visualizing: t("Extracting questions…"),
    extracting_questions: t("Extracting questions…"),
    matching_answers: t("Extracting answers…"),
    reasoning: t("Reasoning over questions and answers…"),
    preview: t("Generating question preview…"),
    validating: t("Generating question preview…"),
    ready: t("Preview ready"),
  };
  const [filename, setFilename] = useState("");
  const [busy, setBusy] = useState(false);
  const pending = useRef(false);
  const [error, setError] = useState("");
  useEffect(() => {
    let alive = true;
    const refreshModel = () => {
      void getDocumentImportMode().then(result => {
        if (alive) setImportMode(result.mode);
      }).catch(() => { if (alive) setImportMode(null); });
    };
    refreshModel();
    window.addEventListener("focus", refreshModel);
    return () => { alive = false; window.removeEventListener("focus", refreshModel); };
  }, []);
  useEffect(() => {
    let alive = true;
    let initial = true;
    const refresh = async () => {
      try {
        const jobs = await getDocumentImportJobs(courseId);
        if (!alive) return;
        setDocumentJobs(jobs);
        if (initial) {
          initial = false;
          const remembered = window.localStorage.getItem(draftKey);
          const chosen = jobs.find(job => job.status === "processing" || job.status === "queued") ||
            jobs.find(job => job.token === remembered) || jobs[0];
          if (chosen && !draftRef.current) {
            const draft = await getDocumentImport(chosen.token);
            if (!alive || draftRef.current) return;
            draftRef.current = draft;
            setDocumentDraft(draft);
            setTarget(draft.target);
            setFilename(draft.filename);
            window.localStorage.setItem(draftKey, draft.token);
            if (draft.status === "failed") setError(draft.error || t("Document parsing failed"));
          }
        }
      } catch (err) { if (alive) setError(err instanceof Error ? err.message : String(err)); }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 2000);
    return () => { alive = false; window.clearInterval(timer); };
  }, [courseId, draftKey, t]);
  useEffect(() => {
    if (!documentDraft || (documentDraft.status !== "processing" && documentDraft.status !== "queued")) return;
    let alive = true;
    const timer = window.setTimeout(() => {
      void getDocumentImport(documentDraft.token).then(next => {
        if (!alive) return;
        draftRef.current = next;
        setDocumentDraft(next);
        if (next.status === "failed") setError(next.error || t("Document parsing failed"));
      }).catch(err => { if (alive) setError(String(err)); });
    }, 1500);
    return () => { alive = false; window.clearTimeout(timer); };
  }, [documentDraft, t]);

  const saveDocument = useCallback(async () => {
    if (savePromiseRef.current) return savePromiseRef.current;
    const task = (async () => {
      while (dirtyRef.current && draftRef.current) {
        const snapshot = draftRef.current;
        const sequence = sequenceRef.current;
        dirtyRef.current = false;
        let saved: DocumentImportDraft;
        try { saved = await updateDocumentImport(snapshot); }
        catch (err) { dirtyRef.current = true; throw err; }
        if (draftRef.current?.token !== saved.token) return;
        if (sequence === sequenceRef.current) {
          draftRef.current = saved;
          setDocumentDraft(saved);
        } else {
          const next = { ...draftRef.current, revision: saved.revision };
          draftRef.current = next;
          setDocumentDraft(next);
        }
      }
    })();
    savePromiseRef.current = task;
    try { await task; } finally { savePromiseRef.current = null; }
  }, []);
  useEffect(() => {
    if (!documentDraft || !dirtyRef.current) return;
    const timer = window.setTimeout(() => { void saveDocument().catch(err => setError(String(err))); }, 900);
    return () => window.clearTimeout(timer);
  }, [documentDraft, saveDocument]);

  function editDocument(next: DocumentImportDraft) {
    draftRef.current = next;
    dirtyRef.current = true;
    sequenceRef.current++;
    setDocumentDraft(next);
  }
  async function select(files: File[]) {
    if (!files.length || pending.current) return;
    if (files.length > 10) {
      setError(t("Select up to 10 files per batch"));
      return;
    }
    pending.current = true;
    setBusy(true);
    setError("");
    if (dirtyRef.current) {
      try { await saveDocument(); }
      catch (err) {
        setError(err instanceof Error ? err.message : String(err));
        pending.current = false;
        setBusy(false);
        return;
      }
    }
    setPreview(null);
    setDocumentDraft(null);
    draftRef.current = null;
    dirtyRef.current = false;
    setFilename(files[0].name);
    try {
      if (files.every(file => /\.(pdf|doc|docx|md|txt)$/i.test(file.name))) {
        let first: DocumentImportDraft | null = null;
        for (const file of files) {
          const draft = await previewDocumentImport(file, target, courseId);
          first ??= draft;
        }
        if (!first) return;
        const draft = first;
        draftRef.current = draft;
        setDocumentDraft(draft);
        setTarget(draft.target);
        window.localStorage.setItem(draftKey, draft.token);
        setDocumentJobs(await getDocumentImportJobs(courseId));
      } else {
        if (files.length !== 1) throw new Error(t("Select only document files for a batch upload"));
        window.localStorage.removeItem(draftKey);
        setPreview(await previewPracticeImport(files[0], target, courseId));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }
  async function commitDocument() {
    if (!draftRef.current || pending.current) return;
    pending.current = true;
    setBusy(true);
    setError("");
    try {
      await saveDocument();
      const token = draftRef.current.token;
      const result = await commitPracticeImport(token);
      window.localStorage.removeItem(draftKey);
      onImported(t("Imported {{created}} questions; skipped {{duplicates}} duplicates.", result));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }
  async function cancelDocument() {
    if (!documentDraft || (documentDraft.status !== "processing" && documentDraft.status !== "queued")) return;
    try { await cancelDocumentImport(documentDraft.token); }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); return; }
    setDocumentJobs(await getDocumentImportJobs(courseId));
    const next = await getDocumentImport(documentDraft.token);
    draftRef.current = next;
    setDocumentDraft(next);
  }
  async function openDocumentJob(token: string) {
    try {
      await saveDocument();
      const draft = await getDocumentImport(token);
      draftRef.current = draft;
      setDocumentDraft(draft);
      setTarget(draft.target);
      setFilename(draft.filename);
      setError(draft.status === "failed" ? draft.error || t("Document parsing failed") : "");
      window.localStorage.setItem(draftKey, token);
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
  }
  async function retryDocument() {
    if (!documentDraft || documentDraft.status !== "failed") return;
    setError("");
    try {
      const next = await retryDocumentImport(documentDraft.token);
      draftRef.current = next;
      setDocumentDraft(next);
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
  }
  async function deleteDocumentJob(token: string) {
    try {
      await cancelDocumentImport(token);
      setDocumentJobs(jobs => jobs.filter(job => job.token !== token));
      if (draftRef.current?.token === token) {
        draftRef.current = null;
        dirtyRef.current = false;
        setDocumentDraft(null);
        setFilename("");
        window.localStorage.removeItem(draftKey);
      }
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
  }
  async function commit() {
    if (!preview?.token || pending.current) return;
    pending.current = true;
    setBusy(true);
    setError("");
    try {
      const result = await commitPracticeImport(preview.token);
      onImported(t("Imported {{created}} questions; skipped {{duplicates}} duplicates.", result));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }
  return (
    <section
      aria-labelledby="practice-import-title"
      className="rounded-2xl border border-border bg-card p-5 sm:p-6"
    >
      <div className="flex items-center justify-between gap-3">
        <h2 id="practice-import-title" className="flex items-center gap-2 text-base font-semibold">
          <FileSpreadsheet size={18} />
          {t("Import questions")}
        </h2>
        {onClose && <button type="button" onClick={onClose} disabled={busy} aria-label={t("Close import")} className="rounded-lg p-2 hover:bg-muted disabled:opacity-50"><X size={17} /></button>}
      </div>
      <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
        {t(
      "Import a PDF, Word, Markdown or text exam with local script rules, or use the XLSX, CSV, TSV and JSON templates. Documents: up to 10 MB and 30 pages. Scanned pages and uncertain formulas or diagrams need manual review. Review every question before saving.",
        )}
      </p>
      <p className="mt-2 text-xs text-muted-foreground" role="status">
        {t("Question import mode")}: <span className="font-medium text-foreground">{t(importMode === "local_script" ? "Local script rules (no model inference)" : "Loading import mode…")}</span>
      </p>
      <div className="mt-4 rounded-xl border border-border bg-muted/10 p-4">
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-sm">
          {t("Import into")}
          <select
            value={target}
            disabled={busy || documentDraft?.status === "processing" || documentDraft?.status === "queued"}
            onChange={event => {
              const nextTarget = event.target.value as typeof target;
              if (draftRef.current?.status === "ready") {
                editDocument({ ...draftRef.current, target: nextTarget });
                setTarget(nextTarget);
                return;
              }
              setTarget(nextTarget);
              setPreview(null);
              setDocumentDraft(null);
              draftRef.current = null;
              dirtyRef.current = false;
              window.localStorage.removeItem(draftKey);
              setFilename("");
            }}
            className="rounded-lg border border-border bg-background p-2"
          >
            <option value="bank">{t("Question Bank")}</option>
            <option value="mistakes">{t("Mistakes")}</option>
          </select>
        </label>
        {(["xlsx", "csv"] as const).map(format => (
          <button
            key={format}
            type="button"
            onClick={() => {
              void downloadPracticeTemplate(format).catch(err => setError(String(err)));
            }}
            className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-2 text-xs hover:bg-muted"
          >
            <Download size={14} />
            {t("Download {{format}} template", { format: format.toUpperCase() })}
          </button>
        ))}
      </div>
      <label
        className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-dashed border-border bg-muted/20 px-3 py-2 text-sm"
        onDragOver={event => event.preventDefault()}
        onDrop={event => { event.preventDefault(); void select(Array.from(event.dataTransfer.files)); }}
      >
        <span className="shrink-0 font-medium">{t("Choose a question file")}</span>
        <input
          key={target}
          type="file"
          multiple
          accept=".pdf,.doc,.docx,.md,.txt,.xlsx,.csv,.tsv,.json"
          disabled={busy}
          onChange={event => {
            void select(Array.from(event.target.files || []));
            event.target.value = "";
          }}
          className="min-w-0 flex-1 text-xs file:mr-2 file:rounded-md file:border-0 file:bg-primary/10 file:px-2.5 file:py-1.5 file:text-primary"
        />
        <span className="w-full text-xs text-muted-foreground">
          {t(
            "Select up to 10 document files at once; progress is saved when you leave and return.",
          )}
        </span>
      </label>
      {busy && (
        <p role="status" className="mt-3 flex items-center gap-2 text-sm">
          <Loader2 size={16} className="animate-spin" />
          {t("Processing…")}
        </p>
      )}
      </div>
      <div className="mt-4 grid gap-5 lg:grid-cols-[300px_minmax(0,1fr)]">
      <aside className="space-y-4">
      <div className="space-y-2 rounded-xl border border-border p-3 text-sm">
        <p className="font-medium">{t("Question import tasks")} · {t("Files")}: {documentJobs.length} · {t("Active imports")}: {documentJobs.filter(job => job.status === "processing" || job.status === "queued").length}</p>
        {documentJobs.length === 0 && <p className="py-4 text-xs text-muted-foreground">{t("No document imports yet")}</p>}
        <ul className="max-h-[50vh] space-y-1 overflow-y-auto">{documentJobs.map(job => <li key={job.token} className={`flex items-start gap-1 rounded-lg px-2 py-2 ${documentDraft?.token === job.token ? "bg-muted" : "hover:bg-muted/60"}`}>
          <button type="button" onClick={() => void openDocumentJob(job.token)} className="min-w-0 flex-1 text-left">
            <span className="block break-all font-medium">{job.filename}</span><span className="mt-1 block text-xs text-muted-foreground">{stageLabels[job.stage || "queued"] || job.stage} · {job.percent ?? 0}% · {t(job.status || "queued")}</span>
            <span className="mt-1 block h-1.5 rounded-full bg-background"><span className="block h-1.5 rounded-full bg-primary" style={{ width: `${job.percent ?? 0}%` }} /></span>
          </button>
          <button type="button" aria-label={t("Delete import task")} onClick={() => void deleteDocumentJob(job.token)} className="rounded p-1.5 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"><Trash2 size={14} /></button>
        </li>)}</ul>
      </div>
      {documentJobs.length > 1 && <ProcessLogs logs={documentJobs.flatMap(job => (job.logs || []).slice(-3).map(line => `${job.filename}: ${line}`))} executing={documentJobs.some(job => job.status === "processing" || job.status === "queued")} title={t("Overall question import logs")} />}
      </aside>
      <div className="min-w-0 border-t border-border pt-4 lg:border-l lg:border-t-0 lg:pl-5 lg:pt-0">
      {(documentDraft?.status === "processing" || documentDraft?.status === "queued") && (
        <div role="status" className="mt-3 space-y-2 text-sm">
          <div className="flex items-center gap-3"><Loader2 size={16} className="animate-spin" />
            <span>{documentDraft.filename}: {stageLabels[documentDraft.stage || "queued"]} ({documentDraft.percent ?? 0}%)</span>
            <button type="button" onClick={() => void cancelDocument()} className="underline">{t("Cancel parsing")}</button>
          </div>
          <div role="progressbar" aria-label={t("Question import progress")} aria-valuemin={0} aria-valuemax={100} aria-valuenow={documentDraft.percent ?? 0} className="h-2 rounded-full bg-muted"><div className="h-2 rounded-full bg-primary transition-all" style={{ width: `${documentDraft.percent ?? 0}%` }} /></div>
          <p className="text-xs text-muted-foreground">{t("You may return to this draft later.")}</p>
        </div>
      )}
      {documentDraft && <div className="mt-3"><ProcessLogs logs={documentDraft.logs || []} executing={documentDraft.status === "processing" || documentDraft.status === "queued"} title={t("Question import logs")} /></div>}
      {documentDraft?.status === "failed" && (
        <button type="button" onClick={() => void retryDocument()} className="mt-3 rounded border border-border px-3 py-1.5 text-sm">{t("Retry parsing")}</button>
      )}
      {error && (
        <p role="alert" className="mt-3 max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-destructive/5 p-3 text-sm text-destructive">
          {error}
        </p>
      )}
      <div className="mt-4">
      {preview && (
        <div className="mt-4 space-y-3">
          <p className="break-all text-sm font-medium">
            {filename} ·{" "}
            {t("{{valid}} of {{total}} rows are valid", {
              valid: preview.valid,
              total: preview.total,
            })}
          </p>
          {preview.errors.length > 0 && (
            <div
              role="alert"
              className="max-h-40 overflow-auto rounded-lg bg-destructive/5 p-3 text-sm text-destructive"
            >
              <p className="mb-2 font-medium">
                {t("Nothing has been imported. Fix these rows and select the file again.")}
              </p>
              {preview.errors.map(error => (
                <p key={error.row}>
                  {t("Row {{row}}", { row: error.row })}: {error.message}
                </p>
              ))}
            </div>
          )}
          <ol className="divide-y divide-border rounded-lg border border-border px-3">
            {preview.samples.map((question, index) => (
              <li key={index} className="py-3 text-sm">
                <p className="line-clamp-2">{question.question}</p>
                <p className="mt-1 truncate text-xs text-muted-foreground">
                  {t("Correct answer")}: {question.correct_answer}
                  {question.tags.length ? ` · ${question.tags.join(" / ")}` : ""}
                </p>
              </li>
            ))}
          </ol>
          <p className="text-xs text-muted-foreground">
            {t(
              "Existing questions are kept. Identical imports are skipped and their tags are combined.",
            )}
          </p>
          <button
            type="button"
            disabled={busy || !preview.token}
            onClick={() => void commit()}
            className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-40"
          >
            {t("Confirm import")}
          </button>
        </div>
      )}
      {documentDraft && documentDraft.status !== "processing" && documentDraft.status !== "queued" && documentDraft.status !== "failed" && documentDraft.status !== "cancelled" && <DocumentImportReview
        draft={documentDraft}
        onChange={editDocument}
        onSave={() => { void saveDocument().catch(err => setError(String(err))); }}
        onCommit={() => { void commitDocument(); }}
        busy={busy}
      />}
      {!documentDraft && !preview && <div className="flex min-h-64 items-center justify-center rounded-xl border border-dashed border-border p-8 text-center text-sm text-muted-foreground">{t("Select an import task or upload a document to see progress and preview here.")}</div>}
      </div>
      </div>
      </div>
    </section>
  );
}
