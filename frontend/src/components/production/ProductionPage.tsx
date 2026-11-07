import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useLocation, useParams, useSearch } from "wouter";
import { useTranslation } from "react-i18next";
import { Film, FolderOpen, Loader2, Plus, Settings } from "lucide-react";
import { Button, buttonVariants } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { LoadingState } from "@/components/shared/LoadingState";
import { useProductionProject } from "./useProductionProject";
import { productionPath, projectPath, PRODUCTION_STEPS } from "./production-paths";
import { EpisodeCost } from "./management/ProductionCosts";
import { WorkPicker } from "./management/WorkPicker";
import { ScriptInput } from "./script/ScriptInput";
import { JianyingDeliveryCard } from "./output/JianyingDeliveryCard";
import { ProductionEditor } from "./output/ProductionEditor";
import { ProductionAssetCard } from "./assets/ProductionAssetCard";
import { ProductionDerivativeCard } from "./assets/ProductionDerivativeCard";
import { ProductionVideoCard } from "./videos/ProductionVideoCard";
import { BreakdownEditor } from "./breakdown/BreakdownEditor";
import { dialogueSpeakers, extractMentions } from "@/utils/reference-mentions";
import { errMsg } from "@/utils/async";
import { enqueuePromptAuthoring, enqueueProductionAssets, enqueueProductionRender, enqueueProductionVideos, enqueueScriptPlan } from "@/actions/generation";
import { API } from "@/api";
import { useAppStore } from "@/stores/app-store";
import { useHasUnsavedChanges } from "@/components/shared/edit-unit/LeaveGuard";
import { BreakdownStatus } from "./breakdown/BreakdownStatus";
import { useScriptPlanEntry } from "@/hooks/useScriptPlanEntry";
import { isResourceBusy, useActiveResourceIds, useLatestTasksByResource } from "@/stores/tasks-store";
import { scriptPlanResourceId, promptAuthoringResourceId } from "@/actions/generation";
import { Progress, ProgressLabel, ProgressValue } from "@/components/ui/progress";
import { useProductionVideoProgress } from "./videos/useProductionVideoProgress";
import { AlertDialog, AlertDialogAction, AlertDialogBody, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "@/components/ui/alert-dialog";

export function ProductionPage() {
  const { t } = useTranslation("dashboard");
  const { projectName } = useParams<{ projectName?: string }>();
  const [location, navigate] = useLocation();
  const search = useSearch();
  const actionScope = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    actionScope.current = controller;
    return () => controller.abort();
  }, [location, search]);
  const reportBackground = (message: string, failed = false) => useAppStore.getState().pushToast(message, failed ? "error" : "info");
  const requestedStep = new URLSearchParams(search).get("step");
  const episodeQuery = new URLSearchParams(search).get("episode");
  const requestedEpisode = episodeQuery && /^[1-9][0-9]*$/.test(episodeQuery) ? Number(episodeQuery) : null;
  const { episode, project, units, source, workflow, production, review, refresh, loading, error } = useProductionProject(projectName ?? null, episodeQuery !== null && requestedEpisode === null ? -1 : requestedEpisode);
  const [picker, setPicker] = useState(false);
  const [confirmReplan, setConfirmReplan] = useState(false);
  const [replanReason, setReplanReason] = useState("");
  const step = PRODUCTION_STEPS.find((s) => s === requestedStep) ?? (workflow?.content?.formal_script === "present" ? "assets" : "script");
  const [actionError, setActionError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [timelineBlocked, setTimelineBlocked] = useState<Record<string, boolean>>({});
  const onTimelineValidation = useCallback((key: string, blocked: boolean) => {
    setTimelineBlocked((current) => current[key] === blocked ? current : { ...current, [key]: blocked });
  }, []);
  const hasUnsavedChanges = useHasUnsavedChanges();
  useEffect(() => {
    if (!projectName || loading || !project) return;
    if (!episode || (episodeQuery !== null && requestedEpisode === null)) navigate(projectPath(projectName), { replace: true });
  }, [episode, episodeQuery, loading, navigate, project, projectName, requestedEpisode, step]);
  const { busy: planning, latestTask: planTask } = useScriptPlanEntry(projectName ?? "", episode ?? 0);
  const planFailed = planTask?.status === "failed" || planTask?.status === "cancelled";
  const retryPlan = step === "breakdown" && !review?.content && planFailed;
  const imageCharacters = useActiveResourceIds("character", projectName);
  const imageScenes = useActiveResourceIds("scene", projectName);
  const imageProps = useActiveResourceIds("prop", projectName);
  const imageDerivatives = useActiveResourceIds("character_derivative", projectName);
  const imagesBusy = imageCharacters.size + imageScenes.size + imageProps.size + imageDerivatives.size > 0;
  const videosBusy = useActiveResourceIds("reference_video", projectName).size > 0;
  const authoring = useActiveResourceIds("text_episode_script", projectName).has(promptAuthoringResourceId(episode ?? 0));
  const authoringTask = useLatestTasksByResource(projectName ?? "", "text_episode_script").get(promptAuthoringResourceId(episode ?? 0));
  const authoringFailed = authoringTask?.status === "failed" || authoringTask?.status === "cancelled";
  const active = loading || imagesBusy || videosBusy || authoring || planning || starting || production?.status === "active" || production?.status === "submitting" || production?.status === "unknown";
  const promptsReady = workflow?.content?.formal_script === "present" && units.length > 0 && !units.some((unit) => unit.pending_authoring);
  const failedUnitIds = production?.status === "failed" ? production.members?.filter((member) => ["failed", "cancelled", "blocked"].includes(member.status)).map((member) => member.unit_id) : undefined;
  const retryVideos = production?.stage === "videos" && production.action === "videos" && Boolean(failedUnitIds?.length);
  const videoTimelineBlocked = units.some((unit) => (!retryVideos || failedUnitIds?.includes(unit.unit_id)) && timelineBlocked[`${projectName}/${episode}/${unit.unit_id}`]);
  const output = production?.output;
  const outputCurrent = output?.status === "current" && output.version != null && Boolean(output.artifact_path);
  const previousOutputUrl = projectName && output?.artifact_path && output.version != null ? API.getFileUrl(projectName, output.artifact_path, output.version) : null;
  const outputUrl = projectName && outputCurrent ? API.getFileUrl(projectName, output.artifact_path, output.version) : null;
  const canRebuild = units.length > 0 && units.every((unit) => unit.generated_assets?.video_clip);
  const adoptedNames = new Set(units.flatMap((unit) => [...extractMentions(unit.text), ...dialogueSpeakers(unit.text)]).flatMap((name) => [name, name.split("/")[0]]));
  const visualNames = new Set(units.flatMap((unit) => extractMentions(unit.text)));
  const derivatives = Object.entries(project?.characters ?? {}).flatMap(([owner, character]) => Object.entries(character.derivatives ?? {}).filter(([name]) => visualNames.has(`${owner}/${name}`)).map(([name, asset]) => ({ owner, name, asset, ownerPath: character.character_sheet })));
  const assetItems = [
    ...Object.entries(project?.characters ?? {}).filter(([name]) => adoptedNames.has(name)).map(([, asset]) => asset.character_sheet),
    ...Object.entries(project?.scenes ?? {}).filter(([name]) => adoptedNames.has(name)).map(([, asset]) => asset.scene_sheet),
    ...Object.entries(project?.props ?? {}).filter(([name]) => adoptedNames.has(name)).map(([, asset]) => asset.prop_sheet),
    ...derivatives.map(({ asset }) => asset.character_sheet),
  ];
  const completedImages = assetItems.filter(Boolean).length;
  const allImagesReady = completedImages === assetItems.length && units.length > 0;
  const videoProgress = useProductionVideoProgress(projectName ?? "", units, production);
  const videoCounts = { succeeded: 0, running: 0, queued: 0, failed: 0, waiting: 0, submitting: 0, unknown: 0 };
  for (const item of videoProgress.values()) videoCounts[item.status] += 1;
  const plan = async () => {
    if (!projectName || !episode || active || !replanReason.trim() || hasUnsavedChanges() || isResourceBusy("text_script_plan", projectName, scriptPlanResourceId(episode))) return;
    const scope = actionScope.current;
    setStarting(true); setActionError(null);
    try {
      await enqueueScriptPlan(projectName, episode, { replan_reason: replanReason.trim() });
      if (scope?.signal.aborted) { reportBackground(t("production_submitted_background")); return; }
      setConfirmReplan(false); setReplanReason("");
      refresh(); navigate(productionPath(projectName, "breakdown", episode));
    } catch (e) {
      if (scope?.signal.aborted) reportBackground(errMsg(e), true); else setActionError(errMsg(e));
    } finally { setStarting(false); }
  };
  const requestReplan = () => {
    if (!projectName || !episode || active || isResourceBusy("text_script_plan", projectName, scriptPlanResourceId(episode))) return;
    if (hasUnsavedChanges()) { setActionError(t("common:unsaved_changes")); return; }
    setActionError(null);
    setConfirmReplan(true);
  };
  const recoverDraft = async () => {
    if (!projectName || !episode || active || hasUnsavedChanges()) return;
    setStarting(true); setActionError(null);
    try {
      const draft = await API.getEpisodeDraft(projectName, episode, "reference_script_plan");
      if (!draft.content || !draft.revision || draft.editable_by !== "user") throw new Error(t("production_draft_unavailable"));
      const result = await API.saveEpisodeDraft(projectName, episode, draft.doc_type, draft.content, draft.revision);
      if (!result.adopted) throw new Error(result.draft?.violations.map((v) => v.message).join("；") || t("production_draft_unavailable"));
      refresh();
    } catch (e) { setActionError(errMsg(e)); }
    finally { setStarting(false); }
  };
  const startVideos = async (selectedIds?: string[]) => {
    const blocked = selectedIds ? selectedIds.some((id) => timelineBlocked[`${projectName}/${episode}/${id}`]) : videoTimelineBlocked;
    if (!projectName || !episode || active || blocked || (!promptsReady && !retryVideos) || units.length === 0) return;
    if (hasUnsavedChanges()) { setActionError(t("common:unsaved_changes")); return; }
    const scope = actionScope.current;
    setStarting(true); setActionError(null);
    try {
      const failed = selectedIds ?? (retryVideos ? failedUnitIds : undefined);
      await enqueueProductionVideos(projectName, episode, undefined, failed);
      if (scope?.signal.aborted) { reportBackground(t("production_submitted_background")); return; }
      refresh();
      navigate(productionPath(projectName, "videos", episode));
    } catch (e) {
      if (scope?.signal.aborted) reportBackground(errMsg(e), true); else setActionError(errMsg(e));
    } finally { setStarting(false); }
  };
  const rebuild = async (afterEditSave = false) => {
    if (!projectName || !episode || active) return;
    // The editor calls this only after its save succeeds; its leave-guard effect may still hold the previous dirty value.
    if (!afterEditSave && hasUnsavedChanges()) { setActionError(t("common:unsaved_changes")); return; }
    const scope = actionScope.current;
    setStarting(true); setActionError(null);
    try {
      await enqueueProductionRender(projectName, episode);
      if (scope?.signal.aborted) { reportBackground(t("production_submitted_background")); return; }
      refresh();
      if (step === "videos") navigate(productionPath(projectName, "output", episode));
    }
    catch (cause) { if (scope?.signal.aborted) reportBackground(errMsg(cause), true); else setActionError(errMsg(cause)); }
    finally { setStarting(false); }
  };
  const resumeAssets = async () => {
    if (!projectName || !episode || active || !review?.fingerprint) return;
    if (hasUnsavedChanges()) { setActionError(t("common:unsaved_changes")); return; }
    setStarting(true); setActionError(null);
    try { await enqueueProductionAssets(projectName, episode, review.fingerprint); refresh(); }
    catch (cause) { setActionError(errMsg(cause)); }
    finally { setStarting(false); }
  };
  const prepareVideoPrompts = async (enterVideos = false) => {
    if (!projectName || !episode || active || !allImagesReady || promptsReady || isResourceBusy("text_episode_script", projectName, promptAuthoringResourceId(episode))) return;
    if (hasUnsavedChanges()) { setActionError(t("common:unsaved_changes")); return; }
    const scope = actionScope.current;
    setStarting(true); setActionError(null);
    try {
      await enqueuePromptAuthoring(projectName, episode, {});
      if (scope?.signal.aborted) { reportBackground(t("production_submitted_background")); return; }
      refresh();
      if (enterVideos) navigate(productionPath(projectName, "videos", episode));
    } catch (cause) {
      if (scope?.signal.aborted) reportBackground(errMsg(cause), true); else setActionError(errMsg(cause));
    } finally { setStarting(false); }
  };
  const enterVideos = () => {
    if (!projectName || !episode || !allImagesReady) return;
    if (hasUnsavedChanges()) { setActionError(t("common:unsaved_changes")); return; }
    if (promptsReady || authoring) navigate(productionPath(projectName, "videos", episode));
    else void prepareVideoPrompts(true);
  };
  const assetAction = () => {
    if (allImagesReady) enterVideos();
    else void resumeAssets();
  };
  const groups = ["character", "scene", "prop"] as const;
  return <div className="relative flex h-dvh flex-col overflow-hidden bg-background text-foreground">
    <header className="flex shrink-0 flex-wrap items-center gap-3 border-b border-border px-4 py-3">
      <Link href={projectPath()} className="flex items-center gap-2 font-semibold"><Film aria-hidden className="size-5 text-primary" />XM Studio</Link>
      <span className="min-w-0 flex-1 truncate text-sm text-muted-foreground">{project?.title ?? t("production_new")}{episode ? ` · ${project?.episodes.find((entry) => entry.episode === episode)?.title || t("management_untitled_episode")}` : ""}</span>
      <Link href={projectPath(projectName)} className={buttonVariants({ variant: "outline" })}>{t("management_projects")}</Link>
      <Button variant="outline" onClick={() => setPicker(true)}><FolderOpen aria-hidden />{t("production_open")}</Button>
      <Link href={productionPath()} className={buttonVariants({ variant: "outline" })}><Plus aria-hidden />{t("production_new")}</Link>
      <Link href="/app/settings?section=default-models" onClick={() => sessionStorage.setItem("xm:production-return", window.location.pathname + window.location.search)} className={buttonVariants({ variant: "ghost" })}><Settings aria-hidden />{t("production_models")}</Link>
    </header>
    <nav aria-label={t("production_steps")} className="relative shrink-0 overflow-x-auto scroll-fade-x border-b border-border px-4 py-3">
      <ol className="mx-auto flex max-w-300 gap-2">{PRODUCTION_STEPS.map((s, i) => <li key={s} className="min-w-0 flex-1">
        <Link href={productionPath(projectName, s, episode)} aria-current={step === s ? "step" : undefined} onClick={(event) => {
          if (s === "videos" && step === "assets" && allImagesReady && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey && event.button === 0) {
            event.preventDefault(); enterVideos();
          }
        }} className={buttonVariants({ variant: step === s ? "default" : "ghost", className: "w-full gap-2 px-2" })}>
          <span className="tabular-nums">{i + 1}</span>{t(`production_step_${s}`)}
        </Link></li>)}</ol>
    </nav>
    <main className="relative min-h-0 flex-1 overflow-y-auto [scrollbar-gutter:stable] p-4">
      <div className="mx-auto flex max-w-300 flex-col gap-6">
        <h1 className="text-xl font-semibold">{t(`production_step_${step}`)}</h1>
        {actionError && <p role="alert" className="text-sm text-destructive">{actionError}</p>}
        {production?.error && <p role="alert" className="text-sm text-destructive">{production.status === "unknown" ? t("production_unknown") : production.error}</p>}
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        {loading ? <LoadingState variant="pane" /> : !projectName ? <ScriptInput /> : project && episode && <>
          {step === "script" && <ScriptInput key={`${projectName}/${episode}`} projectName={projectName} episode={episode} source={source ?? ""} disabled={active} readOnly={project.episodes?.find((entry) => entry.episode === episode)?.source_origin === "whole_source"} />}
          {step === "breakdown" && <><BreakdownStatus task={planTask} busy={planning} review={review} />{review?.quarantine?.doc_type === "reference_script_plan" && review.quarantine.content && <Button type="button" variant="outline" className="self-start" disabled={active} onClick={() => void recoverDraft()}>{t("production_draft_recheck")}</Button>}{review?.content && <Button type="button" variant="outline" className="self-start" disabled={active} onClick={requestReplan}>{t("production_breakdown_retry")}</Button>}<div>{!planning && review?.content && episode && <BreakdownEditor project={projectName} episode={episode} projectData={project} review={review} busy={active} onChanged={refresh} onStarted={() => { refresh(); navigate(productionPath(projectName, "assets", episode)); }} />}</div></>}
          {step === "assets" && <><div role="status" className="flex flex-col gap-2 rounded-lg border border-border bg-card p-4"><p className="font-medium">{t("production_images_summary", { done: completedImages, total: assetItems.length })}</p><p className="text-sm text-muted-foreground">{t("production_images_hint")}</p></div>{groups.map((kind) => {
            const items: Record<string, { description: string; generation_prompt?: string; character_sheet?: string; scene_sheet?: string; prop_sheet?: string }> = (kind === "character" ? project.characters : kind === "scene" ? project.scenes : project.props) ?? {};
            return <section key={kind} className="flex flex-col gap-3"><h2 className="font-medium">{t(kind === "character" ? "characters" : kind === "scene" ? "scenes" : "props")}</h2>
              <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,18rem),1fr))] gap-4">{Object.entries(items ?? {}).filter(([name]) => adoptedNames.has(name)).map(([name, a]) => {
                const path = kind === "character" ? a.character_sheet : kind === "scene" ? a.scene_sheet : a.prop_sheet;
                return <ProductionAssetCard key={name} project={projectName} episode={episode} kind={kind} name={name} description={a.description} generationPrompt={a.generation_prompt} path={path} busy={planning || starting || production?.status === "unknown" || production?.status === "submitting" || production?.status === "active" && production.stage !== "assets"} onChanged={refresh} />;
              })}{kind === "character" && derivatives.map(({ owner, name, asset, ownerPath }) => <ProductionDerivativeCard key={`${owner}/${name}`} project={projectName} owner={owner} name={name} description={asset.description} path={asset.character_sheet} ownerPath={ownerPath} busy={planning || starting || production?.status === "unknown" || production?.status === "submitting" || production?.status === "active" && production.stage !== "assets"} onChanged={refresh} />)}</div></section>;
          })}</>}
          {step === "videos" && <>{!promptsReady ? <section className="flex flex-col gap-3 rounded-lg border border-border bg-card p-4">
            <p role="status" className="flex items-center gap-2">{authoring && <Loader2 aria-hidden className="size-4 animate-spin text-primary" />}{t(authoring ? "production_video_authoring" : "production_video_prepare_hint")}</p>
            {!authoring && authoringFailed && <p role="alert" className="whitespace-pre-wrap text-sm text-destructive">{t("prompt_authoring_failed", { reason: authoringTask?.error_message || t("script_plan_failed_unknown") })}</p>}
            {!authoring && <Button variant="outline" className="self-start" disabled={active || !allImagesReady} onClick={() => void prepareVideoPrompts()}>{t("production_prepare_video_prompts")}</Button>}
          </section> : <><p className="text-sm text-muted-foreground">{t("production_video_review_hint")}</p><section aria-label={t("production_videos_progress")} className="flex flex-col gap-3 rounded-lg border border-border bg-card p-4">
            <Progress value={videoCounts.succeeded} max={units.length || 1}>
              <ProgressLabel>{t("production_videos_summary", { done: videoCounts.succeeded, total: units.length })}</ProgressLabel><ProgressValue />
            </Progress>
            <p className="text-sm text-muted-foreground">{t("production_videos_counts", { running: videoCounts.running, queued: videoCounts.queued + videoCounts.submitting, failed: videoCounts.failed, waiting: videoCounts.waiting + videoCounts.unknown })}</p>
            <p role="status" className="flex items-center gap-2 text-sm">{production?.stage === "render" && production.status === "active" && <Loader2 aria-hidden className="size-4 animate-spin text-primary" />}{t(production?.stage === "render" && production.status === "active" ? "production_videos_rendering" : "production_videos_hint")}</p>
          </section><div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,20rem),1fr))] gap-4">{episode && units.map((u, i) => <ProductionVideoCard key={u.unit_id} project={projectName} episode={episode} unit={u} index={i} production={production} progress={videoProgress.get(u.unit_id)!} busy={active} onGenerate={() => startVideos([u.unit_id])} onChanged={refresh} onTimelineValidation={onTimelineValidation} />)}</div></>}</>}
          {step === "output" && (previousOutputUrl ? <><video controls src={previousOutputUrl} className="max-h-[70vh] w-full"><track kind="captions" /></video>{!outputCurrent && <p className="text-sm text-muted-foreground">{t("production_output_changed")}</p>}{!outputCurrent && !active && canRebuild && <Button variant="outline" className="self-start" onClick={() => void rebuild()}>{t("production_rebuild_output")}</Button>}</> : <div className="flex flex-col gap-3"><p className="text-sm text-muted-foreground">{t(output && output.status !== "missing" ? "production_output_changed" : "production_wait_output")}</p>{!active && canRebuild && <Button variant="outline" className="self-start" onClick={() => void rebuild()}>{t("production_rebuild_output")}</Button>}</div>)}
          {step === "output" && production?.timeline_id && <ProductionEditor project={projectName} episode={episode} units={units} busy={active} onChanged={refresh} onRender={() => rebuild(true)} />}
          {step === "output" && production?.timeline_id && <JianyingDeliveryCard key={`${projectName}/${episode}`} project={projectName} episode={episode} busy={active} revision={JSON.stringify(production)} />}
          {step === "output" && <EpisodeCost project={projectName} episode={episode} revision={JSON.stringify(production)} />}
        </>}
      </div>
    </main>
    <footer className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-t border-border bg-card px-4 py-3"><p className="text-sm text-muted-foreground">{active ? t("production_running") : step === "assets" ? t("production_video_targets", { count: units.length }) : t("production_saved")}</p>{step === "output" && outputUrl ? <a className={buttonVariants()} href={outputUrl} download>{t("production_download")}</a> : <Button form={step === "breakdown" && !retryPlan || !projectName || step === "script" ? "production-action" : undefined} type={step === "breakdown" && !retryPlan || !projectName || step === "script" ? "submit" : "button"} onClick={retryPlan && projectName ? requestReplan : step === "assets" ? assetAction : step === "videos" ? () => void (canRebuild ? rebuild() : startVideos()) : undefined} disabled={active || (step !== "script" && step !== "breakdown" && step !== "assets" && step !== "videos") || (step === "breakdown" && !retryPlan && (!review?.content || !!review.quarantine)) || (step === "assets" && !allImagesReady && (!review?.fingerprint || review.status !== "confirmed")) || (step === "videos" && !canRebuild && (!allImagesReady || !promptsReady || videoTimelineBlocked))}>{t(planning ? "production_breakdown_running" : retryPlan ? "production_breakdown_retry" : step === "script" ? "production_start" : step === "breakdown" ? "production_confirm_assets" : step === "assets" ? allImagesReady ? promptsReady ? "production_review_videos" : "production_confirm_assets_write_prompts" : "production_confirm_image_prompts" : step === "videos" && canRebuild ? "production_confirm_merge" : step === "videos" && retryVideos ? "production_retry_failed" : step === "videos" ? "production_confirm_videos" : "production_download")}</Button>}</footer>
    {picker && <WorkPicker onClose={() => setPicker(false)} />}
    <AlertDialog open={confirmReplan} onOpenChange={(open) => { if (!starting) setConfirmReplan(open); }}>
      <AlertDialogContent>
        <AlertDialogHeader><AlertDialogTitle>{t("production_breakdown_retry")}</AlertDialogTitle></AlertDialogHeader>
        <AlertDialogBody><div className="flex flex-col gap-3">
          <AlertDialogDescription>{t(review?.status === "confirmed" ? "script_plan_replace_confirmed_hint" : review?.quarantine ? "script_plan_replace_draft_warning" : review?.content ? "script_plan_replace_pending_warning" : "script_plan_retry_failed_hint")}</AlertDialogDescription>
          <Label htmlFor="replan-reason">{t("script_plan_replan_reason")}</Label>
          <Textarea id="replan-reason" value={replanReason} onChange={(event) => setReplanReason(event.target.value)} placeholder={t("script_plan_replan_reason_placeholder")} maxLength={4000} rows={5} disabled={starting} aria-describedby="replan-reason-hint" />
          <p id="replan-reason-hint">{t("script_plan_replan_reason_hint")}</p>
          {actionError && <p role="alert" className="text-destructive">{actionError}</p>}
        </div></AlertDialogBody>
        <AlertDialogFooter><AlertDialogCancel disabled={starting}>{t("common:cancel")}</AlertDialogCancel><AlertDialogAction disabled={active || !replanReason.trim()} onClick={() => void plan()}>{starting ? t("production_breakdown_running") : t("production_breakdown_retry")}</AlertDialogAction></AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </div>;
}
