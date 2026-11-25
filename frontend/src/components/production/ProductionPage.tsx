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
import { ProductionAssetCard } from "./assets/ProductionAssetCard";
import { ProductionDerivativeCard } from "./assets/ProductionDerivativeCard";
import { ProductionVideoCard } from "./videos/ProductionVideoCard";
import { BreakdownEditor } from "./breakdown/BreakdownEditor";
import { resolveCharacterForm } from "@/utils/reference-mentions";
import { errMsg } from "@/utils/async";
import { enqueuePromptAuthoring, enqueueProductionAssets, enqueueProductionRender, enqueueProductionFinish, enqueueProductionVideos, enqueueScriptPlan } from "@/actions/generation";
import { API } from "@/api";
import { useAppStore } from "@/stores/app-store";
import { useHasUnsavedChanges } from "@/components/shared/edit-unit/LeaveGuard";
import { BreakdownStatus } from "./breakdown/BreakdownStatus";
import { ReferenceScriptPlanPreviewPanel } from "@/components/canvas/reference/ReferenceScriptPlanPreviewPanel";
import { PromptAuthoringDraftPanel, usePromptAuthoringDraft } from "@/components/canvas/reference/PromptAuthoringDraftPanel";
import { useScriptPlanEntry } from "@/hooks/useScriptPlanEntry";
import { isResourceBusy, useActiveResourceIds, useLatestTasksByResource } from "@/stores/tasks-store";
import { scriptPlanResourceId, promptAuthoringResourceId, draftRepairResourceId } from "@/actions/generation";
import { Progress, ProgressLabel, ProgressValue } from "@/components/ui/progress";
import { useProductionVideoProgress } from "./videos/useProductionVideoProgress";
import { videoPromptReferenceRevision } from "./videos/video-prompt-preview";
import { useProjectsStore } from "@/stores/projects-store";
import { buildMentionLookup } from "@/utils/reference-mentions";
import { AlertDialog, AlertDialogAction, AlertDialogBody, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "@/components/ui/alert-dialog";

const finishErrors = [
  "finish_music_too_short", "finish_requires_local", "finish_inputs_changed", "finish_timeline_changed",
  "finish_video_not_ready", "finish_configure_jianying", "finish_music_result_unavailable",
  "finish_configure_minimax", "finish_render_failed", "finish_output_not_current",
  "finish_draft_conflict", "finish_close_jianying", "finish_draft_failed",
] as const;
function isFinishError(value: string): value is typeof finishErrors[number] {
  return finishErrors.some((key) => key === value);
}

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
  const { episode, project, units, source, sourceRevision, workflow, production, review, refresh, loading, refreshing, error } = useProductionProject(projectName ?? null, episodeQuery !== null && requestedEpisode === null ? -1 : requestedEpisode);
  // Continuity validation checks the adjacent saved units as well as the edited one.
  const handoffRevisions = units.map((unit) => unit.prompt_policy_version === "drama_detail_v1"
    ? JSON.stringify([unit.unit_id, unit.text, unit.generation_prompt, unit.pending_authoring]) : "");
  const mediaRevision = useProjectsStore((state) => videoPromptReferenceRevision(state.assetFingerprints));
  const videoReferenceRevision = JSON.stringify([workflow?.project_revision, mediaRevision,
    workflow?.content?.referenced_assets?.map(({ name, asset_type, owner, sheet_state }) =>
      JSON.stringify([asset_type, name, owner, sheet_state])).sort()]);
  const mentionLookup = buildMentionLookup(project);
  const [picker, setPicker] = useState(false);
  const [confirmReplan, setConfirmReplan] = useState(false);
  const [replanReason, setReplanReason] = useState("");
  const [repairOnce, setRepairOnce] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [timelineBlocked, setTimelineBlocked] = useState<Record<string, boolean>>({});
  const onTimelineValidation = useCallback((key: string, blocked: boolean) => {
    setTimelineBlocked((current) => current[key] === blocked ? current : { ...current, [key]: blocked });
  }, []);
  const hasUnsavedChanges = useHasUnsavedChanges();
  const { busy: planning, latestTask: planTask } = useScriptPlanEntry(projectName ?? "", episode ?? 0);
  const planFailed = planTask?.status === "failed" || planTask?.status === "cancelled";
  const assetItems = workflow?.content?.referenced_assets ?? [];
  const imageCharacters = useActiveResourceIds("character", projectName);
  const imageScenes = useActiveResourceIds("scene", projectName);
  const imageProps = useActiveResourceIds("prop", projectName);
  const imageDerivatives = useActiveResourceIds("character_derivative", projectName);
  const imagesBusy = assetItems.some(({ asset_type: kind, name, owner }) => (
    kind === "character" ? (owner ? imageDerivatives : imageCharacters).has(name)
    : kind === "scene" ? imageScenes.has(name) : kind === "prop" && imageProps.has(name)
  ));
  const activeVideos = useActiveResourceIds("reference_video", projectName);
  const videosBusy = units.some((unit) => activeVideos.has(unit.unit_id));
  const authoring = useActiveResourceIds("text_episode_script", projectName).has(promptAuthoringResourceId(episode ?? 0));
  const authoringTask = useLatestTasksByResource(projectName ?? "", "text_episode_script").get(promptAuthoringResourceId(episode ?? 0));
  const repairTask = useLatestTasksByResource(projectName ?? "", "text_draft_repair").get(draftRepairResourceId(episode ?? 0, "reference_prompt_authoring"));
  const promptDraftPresent = workflow?.content?.drafts.some((draft) => draft.kind === "reference_video_prompt_authoring") ?? false;
  const repairedAuthoring = !promptDraftPresent && repairTask?.status === "succeeded" && repairTask.result?.adopted === true
    && !!authoringTask && repairTask.queued_at > authoringTask.queued_at;
  const authoringFailed = !repairedAuthoring && (authoringTask?.status === "failed" || authoringTask?.status === "cancelled");
  const pendingPromptCount = units.filter((unit) => unit.pending_authoring).length;
  const operationBusy = imagesBusy || videosBusy || authoring || planning || starting || production?.status === "active" || production?.status === "submitting" || production?.status === "unknown";
  const active = loading || refreshing || operationBusy;
  const promptsReady = workflow?.content?.formal_script === "present" && units.length > 0 && !units.some((unit) => unit.pending_authoring);
  const failedUnitIds = production?.status === "failed" ? production.members?.filter((member) => ["failed", "cancelled", "blocked"].includes(member.status)).map((member) => member.unit_id) : undefined;
  const retryVideos = production?.stage === "videos" && production.action === "videos" && Boolean(failedUnitIds?.length);
  const videoTimelineBlocked = units.some((unit) => (!retryVideos || failedUnitIds?.includes(unit.unit_id)) && timelineBlocked[`${projectName}/${episode}/${unit.unit_id}`]);
  const output = production?.output;
  const outputCurrent = output?.status === "current" && output.version != null && Boolean(output.artifact_path);
  const previousOutputUrl = projectName && output?.artifact_path && output.version != null ? API.getFileUrl(projectName, output.artifact_path, output.version) : null;
  const outputUrl = projectName && outputCurrent ? API.getFileUrl(projectName, output.artifact_path, output.version) : null;
  const inputGate = workflow?.gates.production_inputs;
  const canRebuild = inputGate?.videos_ready === true;
  const completedImages = assetItems.filter((asset) => asset.sheet_state === "current" || asset.sheet_state === "stale").length;
  const allImagesReady = inputGate?.references_ready === true;
  const openSavedBreakdown = Boolean(review?.content || review?.quarantine);
  const savedPlanStale = workflow?.content?.episode_plan_stale || workflow?.artifacts?.script_plan?.state === "stale";
  const explicitStep = PRODUCTION_STEPS.find((s) => s === requestedStep);
  const resumedStep = (
    planning || review?.quarantine || savedPlanStale ? "breakdown"
      : outputCurrent || production?.action === "finish" ? "output"
      : promptsReady && allImagesReady ? "videos"
      : review?.status === "confirmed" || workflow?.content?.formal_script === "present" ? "assets"
      : openSavedBreakdown || planFailed || planTask?.status === "succeeded" ? "breakdown"
      : "script"
  );
  const step = explicitStep ?? resumedStep;
  useEffect(() => {
    if (!projectName || loading || !project) return;
    if (!episode || (episodeQuery !== null && requestedEpisode === null)) {
      navigate(projectPath(projectName), { replace: true });
    } else if (!explicitStep) {
      // Resolve once on entry; later polling must not switch away from an open editor.
      navigate(productionPath(projectName, resumedStep, episode), { replace: true });
    }
  }, [episode, episodeQuery, explicitStep, loading, navigate, project, projectName, requestedEpisode, resumedStep]);
  const retryPlan = step === "breakdown" && !openSavedBreakdown && planFailed;
  const submitForm = (step === "breakdown" && !retryPlan) || (step === "script" && !openSavedBreakdown);
  const assetSummary = workflow?.content?.asset_summary;
  const videoProgress = useProductionVideoProgress(projectName ?? "", units, production);
  const videoCounts = { succeeded: 0, running: 0, queued: 0, failed: 0, waiting: 0, submitting: 0, unknown: 0 };
  for (const item of videoProgress.values()) videoCounts[item.status] += 1;
  const plan = async () => {
    if (!projectName || !episode || active || !replanReason.trim() || hasUnsavedChanges() || isResourceBusy("text_script_plan", projectName, scriptPlanResourceId(episode))) return;
    const scope = actionScope.current;
    setStarting(true); setActionError(null);
    try {
      await enqueueScriptPlan(projectName, episode, { replan_reason: replanReason.trim(), ...(review?.repair_allowed && repairOnce ? { repair_once: true } : {}) });
      if (scope?.signal.aborted) { reportBackground(t("production_submitted_background")); return; }
      setConfirmReplan(false); setReplanReason(""); setRepairOnce(false);
      refresh(); navigate(productionPath(projectName, "breakdown", episode));
    } catch (e) {
      if (scope?.signal.aborted) reportBackground(errMsg(e), true); else setActionError(errMsg(e));
    } finally { setStarting(false); }
  };
  const requestReplan = () => {
    if (!projectName || !episode || active || isResourceBusy("text_script_plan", projectName, scriptPlanResourceId(episode))) return;
    if (hasUnsavedChanges()) { setActionError(t("common:unsaved_changes")); return; }
    setActionError(null);
    setRepairOnce(false); setConfirmReplan(true);
  };
  const revisePlan = async () => {
    if (!projectName || !episode || active || isResourceBusy("text_script_plan", projectName, scriptPlanResourceId(episode)) || !review?.fingerprint || hasUnsavedChanges()) return;
    const scope = actionScope.current;
    setStarting(true); setActionError(null);
    try {
      await API.startScriptReviewRevision(projectName, episode, review.fingerprint);
      if (scope?.signal.aborted) return;
      refresh(); navigate(productionPath(projectName, "breakdown", episode));
    } catch (cause) { if (!scope?.signal.aborted) setActionError(errMsg(cause)); }
    finally { setStarting(false); }
  };
  const repairPlan = async () => {
    const draft = review?.quarantine;
    if (!projectName || !episode || !review?.repair_allowed || active || !draft?.revision || !draft.repair?.available || isResourceBusy("text_script_plan", projectName, scriptPlanResourceId(episode))) return;
    if (hasUnsavedChanges()) { setActionError(t("common:unsaved_changes")); return; }
    const scope = actionScope.current;
    setStarting(true); setActionError(null);
    try {
      await enqueueScriptPlan(projectName, episode, { repair_draft_revision: draft.revision });
      if (scope?.signal.aborted) { reportBackground(t("production_submitted_background")); return; }
      refresh();
    } catch (cause) { if (scope?.signal.aborted) reportBackground(errMsg(cause), true); else setActionError(errMsg(cause)); }
    finally { setStarting(false); }
  };
  const recheckPlan = async () => {
    const taskId = review?.repair_progress?.can_recheck ? review.repair_progress.task_id : null;
    if (!projectName || !episode || !taskId || active || isResourceBusy("text_script_plan", projectName, scriptPlanResourceId(episode))) return;
    if (hasUnsavedChanges()) { setActionError(t("common:unsaved_changes")); return; }
    const scope = actionScope.current;
    setStarting(true); setActionError(null);
    try {
      await enqueueScriptPlan(projectName, episode, { repair_recheck_task_id: taskId });
      if (scope?.signal.aborted) { reportBackground(t("production_submitted_background")); return; }
      refresh();
    } catch (cause) { if (scope?.signal.aborted) reportBackground(errMsg(cause), true); else setActionError(errMsg(cause)); }
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
  const rebuild = async () => {
    if (!projectName || !episode || active) return;
    if (hasUnsavedChanges()) { setActionError(t("common:unsaved_changes")); return; }
    const scope = actionScope.current;
    setStarting(true); setActionError(null);
    try {
      if (step === "videos") await enqueueProductionFinish(projectName, episode);
      else await enqueueProductionRender(projectName, episode);
      if (scope?.signal.aborted) { reportBackground(t("production_submitted_background")); return; }
      refresh();
      if (step === "videos") navigate(productionPath(projectName, "output", episode));
    }
    catch (cause) { if (scope?.signal.aborted) reportBackground(errMsg(cause), true); else setActionError(errMsg(cause)); }
    finally { setStarting(false); }
  };
  const resumeFinish = async () => {
    if (!projectName || !episode || active || hasUnsavedChanges()) return;
    setStarting(true); setActionError(null);
    try { await enqueueProductionFinish(projectName, episode, true); refresh(); }
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
  const prepareVideoPrompts = async (enterVideos = false) => {
    if (!projectName || !episode || active || !allImagesReady || promptsReady || workflow?.operations.author_prompts?.state !== "admitted" || isResourceBusy("text_episode_script", projectName, promptAuthoringResourceId(episode))) return;
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
        {production?.error && <p role="alert" className="text-sm text-destructive">{production.action === "finish" && isFinishError(production.error) ? t(production.error) : production.status === "unknown" ? t("production_unknown") : production.error}</p>}
        {production?.action === "finish" && production.status === "failed" && <Button variant="outline" className="self-start" disabled={active} onClick={() => void resumeFinish()}>{t("finish_resume")}</Button>}
        {production?.action === "finish" && production.status === "active" && <p role="status" className="text-sm">{t(production.stage === "draft" ? "finish_draft" : "finish_render")}</p>}
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        {loading ? <LoadingState variant="pane" /> : !projectName ? <ScriptInput /> : project && episode && <>
          {step === "script" && <ScriptInput key={`${projectName}/${episode}`} projectName={projectName} episode={episode} source={source ?? ""} sourceRevision={sourceRevision} openSavedBreakdown={openSavedBreakdown} disabled={active} repairEnabled={review?.repair_allowed === true} readOnly={project.episodes?.find((entry) => entry.episode === episode)?.source_origin === "whole_source"} />}
          {step === "breakdown" && <>
            <BreakdownStatus task={planTask} busy={planning} review={review} />
            {review?.repair_progress?.can_recheck && <Button type="button" variant="outline" className="self-start" disabled={active} onClick={() => void recheckPlan()}>{t("production_recheck_saved_result")}</Button>}
            {review?.repair_progress?.recheck_reason && <p role="status" className="text-sm text-muted-foreground">{review.repair_progress.recheck_reason}</p>}
            {review?.quarantine?.doc_type === "reference_script_plan" ? <ReferenceScriptPlanPreviewPanel
              key={`${projectName}:${episode}`} allowAgentActions={false}
              projectName={projectName}
              episode={episode}
              lookup={mentionLookup}
              onOpenTimeline={() => navigate(productionPath(projectName, "videos", episode))}
            /> : null}
            {review?.quarantine?.doc_type === "reference_script_plan" && review.repair_allowed && <Button type="button" variant="outline" className="self-start" disabled={active || !review.quarantine.repair?.available} onClick={() => void repairPlan()}>{t("production_repair_errors")}</Button>}
            {!review?.quarantine && review?.content && <Button type="button" variant="outline" className="self-start" disabled={active} onClick={requestReplan}>{t("production_breakdown_retry")}</Button>}
            <div>{!planning && review?.content && episode && !review.quarantine && <BreakdownEditor project={projectName} episode={episode} projectData={project} review={review} busy={active} onChanged={refresh} onStarted={() => { refresh(); navigate(productionPath(projectName, "assets", episode)); }} />}</div>
          </>}
          {step === "assets" && <><div role="status" className="flex flex-col gap-2 rounded-lg border border-border bg-card p-4"><p className="font-medium">{t("production_images_summary", { done: completedImages, total: assetItems.length })}</p><p className="text-sm text-muted-foreground">{t("production_images_hint")}</p>{assetSummary && <><p className="text-sm">{t("production_assets_counts", assetSummary)}</p>{assetSummary.unused_suggestions.length > 0 && <p className="text-sm text-muted-foreground">{t("production_assets_unused", { names: assetSummary.unused_suggestions.join("、") })}</p>}</>}</div>{groups.map((kind) => {
            const assets = assetItems.filter((asset) => asset.asset_type === kind);
            return <section key={kind} className="flex flex-col gap-3"><h2 className="font-medium">{t(kind === "character" ? "characters" : kind === "scene" ? "scenes" : "props")}</h2>
              <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,18rem),1fr))] gap-4">{assets.map((asset) => {
                if (kind === "character") {
                  const form = resolveCharacterForm(project.characters, asset.name);
                  if (!form) return null;
                  if (form.derivativeName) return <ProductionDerivativeCard key={asset.name} project={projectName} owner={form.ownerName} name={form.derivativeName} description={form.asset.description} path={form.asset.character_sheet} ownerPath={project.characters?.[form.ownerName]?.character_sheet} stale={asset.sheet_state === "stale"} busy={planning || starting || production?.status === "unknown" || production?.status === "submitting" || production?.status === "active" && production.stage !== "assets"} onChanged={refresh} />;
                  const base = project.characters?.[form.ownerName];
                  if (!base) return null;
                  return <ProductionAssetCard key={asset.name} project={projectName} episode={episode} kind="character" name={form.ownerName} description={base.description} generationPrompt={base.generation_prompt} path={base.character_sheet} stale={asset.sheet_state === "stale"} busy={planning || starting || production?.status === "unknown" || production?.status === "submitting" || production?.status === "active" && production.stage !== "assets"} onChanged={refresh} />;
                }
                if (kind === "scene") {
                  const item = project.scenes?.[asset.name];
                  if (!item) return null;
                  return <ProductionAssetCard key={asset.name} project={projectName} episode={episode} kind="scene" name={asset.name} description={item.description} generationPrompt={item.generation_prompt} path={item.scene_sheet} stale={asset.sheet_state === "stale"} busy={planning || starting || production?.status === "unknown" || production?.status === "submitting" || production?.status === "active" && production.stage !== "assets"} onChanged={refresh} />;
                }
                const item = project.props?.[asset.name];
                if (!item) return null;
                return <ProductionAssetCard key={asset.name} project={projectName} episode={episode} kind="prop" name={asset.name} description={item.description} generationPrompt={item.generation_prompt} path={item.prop_sheet} stale={asset.sheet_state === "stale"} busy={planning || starting || production?.status === "unknown" || production?.status === "submitting" || production?.status === "active" && production.stage !== "assets"} onChanged={refresh} />;
              })}</div></section>;
          })}</>}
          {step === "videos" && <><ProductionPromptDraftRecovery key={`${projectName}:${episode}`} projectName={projectName} episode={episode} onSettled={refresh} />{!promptsReady ? <section className="flex flex-col gap-3 rounded-lg border border-border bg-card p-4">
            <p role="status" className="flex items-center gap-2">{authoring && <Loader2 aria-hidden className="size-4 animate-spin text-primary" />}{t(authoring ? "production_video_authoring" : repairedAuthoring ? "production_video_repaired_continue" : "production_video_prepare_hint", { done: units.length - pendingPromptCount, total: units.length, remaining: pendingPromptCount })}</p>
            {!authoring && authoringFailed && <p role="alert" className="whitespace-pre-wrap text-sm text-destructive">{t("prompt_authoring_failed", { reason: authoringTask?.error_message || t("script_plan_failed_unknown") })}</p>}
            {!authoring && <Button variant="outline" className="self-start" disabled={active || !allImagesReady || workflow?.operations.author_prompts?.state !== "admitted"} onClick={() => void prepareVideoPrompts()}>{t(repairedAuthoring ? "production_continue_video_prompts" : "production_prepare_video_prompts")}</Button>}
          </section> : <><p className="text-sm text-muted-foreground">{t("production_video_review_hint")}</p><section aria-label={t("production_videos_progress")} className="flex flex-col gap-3 rounded-lg border border-border bg-card p-4">
            <Progress value={videoCounts.succeeded} max={units.length || 1}>
              <ProgressLabel>{t("production_videos_summary", { done: videoCounts.succeeded, total: units.length })}</ProgressLabel><ProgressValue />
            </Progress>
            <p className="text-sm text-muted-foreground">{t("production_videos_counts", { running: videoCounts.running, queued: videoCounts.queued + videoCounts.submitting, failed: videoCounts.failed, waiting: videoCounts.waiting + videoCounts.unknown })}</p>
            <p role="status" className="flex items-center gap-2 text-sm">{production?.stage === "render" && production.status === "active" && <Loader2 aria-hidden className="size-4 animate-spin text-primary" />}{t(production?.stage === "render" && production.status === "active" ? "production_videos_rendering" : "production_videos_hint")}</p>
          </section><div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,20rem),1fr))] gap-4">{episode && units.map((u, i) => <ProductionVideoCard key={`${projectName}/${episode}/${u.unit_id}`} referenceRevision={JSON.stringify([videoReferenceRevision, i === 0, handoffRevisions[i - 1], handoffRevisions[i + 1]])} project={projectName} episode={episode} unit={u} index={i} production={production} progress={videoProgress.get(u.unit_id)!} busy={operationBusy} generationSyncing={refreshing} onGenerate={() => startVideos([u.unit_id])} onChanged={refresh} onTimelineValidation={onTimelineValidation} />)}</div></>}</>}
          {step === "output" && (previousOutputUrl ? <><video controls src={previousOutputUrl} className="max-h-[70vh] w-full"><track kind="captions" /></video>{!outputCurrent && <p className="text-sm text-muted-foreground">{t("production_output_changed")}</p>}{!outputCurrent && !active && canRebuild && <Button variant="outline" className="self-start" onClick={() => void rebuild()}>{t("production_rebuild_output")}</Button>}</> : <div className="flex flex-col gap-3"><p className="text-sm text-muted-foreground">{t(output && output.status !== "missing" ? "production_output_changed" : "production_wait_output")}</p>{!active && canRebuild && <Button variant="outline" className="self-start" onClick={() => void rebuild()}>{t("production_rebuild_output")}</Button>}</div>)}
          {step === "output" && production?.timeline_id && <JianyingDeliveryCard key={`${projectName}/${episode}`} project={projectName} episode={episode} busy={active} revision={JSON.stringify(production)} />}
          {step === "output" && <EpisodeCost project={projectName} episode={episode} revision={JSON.stringify(production)} />}
        </>}
      </div>
    </main>
    {review?.status === "confirmed" && !review.quarantine && <div className="px-4 py-2"><Button variant="outline" disabled={active} onClick={() => void revisePlan()}>{t("common:edit")} · {t("production_step_breakdown")}</Button></div>}
    <footer className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-t border-border bg-card px-4 py-3"><p className="text-sm text-muted-foreground">{loading || (step !== "videos" && refreshing) ? t("common:loading") : operationBusy ? t("production_running") : step === "videos" && error ? error : step === "assets" ? t("production_video_targets", { count: units.length }) : step === "videos" && canRebuild ? t("finish_merge_hint") : t("production_saved")}</p>{step === "output" && outputUrl ? <a className={buttonVariants()} href={outputUrl} download>{t("production_download")}</a> : <Button form={submitForm ? "production-action" : undefined} type={submitForm ? "submit" : "button"} onClick={step === "script" && openSavedBreakdown && projectName && episode ? () => navigate(productionPath(projectName, "breakdown", episode)) : retryPlan && projectName ? requestReplan : step === "assets" ? assetAction : step === "videos" ? () => void (canRebuild ? rebuild() : startVideos()) : undefined} disabled={active || (step !== "script" && step !== "breakdown" && step !== "assets" && step !== "videos") || (step === "breakdown" && !retryPlan && (!review?.content || !!review.quarantine)) || (step === "assets" && (allImagesReady ? !promptsReady && workflow?.operations.author_prompts?.state !== "admitted" : !review?.fingerprint || review.status !== "confirmed")) || (step === "videos" && !canRebuild && (!allImagesReady || !promptsReady || videoTimelineBlocked))}>{t(planning ? "production_breakdown_running" : retryPlan ? "production_breakdown_retry" : step === "script" ? (openSavedBreakdown ? "production_open_saved_breakdown" : "production_start") : step === "breakdown" ? "production_confirm_assets" : step === "assets" ? allImagesReady ? promptsReady ? "production_review_videos" : "production_confirm_assets_write_prompts" : "production_confirm_image_prompts" : step === "videos" && canRebuild ? "production_confirm_merge" : step === "videos" && retryVideos ? "production_retry_failed" : step === "videos" ? "production_confirm_videos" : "production_download")}</Button>}</footer>
    {picker && <WorkPicker onClose={() => setPicker(false)} />}
    <AlertDialog open={confirmReplan} onOpenChange={(open) => { if (!starting) setConfirmReplan(open); }}>
      <AlertDialogContent>
        <AlertDialogHeader><AlertDialogTitle>{t("production_breakdown_retry")}</AlertDialogTitle></AlertDialogHeader>
        <AlertDialogBody><div className="flex flex-col gap-3">
          <AlertDialogDescription>{t(review?.status === "confirmed" ? "script_plan_replace_confirmed_hint" : review?.quarantine ? "script_plan_replace_draft_warning" : review?.content ? "script_plan_replace_pending_warning" : "script_plan_retry_failed_hint")}</AlertDialogDescription>
          <Label htmlFor="replan-reason">{t("script_plan_replan_reason")}</Label>
          <div className="text-foreground"><Textarea id="replan-reason" value={replanReason} onChange={(event) => setReplanReason(event.target.value)} placeholder={t("script_plan_replan_reason_placeholder")} maxLength={4000} rows={5} disabled={starting} aria-describedby="replan-reason-hint" /></div>
          <p id="replan-reason-hint">{t("script_plan_replan_reason_hint")}</p>
          {review?.repair_allowed && <label className="flex items-start gap-2 text-sm"><input type="checkbox" checked={repairOnce} onChange={(event) => setRepairOnce(event.target.checked)} disabled={starting} />{t("production_repair_once")}</label>}
          {actionError && <p role="alert" className="text-destructive">{actionError}</p>}
        </div></AlertDialogBody>
        <AlertDialogFooter><AlertDialogCancel disabled={starting}>{t("common:cancel")}</AlertDialogCancel><AlertDialogAction disabled={active || !replanReason.trim()} onClick={() => void plan()}>{starting ? t("production_breakdown_running") : t("production_breakdown_retry")}</AlertDialogAction></AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </div>;
}

function ProductionPromptDraftRecovery({
  projectName,
  episode,
  onSettled,
}: {
  projectName: string;
  episode: number;
  onSettled: () => void;
}) {
  const task = useLatestTasksByResource(projectName, "text_episode_script").get(promptAuthoringResourceId(episode));
  // 任务失败先落草稿，任务状态事件也应刷新恢复入口，不能只依赖独立的草稿事件。
  const { view, refresh } = usePromptAuthoringDraft(projectName, episode, `${task?.task_id ?? ""}:${task?.status ?? ""}`);
  if (!view) return null;
  return <PromptAuthoringDraftPanel allowAgentActions={false} projectName={projectName} episode={episode} view={view} onSettled={() => { refresh(); onSettled(); }} />;
}
