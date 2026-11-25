import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import "@/i18n";
import { API } from "@/api";
import type { ScriptBatchState } from "@/types/production";
import { WholeScriptPanel } from "./WholeScriptPanel";

afterEach(() => vi.restoreAllMocks());

function showBatch(state: ScriptBatchState) {
  vi.spyOn(API, "getScriptBatch").mockResolvedValue(state);
  const control = vi.spyOn(API, "controlScriptBatch").mockResolvedValue(state);
  render(<WholeScriptPanel project="demo" disabled={false} onImported={vi.fn()} onActive={vi.fn()} />);
  return control;
}

const unknown: ScriptBatchState = {
  status: "unknown",
  import_id: "import-1",
  imports: [],
  items: [{ episode: 1, status: "unknown", requires_provider_check: true }],
};

it("keeps a timed-out request waiting when provider confirmation is cancelled", async () => {
  const user = userEvent.setup();
  const control = showBatch(unknown);
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  await user.click(await screen.findByRole("button", { name: "已确认未受理，解除等待" }));
  expect(confirm).toHaveBeenCalledWith(expect.stringContaining("没有受理原请求"));
  expect(control).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "继续未完成集" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "重新核对原任务" })).not.toBeInTheDocument();
});

it("releases a provider-checked wait first and only resumes on a separate click", async () => {
  const user = userEvent.setup();
  const control = showBatch(unknown);
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.spyOn(API, "getScriptBatch").mockResolvedValue({
    ...unknown, status: "stopped", items: [{ episode: 1, status: "pending" }],
  });
  await user.click(await screen.findByRole("button", { name: "已确认未受理，解除等待" }));
  await screen.findByRole("button", { name: "继续未完成集" });
  expect(control).toHaveBeenCalledExactlyOnceWith("demo", "resolve_unknown", undefined, true);
  await user.click(screen.getByRole("button", { name: "继续未完成集" }));
  await waitFor(() => expect(control).toHaveBeenLastCalledWith("demo", "continue", undefined, false));
});

it("rechecks an unknown queue submission without asserting provider non-acceptance", async () => {
  const user = userEvent.setup();
  const control = showBatch({ ...unknown, items: [{ episode: 1, status: "unknown" }] });
  await user.click(await screen.findByRole("button", { name: "重新核对原任务" }));
  expect(control).toHaveBeenCalledExactlyOnceWith("demo", "resolve_unknown");
});

it("offers only rechecking when a known running task could not be queried", async () => {
  const user = userEvent.setup();
  const control = showBatch({ ...unknown, items: [{ episode: 1, status: "active" }] });
  await user.click(await screen.findByRole("button", { name: "重新核对原任务" }));
  expect(control).toHaveBeenCalledExactlyOnceWith("demo", "resolve_unknown");
  expect(screen.queryByRole("button", { name: "已确认未受理，解除等待" })).not.toBeInTheDocument();
});
