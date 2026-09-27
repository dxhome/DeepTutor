"use client";

import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import MarkdownRenderer from "@/components/common/MarkdownRenderer";
import { documentImportSourceUrl, type DocumentImportDraft, type DocumentImportItem } from "@/lib/practice-api";

const TYPES = ["fill_blank", "single_choice", "multi_choice", "true_false", "short_answer"];

function localErrors(item: DocumentImportItem): string[] {
  const errors: string[] = [];
  if (!TYPES.includes(item.question_type)) errors.push("Choose a supported question type");
  if (!item.question.trim()) errors.push("Question is required");
  if (!item.correct_answer.trim()) errors.push("Correct answer is required");
  if (item.question.length > 20000 || item.correct_answer.length > 20000 || item.explanation.length > 20000)
    errors.push("A field exceeds 20,000 characters");
  if (item.tags.length > 20 || item.tags.some(tag => tag.length > 100))
    errors.push("Too many tags or a tag is too long");
  if (["single_choice", "multi_choice"].includes(item.question_type)) {
    const keys = Object.keys(item.options).filter(key => item.options[key]?.trim());
    if (keys.length < 2) errors.push("Choice questions need at least two options");
    const answers = item.correct_answer.toUpperCase().split(/[,;，；\s]+/).filter(Boolean);
    if (answers.some(answer => !keys.includes(answer)) || (item.question_type === "single_choice" && answers.length !== 1))
      errors.push("Answer must match an option");
  }
  return errors;
}

