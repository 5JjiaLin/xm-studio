import { CheckCircle2, Circle, CircleAlert, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { TaskElapsedReadout } from "@/components/shared/TaskElapsedReadout";
import type { TaskItem, ScriptReviewState } from "@/types";

export function BreakdownStatus({ task, busy, review }: { task?: TaskItem; busy: boolean; review: ScriptReviewState | null }) {
  const { t } = useTranslation("dashboard");
  const failed = task?.status === "failed" || task?.status === "cancelled";
  const ready = !busy && Boolean(review?.content);
  const received = busy || Boolean(task) || ready;
  const running = task?.status === "running";
  const content = ready && review?.content && "units" in review.content ? review.content : null;
  const title = busy ? running ? "production_breakdown_running" : "production_breakdown_queued" : ready ? "production_breakdown_ready" : failed ? "production_breakdown_failed" : task?.status === "succeeded" ? "production_breakdown_syncing" : "production_breakdown_empty";
  const stages = [
    { label: "production_breakdown_received", done: received, current: false },
    { label: "production_breakdown_model", done: ready || task?.status === "succeeded", current: busy },
    { label: "production_breakdown_review", done: ready, current: !ready && task?.status === "succeeded" },
  ];
  return <section aria-label={t("production_breakdown_progress")} className="flex flex-col gap-4 rounded-xl border border-border bg-card p-5">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div role="status" className="flex items-center gap-2 font-medium">
        {busy ? <Loader2 aria-hidden className="size-5 animate-spin text-primary" /> : failed && !ready ? <CircleAlert aria-hidden className="size-5 text-destructive" /> : ready ? <CheckCircle2 aria-hidden className="size-5 text-primary" /> : <Circle aria-hidden className="size-5 text-muted-foreground" />}
        {t(title)}
      </div>
      {task && <span className="flex items-center gap-2 text-sm text-muted-foreground"><span>{t(busy ? running ? "production_breakdown_elapsed" : "production_breakdown_waited" : "production_breakdown_duration")}</span><TaskElapsedReadout task={task} /></span>}
    </div>
    <ol className="flex flex-wrap gap-4">{stages.map((stage, index) => <li key={stage.label} className="flex items-center gap-2 text-sm">
      {stage.done ? <CheckCircle2 aria-hidden className="size-4 text-primary" /> : stage.current ? <Loader2 aria-hidden className="size-4 animate-spin text-primary" /> : <Circle aria-hidden className="size-4 text-muted-foreground" />}
      <span>{index + 1}. {t(stage.label)}</span>
    </li>)}</ol>
    <p className="text-sm text-muted-foreground">{t(busy ? "production_breakdown_wait_hint" : ready ? "production_breakdown_ready_hint" : failed ? "production_breakdown_retry_hint" : "production_breakdown_empty_hint")}</p>
    {content && <p className="text-sm">{t("production_breakdown_summary", { units: content.units.length, characters: content.new_assets?.filter((asset) => asset.type === "character").length ?? 0, scenes: content.new_assets?.filter((asset) => asset.type === "scene").length ?? 0, props: content.new_assets?.filter((asset) => asset.type === "prop").length ?? 0 })}</p>}
    {failed && !ready && task?.error_message && <p role="alert" className="whitespace-pre-wrap wrap-break-word text-sm text-destructive">{task.error_message}</p>}
  </section>;
}
