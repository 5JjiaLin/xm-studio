import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "wouter";
import { useTranslation } from "react-i18next";
import { ArrowDown, ArrowUp, Film, Settings } from "lucide-react";
import { ProductionCosts, useProductionCosts } from "./ProductionCosts";
import { API } from "@/api";
import type { EpisodeMeta, ProjectData, ProjectSummary } from "@/types/project";
import type { ProductionState } from "@/types/production";
import { Button, buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogBody, DialogFooter } from "@/components/ui/dialog";
import { LoadingState } from "@/components/shared/LoadingState";
import { useEditUnit } from "@/components/shared/edit-unit/useEditUnit";
import { errMsg } from "@/utils/async";
import { WholeScriptPanel } from "../script/WholeScriptPanel";
import { ScriptInput } from "../script/ScriptInput";
import { FeedbackExportButton } from "../feedback/FeedbackExportButton";
import { productionPath, projectPath } from "../production-paths";

function ManagementLayout({ title, projects, loading = false, children }: { title: string; projects: Pick<ProjectSummary, "name" | "title">[]; loading?: boolean; children: React.ReactNode }) {
  const { t } = useTranslation("dashboard");
  return <div className="flex h-dvh flex-col bg-background text-foreground">
    <header className="flex shrink-0 flex-wrap items-center gap-3 border-b border-border px-4 py-3">
      <Link href={projectPath()} className="flex items-center gap-2 font-semibold"><Film aria-hidden className="size-5 text-primary" />XM Studio</Link>
      <span className="min-w-0 flex-1 truncate text-sm text-muted-foreground">{title}</span>
      <FeedbackExportButton projects={projects} disabled={loading} />
      <Link href="/app/settings?section=default-models" onClick={() => sessionStorage.setItem("xm:production-return", window.location.pathname + window.location.search)} className={buttonVariants({ variant: "ghost" })}><Settings aria-hidden />{t("production_models")}</Link>
    </header>
    <main className="relative min-h-0 flex-1 overflow-y-auto p-4"><div className="mx-auto flex max-w-300 flex-col gap-6">{children}</div></main>
  </div>;
}

function NameDialog({ title, initial = "", save, onClose }: { title: string; initial?: string; save: (value: string) => Promise<unknown>; onClose: () => void }) {
  const { t } = useTranslation("dashboard");
  const persist = useCallback(async (value: string) => { await save(value.trim()); }, [save]);
  const unit = useEditUnit({ source: initial, save: persist });
  const busy = unit.status === "saving";
  const close = () => { unit.discard(); onClose(); };
  return <Dialog open onOpenChange={(open) => { if (!open && !busy) close(); }}><DialogContent>
    <DialogHeader><DialogTitle>{title}</DialogTitle></DialogHeader>
    <form onSubmit={(event) => { event.preventDefault(); if (unit.value.trim()) void unit.save().then((ok) => { if (ok) onClose(); }); }}>
      <DialogBody><Label htmlFor="management-name">{t("management_name")}</Label><Input id="management-name" required maxLength={200} disabled={busy} value={unit.value} onChange={(event) => unit.setValue(event.target.value)} />{unit.error && <p role="alert" className="text-sm text-destructive">{unit.error}</p>}</DialogBody>
      <DialogFooter><Button type="button" variant="outline" disabled={busy} onClick={close}>{t("common:cancel")}</Button><Button type="submit" disabled={busy || !unit.value.trim()}>{t("common:save")}</Button></DialogFooter>
    </form>
  </DialogContent></Dialog>;
}

