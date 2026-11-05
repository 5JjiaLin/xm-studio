import { useCallback, useEffect, useRef, useState } from "react";
import { API } from "@/api";
import { useProjectsStore } from "@/stores/projects-store";
import { useTaskRefresh } from "@/hooks/useTaskRefresh";
import { useTasksStore } from "@/stores/tasks-store";
import type { ReferenceVideoUnit } from "@/types/reference-video";
import type { ProductionState } from "@/types/production";
import type { ScriptReviewState } from "@/types";
import type { WorkflowStatus } from "@/types/workflow";
import { errMsg } from "@/utils/async";

/** Single project stream; refreshes content without platform navigation or Agent sessions. */
export function useProductionProject(projectName: string | null, requestedEpisode: number | null = null) {
  const project = useProjectsStore((s) => s.currentProjectData);
  const scripts = useProjectsStore((s) => s.currentScripts);
  const [episode, setEpisode] = useState<number | null>(null);
  const [production, setProduction] = useState<ProductionState | null>(null);
  const [review, setReview] = useState<ScriptReviewState | null>(null);
  const refreshRef = useRef<() => void>(() => {});
  const reload = useCallback(() => refreshRef.current(), []);
  const [workflow, setWorkflow] = useState<WorkflowStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(!!projectName);
  const [units, setUnits] = useState<ReferenceVideoUnit[]>([]);
  const [source, setSource] = useState("");
  useTaskRefresh(projectName, !!projectName);
  useEffect(() => {
    const controller = new AbortController();
    const store = useProjectsStore.getState();
    store.setCurrentProject(projectName, null);
    if (!projectName) return () => controller.abort();
    let running = false;
    let again = false;
    const refresh = async () => {
      if (running) { again = true; return; }
      running = true;
      do {
        again = false;
        try {
          const result = await store.refreshProject(projectName, { onError: (e) => { if (!controller.signal.aborted) setError(errMsg(e)); } });
          if (result !== "success" || controller.signal.aborted) { running = false; return; }
          const current = useProjectsStore.getState().currentProjectData;
          const episodes = current?.episodes ?? [];
          // Legacy links resolve only when there is exactly one episode. Never guess.
          const selected = requestedEpisode ?? (episodes.length === 1 ? episodes[0].episode : null);
          const entry = episodes.find((item) => item.episode === selected);
          if (!entry || current?.production_management?.archived || current?.production_management?.archived_episodes?.includes(entry.episode)) {
            setEpisode(null); setLoading(false); running = false; return;
          }
          const episode = entry.episode;
          setEpisode(episode);
          const status = await API.getWorkflowStatus(projectName, { signal: controller.signal, episode });
          if (controller.signal.aborted) { running = false; return; }
          setWorkflow(status); setError(null);
          if (episode) {
            const [progress, content] = await Promise.all([API.getProduction(projectName, episode, { signal: controller.signal }), API.getScriptReview(projectName, episode, { signal: controller.signal })]);
            if (controller.signal.aborted) { running = false; return; }
            setProduction(progress); setReview(content);
          }
          if (episode && status.content?.formal_script === "present" && useProjectsStore.getState().currentProjectData?.generation_mode === "reference_video") {
            const data = await API.listReferenceVideoUnits(projectName, episode, { signal: controller.signal });
            if (!controller.signal.aborted) setUnits(data.units);
          } else setUnits([]);
          const path = status.content?.episode_source === "absent" ? null : status.target?.source;
          if (path) {
            const text = await API.getSourceContent(projectName, path.replace(/^source\//, ""), { signal: controller.signal });
            if (!controller.signal.aborted) setSource(text);
          } else setSource("");
        } catch (e) {
          if (!controller.signal.aborted) setError(errMsg(e));
        } finally {
          if (!controller.signal.aborted) setLoading(false);
        }
      } while (again && !controller.signal.aborted);
      running = false;
    };
    refreshRef.current = () => { void refresh(); };
    void refresh();
    const stream = API.openProjectEventStream({
      projectName,
      onSnapshot: () => { void refresh(); },
      onChanges: () => {
        void refresh();
        void useTasksStore.getState().refreshTasks();
      },
      onProjectDeleted: () => { controller.abort(); store.setCurrentProject(null, null); setError("Project deleted"); setLoading(false); },
    });
    const timer = setInterval(() => { void refresh(); }, 3000);
    return () => { clearInterval(timer); controller.abort(); stream.close(); store.setCurrentProject(null, null); };
  }, [projectName, requestedEpisode]);
  return { episode, project: useProjectsStore.getState().currentProjectName === projectName ? project : null, scripts, units, source, workflow, production, review, refresh: reload, loading, error };
}
