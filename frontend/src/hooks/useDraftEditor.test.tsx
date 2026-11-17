import { act, render, renderHook, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import { API, ApiRequestError } from "@/api";
import { LeaveGuardProvider, useConfirmLeave } from "@/components/shared/edit-unit/LeaveGuard";
import { useDraftEditor } from "@/hooks/useDraftEditor";
import type { EpisodeDraftView, SaveEpisodeDraftResult } from "@/types";

type Content = { units: { text: string }[] };
const narrow = (content: Record<string, unknown> | null) => content as Content | null;
const noop = () => {};
const edited = { units: [{ text: "我的修改" }] };

function draft(revision = "r1", text = "待修复正文"): EpisodeDraftView {
  return {
    doc_type: "reference_prompt_authoring", revision, editable_by: "user", episode: 1, item_ids: ["E1U1"],
    content: { units: [{ text }] }, formal_exists: true, soft_violations: [],
    violations: [{ code: "unregistered_mention", message: "资产未登记", label: "", line: null }],
  };
}

function setup(view = draft()) {
  return renderHook(({ episode, view: next }) => useDraftEditor<Content>({
    projectName: "p", episode, view: next, narrow, onSettled: noop,
  }), { initialProps: { episode: 1, view } });
}

describe("useDraftEditor recovery", () => {
  afterEach(() => vi.restoreAllMocks());

  it("retains local edits after a revision conflict until explicitly reloading", async () => {
    vi.spyOn(API, "saveEpisodeDraft").mockRejectedValue(new ApiRequestError("冲突", { code: "revision_conflict" }, 409));
    const { result, rerender } = setup();
    act(() => result.current.setContent(() => edited));
    let saved: boolean | undefined;
    await act(async () => { saved = await result.current.save(); });
    expect(saved).toBe(false);
    expect(result.current.content).toEqual(edited);
    expect(result.current.dirty).toBe(true);
    rerender({ episode: 1, view: draft("r2", "另一处的修改") });
    expect(result.current.outdated).toBe(true);
    expect(result.current.content).toEqual(edited);
    act(() => result.current.reloadLatest());
    expect(result.current.content?.units[0].text).toBe("另一处的修改");
    expect(result.current.dirty).toBe(false);
  });

  it("saves and revalidates an unchanged invalid draft", async () => {
    const view = draft();
    vi.spyOn(API, "saveEpisodeDraft").mockResolvedValue({ episode: 1, doc_type: view.doc_type, adopted: false, draft: { ...view, revision: "r2" } });
    const { result } = setup(view);
    let saved: boolean | undefined;
    await act(async () => { saved = await result.current.save(); });
    expect(saved).toBe(true);
    expect(result.current.content).toEqual(view.content);
    expect(result.current.dirty).toBe(false);
    expect(result.current.saving).toBe(false);
  });

  it("does not apply a late save response to the next episode", async () => {
    let finish!: (value: SaveEpisodeDraftResult) => void;
    vi.spyOn(API, "saveEpisodeDraft").mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    const { result, rerender } = setup();
    act(() => result.current.setContent(() => edited));
    let saving!: Promise<boolean>;
    act(() => { saving = result.current.save(); });
    expect(result.current.saving).toBe(true);
    rerender({ episode: 2, view: draft("r1", "第二集正文") });
    expect(result.current.content?.units[0].text).toBe("第二集正文");
    expect(result.current.dirty).toBe(false);
    expect(result.current.saving).toBe(false);
    await act(async () => {
      finish({ episode: 1, doc_type: "reference_prompt_authoring", adopted: false, draft: draft("r2", "第一集已保存") });
      await saving;
    });
    expect(result.current.content?.units[0].text).toBe("第二集正文");
    expect(result.current.saving).toBe(false);
  });

  it("retains edits when discarding a stale draft is rejected", async () => {
    vi.spyOn(API, "discardEpisodeDraft").mockRejectedValue(new ApiRequestError("冲突", { code: "revision_conflict" }, 409));
    const { result } = setup();
    act(() => result.current.setContent(() => edited));
    await act(async () => { await result.current.discard(); });
    expect(result.current.content).toEqual(edited);
    expect(result.current.dirty).toBe(true);
    expect(result.current.discarding).toBe(false);
  });

  it("keeps the editor open if saving through the leave guard fails", async () => {
    vi.spyOn(API, "saveEpisodeDraft").mockRejectedValue(new Error("保存失败"));
    const user = userEvent.setup();
    const left = vi.fn();
    const view = draft();
    function Editor() {
      const editor = useDraftEditor<Content>({ projectName: "p", episode: 1, view, narrow, onSettled: noop });
      const leave = useConfirmLeave();
      return <>
        <textarea aria-label="草稿正文" value={editor.content?.units[0].text ?? ""} onChange={() => editor.setContent(() => edited)} />
        <button onClick={() => editor.setContent(() => edited)}>修改</button>
        <button onClick={() => leave(left)}>离开</button>
      </>;
    }
    render(<LeaveGuardProvider><Editor /></LeaveGuardProvider>);
    await user.click(screen.getByRole("button", { name: "修改" }));
    await user.click(screen.getByRole("button", { name: "离开" }));
    expect(screen.getByRole("alertdialog")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "保存并离开" }));
    expect(screen.getByRole("textbox", { name: "草稿正文" })).toHaveValue("我的修改");
    expect(left).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "离开" }));
    expect(screen.getByRole("alertdialog")).toBeVisible();
  });
});