function ProjectList() {
  const { t } = useTranslation("dashboard");
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [archived, setArchived] = useState(false);
  const [creating, setCreating] = useState(false);
  const [renaming, setRenaming] = useState<ProjectSummary | null>(null);
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const c = new AbortController();
    void API.listProjects({ signal: c.signal }).then((result) => { if (!c.signal.aborted) { setProjects(result.projects); setError(null); } })
      .catch((cause) => { if (!c.signal.aborted) setError(errMsg(cause)); }).finally(() => { if (!c.signal.aborted) setLoading(false); });
    return () => c.abort();
  }, [revision]);
  const archive = async (project: ProjectSummary) => {
    setBusy(true); setError(null);
    try { await API.setProductionArchive(project.name, !archived); setRevision((r) => r + 1); }
    catch (cause) { setError(errMsg(cause)); } finally { setBusy(false); }
  };
  const rename = useCallback(async (title: string) => {
    if (renaming) { await API.updateProject(renaming.name, { title }); setRevision((r) => r + 1); }
  }, [renaming]);
  return <ManagementLayout title={t("management_projects")} projects={projects} loading={loading}>
    <div className="flex flex-wrap items-center gap-3"><h1 className="flex-1 text-xl font-semibold">{t("management_projects")}</h1>
      <Button variant="outline" onClick={() => { setArchived((v) => !v); setCreating(false); }}>{t(archived ? "management_active" : "management_archived")}</Button>
      <Button onClick={() => setCreating((v) => !v)}>{t(creating ? "common:cancel" : "management_new_project")}</Button>
    </div>
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    {creating ? <section className="flex flex-col gap-4"><h2 className="font-medium">{t("management_new_project")}</h2><ScriptInput createOnly /><Button form="production-action" type="submit" className="self-start">{t("management_create")}</Button></section> : loading ? <LoadingState variant="pane" /> : <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,22rem),1fr))] gap-4">
      {projects.filter((p) => !!p.production_management?.archived === archived).map((project) => <article key={project.name} className="flex flex-col gap-4 rounded-lg border border-border bg-card p-4">
        {project.thumbnail && <img src={project.thumbnail} alt="" className="h-36 w-full rounded-md object-cover" />}
        <h2 className="wrap-break-word font-semibold">{project.title || t("management_untitled_project")}</h2>
        <p className="text-sm text-muted-foreground">{t("management_episode_count", { count: project.episode_count ?? 0 })}</p>
        {project.last_activity_at && <p className="text-sm text-muted-foreground">{t("management_updated")} {new Date(project.last_activity_at).toLocaleString()}</p>}
        <div className="flex flex-wrap gap-2"><Link href={projectPath(project.name)} className={buttonVariants()}>{t("management_open")}</Link><Button variant="outline" disabled={busy || archived} onClick={() => setRenaming(project)}>{t("management_rename")}</Button><Button variant="ghost" disabled={busy} onClick={() => void archive(project)}>{t(archived ? "management_restore" : "management_archive")}</Button></div>
      </article>)}
      {!projects.some((p) => !!p.production_management?.archived === archived) && <p className="text-sm text-muted-foreground">{t("management_empty")}</p>}
    </div>}
    {renaming && <NameDialog title={t("management_rename")} initial={renaming.title} save={rename} onClose={() => setRenaming(null)} />}
  </ManagementLayout>;
}

function EpisodeOutput({ project, episode }: { project: string; episode: number }) {
  const { t } = useTranslation("dashboard");
  const [state, setState] = useState<ProductionState | null>(null);
  useEffect(() => {
    const c = new AbortController();
    void API.getProduction(project, episode, { signal: c.signal }).then((data) => { if (!c.signal.aborted) setState(data); }).catch(() => { /* Production page presents retryable read errors. */ });
    return () => c.abort();
  }, [episode, project]);
  const output = state?.output;
  if (output?.status !== "current" || output.version == null || !output.artifact_path) return null;
  return <><Link href={productionPath(project, "output", episode)} className={buttonVariants({ variant: "outline" })}>{t("management_preview")}</Link><a href={API.getFileUrl(project, output.artifact_path, output.version)} download className={buttonVariants({ variant: "ghost" })}>{t("production_download")}</a></>;
}

