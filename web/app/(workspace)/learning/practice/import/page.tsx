import { Suspense } from "react";
import { QuestionImportPage } from "@/components/learning/practice/QuestionImportPage";
import { LearningSkeleton } from "@/components/learning/LearningShell";

export default function Page() {
  return <Suspense fallback={<LearningSkeleton />}><QuestionImportPage /></Suspense>;
}
