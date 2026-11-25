import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { makeTask } from "@/test/factories";
import type { ScriptReviewState } from "@/types";
import { BreakdownStatus } from "./BreakdownStatus";

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

const task = makeTask({ status: "failed", error_message: "historical failure" });
const review: ScriptReviewState = {
  episode: 1, content_mode: "drama", status: "pending_review", fingerprint: null, confirmed_at: null,
  content: null, supported_durations: null, duration_tiers: null, episode_target_duration: null, script_overwrite: null,
  quarantine: { doc_type: "reference_script_plan", revision: "r1", editable_by: "user", content: { units: [] },
    violations: [], soft_violations: [], formal_exists: false },
};

describe("revalidated breakdown", () => {
  it("presents the clean saved draft rather than the previous task error as a current blocker", () => {
    render(<BreakdownStatus task={task} busy={false} review={review} />);
    expect(screen.getByRole("status")).toHaveTextContent("production_breakdown_adoptable");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("historical failure").closest("details")).not.toHaveAttribute("open");
  });
  it("shows uncovered draft lines as advisory details rather than a blocker or stale formal warning", () => {
    render(<BreakdownStatus busy={false} task={task} review={{ ...review, coverage_advisory_mode: true,
      coverage_advisories: ["old"], quarantine: { ...review.quarantine!, coverage_advisories: ["3: missing action"] } }} />);
    expect(screen.getByRole("status")).toHaveTextContent("production_breakdown_adoptable");
    expect(screen.getByText("3: missing action")).toBeInTheDocument();
    expect(screen.queryByText("old")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
  it("does not hide a failed task when there is no revalidated draft", () => {
    render(<BreakdownStatus task={task} busy={false} review={{ ...review, quarantine: null }} />);
    expect(screen.getByRole("alert")).toHaveTextContent("historical failure");
  });
});