function EpisodeList({ name }: { name: string }) {
  const { t } = useTranslation("dashboard");
  const [project, setProject] = useState<ProjectData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const costs = useProductionCosts(name, revision);
  const [showArchived, setShowArchived] = useState(false);
  const [editing, setEditing] = useState<EpisodeMeta | "new" | null>(null);
  const [busy, setBusy] = useState(false);
  const [batchActive, setBatchActive] = useState(false);
  const refresh = useCallback(() => setRevision((r) => r + 1), []);
  useEffect(() => {
    const c = new AbortController();
    void API.getProject(name, { signal: c.signal }).then((result) => { if (!c.signal.aborted) { setProject(result.project); setError(null); } }).catch((cause) => { if (!c.signal.aborted) setError(errMsg(cause)); });
    return () => c.abort();
  }, [name, revision]);
  const run = async (action: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await action(); setRevision((r) => r + 1); } catch (cause) { setError(errMsg(cause)); } finally { setBusy(false); }
  };
  const save = useCallback(async (title: string) => {
    if (editing === "new") await API.createEpisode(name, { title });
    else if (editing) await API.updateEpisode(name, editing.episode, { title });
    setRevision((r) => r + 1);
  }, [editing, name]);
  const episodes = project?.episodes ?? [];
  const archivedIds = project?.production_management?.archived_episodes ?? [];
  const projectArchived = !!project?.production_management?.archived;
  const visible = episodes.filter((ep) => archivedIds.includes(ep.episode) === showArchived);
  return <ManagementLayout title={project?.title ?? t("management_episodes")} projects={project ? [{ name, title: project.title }] : []}>
    <Link href={projectPath()} className="self-start text-sm text-primary">{t("management_back")}</Link>
    <div className="flex flex-wrap items-center gap-3"><h1 className="min-w-0 flex-1 wrap-break-word text-xl font-semibold">{project?.title || t("management_episodes")}</h1>
      <Button variant="outline" onClick={() => setRevision((r) => r + 1)}>{t("management_refresh")}</Button>
      <Button variant="outline" onClick={() => setShowArchived((v) => !v)}>{t(showArchived ? "management_active" : "management_archived")}</Button>
      <Button disabled={projectArchived || !project || batchActive} onClick={() => setEditing("new")}>{t("management_new_episode")}</Button>
    </div>
    {project && <WholeScriptPanel project={name} disabled={projectArchived || busy} onImported={refresh} onActive={setBatchActive} />}
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    {projectArchived && <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border p-4"><p>{t("management_project_archived")}</p><Button disabled={busy || batchActive} onClick={() => void run(() => API.setProductionArchive(name, false))}>{t("management_restore")}</Button></div>}
    {!project && !error ? <LoadingState variant="pane" /> : <div className="flex flex-col gap-4">{visible.map((ep, index) => {
      const position = episodes.findIndex((entry) => entry.episode === ep.episode);
      return <article key={ep.episode} className="flex flex-col gap-3 rounded-lg border border-border bg-card p-4">
        <h2 className="wrap-break-word font-semibold">{t("management_episode_position", { position: index + 1 })} · {ep.title || t("management_untitled_episode")}</h2>
        <p className="text-sm text-muted-foreground">{t(ep.status === "completed" ? "production_step_output" : ep.script_status === "generated" ? "production_step_videos" : ep.script_status === "segmented" ? "production_step_breakdown" : "production_step_script")}{ep.videos && ep.videos.total > 0 ? ` · ${ep.videos.available - ep.videos.stale}/${ep.videos.total}` : ""}</p>
        {costs.data?.episodes.find((item) => item.episode === ep.episode) ? <ProductionCosts cost={costs.data.episodes.find((item) => item.episode === ep.episode)!} /> : <p className="text-sm text-muted-foreground">{t(costs.error ? "production_cost_load_failed" : "common:loading")}</p>}
        <div className="flex flex-wrap gap-2">
          {!showArchived && !projectArchived && <><Link href={productionPath(name, "script", ep.episode)} className={buttonVariants()}>{t("management_continue")}</Link><EpisodeOutput key={`${ep.episode}/${revision}`} project={name} episode={ep.episode} /></>}
          <Button variant="outline" disabled={busy || batchActive || showArchived || projectArchived} onClick={() => setEditing(ep)}>{t("management_rename")}</Button>
          {!showArchived && !projectArchived && <><Button variant="outline" aria-label={t("management_up", { title: ep.title })} disabled={busy || batchActive || position === 0} onClick={() => void run(() => API.moveEpisode(name, ep.episode, episodes[position - 2]?.episode ?? null))}><ArrowUp aria-hidden /></Button><Button variant="outline" aria-label={t("management_down", { title: ep.title })} disabled={busy || batchActive || position === episodes.length - 1} onClick={() => void run(() => API.moveEpisode(name, ep.episode, episodes[position + 1].episode))}><ArrowDown aria-hidden /></Button></>}
          <Button variant="ghost" disabled={busy || batchActive || projectArchived} onClick={() => void run(() => API.setProductionArchive(name, !showArchived, ep.episode))}>{t(showArchived ? "management_restore" : "management_archive")}</Button>
        </div>
      </article>;
    })}{project && visible.length === 0 && <p className="text-sm text-muted-foreground">{t("management_empty")}</p>}</div>}
    {costs.data && costs.data.unassigned.call_count > 0 && <ProductionCosts cost={costs.data.unassigned} unassigned />}
    {editing && <NameDialog title={t(editing === "new" ? "management_new_episode" : "management_rename")} initial={editing === "new" ? "" : editing.title} save={save} onClose={() => setEditing(null)} />}
  </ManagementLayout>;
}

export function ProjectsPage() {
  const { projectName } = useParams<{ projectName?: string }>();
  return projectName ? <EpisodeList key={projectName} name={projectName} /> : <ProjectList />;
}
