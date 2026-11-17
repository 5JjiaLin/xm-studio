import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { API, type ProjectEventStreamOptions } from "@/api";
import { useProjectsStore } from "@/stores/projects-store";
import { useAppStore } from "@/stores/app-store";
import { defaultTaskStats, useTasksStore } from "@/stores/tasks-store";
import { createDeferred } from "@/test/deferred";
import { FakeSseStream } from "@/test/fakeSseStream";
import type { ProductionState } from "@/types/production";
import type { WorkflowStatus } from "@/types/workflow";
import type { ScriptReviewState } from "@/types";
import { useProductionProject } from "./useProductionProject";

function project(title = "项目"): Awaited<ReturnType<typeof API.getProject>> {
  return { project: {
    title, content_mode: "drama", generation_mode: "reference_video", style: "写实",
    episodes: [1, 2].map((episode) => ({ episode, title: `第${episode}集`, script_file: `scripts/episode_${episode}.json` })),
    characters: {}, scenes: {}, props: {},
  }, scripts: {} };
}

function workflow(episode: number): WorkflowStatus {
  return {
    schema_version: 2, project_revision: "p1", source_revision: "s1",
    project: { content_mode: "drama", generation_mode: "reference_video", grid_storyboard: false },
    target: { episode, script: `scripts/episode_${episode}.json`, script_filename: `episode_${episode}.json`, source: `source/episode_${episode}.txt` },
    blockers: [], issues: [], operations: {}, gates: {}, artifacts: {},
    next_action: { type: "none", args: {}, requested_ids: [], requires_confirmation: false, reason: "" }, next_alternatives: [],
    content: {
      episode_count: 2, whole_source: "absent", source_remaining: false, ad_inputs: "not_applicable",
      products_without_selling_points: [], episode_source: "present", episode_plan_stale: false,
      drafts: [], formal_script: "present", pending_authoring_ids: [], needs_replan_ids: [],
      referenced_assets_without_sheet: [], unregistered_references: [], referenced_asset_sheets_stale: [],
      referenced_assets_without_description: [],
    },
  };
}

function review(episode: number): ScriptReviewState {
  return {
    episode, content_mode: "drama", status: "confirmed", fingerprint: `fp${episode}`, confirmed_at: null,
    content: null, quarantine: null, supported_durations: null, duration_tiers: null,
    episode_target_duration: null, script_overwrite: null,
  };
}

