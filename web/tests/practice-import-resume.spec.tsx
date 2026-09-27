import { render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { ReviewHome } from "@/components/learning/practice/ReviewHome";
import { initI18n } from "@/i18n/init";
import * as api from "@/lib/practice-api";

vi.mock("@/lib/practice-api", async importOriginal => ({
  ...(await importOriginal<typeof api>()),
  getDocumentImportJobs: vi.fn(),
  getPracticeSummary: vi.fn(),
}));
vi.mock("@/hooks/useChatWorkspaces", () => ({ useChatWorkspaces: () => ({ workspaces: [] }) }));
vi.mock("@/components/learning/LibraryWorkspace", () => ({
  useLearningCreation: () => ({ begin: vi.fn(), dialog: null }),
}));
vi.mock("@/components/learning/LearningShell", () => ({
  LearningShell: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  LearningErrorState: () => null,
}));
vi.mock("@/components/learning/practice/PracticeImport", () => ({
  PracticeImport: () => <div>Question import opened automatically</div>,
}));
vi.mock("@/components/learning/practice/PracticeInsights", () => ({
  PracticeInsights: () => null,
}));

initI18n("en");
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getPracticeSummary).mockResolvedValue({
    total: 0, mistakes: 0, due: 0, overdue: 0, reviewed_today: 0,
    next_due_at: null, day_end: 0, timezone: "UTC",
  });
});

it("opens the import panel when entering practice with a running exam task", async () => {
  vi.mocked(api.getDocumentImportJobs).mockResolvedValue([{
    token: "a".repeat(32), filename: "xuhui.pdf", target: "bank",
    status: "processing", stage: "extracting_questions", percent: 40,
  }]);
  render(<ReviewHome />);
  expect(await screen.findByText("Question import opened automatically")).toBeInTheDocument();
});