export function DocumentImportReview({
  draft,
  onChange,
  onSave,
  onCommit,
  busy,
}: {
  draft: DocumentImportDraft;
  onChange: (next: DocumentImportDraft) => void;
  onSave: () => void;
  onCommit: () => void;
  busy: boolean;
}) {
  const { t } = useTranslation();
  const [filter, setFilter] = useState<"all" | "issues" | "errors" | "duplicates" | "ready" | "selected">("all");
  const [bulkTags, setBulkTags] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [sourceOpen, setSourceOpen] = useState(false);
  const splitCount = useRef(0);
  const update = (id: string, changes: Partial<DocumentImportItem>) => {
    onChange({ ...draft, items: draft.items.map(item => item.id === id ? { ...item, ...changes } : item) });
  };
  const selected = draft.items.filter(item => item.selected);
  const problems = selected.filter(item => localErrors(item).length || (item.warnings.length && !item.confirmed));
  const duplicates = selected.filter(item => item.duplicate).length;
  const confirmable = selected.filter(item => item.warnings.length > 0 && !item.confirmed && localErrors(item).length === 0);
  const shown = draft.items.filter(item => {
    if (filter === "all") return true;
    if (filter === "selected") return item.selected;
    if (filter === "errors") return localErrors(item).length > 0;
    if (filter === "duplicates") return !!item.duplicate;
    if (filter === "ready") return !localErrors(item).length && (!item.warnings.length || item.confirmed) && !item.duplicate;
    return !!(localErrors(item).length || (item.warnings.length && !item.confirmed));
  });
  const sourceUrl = documentImportSourceUrl(draft.token);

  function split(index: number) {
    const original = draft.items[index];
    const newItem: DocumentImportItem = {
      ...original, id: `${original.id}-split-${++splitCount.current}`, number: `${original.number}b`,
      question: "", correct_answer: "", explanation: "", options: {},
      warnings: [], errors: ["Question and correct answer are required"], confirmed: false,
    };
    const items = [...draft.items];
    items.splice(index + 1, 0, newItem);
    onChange({ ...draft, items });
    setExpanded(newItem.id);
  }
  function merge(index: number) {
    if (index >= draft.items.length - 1) return;
    const items = [...draft.items];
    const next = items[index + 1];
    items[index] = {
      ...items[index], question: `${items[index].question}\n\n${next.question}`.trim(),
      correct_answer: `${items[index].correct_answer}\n${next.correct_answer}`.trim(),
      explanation: `${items[index].explanation}\n${next.explanation}`.trim(),
      warnings: [...items[index].warnings, ...next.warnings], confirmed: false,
    };
    items.splice(index + 1, 1);
    onChange({ ...draft, items });
  }

  return (
    <div className="mt-5 space-y-4">
      <div className="rounded-lg border border-border bg-muted/20 p-3 text-sm">
        <strong>{draft.filename}</strong> · {draft.items.length} {t("candidates")}, {selected.length} {t("selected")}, {problems.length} {t("need review")}, {duplicates} {t("duplicates")}, {Math.max(0, selected.length - duplicates)} {t("expected new")}
      </div>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <label>{t("Show")}: <select value={filter} onChange={event => setFilter(event.target.value as typeof filter)} className="rounded border border-border bg-background p-1.5">
          <option value="all">{t("All questions")}</option><option value="issues">{t("Needs review")}</option><option value="errors">{t("Validation errors")}</option><option value="duplicates">{t("Duplicate question")}</option><option value="ready">{t("Ready questions")}</option><option value="selected">{t("Selected questions")}</option>
        </select></label>
        <button type="button" onClick={() => onChange({ ...draft, items: draft.items.map(item => shown.some(visible => visible.id === item.id) ? { ...item, selected: true } : item) })} className="rounded border border-border px-3 py-1.5">{t("Select shown")}</button>
        <button type="button" onClick={() => onChange({ ...draft, items: draft.items.map(item => shown.some(visible => visible.id === item.id) ? { ...item, selected: false } : item) })} className="rounded border border-border px-3 py-1.5">{t("Exclude shown")}</button>
        <button type="button" disabled={!confirmable.length} onClick={() => onChange({ ...draft, items: draft.items.map(item => confirmable.some(candidate => candidate.id === item.id) ? { ...item, confirmed: true } : item) })} className="rounded border border-border px-3 py-1.5 disabled:opacity-40">{t("Confirm all reviewable questions")} ({confirmable.length})</button>
        <label className="flex items-center gap-1">{t("Tags for shown questions")}<input value={bulkTags} onChange={event => setBulkTags(event.target.value)} className="w-40 rounded border border-border bg-background p-1.5" /></label>
        <button type="button" disabled={!bulkTags.trim()} onClick={() => {
          const additions = bulkTags.split(/[,，]/).map(value => value.trim()).filter(Boolean);
          onChange({ ...draft, items: draft.items.map(item => shown.some(visible => visible.id === item.id) ? { ...item, tags: [...new Set([...item.tags, ...additions])] } : item) });
          setBulkTags("");
        }} className="rounded border border-border px-3 py-1.5 disabled:opacity-40">{t("Apply tags")}</button>
        <button type="button" onClick={onSave} disabled={busy} className="rounded border border-border px-3 py-1.5">{t("Save draft")}</button>
        {draft.filename.toLowerCase().endsWith(".pdf") && <button type="button" onClick={() => setSourceOpen(open => !open)} className="rounded border border-border px-3 py-1.5">{t("View source PDF")}</button>}
      </div>
      {sourceOpen && draft.filename.toLowerCase().endsWith(".pdf") && <iframe title={t("Source PDF")} src={sourceUrl} className="h-[520px] w-full rounded border border-border" />}
      <ol className="space-y-3">
        {shown.map(item => {
          const index = draft.items.findIndex(entry => entry.id === item.id);
          const errors = localErrors(item);
          const open = expanded === item.id;
          return <li key={item.id} className="rounded-xl border border-border bg-card p-4 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <label className="flex items-center gap-2"><input type="checkbox" checked={item.selected} onChange={event => update(item.id, { selected: event.target.checked })} />{t("Include")}</label>
              <button type="button" onClick={() => setExpanded(open ? null : item.id)} className="font-medium underline">{t("Question")} {item.number}</button>
              {item.page && <span className="text-xs text-muted-foreground">{t("Page")} {item.page}</span>}
              {errors.length > 0 && <span className="rounded bg-destructive/10 px-2 py-0.5 text-xs text-destructive">{t("Needs review")}</span>}
              {typeof item.confidence === "number" && <span className={`rounded px-2 py-0.5 text-xs ${item.confidence < 70 ? "bg-amber-100 text-amber-800" : "bg-emerald-100 text-emerald-800"}`}>{item.confidence < 70 ? t("Check accuracy") : t("Likely accurate")} · {item.confidence}</span>}
              {item.warnings.length > 0 && <span className="rounded bg-amber-100 px-2 py-0.5 text-xs text-amber-800">{t("Parsing warning")}</span>}
              {item.duplicate && <span className="rounded bg-muted px-2 py-0.5 text-xs">{t("Duplicate question")}</span>}
            </div>
            <div className="mt-2 max-h-36 overflow-auto"><MarkdownRenderer content={item.question || t("Empty question")} /></div>
            {!open && <p className="mt-1 text-xs text-muted-foreground">{t("Correct answer")}: {item.correct_answer || t("Missing answer")}</p>}
            {open && <div className="mt-4 space-y-3">
              <label className="block">{t("Question number")}<input value={item.number} onChange={event => update(item.id, { number: event.target.value })} className="mt-1 w-full rounded border border-border bg-background p-2" /></label>
              <label className="block">{t("Question")}<textarea value={item.question} onChange={event => update(item.id, { question: event.target.value })} rows={5} className="mt-1 w-full rounded border border-border bg-background p-2" /></label>
              <label className="block">{t("Question type")}<select value={item.question_type} onChange={event => update(item.id, { question_type: event.target.value })} className="mt-1 w-full rounded border border-border bg-background p-2">{TYPES.map(type => <option key={type} value={type}>{type}</option>)}</select></label>
              {["single_choice", "multi_choice"].includes(item.question_type) && <div className="grid gap-2 sm:grid-cols-2">{"ABCDEFGHIJ".split("").filter(key => key in item.options || "ABCD".includes(key)).map(key => <label key={key}>{key}<input value={item.options[key] || ""} onChange={event => update(item.id, { options: { ...item.options, [key]: event.target.value } })} className="ml-2 rounded border border-border bg-background p-2" /></label>)}</div>}
              <label className="block">{t("Correct answer")}<textarea value={item.correct_answer} onChange={event => update(item.id, { correct_answer: event.target.value })} rows={3} className="mt-1 w-full rounded border border-border bg-background p-2" /></label>
              <label className="block">{t("Explanation")}<textarea value={item.explanation} onChange={event => update(item.id, { explanation: event.target.value })} rows={4} className="mt-1 w-full rounded border border-border bg-background p-2" /></label>
              <label className="block">{t("Tags")}<input value={item.tags.join(", ")} onChange={event => update(item.id, { tags: event.target.value.split(/[,，]/).map(value => value.trim()).filter(Boolean) })} className="mt-1 w-full rounded border border-border bg-background p-2" /></label>
              <div className="flex gap-2"><button type="button" onClick={() => split(index)} className="rounded border border-border px-2 py-1">{t("Split here")}</button><button type="button" onClick={() => merge(index)} disabled={index === draft.items.length - 1} className="rounded border border-border px-2 py-1 disabled:opacity-40">{t("Merge with next")}</button></div>
              {item.source_excerpt && <details><summary className="cursor-pointer">{t("Source text")}</summary><pre className="mt-2 max-h-44 overflow-auto whitespace-pre-wrap rounded bg-muted p-2 text-xs">{item.source_excerpt}</pre></details>}
            </div>}
            {item.warnings.length > 0 && <div className="mt-2 rounded bg-amber-50 p-2 text-xs text-amber-900">{item.warnings.join("; ")}<label className="mt-2 flex items-center gap-2"><input type="checkbox" checked={item.confirmed} onChange={event => update(item.id, { confirmed: event.target.checked })} />{t("I checked this question against the source")}</label></div>}
            {item.confidence_reasons?.length ? <details className="mt-2 text-xs text-muted-foreground"><summary className="cursor-pointer">{t("Accuracy analysis details")}</summary><ul className="list-disc pl-5">{item.confidence_reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul><p>{t("Rule-based extraction quality indicator; not a model probability.")}</p></details> : null}
            {errors.length > 0 && <p role="alert" className="mt-2 text-xs text-destructive">{errors.join("; ")}</p>}
          </li>;
        })}
      </ol>
      <button type="button" onClick={onCommit} disabled={busy || selected.length === 0 || problems.length > 0} className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-40">{t("Confirm import")} ({selected.length})</button>
    </div>
  );
}