describe("useProductionProject scoped reads", () => {
  const streams: ProjectEventStreamOptions[] = [];

  beforeEach(() => {
    useProjectsStore.setState(useProjectsStore.getInitialState(), true);
    useAppStore.setState(useAppStore.getInitialState(), true);
    useTasksStore.setState(useTasksStore.getInitialState(), true);
    streams.length = 0;
    FakeSseStream.reset();
    vi.spyOn(API, "listTasks").mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    vi.spyOn(API, "getTaskStats").mockResolvedValue({ stats: defaultTaskStats });
    vi.spyOn(API, "getProject").mockResolvedValue(project());
    vi.spyOn(API, "openProjectEventStream").mockImplementation((options) => {
      streams.push(options);
      return new FakeSseStream();
    });
    vi.spyOn(API, "getWorkflowStatus").mockImplementation(async (_name, options) => workflow(options?.episode ?? 1));
    vi.spyOn(API, "getProduction").mockImplementation(async (_name, episode) => ({ episode, status: "idle", stage: null }));
    vi.spyOn(API, "getScriptReview").mockImplementation(async (_name, episode) => review(episode));
    vi.spyOn(API, "listReferenceVideoUnits").mockImplementation(async (_name, episode) => ({
      units: [{ unit_id: `E${episode}U1`, text: `第${episode}集`, duration_seconds: 12, note: null }],
      unit_capabilities: {},
    }));
    vi.spyOn(API, "getSourceContent").mockImplementation(async (_name, filename) => filename);
  });

  afterEach(() => vi.restoreAllMocks());

  it("commits project, review, units and source only when every read has completed", async () => {
    const pending = createDeferred<string>();
    vi.spyOn(API, "getSourceContent").mockReturnValue(pending.promise);
    const { result } = renderHook(() => useProductionProject("p", 1));
    await waitFor(() => expect(API.getSourceContent).toHaveBeenCalledTimes(1));
    expect(result.current.project).toBeNull();
    expect(result.current.review).toBeNull();
    expect(result.current.units).toEqual([]);
    expect(result.current.loading).toBe(true);
    await act(async () => pending.resolve("第一集原文"));
    expect(result.current.episode).toBe(1);
    expect(result.current.review?.episode).toBe(1);
    expect(result.current.units[0].unit_id).toBe("E1U1");
    expect(result.current.source).toBe("第一集原文");
    expect(result.current.loading).toBe(false);
  });

  it("hides the previous episode immediately and ignores its late response", async () => {
    const late = createDeferred<ProductionState>();
    const get = vi.spyOn(API, "getProduction");
    const { result, rerender } = renderHook(({ episode }) => useProductionProject("p", episode), { initialProps: { episode: 1 } });
    await waitFor(() => expect(result.current.episode).toBe(1));
    get.mockReturnValueOnce(late.promise);
    act(() => result.current.refresh());
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    rerender({ episode: 2 });
    expect(result.current.units).toEqual([]);
    expect(result.current.source).toBe("");
    expect(result.current.review).toBeNull();
    await waitFor(() => expect(result.current.episode).toBe(2));
    await act(async () => late.resolve({ episode: 1, status: "failed", stage: null }));
    expect(result.current.production?.episode).toBe(2);
    expect(result.current.units[0].unit_id).toBe("E2U1");
    expect(result.current.source).toBe("episode_2.txt");
  });

  it("does not expose a new project together with an old project's units", async () => {
    const pending = createDeferred<WorkflowStatus>();
    const { result, rerender } = renderHook(({ name }) => useProductionProject(name, 1), { initialProps: { name: "p" } });
    await waitFor(() => expect(result.current.episode).toBe(1));
    vi.spyOn(API, "getWorkflowStatus").mockReturnValueOnce(pending.promise);
    vi.spyOn(API, "getProject").mockResolvedValue(project("新项目"));
    rerender({ name: "q" });
    await waitFor(() => expect(useProjectsStore.getState().currentProjectData?.title).toBe("新项目"));
    expect(result.current.project).toBeNull();
    expect(result.current.units).toEqual([]);
    await act(async () => pending.resolve(workflow(1)));
    expect(result.current.project?.title).toBe("新项目");
  });

  it("discards the entire combination and rereads when an SSE snapshot arrives mid-read", async () => {
    const pending = createDeferred<string>();
    const source = vi.spyOn(API, "getSourceContent").mockReturnValueOnce(pending.promise).mockResolvedValue("新版原文");
    const { result } = renderHook(() => useProductionProject("p", 1));
    await waitFor(() => expect(source).toHaveBeenCalledTimes(1));
    act(() => streams[0].onSnapshot?.({ project_name: "p", fingerprint: "new", generated_at: "2026-11-12" }));
    await act(async () => pending.resolve("旧版原文"));
    await waitFor(() => expect(result.current.source).toBe("新版原文"));
    expect(API.getProject).toHaveBeenCalledTimes(2);
    expect(source).toHaveBeenCalledTimes(2);
  });

  it("rereads after a change during the project request as well as during detail requests", async () => {
    const pending = createDeferred<Awaited<ReturnType<typeof API.getProject>>>();
    const get = vi.spyOn(API, "getProject").mockReturnValueOnce(pending.promise).mockResolvedValue(project("新版"));
    const { result } = renderHook(() => useProductionProject("p", 1));
    act(() => streams[0].onSnapshot?.({ project_name: "p", fingerprint: "new", generated_at: "2026-11-12" }));
    await act(async () => pending.resolve(project("旧版")));
    await waitFor(() => expect(result.current.project?.title).toBe("新版"));
    expect(get).toHaveBeenCalledTimes(2);
  });

  it("discards a read if another consumer updates the shared project revision", async () => {
    const pending = createDeferred<string>();
    const source = vi.spyOn(API, "getSourceContent").mockReturnValueOnce(pending.promise).mockResolvedValue("已重读");
    const { result } = renderHook(() => useProductionProject("p", 1));
    await waitFor(() => expect(source).toHaveBeenCalledTimes(1));
    act(() => useProjectsStore.getState().setCurrentProject("p", project("外部修改").project));
    await act(async () => pending.resolve("失效原文"));
    await waitFor(() => expect(result.current.source).toBe("已重读"));
    expect(API.getProject).toHaveBeenCalledTimes(2);
  });

  it("keeps the last complete display on failure, blocks actions, and recovers without unmounting", async () => {
    const get = vi.spyOn(API, "getProduction");
    const { result } = renderHook(() => useProductionProject("p", 1));
    await waitFor(() => expect(result.current.episode).toBe(1));
    const savedProject = result.current.project;
    get.mockRejectedValueOnce(new Error("读取失败"));
    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.error).toBe("读取失败"));
    expect(result.current.project).toBe(savedProject);
    expect(result.current.units[0].unit_id).toBe("E1U1");
    expect(result.current.loading).toBe(false);
    expect(result.current.refreshing).toBe(true);
    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.refreshing).toBe(false));
    expect(result.current.error).toBeNull();
  });

  it("keeps the editor mounted while a normal refresh is pending", async () => {
    const pending = createDeferred<string>();
    const { result } = renderHook(() => useProductionProject("p", 1));
    await waitFor(() => expect(result.current.episode).toBe(1));
    const savedProject = result.current.project;
    vi.spyOn(API, "getSourceContent").mockReturnValueOnce(pending.promise);
    act(() => result.current.refresh());
    await waitFor(() => expect(API.getSourceContent).toHaveBeenCalledTimes(2));
    expect(result.current.loading).toBe(false);
    expect(result.current.refreshing).toBe(true);
    expect(result.current.project).toBe(savedProject);
    expect(result.current.episode).toBe(1);
    await act(async () => pending.resolve("刷新后"));
    expect(result.current.refreshing).toBe(false);
    expect(result.current.source).toBe("刷新后");
  });

  it("retains a failed shared-project refresh as an error rather than exposing a ready old snapshot", async () => {
    const { result } = renderHook(() => useProductionProject("p", 1));
    await waitFor(() => expect(result.current.episode).toBe(1));
    vi.spyOn(API, "getProject").mockRejectedValueOnce(new Error("项目读取失败"));
    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.error).toBe("项目读取失败"));
    expect(result.current.loading).toBe(false);
    expect(result.current.refreshing).toBe(true);
    expect(result.current.episode).toBe(1);
    expect(API.getProduction).toHaveBeenCalledTimes(1);
  });

  it("clears episode content for an invalid episode without guessing", async () => {
    const { result, rerender } = renderHook(({ episode }) => useProductionProject("p", episode), { initialProps: { episode: 1 } });
    await waitFor(() => expect(result.current.episode).toBe(1));
    rerender({ episode: 99 });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.project).not.toBeNull();
    expect(result.current.episode).toBeNull();
    expect(result.current.workflow).toBeNull();
    expect(result.current.units).toEqual([]);
    expect(result.current.source).toBe("");
  });

  it("ends loading and closes the stream when the project is deleted", async () => {
    const { result, unmount } = renderHook(() => useProductionProject("p", 1));
    await waitFor(() => expect(result.current.episode).toBe(1));
    act(() => streams[0].onProjectDeleted?.({ project_name: "p" }));
    expect(result.current.project).toBeNull();
    expect(result.current.units).toEqual([]);
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBe("项目已移入回收站，请返回项目列表查看。");
    expect(FakeSseStream.instances[0].closed).toBe(true);
    unmount();
    expect(useProjectsStore.getState().currentProjectName).toBeNull();
  });

  it("aborts pending reads and ignores late events after unmount", async () => {
    const pending = createDeferred<string>();
    vi.spyOn(API, "getSourceContent").mockReturnValue(pending.promise);
    const { unmount } = renderHook(() => useProductionProject("p", 1));
    await waitFor(() => expect(API.getSourceContent).toHaveBeenCalledTimes(1));
    const signal = vi.mocked(API.getSourceContent).mock.calls[0][2]?.signal;
    unmount();
    expect(signal?.aborted).toBe(true);
    expect(FakeSseStream.instances[0].closed).toBe(true);
    const calls = vi.mocked(API.getProject).mock.calls.length;
    act(() => streams[0].onSnapshot?.({ project_name: "p", fingerprint: "late", generated_at: "2026-11-12" }));
    await act(async () => pending.resolve("迟到"));
    expect(API.getProject).toHaveBeenCalledTimes(calls);
    expect(useProjectsStore.getState().currentProjectName).toBeNull();
  });
});
