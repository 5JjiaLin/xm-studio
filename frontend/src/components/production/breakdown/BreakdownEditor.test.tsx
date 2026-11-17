import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { API, ApiRequestError } from "@/api";
import { LeaveGuardProvider } from "@/components/shared/edit-unit/LeaveGuard";
import type { ProjectData, ScriptReviewState } from "@/types";
import { BreakdownEditor } from "./BreakdownEditor";

const state = (fingerprint: string, text = "原稿"): ScriptReviewState => ({
  episode: 1, content_mode: "drama", confirmed_at: null, supported_durations: [8], duration_tiers: null, episode_target_duration: null, status: "pending_review", fingerprint, content: {
    units: [{ unit_id: "E1U01", text, source_text: "原文", duration_seconds: 8 }],
  }, quarantine: null, script_overwrite: null,
} as ScriptReviewState);
const changed = vi.fn();
const page = (review: ScriptReviewState) => <LeaveGuardProvider><BreakdownEditor project="demo" episode={1} projectData={{ characters: {}, scenes: {}, props: {} } as ProjectData} review={review} busy={false} onStarted={changed} onChanged={changed} /></LeaveGuardProvider>;
const body = () => screen.getAllByRole("textbox")[0];
afterEach(() => vi.restoreAllMocks());

it("keeps a dirty buffer bound to fp1 when external fp2 arrives", async () => {
  const save = vi.spyOn(API, "saveScriptReviewContent").mockRejectedValue(new ApiRequestError("冲突", 409));
  const { rerender } = render(page(state("fp1")));
  fireEvent.change(body(), { target: { value: "本地稿" } });
  rerender(page(state("fp2", "外部稿")));
  expect(body()).toHaveValue("本地稿");
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(save).toHaveBeenCalledWith("demo", 1, expect.objectContaining({ units: [expect.objectContaining({ text: "本地稿" })] }), "fp1"));
  expect(await screen.findByText("冲突")).toBeInTheDocument();
  expect(body()).toHaveValue("本地稿");
  fireEvent.click(screen.getByRole("button", { name: "放弃修改" }));
  expect(body()).toHaveValue("外部稿");
});

it("uses the save response fingerprint for the next save before polling", async () => {
  const save = vi.spyOn(API, "saveScriptReviewContent").mockResolvedValueOnce(state("fp2", "第一次")).mockResolvedValueOnce(state("fp3", "第二次"));
  render(page(state("fp1")));
  fireEvent.change(body(), { target: { value: "第一次" } });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "保存" })).toBeDisabled());
  fireEvent.change(body(), { target: { value: "第二次" } });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(save).toHaveBeenLastCalledWith("demo", 1, expect.anything(), "fp2"));
});

it("confirms the saved fingerprint rather than the pre-save fingerprint", async () => {
  vi.spyOn(API, "saveScriptReviewContent").mockResolvedValue(state("fp2", "本地稿"));
  vi.spyOn(API, "getScriptReview").mockResolvedValue(state("fp1"));
  const confirm = vi.spyOn(API, "confirmScriptReview").mockResolvedValue({ ...state("fp2"), status: "confirmed" });
  const { container } = render(page(state("fp1")));
  fireEvent.change(body(), { target: { value: "本地稿" } });
  fireEvent.submit(container.querySelector("form")!);
  await waitFor(() => expect(confirm).toHaveBeenCalledWith("demo", 1, { planFingerprint: "fp2", overwriteRevision: undefined }));
});

it("blocks deleting a base with dependents until a compatible replacement retargets them", async () => {
  vi.spyOn(API, "previewAssetPrompt").mockResolvedValue({ text: "完整提示词" } as never);
  const save = vi.spyOn(API, "saveScriptReviewContent").mockResolvedValue(state("fp2"));
  const review = state("fp1", "@[小桃/雨衣]等候。\n@[小桃/雨衣]{你好@[小桃]。}");
  (review.content as { new_assets?: unknown[] }).new_assets = [
    { type: "character", name: "小桃", description: "少女", decision: "register", reason: "原文" },
    { type: "character", name: "雨衣", asset_name: "雨衣", description: "披雨衣", decision: "derivative", target: "小桃", reason: "原文" },
    { type: "character", name: "小红", description: "少女", decision: "register", reason: "原文" },
  ];
  render(page(review));
  fireEvent.click(screen.getAllByRole("button", { name: "从本集移除" })[0]);
  const remove = screen.getAllByRole("button", { name: "从本集移除" }).at(-1)!;
  expect(remove).toBeDisabled();
  fireEvent.change(screen.getByRole("combobox", { name: "替换引用" }), { target: { value: "小红" } });
  expect(remove).toBeEnabled();
  fireEvent.click(remove);
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(save).toHaveBeenCalled());
  const payload = save.mock.calls[0][2] as { new_assets: { name: string; target?: string }[]; units: { text: string }[] };
  expect(payload.new_assets.map((a) => a.name)).toEqual(["雨衣", "小红"]);
  expect(payload.new_assets[0].target).toBe("小红");
  expect(payload.units[0].text).toBe("@[小红/雨衣]等候。\n@[小红/雨衣]{你好@[小桃]。}");
});
