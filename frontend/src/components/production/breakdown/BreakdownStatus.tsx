import { CheckCircle2, Circle, CircleAlert, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { TaskElapsedReadout } from "@/components/shared/TaskElapsedReadout";
import type { TaskItem, ScriptReviewState } from "@/types";

export function BreakdownStatus({ task, busy, review }: { task?: TaskItem; busy: boolean; review: ScriptReviewState | null }) {
  const { t } = useTranslation("dashboard");
  const failed = task?.status === "failed" || task?.status === "cancelled";
  const ready = !busy && Boolean(review?.content) && !review?.quarantine;
  const violations = review?.quarantine?.violations ?? [];
  const draft = review?.quarantine;
  const coverage = (draft ? draft.coverage_advisories : review?.coverage_advisories) ?? [];
  const canAdopt = !busy && draft?.editable_by === "user" && Boolean(draft.content && draft.revision) && violations.length === 0;
  const phase = review?.repair_progress?.task_id === task?.task_id ? review?.repair_progress?.phase : null;
  const received = busy || Boolean(task) || ready || canAdopt;
  const running = task?.status === "running";
  const syncing = !busy && !ready && !canAdopt && !draft && task?.status === "succeeded";
  const content = ready && review?.content && "units" in review.content ? review.content : null;
  const title = busy ? running ? "production_breakdown_running" : "production_breakdown_queued" : canAdopt ? "production_breakdown_adoptable" : ready ? "production_breakdown_ready" : failed ? "production_breakdown_failed" : task?.status === "succeeded" ? "production_breakdown_syncing" : "production_breakdown_empty";
  const stages = [
    { label: "production_breakdown_received", done: received, current: false },
    { label: "production_breakdown_model", done: ready || canAdopt || task?.status === "succeeded", current: busy },
    { label: "production_breakdown_review", done: ready, current: !canAdopt && !ready && task?.status === "succeeded" },
  ];
  return <section aria-label={t("production_breakdown_progress")} className="flex flex-col gap-4 rounded-xl border border-border bg-card p-5">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div role="status" className="flex items-center gap-2 font-medium">
        {busy ? <Loader2 aria-hidden className="size-5 animate-spin text-primary" /> : failed && !ready && !canAdopt ? <CircleAlert aria-hidden className="size-5 text-destructive" /> : ready || canAdopt ? <CheckCircle2 aria-hidden className="size-5 text-primary" /> : <Circle aria-hidden className="size-5 text-muted-foreground" />}
        {busy && phase === "repairing" ? t("production_repair_running") : busy && (phase === "validating" || phase === "revalidating" || phase === "committing") ? t("production_repair_validating") : t(title)}
      </div>
      {task && <span className="flex items-center gap-2 text-sm text-muted-foreground"><span>{t(busy ? running ? "production_breakdown_elapsed" : "production_breakdown_waited" : "production_breakdown_duration")}</span><TaskElapsedReadout task={task} /></span>}
    </div>
    <ol className="flex flex-wrap gap-4">{stages.map((stage, index) => <li key={stage.label} className="flex items-center gap-2 text-sm">
      {stage.done ? <CheckCircle2 aria-hidden className="size-4 text-primary" /> : stage.current ? <Loader2 aria-hidden className="size-4 animate-spin text-primary" /> : <Circle aria-hidden className="size-4 text-muted-foreground" />}
      <span>{index + 1}. {t(stage.label)}</span>
    </li>)}</ol>
    <p className="text-sm text-muted-foreground">{t(busy ? "production_breakdown_wait_hint" : canAdopt ? "draft_no_violations_hint" : ready ? "production_breakdown_ready_hint" : failed ? task?.error_code === "draft_quarantined" ? "production_breakdown_quarantined_hint" : "production_breakdown_retry_hint" : syncing ? "production_breakdown_syncing_hint" : "production_breakdown_empty_hint")}</p>
    {content && <p className="text-sm">{t("production_breakdown_summary", { units: content.units.length, characters: content.new_assets?.filter((asset) => asset.type === "character").length ?? 0, scenes: content.new_assets?.filter((asset) => asset.type === "scene").length ?? 0, props: content.new_assets?.filter((asset) => asset.type === "prop").length ?? 0 })}</p>}
    {review?.coverage_advisory_mode && <div className="rounded border border-border p-3 text-sm">
      <p>{t("production_coverage_advisory_mode")}</p>
      {!busy && coverage.length > 0 && <details><summary className="cursor-pointer">{t("production_coverage_advisory_count", { total: coverage.length })}</summary>
        <ul className="relative mt-2 max-h-64 overflow-auto">{coverage.map((line, index) => <li key={index} className="whitespace-pre-wrap break-words">{line}</li>)}</ul></details>}
    </div>}
    {!busy && violations.length > 0 && <div role="alert" className="flex flex-col gap-2 text-sm text-destructive"><p>{t("production_repair_problem_count", { count: violations.length })}</p><ul className="list-disc pl-5">{violations.map((issue, index) => <li key={`${issue.code}-${index}`}>{issue.message}</li>)}</ul></div>}
    {failed && !ready && task?.error_message && (!canAdopt && violations.length === 0
      ? <p role="alert" className="whitespace-pre-wrap wrap-break-word text-sm text-destructive">{task.error_message}</p>
      : <details className="text-sm"><summary className="cursor-pointer text-muted-foreground">{t("production_repair_details")}</summary><p className="mt-2 whitespace-pre-wrap wrap-break-word text-destructive">{task.error_message}</p></details>)}
  </section>;
}
