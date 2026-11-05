import { FeedbackReview } from "../feedback/FeedbackReview";
import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "@/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useEditUnit } from "@/components/shared/edit-unit/useEditUnit";
import { enqueueCharacter, enqueueScene, enqueueProp } from "@/actions/generation";
import { isResourceBusy, selectActiveResourceIds, useLatestTasksByResource, useTasksStore } from "@/stores/tasks-store";
import { errMsg } from "@/utils/async";
import { useProjectsStore } from "@/stores/projects-store";
import { CheckCircle2, CircleAlert, Clock3, Loader2 } from "lucide-react";
import { CharacterAngleCropper } from "./CharacterAngleCropper";
import { AssetPromptEditor } from "./AssetPromptEditor";
import { TaskElapsedReadout } from "@/components/shared/TaskElapsedReadout";

export function ProductionAssetCard({ project, episode, kind, name, description, generationPrompt = "", path, busy: productionBusy, onChanged }: { project: string; episode: number; kind: "character" | "scene" | "prop"; name: string; description: string; generationPrompt?: string; path?: string; busy: boolean; onChanged: () => void }) {
  const { t } = useTranslation("dashboard");
  const [promptOpen, setPromptOpen] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const latest = useLatestTasksByResource(project, kind).get(name);
  const fingerprint = useProjectsStore((state) => path ? state.assetFingerprints[path] ?? null : null);
  const occupied = useTasksStore((s) => selectActiveResourceIds(s.tasks, kind, project, s.optimisticActive).has(name));
  const locked = productionBusy || busy || occupied;
  const save = useCallback(async (value: { generation_prompt: string }) => {
    if (isResourceBusy(kind, project, name)) throw new Error(t("production_running"));
    await API.saveProductionAssetPrompt(project, kind, name, value);
    onChanged();
  }, [kind, project, name, onChanged, t]);
  const unit = useEditUnit({ source: { generation_prompt: generationPrompt }, save });
  const failure = !occupied && (latest?.status === "failed" || latest?.status === "cancelled") ? latest : undefined;
  const statusKey = busy ? "production_image_submitting" : occupied ? latest?.status === "running" ? "production_image_running" : "production_image_queued" : failure ? "production_image_failed" : path ? "production_image_ready" : "production_image_waiting";
  const regenerate = async () => {
    if (locked || unit.dirty || unit.status === "saving" || isResourceBusy(kind, project, name)) return;
    setBusy(true); setError(null);
    try {
      if (isResourceBusy(kind, project, name)) return;
      if (kind === "character") await enqueueCharacter(project, name, episode);
      else if (kind === "scene") await enqueueScene(project, name, episode);
      else await enqueueProp(project, name, episode);
      await useTasksStore.getState().refreshTasks(); onChanged();
    } catch (cause) { setError(errMsg(cause)); }
    finally { setBusy(false); }
  };
  const upload = async (file: File) => {
    if (locked || unit.status === "saving" || unit.dirty || isResourceBusy(kind, project, name)) return;
    setBusy(true); setError(null);
    try { await API.uploadFile(project, kind, file, name, { onConflict: "replace" }); onChanged(); }
    catch (e) { setError(errMsg(e)); } finally { setBusy(false); }
  };
  return <article aria-label={name} className="overflow-hidden rounded-lg border border-border bg-card">
    {!path && <div className="flex aspect-video items-center justify-center bg-muted text-muted-foreground">{occupied || busy ? <Loader2 aria-hidden className="size-8 animate-spin" /> : failure ? <CircleAlert aria-hidden className="size-8" /> : <Clock3 aria-hidden className="size-8" />}</div>}
    {path && <a href={API.getFileUrl(project, path, fingerprint)} target="_blank" rel="noreferrer"><img src={API.getFileUrl(project, path, fingerprint)} alt={name} className="aspect-video w-full object-contain" /></a>}
    <div className="flex flex-col gap-3 p-4"><h3 className="text-sm font-medium wrap-break-word">{name}</h3>
      <div role="status" className="flex flex-wrap items-center gap-2 text-sm">
        {occupied || busy ? <Loader2 aria-hidden className="size-4 animate-spin text-primary" /> : failure ? <CircleAlert aria-hidden className="size-4 text-destructive" /> : path ? <CheckCircle2 aria-hidden className="size-4 text-primary" /> : <Clock3 aria-hidden className="size-4 text-muted-foreground" />}
        <span>{t(statusKey)}</span>{latest && <TaskElapsedReadout task={latest} />}
      </div>
      <p className="text-xs text-muted-foreground">{t("production_asset_reference_facts_hint")}</p>
      {kind === "character" && path && <CharacterAngleCropper key={`${path}:${fingerprint}`} project={project} name={name} path={path} disabled={locked || unit.dirty || unit.status === "saving"} onBusy={setBusy} onChanged={onChanged} />}
      <details open={promptOpen} onToggle={(event) => setPromptOpen(event.currentTarget.open)}><summary className="cursor-pointer text-sm">{t("production_edit_prompt")}</summary><div className="mt-3 flex flex-col gap-2">{promptOpen && <AssetPromptEditor project={project} kind={kind} name={name} description={description} prompt={unit.value.generation_prompt} disabled={locked || unit.status === "saving"} onChange={(prompt) => unit.setValue((value) => ({ ...value, generation_prompt: prompt }))} />}<Button variant="outline" disabled={locked || !unit.dirty || unit.status === "saving"} onClick={() => void unit.save()}>{t("common:save")}</Button></div></details>
      {(error || unit.error || failure?.error_message) && <p role="alert" className="text-sm text-destructive">{error || unit.error || failure?.error_message}</p>}
      {!unit.dirty && <FeedbackReview project={project} resource={`${kind}/${name}`} layer="text" versionKey={generationPrompt} />}
      {path && <FeedbackReview project={project} resource={`media/${kind === "character" ? "characters" : kind === "scene" ? "scenes" : "props"}/${name}`} layer="media" versionKey={`${path}:${fingerprint}`} />}
      <p className="text-xs text-muted-foreground">{t("production_regenerate_impact")}</p>
      <p className="text-xs text-muted-foreground">{t("production_prompt_review_save_hint")}</p><Button variant="outline" disabled={locked || unit.dirty || unit.status === "saving"} onClick={() => void regenerate()}>{t(path ? "production_regenerate_paid" : "production_generate_image")}</Button>
      <Label htmlFor={`upload-${kind}-${name}`}>{t("production_upload_replace")}</Label><Input id={`upload-${kind}-${name}`} type="file" accept="image/png,image/jpeg,image/webp" disabled={locked || unit.dirty || unit.status === "saving"} onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; if (f) void upload(f); }} />
    </div>
  </article>;
}
