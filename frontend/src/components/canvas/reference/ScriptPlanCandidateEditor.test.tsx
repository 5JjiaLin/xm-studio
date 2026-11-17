import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { API } from "@/api";
import type { ScriptReviewQuarantine } from "@/types";
import { LeaveGuardProvider } from "@/components/shared/edit-unit/LeaveGuard";
import { ScriptPlanCandidateEditor } from "./ScriptPlanCandidateEditor";

afterEach(() => vi.restoreAllMocks());
it("retains broken JSON, rejects duplicate keys and saves repairs with the original revision", async () => {
  const save = vi.spyOn(API, "saveEpisodeDraft").mockResolvedValue({ adopted: true, draft: null } as never);
  const onChanged = vi.fn();
  const draft = { content: { raw_response: '{"units": [' }, revision: "rev1", doc_type: "reference_script_plan", editable_by: "user", violations: [], soft_violations: [], formal_exists: true } satisfies ScriptReviewQuarantine;
  render(<LeaveGuardProvider><ScriptPlanCandidateEditor project="demo" episode={1} draft={draft} onChanged={onChanged} /></LeaveGuardProvider>);
  const text = screen.getByRole("textbox", { name: "JSON" });
  expect((text as HTMLTextAreaElement).value).toContain("raw_response");
  fireEvent.change(text, { target: { value: '{"units":[], "units":[]}' } });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("该名称已被使用");
  expect(save).not.toHaveBeenCalled();
  const repaired = { units: [{ duration_seconds: 8, source_text: "原文", text: "门开了。" }] };
  fireEvent.change(text, { target: { value: JSON.stringify(repaired) } });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(save).toHaveBeenCalledExactlyOnceWith("demo", 1, "reference_script_plan", repaired, "rev1"));
  expect(onChanged).toHaveBeenCalledOnce();
});
