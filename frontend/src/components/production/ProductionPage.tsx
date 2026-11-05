import { FeedbackSettings } from "./feedback/FeedbackSettings";
import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useParams, useSearch } from "wouter";
import { useTranslation } from "react-i18next";
import { Film, FolderOpen, Loader2, Plus, Settings } from "lucide-react";
import { Button, buttonVariants } from "@/components/ui/button";
import { LoadingState } from "@/components/shared/LoadingState";
import { useProductionProject } from "./useProductionProject";
import { productionPath, projectPath, PRODUCTION_STEPS } from "./production-paths";
import { EpisodeCost } from "./management/ProductionCosts";
import { WorkPicker } from "./management/WorkPicker";
import { ScriptInput } from "./script/ScriptInput";
import { ProductionEditor } from "./output/ProductionEditor";
import { ProductionAssetCard } from "./assets/ProductionAssetCard";
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
import { isResourceBusy, useActiveResourceIds } from "@/stores/tasks-store";
import { scriptPlanResourceId, promptAuthoringResourceId } from "@/actions/generation";
import { Progress, ProgressLabel, ProgressValue } from "@/components/ui/progress";
import { useProductionVideoProgress } from "./videos/useProductionVideoProgress";

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
  const step = PRODUCTION_STEPS.find((s) => s === requestedStep) ?? (workflow?.content?.formal_script === "present" ? "assets" : "script");
  const [actionError, setActionError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const hasUnsavedChanges = useHasUnsavedChanges();
  const previousStatus = useRef(production?.status);
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
  const imagesBusy = imageCharacters.size + imageScenes.size + imageProps.size > 0;
  const authoring = useActiveResourceIds("text_episode_script", projectName).has(promptAuthoringResourceId(episode ?? 0));
  const active = loading || imagesBusy || authoring || planning || starting || production?.status === "active" || production?.status === "submitting" || production?.status === "unknown";
  const assetsReady = workflow?.content?.formal_script === "present" && units.length > 0 && !units.some((unit) => unit.pending_authoring);
  const failedUnitIds = production?.status === "failed" ? production.members?.filter((member) => ["failed", "cancelled", "blocked"].includes(member.status)).map((member) => member.unit_id) : undefined;
  const retryVideos = production?.stage === "videos" && production.action === "videos" && Boolean(failedUnitIds?.length);
  const output = production?.output;
  const outputCurrent = output?.status === "current" && output.version != null && Boolean(output.artifact_path);
  const previousOutputUrl = projectName && output?.artifact_path && output.version != null ? API.getFileUrl(projectName, output.artifact_path, output.version) : null;
  const outputUrl = projectName && outputCurrent ? API.getFileUrl(projectName, output.artifact_path, output.version) : null;
  const canRebuild = units.length > 0 && units.every((unit) => unit.generated_assets?.video_clip);
  const adoptedNames = new Set(units.flatMap((unit) => [...extractMentions(unit.text), ...dialogueSpeakers(unit.text)]).flatMap((name) => [name, name.split("/")[0]]));
  const assetItems = [
    ...Object.entries(project?.characters ?? {}).filter(([name]) => adoptedNames.has(name)).map(([, asset]) => asset.character_sheet),
    ...Object.entries(project?.scenes ?? {}).filter(([name]) => adoptedNames.has(name)).map(([, asset]) => asset.scene_sheet),
    ...Object.entries(project?.props ?? {}).filter(([name]) => adoptedNames.has(name)).map(([, asset]) => asset.prop_sheet),
  ];
  const completedImages = assetItems.filter(Boolean).length;
  const allImagesReady = completedImages === assetItems.length && units.length > 0;
  const videoProgress = useProductionVideoProgress(projectName ?? "", units, production);
  const videoCounts = { succeeded: 0, running: 0, queued: 0, failed: 0, waiting: 0, submitting: 0, unknown: 0 };
  for (const item of videoProgress.values()) videoCounts[item.status] += 1;
  const plan = async () => {
    if (!projectName || !episode || active || isResourceBusy("text_script_plan", projectName, scriptPlanResourceId(episode))) return;
    const scope = actionScope.current;
    setStarting(true); setActionError(null);
    try {
      await enqueueScriptPlan(projectName, episode, {});
      if (scope?.signal.aborted) { reportBackground(t("production_submitted_background")); return; }
      refresh(); navigate(productionPath(projectName, "breakdown", episode));
    } catch (e) {
      if (scope?.signal.aborted) reportBackground(errMsg(e), true); else setActionError(errMsg(e));
    } finally { setStarting(false); }
  };
  useEffect(() => {
    const wasRunning = previousStatus.current === "active" || previousStatus.current === "submitting";
    previousStatus.current = production?.status;
    if (wasRunning && production?.status === "completed" && outputCurrent && step === "videos" && !hasUnsavedChanges()) navigate(productionPath(projectName, "output", episode));
  }, [episode, hasUnsavedChanges, navigate, outputCurrent, production?.status, projectName, step]);
  const startVideos = async () => {
    if (!projectName || !episode || active || (!assetsReady && !retryVideos) || units.length === 0) return;
    if (hasUnsavedChanges()) { setActionError(t("common:unsaved_changes")); return; }
    const scope = actionScope.current;
    setStarting(true); setActionError(null);
    try {
      const failed = retryVideos ? failedUnitIds : undefined;
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
    setStarting(true); setActionError(null);
    try { await enqueueProductionRender(projectName, episode); refresh(); }
    catch (cause) { setActionError(errMsg(cause)); }
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
  const prepareVideoPrompts = async () => {
    if (!projectName || !episode || active || !allImagesReady) return;
    if (hasUnsavedChanges()) { setActionError(t("common:unsaved_changes")); return; }
    setStarting(true); setActionError(null);
    try { await enqueuePromptAuthoring(projectName, episode, {}); refresh(); }
    catch (cause) { setActionError(errMsg(cause)); }
    finally { setStarting(false); }
  };
  const assetAction = () => {
    if (hasUnsavedChanges()) { setActionError(t("common:unsaved_changes")); return; }
    if (allImagesReady) navigate(productionPath(projectName, "videos", episode));
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
        <Link href={productionPath(projectName, s, episode)} aria-current={step === s ? "step" : undefined} className={buttonVariants({ variant: step === s ? "default" : "ghost", className: "w-full gap-2 px-2" })}>
          <span className="tabular-nums">{i + 1}</span>{t(`production_step_${s}`)}
        </Link></li>)}</ol>
    </nav>
    <main className="relative min-h-0 flex-1 overflow-y-auto [scrollbar-gutter:stable] p-4">
      <div className="mx-auto flex max-w-300 flex-col gap-6">
        <FeedbackSettings project={projectName} />
        <h1 className="text-xl font-semibold">{t(`production_step_${step}`)}</h1>
        {actionError && <p role="alert" className="text-sm text-destructive">{actionError}</p>}
        {production?.error && <p role="alert" className="text-sm text-destructive">{production.status === "unknown" ? t("production_unknown") : production.error}</p>}
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        {loading ? <LoadingState variant="pane" /> : !projectName ? <ScriptInput /> : project && episode && <>
          {step === "script" && <ScriptInput key={`${projectName}/${episode}`} projectName={projectName} episode={episode} source={source ?? ""} disabled={active} />}
          {step === "breakdown" && <><BreakdownStatus task={planTask} busy={planning} review={review} /><div>{!planning && review?.content && episode && <BreakdownEditor project={projectName} episode={episode} projectData={project} review={review} busy={active} onChanged={refresh} onStarted={() => { refresh(); navigate(productionPath(projectName, "assets", episode)); }} />}</div></>}
          {step === "assets" && <><div role="status" className="flex flex-col gap-2 rounded-lg border border-border bg-card p-4"><p className="font-medium">{t("production_images_summary", { done: completedImages, total: assetItems.length })}</p><p className="text-sm text-muted-foreground">{t("production_images_hint")}</p></div>{groups.map((kind) => {
            const items: Record<string, { description: string; generation_prompt?: string; character_sheet?: string; scene_sheet?: string; prop_sheet?: string }> = (kind === "character" ? project.characters : kind === "scene" ? project.scenes : project.props) ?? {};
            return <section key={kind} className="flex flex-col gap-3"><h2 className="font-medium">{t(kind === "character" ? "characters" : kind === "scene" ? "scenes" : "props")}</h2>
              <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,18rem),1fr))] gap-4">{Object.entries(items ?? {}).filter(([name]) => adoptedNames.has(name)).map(([name, a]) => {
                const path = kind === "character" ? a.character_sheet : kind === "scene" ? a.scene_sheet : a.prop_sheet;
                return <ProductionAssetCard key={name} project={projectName} episode={episode} kind={kind} name={name} description={a.description} generationPrompt={a.generation_prompt} path={path} busy={planning || starting || production?.status === "unknown" || production?.status === "submitting" || production?.status === "active" && production.stage !== "assets"} onChanged={refresh} />;
              })}</div></section>;
          })}</>}
          {step === "videos" && <><Button variant="outline" className="self-start" disabled={active || !allImagesReady} onClick={() => void prepareVideoPrompts()}>{t("production_prepare_video_prompts")}</Button><p className="text-sm text-muted-foreground">{t(assetsReady ? "production_video_review_hint" : "production_video_prepare_hint")}</p><section aria-label={t("production_videos_progress")} className="flex flex-col gap-3 rounded-lg border border-border bg-card p-4">
            <Progress value={videoCounts.succeeded} max={units.length || 1}>
              <ProgressLabel>{t("production_videos_summary", { done: videoCounts.succeeded, total: units.length })}</ProgressLabel><ProgressValue />
            </Progress>
            <p className="text-sm text-muted-foreground">{t("production_videos_counts", { running: videoCounts.running, queued: videoCounts.queued + videoCounts.submitting, failed: videoCounts.failed, waiting: videoCounts.waiting + videoCounts.unknown })}</p>
            <p role="status" className="flex items-center gap-2 text-sm">{production?.stage === "render" && production.status === "active" && <Loader2 aria-hidden className="size-4 animate-spin text-primary" />}{t(production?.stage === "render" && production.status === "active" ? "production_videos_rendering" : "production_videos_hint")}</p>
          </section><div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,20rem),1fr))] gap-4">{episode && units.map((u, i) => <ProductionVideoCard key={u.unit_id} project={projectName} episode={episode} unit={u} index={i} production={production} progress={videoProgress.get(u.unit_id)!} onChanged={refresh} onUploaded={rebuild} />)}</div>{!active && canRebuild && <Button variant="outline" className="self-start" onClick={() => void rebuild()}>{t("production_rebuild_output")}</Button>}</>}
          {step === "output" && (previousOutputUrl ? <><video controls src={previousOutputUrl} className="max-h-[70vh] w-full"><track kind="captions" /></video>{!outputCurrent && <p className="text-sm text-muted-foreground">{t("production_output_changed")}</p>}{!outputCurrent && !active && canRebuild && <Button variant="outline" className="self-start" onClick={() => void rebuild()}>{t("production_rebuild_output")}</Button>}</> : <div className="flex flex-col gap-3"><p className="text-sm text-muted-foreground">{t(output && output.status !== "missing" ? "production_output_changed" : "production_wait_output")}</p>{!active && canRebuild && <Button variant="outline" className="self-start" onClick={() => void rebuild()}>{t("production_rebuild_output")}</Button>}</div>)}
          {step === "output" && production?.timeline_id && <ProductionEditor project={projectName} episode={episode} units={units} busy={active} onChanged={refresh} onRender={() => rebuild(true)} />}
          {step === "output" && <EpisodeCost project={projectName} episode={episode} revision={JSON.stringify(production)} />}
        </>}
      </div>
    </main>
    <footer className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-t border-border bg-card px-4 py-3"><p className="text-sm text-muted-foreground">{active ? t("production_running") : step === "assets" ? t("production_video_targets", { count: units.length }) : t("production_saved")}</p>{step === "output" && outputUrl ? <a className={buttonVariants()} href={outputUrl} download>{t("production_download")}</a> : <Button form={step === "breakdown" && !retryPlan || !projectName || step === "script" ? "production-action" : undefined} type={step === "breakdown" && !retryPlan || !projectName || step === "script" ? "submit" : "button"} onClick={retryPlan && projectName ? () => void plan() : step === "assets" ? assetAction : step === "videos" ? () => void startVideos() : undefined} disabled={active || (step !== "script" && step !== "breakdown" && step !== "assets" && step !== "videos") || (step === "breakdown" && !retryPlan && (!review?.content || !!review.quarantine)) || (step === "assets" && !allImagesReady && (!review?.fingerprint || review.status !== "confirmed")) || (step === "videos" && (!allImagesReady || !assetsReady))}>{t(planning ? "production_breakdown_running" : retryPlan ? "production_breakdown_retry" : step === "script" ? "production_start" : step === "breakdown" ? "production_confirm_assets" : step === "assets" ? allImagesReady ? "production_review_videos" : "production_confirm_image_prompts" : step === "videos" && retryVideos ? "production_retry_failed" : step === "videos" ? "production_confirm_videos" : "production_download")}</Button>}</footer>
    {picker && <WorkPicker onClose={() => setPicker(false)} />}
  </div>;
}
