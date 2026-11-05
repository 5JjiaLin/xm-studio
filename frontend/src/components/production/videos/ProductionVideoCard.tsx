import { FeedbackReview } from "../feedback/FeedbackReview";
import { useCallback, useEffect, useState, useRef } from "react";
import { CheckCircle2, CircleAlert, Clock3, Loader2 } from "lucide-react";
import { TaskElapsedReadout } from "@/components/shared/TaskElapsedReadout";
import type { VideoProgress } from "./useProductionVideoProgress";
import { useTranslation } from "react-i18next";
import { CharacterAnglePicker } from "../assets/CharacterAnglePicker";
import { API } from "@/api";
import { enqueueProductionVideos } from "@/actions/generation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
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
  onChanged,
  onUploaded,
}: {
  project: string;
  episode: number;
  unit: ReferenceVideoUnit;
  index: number;
  production: ProductionState | null;
  progress: VideoProgress;
  onChanged: () => void;
  onUploaded: () => Promise<void>;
}) {
  const { t } = useTranslation("dashboard");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const occupied = useTasksStore((state) => selectActiveResourceIds(state.tasks, "reference_video", project, state.optimisticActive).has(unit.unit_id));
  const locked = busy || occupied || ["active", "submitting", "unknown"].includes(production?.status ?? "");
  const projectData = useProjectsStore((state) => state.currentProjectData);
  const characters = projectData?.characters;
  const fingerprints = useProjectsStore((state) => state.assetFingerprints);
  const [promptOpen, setPromptOpen] = useState(true);
  const [preview, setPreview] = useState<ReferenceUnitPromptPreview | null>(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [previewKey, setPreviewKey] = useState<string | null>(null);
  const save = useCallback(async (value: { text: string; generation_prompt: string }) => {
    if (isResourceBusy("reference_video", project, unit.unit_id)) throw new Error(t("production_running"));
    const result = await API.patchReferenceVideoUnit(project, episode, unit.unit_id, { prompt: value.text, generation_prompt: value.generation_prompt });
    onChanged();
    return { text: result.unit.text, generation_prompt: result.unit.generation_prompt || "" };
  }, [episode, onChanged, project, unit.unit_id, t]);
  const editor = useEditUnit({ source: { text: unit.text, generation_prompt: unit.generation_prompt || "" }, save });
  const usingManualPrompt = !!editor.value.generation_prompt;
  const bodyChangedWithManual = usingManualPrompt && editor.value.text !== editor.savedValue.text;
  const previewController = useRef<AbortController | null>(null);
  const [previewRound, setPreviewRound] = useState(0);
  const requestKey = JSON.stringify([project, episode, unit.unit_id, editor.value.text, editor.value.generation_prompt, unit.pending_authoring, projectData, fingerprints, previewRound]);
  const previewCurrent = previewKey === requestKey;
  useEffect(() => {
    if (!promptOpen) return;
    previewController.current?.abort();
    const controller = new AbortController();
    previewController.current = controller;
    void Promise.resolve().then(() => { if (!controller.signal.aborted) { setPreviewBusy(true); setError(null); } });
    void API.previewReferenceUnitPrompt(project, episode, unit.unit_id, editor.value.text, { signal: controller.signal, generationPrompt: editor.value.generation_prompt }).then((value) => { if (!controller.signal.aborted) { setPreview(value); setPreviewKey(requestKey); } }).catch((cause) => { if (!controller.signal.aborted) { setPreviewKey(null); setError(errMsg(cause)); } }).finally(() => { if (!controller.signal.aborted) setPreviewBusy(false); });
    return () => controller.abort();
  }, [project, episode, unit.unit_id, editor.value.text, editor.value.generation_prompt, unit.pending_authoring, promptOpen, projectData, fingerprints, previewRound, requestKey]);
  const refreshPreview = () => setPreviewRound((value) => value + 1);
  const regenerate = async () => {
    if (locked || editor.dirty || !previewCurrent || previewBusy || unit.pending_authoring || !preview?.text || preview.unavailable || isResourceBusy("reference_video", project, unit.unit_id)) return;
    setBusy(true); setError(null);
    try {
      await enqueueProductionVideos(project, episode, undefined, [unit.unit_id]);
      onChanged();
    } catch (cause) { setError(errMsg(cause)); }
    finally { setBusy(false); }
  };
  const upload = async (file: File) => {
    if (locked || editor.dirty || isResourceBusy("reference_video", project, unit.unit_id)) return;
    setBusy(true); setError(null);
    try {
      const result = await API.uploadReferenceUnitVideo(project, episode, unit.unit_id, file);
      useProjectsStore.getState().updateAssetFingerprints(result.asset_fingerprints ?? {});
      onChanged();
      await onUploaded();
    } catch (cause) {
      setError(errMsg(cause));
    } finally { setBusy(false); }
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
        <Textarea id={`video-prompt-${unit.unit_id}`} disabled={locked || editor.status === "saving"} value={editor.value.text} onChange={(event) => { editor.setValue((v) => ({ ...v, text: event.target.value })); }} />
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
        <p className="text-xs text-muted-foreground">{t("production_video_bindings_hint")}</p>
        <p className="text-xs text-muted-foreground">{t(usingManualPrompt ? "production_video_manual_source" : "production_video_default_source")}</p>
        {bodyChangedWithManual && <p role="status" className="text-sm text-warn">{t("production_video_manual_review")}</p>}
        <Button type="button" variant="outline" disabled={previewBusy} onClick={() => void refreshPreview()}>{t("production_video_refresh_preview")}</Button>
        {preview?.unavailable && <p role="alert" className="text-sm text-destructive">{preview.unavailable}</p>}
        {preview?.text && <>
          <Label>{t("production_video_declarations")}</Label><pre className="whitespace-pre-wrap wrap-break-word rounded-md bg-muted p-3 text-xs">{preview.declarations || "—"}</pre>
          <Label htmlFor={`video-creative-${unit.unit_id}`}>{t("production_video_creative")}</Label><Textarea id={`video-creative-${unit.unit_id}`} className="min-h-48" disabled={locked || editor.status === "saving"} value={editor.value.generation_prompt || preview.creative_text || ""} onChange={(e) => editor.setValue((v) => ({ ...v, generation_prompt: e.target.value }))} />
          {editor.value.generation_prompt && <p className="text-xs text-muted-foreground">{t("production_prompt_manual")}</p>}
          <Button type="button" variant="ghost" disabled={locked || !editor.value.generation_prompt} onClick={() => { editor.setValue((v) => ({ ...v, generation_prompt: "" })); }}>{t("production_prompt_default")}</Button>
          <details><summary className="cursor-pointer text-sm">{t("production_full_video_prompt")}</summary><pre className="mt-2 whitespace-pre-wrap wrap-break-word text-xs">{editor.value.generation_prompt ? `${preview.declarations ? preview.declarations + "\n\n" : ""}${editor.value.generation_prompt}` : preview.text}</pre><Button type="button" variant="outline" onClick={() => { void navigator.clipboard.writeText(editor.value.generation_prompt ? `${preview.declarations ? preview.declarations + "\n\n" : ""}${editor.value.generation_prompt}` : preview.text || "").catch((e) => setError(errMsg(e))); }}>{t("production_prompt_copy")}</Button></details>
          <details><summary className="cursor-pointer text-sm">{t("production_request_details")}</summary><pre className="whitespace-pre-wrap wrap-break-word text-xs">{JSON.stringify({ parameters: preview.parameters, system_message: preview.system_message ?? null, images: preview.references, audio: preview.audio_references ?? [] }, null, 2)}</pre></details>
        </>}
        <Button variant="outline" disabled={locked || !editor.dirty || editor.status === "saving"} onClick={() => void editor.save()}>{t("common:save")}</Button>
      </div>
    </details>
    {!editor.dirty && <FeedbackReview project={project} resource={`unit/${projectData?.episodes.find((entry) => entry.episode === episode)?.script_file || `scripts/episode_${episode}.json`}/${unit.unit_id}`} layer="text" versionKey={`${unit.text}:${unit.generation_prompt}`} />}
    {source && <FeedbackReview project={project} resource={`media/reference_videos/${unit.unit_id}`} layer="media" versionKey={`${source}:${fingerprints[source]}`} />}
    <p className="mt-3 text-xs text-muted-foreground">{t("production_video_regenerate_impact")}</p>
    {(error || editor.error || failure) && <p role="alert" className="mt-2 text-sm text-destructive">{error || editor.error || failure}</p>}
    <div className="mt-3 flex flex-wrap items-center gap-2">
      <p className="text-xs text-muted-foreground">{t("production_prompt_review_save_hint")}</p><Button variant="outline" disabled={locked || editor.dirty || !previewCurrent || previewBusy || unit.pending_authoring || !preview?.text || !!preview.unavailable || editor.status === "saving"} onClick={() => void regenerate()}>{t(source ? "production_regenerate_video_paid" : "production_generate_video")}</Button>
      <Label htmlFor={`video-upload-${unit.unit_id}`} className="sr-only">{t("production_upload_video_replace")}</Label>
      <Input id={`video-upload-${unit.unit_id}`} className="max-w-xs" type="file" accept="video/mp4,video/webm,video/quicktime" disabled={locked || editor.dirty} onChange={(event) => { const file = event.target.files?.[0]; event.target.value = ""; if (file) void upload(file); }} />
    </div>
  </article>;
}
