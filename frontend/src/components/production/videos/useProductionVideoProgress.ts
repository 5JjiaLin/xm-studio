import { useLatestTasksByResource, useTaskRowsByIds } from "@/stores/tasks-store";
import type { TaskItem } from "@/types";
import type { ProductionState } from "@/types/production";
import type { ReferenceVideoUnit } from "@/types/reference-video";

export interface VideoProgress {
  status: "waiting" | "submitting" | "queued" | "running" | "succeeded" | "failed" | "unknown";
  task?: TaskItem;
  detail?: string | null;
}

export function useProductionVideoProgress(project: string, units: ReferenceVideoUnit[], production: ProductionState | null) {
  const latest = useLatestTasksByResource(project, "reference_video");
  const members = production?.action === "videos" ? production.members ?? [] : [];
  const tasks = useTaskRowsByIds(members.flatMap((member) => member.task_id ? [member.task_id] : []));
  return new Map(units.map((unit): [string, VideoProgress] => {
    const member = members.find((item) => item.unit_id === unit.unit_id);
    const candidate = latest.get(unit.unit_id);
    const task = member?.task_id
      ? tasks.get(member.task_id)
      : candidate;
    // The same task only advances: a slower queue refresh must not hide a completed batch member.
    const rank = (status?: string) => status === "queued" ? 1 : status === "running" ? 2 : status ? 3 : 0;
    const observed = rank(task?.status) > rank(member?.status) ? task?.status : member?.status;
    const status: VideoProgress["status"] = observed === "running" || observed === "queued" || observed === "succeeded" ? observed
      : observed === "failed" || observed === "cancelled" || observed === "blocked" ? "failed"
      : unit.generated_assets?.video_clip ? "succeeded"
      : production?.action === "videos" && production.status === "submitting" ? "submitting"
      : production?.action === "videos" && production.status === "unknown" ? "unknown" : "waiting";
    return [unit.unit_id, { status, task: task?.status === observed ? task : undefined, detail: status === "failed" ? task?.error_message || member?.problem?.detail : null }];
  }));
}
