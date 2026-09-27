"use client";

import { useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useTranslation } from "react-i18next";
import { ClipboardList } from "lucide-react";
import { activeWorkspaceId, scopedUrl } from "@/lib/workspace-scope";
import { LearningShell } from "../LearningShell";
import SpaceSectionHeader from "@/components/space/SpaceSectionHeader";
import { PracticeImport } from "./PracticeImport";

export function QuestionImportPage({ libraryOnly = false }: { libraryOnly?: boolean }) {
  const { t } = useTranslation();
  const search = useSearchParams();
  const [notice, setNotice] = useState("");
  const courseId = search.get("course")?.trim() || "";
  const target = search.get("view") === "mistakes" ? "mistakes" : "bank";
  const backQuery = new URLSearchParams(search.toString());
  backQuery.delete("create");
  const backPath = libraryOnly ? "/space/questions" : "/learning/practice";
  const suffix = backQuery.toString();
  const back = scopedUrl(`${backPath}${suffix ? `?${suffix}` : ""}`, activeWorkspaceId());
  const content = <div>
    <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
      <Link href={back} className="text-sm text-muted-foreground underline">{t("Back to questions")}</Link>
    </div>
    {notice ? <div role="status" className="rounded-xl border border-emerald-500/30 bg-emerald-500/5 p-6 text-sm"><p className="font-medium text-emerald-700">{notice}</p><Link href={back} className="mt-3 inline-block underline">{t("Back to questions")}</Link></div> : <PracticeImport initialTarget={target} courseId={courseId} onImported={setNotice} />}
  </div>;
  if (libraryOnly) return <main className="w-full">
    <SpaceSectionHeader icon={ClipboardList} title={t("Import questions")} description={t("Upload and review multiple question files before saving them to your question bank.")} />
    {content}
  </main>;
  return <LearningShell wide title={t("Import questions")} subtitle={t("Upload several exam files, follow each task, review extracted questions, and confirm what to save.")}>{content}</LearningShell>;
}
