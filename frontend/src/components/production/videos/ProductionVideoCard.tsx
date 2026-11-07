import { useCallback, useEffect, useState, useRef } from "react";
import { CheckCircle2, CircleAlert, Clock3, Loader2 } from "lucide-react";
import { TaskElapsedReadout } from "@/components/shared/TaskElapsedReadout";
import type { VideoProgress } from "./useProductionVideoProgress";
import { useTranslation } from "react-i18next";
import { CharacterAnglePicker } from "../assets/CharacterAnglePicker";
import { API } from "@/api";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { useEditUnit } from "@/components/shared/edit-unit/useEditUnit";
import type { ReferenceVideoUnit, ReferenceUnitPromptPreview } from "@/types/reference-video";
import type { ProductionState } from "@/types/production";
import { errMsg } from "@/utils/async";
import { useProjectsStore } from "@/stores/projects-store";
import { isResourceBusy, selectActiveResourceIds, useTasksStore } from "@/stores/tasks-store";

export function ProductionVideoCard({
  project,
  episode,
  unit,
  index,
  production,
  progress,
  busy,
  onGenerate,
  onChanged,
  onTimelineValidation,
}: {
  project: string;
  episode: number;
  unit: ReferenceVideoUnit;
  index: number;
  production: ProductionState | null;
  progress: VideoProgress;
  busy: boolean;
  onGenerate: () => Promise<void>;
  onChanged: () => void;
  onTimelineValidation?: (key: string, blocked: boolean) => void;
}) {
  const { t } = useTranslation("dashboard");
  const [error, setError] = useState<string | null>(null);
  const occupied = useTasksStore((state) => selectActiveResourceIds(state.tasks, "reference_video", project, state.optimisticActive).has(unit.unit_id));
  const locked = busy || occupied || ["active", "submitting", "unknown"].includes(production?.status ?? "");
  const projectData = useProjectsStore((state) => state.currentProjectData);
  const characters = projectData?.characters;
  const fingerprints = useProjectsStore((state) => state.assetFingerprints);
  const [promptOpen, setPromptOpen] = useState(true);
  const [preview, setPreview] = useState<ReferenceUnitPromptPreview | null>(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const save = useCallback(async (value: { text: string; generation_prompt: string }) => {
    if (isResourceBusy("reference_video", project, unit.unit_id)) throw new Error(t("production_running"));
    const result = await API.patchReferenceVideoUnit(project, episode, unit.unit_id, { prompt: value.text, generation_prompt: value.generation_prompt });
    onChanged();
    return { text: result.unit.text, generation_prompt: result.unit.generation_prompt || "" };
  }, [episode, onChanged, project, unit.unit_id, t]);
  const editor = useEditUnit({ source: { text: unit.text, generation_prompt: unit.generation_prompt || "" }, save });
  const [usingManualPrompt, setUsingManualPrompt] = useState(!!unit.generation_prompt);
  const bodyChangedWithManual = usingManualPrompt && editor.value.text !== editor.savedValue.text;
  const previewController = useRef<AbortController | null>(null);
  useEffect(() => {
    if (!promptOpen) return;
    previewController.current?.abort();
    const controller = new AbortController();
    previewController.current = controller;
    void Promise.resolve().then(() => { if (!controller.signal.aborted) { setPreviewBusy(true); setError(null); } });
    void API.previewReferenceUnitPrompt(project, episode, unit.unit_id, editor.value.text, { signal: controller.signal, generationPrompt: editor.value.generation_prompt }).then((value) => { if (!controller.signal.aborted) setPreview(value); }).catch((cause) => { if (!controller.signal.aborted) setError(errMsg(cause)); }).finally(() => { if (!controller.signal.aborted) setPreviewBusy(false); });
    return () => controller.abort();
  }, [project, episode, unit.unit_id, editor.value.text, editor.value.generation_prompt, unit.pending_authoring, unit.prompt_format_version, promptOpen, projectData, fingerprints]);
  const timelineBlocked = preview?.problems?.some((problem) => problem.blocking) ?? false;
  useEffect(() => {
    const key = `${project}/${episode}/${unit.unit_id}`;
    onTimelineValidation?.(key, timelineBlocked);
    return () => onTimelineValidation?.(key, false);
  }, [project, episode, unit.unit_id, timelineBlocked, onTimelineValidation]);
  const focusIssue = (_source: string | undefined, line = 1) => {
    const target = document.getElementById(`video-prompt-${unit.unit_id}`);
    if (target instanceof HTMLTextAreaElement) {
      const start = target.value.split("\n").slice(0, line - 1).reduce((offset, text) => offset + text.length + 1, 0);
      target.focus();
      target.setSelectionRange(start, target.value.indexOf("\n", start) < 0 ? target.value.length : target.value.indexOf("\n", start));
    }
  };
  const source = unit.generated_assets?.video_clip;
  const failure = progress.detail;
  const generating = progress.status === "running" || progress.status === "submitting";
  const title = t("production_unit", { number: index + 1, seconds: unit.duration_seconds });
  return <article aria-label={title} className="rounded-lg border border-border bg-card p-4">
    <h2 className="text-sm font-medium">{title}</h2>
    <div role="status" className="mt-3 flex flex-wrap items-center gap-2 text-sm">
      {generating ? <Loader2 aria-hidden className="size-4 animate-spin text-primary" /> : progress.status === "failed" || progress.status === "unknown" ? <CircleAlert aria-hidden className="size-4 text-destructive" /> : progress.status === "succeeded" ? <CheckCircle2 aria-hidden className="size-4 text-primary" /> : <Clock3 aria-hidden className="size-4 text-muted-foreground" />}
      <span>{t(`production_video_${progress.status}`)}</span>
      {progress.task && <span className="flex items-center gap-1 text-muted-foreground"><span>{t(progress.status === "queued" ? "production_breakdown_waited" : progress.status === "running" ? "production_breakdown_elapsed" : "production_breakdown_duration")}</span><TaskElapsedReadout task={progress.task} /></span>}
    </div>
    {source ? <video controls src={API.getFileUrl(project, source, fingerprints[source] ?? null)} className="mt-3 w-full"><track kind="captions" /></video> : <div className="mt-3 flex aspect-video flex-col items-center justify-center gap-3 rounded-md bg-muted text-muted-foreground">
      {generating ? <Loader2 aria-hidden className="size-8 animate-spin" /> : progress.status === "failed" || progress.status === "unknown" ? <CircleAlert aria-hidden className="size-8" /> : <Clock3 aria-hidden className="size-8" />}
      <p className="text-sm">{t(progress.status === "succeeded" ? "production_video_syncing" : `production_video_${progress.status}`)}</p>
    </div>}
    <details className="mt-3" open={promptOpen} onToggle={(e) => setPromptOpen(e.currentTarget.open)}>
      <summary className="cursor-pointer text-sm">{t("production_edit_video_prompt")}</summary>
      <div className="mt-3 flex flex-col gap-2">
        <Label htmlFor={`video-prompt-${unit.unit_id}`}>{t("production_video_prompt")}</Label>
        <Textarea id={`video-prompt-${unit.unit_id}`} fixedHeight disabled={locked || editor.status === "saving"} value={usingManualPrompt ? editor.value.generation_prompt : editor.value.text} onChange={(event) => { const text = event.target.value; editor.setValue((v) => usingManualPrompt ? ({ ...v, generation_prompt: text }) : ({ ...v, text })); }} />
        {usingManualPrompt && <Button type="button" variant="ghost" className="self-start" disabled={locked} onClick={() => { editor.setValue((v) => ({ ...v, generation_prompt: "" })); setUsingManualPrompt(false); }}>{t("production_prompt_default")}</Button>}
        <section aria-label={t("production_used_assets")} className="flex flex-col gap-2">
          <Label>{t("production_used_assets")}</Label>
          {previewBusy && <p role="status" className="text-xs">{t("common:loading")}</p>}
          {preview?.used_assets?.map((asset) => <div key={`${asset.type}/${asset.name}`} className="flex flex-wrap items-center gap-2 rounded-md border border-border p-2 text-xs">
            {asset.paths.map((path) => <a key={path} href={API.getFileUrl(project, path, fingerprints[path] ?? null)} target="_blank" rel="noreferrer"><img className="size-12 rounded object-contain" src={API.getFileUrl(project, path, fingerprints[path] ?? null)} alt={asset.name} /></a>)}
            <span className="wrap-break-word">{t(asset.type === "character" ? "characters" : asset.type === "scene" ? "scenes" : asset.type === "product" ? "products" : "props")} · {asset.name}</span>
            <span>{t(`production_reference_${asset.status}`, { numbers: asset.request_indices.join(", ") })}</span>
          </div>)}
          {preview?.voice_only?.map((name) => <p key={name} className="text-xs text-muted-foreground">{name} · {t("production_voice_only")}</p>)}
          {preview?.audio_references?.map((audio) => <a key={audio.name} className="text-xs underline" href={API.getFileUrl(project, audio.path, fingerprints[audio.path] ?? null)} target="_blank" rel="noreferrer">{t("production_audio_reference")} · {audio.name}</a>)}
        </section>
        <CharacterAnglePicker project={project} characters={characters ?? {}} text={editor.value.text} disabled={locked || editor.status === "saving"} onChange={(text) => { editor.setValue((v) => ({ ...v, text })); }} />
        {bodyChangedWithManual && <p role="status" className="text-sm text-warn">{t("production_video_manual_review")}</p>}
        {preview?.problems && preview.problems.length > 0 && <div role="alert" className="rounded-md border border-destructive p-3 text-sm text-destructive">
          {preview.problems.map((problem, problemIndex) => <div key={`${problem.code}-${problemIndex}`}>
            <p>{problem.message}</p>
            <Button type="button" variant="ghost" onClick={() => focusIssue(problem.params.source, problem.params.line)}>{t("production_timeline_locate", { line: problem.params.line ?? 1 })}</Button>
          </div>)}
        </div>}
        {preview?.unavailable && <p role="alert" className="text-sm text-destructive">{preview.unavailable}</p>}
        <Button variant="outline" disabled={locked || !editor.dirty || editor.status === "saving" || (usingManualPrompt && !editor.value.generation_prompt.trim())} onClick={() => void editor.save()}>{t("common:save")}</Button>
      </div>
    </details>
    <Button type="button" variant="outline" className="mt-3" disabled={locked || editor.dirty || editor.status === "saving" || timelineBlocked || previewBusy || !!preview?.unavailable || !preview || !!error} onClick={() => {
      if (locked || editor.dirty || editor.status === "saving" || isResourceBusy("reference_video", project, unit.unit_id)) return;
      void onGenerate();
    }}>{t(source ? "production_regenerate_video" : "production_generate_video")}</Button>
    {(error || editor.error || failure) && <p role="alert" className="mt-2 text-sm text-destructive">{error || editor.error || failure}</p>}
  </article>;
}
