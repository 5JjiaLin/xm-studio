import { useCallback, useEffect, useRef, useState } from "react";
import { API } from "@/api";
import { useProjectsStore } from "@/stores/projects-store";
import { useTaskRefresh } from "@/hooks/useTaskRefresh";
import { useTasksStore } from "@/stores/tasks-store";
import type { ReferenceVideoUnit } from "@/types/reference-video";
import type { ProductionState } from "@/types/production";
import type { EpisodeScript, ProjectData, ScriptReviewState } from "@/types";
import type { WorkflowStatus } from "@/types/workflow";
import { errMsg } from "@/utils/async";
import i18n from "@/i18n";

interface ProductionSnapshot {
  projectName: string;
  requestedEpisode: number | null;
  revision: number;
  project: ProjectData;
  scripts: Record<string, EpisodeScript>;
  episode: number | null;
  production: ProductionState | null;
  review: ScriptReviewState | null;
  workflow: WorkflowStatus | null;
  units: ReferenceVideoUnit[];
  source: string;
  sourceRevision: string | null;
}

/** One scoped read, committed together; polling never replaces the user's edit buffers. */
export function useProductionProject(projectName: string | null, requestedEpisode: number | null = null) {
  const storeName = useProjectsStore((s) => s.currentProjectName);
  const storeRevision = useProjectsStore((s) => projectName ? s.projectSnapshotRevisions[projectName] : undefined);
  const [snapshot, setSnapshot] = useState<ProductionSnapshot | null>(null);
  const [failure, setFailure] = useState<{ projectName: string; requestedEpisode: number | null; message: string } | null>(null);
  const refreshRef = useRef<() => void>(() => {});
  const reload = useCallback(() => refreshRef.current(), []);
  useTaskRefresh(projectName, !!projectName);
  useEffect(() => {
    const controller = new AbortController();
    const store = useProjectsStore.getState();
    store.setCurrentProject(projectName, null);
    if (!projectName) return () => controller.abort();
    let running = false;
    let again = false;
    let changes = 0;
    const isCurrent = () => !controller.signal.aborted && useProjectsStore.getState().currentProjectName === projectName;
    const refresh = async () => {
      if (!isCurrent()) return;
      if (running) { again = true; return; }
      running = true;
      try {
        do {
          again = false;
          try {
            const changeBasis = changes;
            const result = await store.refreshProject(projectName, { onError: (cause) => {
              if (isCurrent()) setFailure({ projectName, requestedEpisode, message: errMsg(cause) });
            } });
            if (result !== "success" || !isCurrent()) return;
            const basis = useProjectsStore.getState();
            const current = basis.currentProjectData;
            if (!current) return;
            const revision = basis.projectSnapshotRevisions[projectName];
            const episodes = current.episodes ?? [];
            // Legacy links resolve only when there is exactly one episode. Never guess.
            const selected = requestedEpisode ?? (episodes.length === 1 ? episodes[0].episode : null);
            const entry = episodes.find((item) => item.episode === selected);
            let episode: number | null = null;
            let workflow: WorkflowStatus | null = null;
            let production: ProductionState | null = null;
            let review: ScriptReviewState | null = null;
            let units: ReferenceVideoUnit[] = [];
            let source = "";
            let sourceRevision: string | null = null;
            if (entry && !current.production_management?.archived && !current.production_management?.archived_episodes?.includes(entry.episode)) {
              episode = entry.episode;
              workflow = await API.getWorkflowStatus(projectName, { signal: controller.signal, episode });
              if (!isCurrent()) return;
              const path = workflow.content?.episode_source === "absent" ? null : workflow.target?.source;
              const [progress, content, unitData, text] = await Promise.all([
                API.getProduction(projectName, episode, { signal: controller.signal }),
                API.getScriptReview(projectName, episode, { signal: controller.signal }),
                workflow.content?.formal_script === "present" && current.generation_mode === "reference_video"
                  ? API.listReferenceVideoUnits(projectName, episode, { signal: controller.signal })
                  : Promise.resolve({ units: [] }),
                path ? API.getSourceContent(projectName, path.replace(/^source\//, ""), {
                  signal: controller.signal,
                  onRevision: (value) => { sourceRevision = value; },
                }) : Promise.resolve(""),
              ]);
              if (!isCurrent()) return;
              production = progress; review = content; units = unitData.units; source = text;
            }
            if (!isCurrent()) return;
            // A refresh or event during these reads invalidates the entire combination.
            if (revision !== useProjectsStore.getState().projectSnapshotRevisions[projectName] || changeBasis !== changes) {
              again = true;
              continue;
            }
            setSnapshot({ projectName, requestedEpisode, revision, project: current, scripts: basis.currentScripts, episode, workflow, production, review, units, source, sourceRevision });
            setFailure(null);
          } catch (cause) {
            if (isCurrent()) setFailure({ projectName, requestedEpisode, message: errMsg(cause) });
          }
        } while (again && isCurrent());
      } finally {
        running = false;
      }
    };
    const requestRefresh = () => { void refresh(); };
    refreshRef.current = requestRefresh;
    requestRefresh();
    const stream = API.openProjectEventStream({
      projectName,
      onSnapshot: () => { if (isCurrent()) { changes += 1; requestRefresh(); } },
      onChanges: () => {
        if (!isCurrent()) return;
        changes += 1;
        requestRefresh();
        void useTasksStore.getState().refreshTasks();
      },
      onProjectDeleted: () => {
        if (!isCurrent()) return;
        controller.abort();
        stream.close();
        clearInterval(timer);
        refreshRef.current = () => {};
        store.setCurrentProject(null, null);
        setFailure({ projectName, requestedEpisode, message: i18n.t("dashboard:production_project_deleted") });
      },
    });
    // Periodic reads are best effort; don't keep the page in a continuous refresh
    // loop if one round is slow. Explicit refreshes and events still requeue.
    const timer = setInterval(() => { if (!running) requestRefresh(); }, 3000);
    return () => {
      clearInterval(timer); controller.abort(); stream.close();
      if (refreshRef.current === requestRefresh) refreshRef.current = () => {};
      if (useProjectsStore.getState().currentProjectName === projectName) store.setCurrentProject(null, null);
    };
  }, [projectName, requestedEpisode]);
  // Scope is checked during render, before the effect cancels the previous requests.
  const visible = storeName === projectName && snapshot?.projectName === projectName && snapshot.requestedEpisode === requestedEpisode ? snapshot : null;
  const error = failure?.projectName === projectName && failure.requestedEpisode === requestedEpisode ? failure.message : null;
  return {
    episode: visible?.episode ?? null,
    project: visible?.project ?? null,
    scripts: visible?.scripts ?? {},
    units: visible?.units ?? [],
    source: visible?.source ?? "",
    sourceRevision: visible?.sourceRevision ?? null,
    workflow: visible?.workflow ?? null,
    production: visible?.production ?? null,
    review: visible?.review ?? null,
    refresh: reload,
    loading: !!projectName && !visible && !error,
    refreshing: !!visible && (visible.revision !== storeRevision || error !== null),
    error,
  };
}
