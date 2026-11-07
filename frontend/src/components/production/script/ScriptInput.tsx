import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useLocation } from "wouter";
import { enqueueScriptPlan } from "@/actions/generation";
import { API } from "@/api";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { useLeaveGuard } from "@/components/shared/edit-unit/LeaveGuard";
import { isResourceBusy } from "@/stores/tasks-store";
import { scriptPlanResourceId } from "@/actions/generation";
import { errMsg } from "@/utils/async";
import { productionPath, projectPath } from "../production-paths";
import { DEFAULT_TEMPLATE_ID, STYLE_TEMPLATES } from "@/data/style-templates";

const DEFAULT_INPUT = { title: "", text: "", aspect: "9:16" as "9:16" | "16:9", templateId: DEFAULT_TEMPLATE_ID };
function initialInput() {
  try { const v: unknown = JSON.parse(sessionStorage.getItem("xm:script-draft") ?? "{}");
    if (!v || typeof v !== "object") return DEFAULT_INPUT;
    const value = v as Record<string, unknown>;
    return { title: typeof value.title === "string" ? value.title : "", text: typeof value.text === "string" ? value.text : "", aspect: value.aspect === "16:9" ? "16:9" as const : "9:16" as const, templateId: typeof value.templateId === "string" && STYLE_TEMPLATES.some((template) => template.id === value.templateId) ? value.templateId : DEFAULT_TEMPLATE_ID };
  }
  catch { return DEFAULT_INPUT; }
}
export function ScriptInput({ projectName, episode: targetEpisode, createOnly = false, source = "", disabled = false, readOnly = false }: { projectName?: string; episode?: number; createOnly?: boolean; source?: string; disabled?: boolean; readOnly?: boolean }) {
  const draftKey = projectName ? `xm:script-draft:${projectName}:${targetEpisode}` : "xm:script-draft";
  const [initial] = useState(() => {
    if (!projectName) return initialInput();
    return { ...DEFAULT_INPUT, text: readOnly ? source : sessionStorage.getItem(draftKey) ?? source };
  });
  const { t } = useTranslation("dashboard");
  const { t: templateText } = useTranslation("templates");
  const [, navigate] = useLocation();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [value, setValue] = useState(initial);
  const [createdName, setCreatedName] = useState<string | null>(projectName ?? null);
  const [createdEpisode, setCreatedEpisode] = useState<number | null>(targetEpisode ?? null);
  const selectedTemplate = STYLE_TEMPLATES.find((template) => template.id === value.templateId);
  useEffect(() => { sessionStorage.setItem(draftKey, projectName ? value.text : JSON.stringify(value)); }, [value, draftKey, projectName]);
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [savedText, setSavedText] = useState(source);
  const [needsConfirmation, setNeedsConfirmation] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [extracting, setExtracting] = useState(false);
  const persist = async (): Promise<{ name: string; episode: number | null } | null> => {
    if (busy || extracting || disabled || (!projectName && !value.title.trim()) || (!createOnly && !value.text.trim())) return null;
    if (createdName && createdEpisode && isResourceBusy("text_script_plan", createdName, scriptPlanResourceId(createdEpisode))) return null;
    setBusy(true); setError(null);
    let name = createdName;
    try {
      if (!name) {
        name = (await API.createProject({ title: value.title.trim(), content_mode: "drama", generation_mode: "reference_video", aspect_ratio: value.aspect, style_template_id: value.templateId })).name;
        setCreatedName(name);
      }
      if (createOnly) return { name, episode: null };
      let episode = createdEpisode;
      if (episode == null) {
        episode = (await API.createEpisode(name, { title: value.title.trim() })).episode;
        setCreatedEpisode(episode);
      }
      if (value.text !== savedText || pendingFile) {
        const result = await API.saveProductionSource(name, episode, value.text, savedText, pendingFile, confirmed);
        if (result.needs_confirmation) { setNeedsConfirmation(true); return null; }
        setSavedText(value.text); setPendingFile(null); setNeedsConfirmation(false); setConfirmed(false);
      }
      sessionStorage.removeItem(draftKey);
      return { name, episode };
    } catch (e) { setError(errMsg(e)); return null; }
    finally { setBusy(false); }
  };
  useLeaveGuard({ dirty: !createOnly && (value.text !== savedText || !!pendingFile), saving: busy || extracting, save: async () => !!(await persist()), discard: () => { setValue((v) => ({ ...v, text: savedText })); setPendingFile(null); sessionStorage.removeItem(draftKey); } });
  const create = async () => {
    const target = await persist();
    if (!target) return;
    if (createOnly) { sessionStorage.removeItem(draftKey); navigate(projectPath(target.name)); return; }
    if (target.episode == null) return;
    setBusy(true);
    try {
      await enqueueScriptPlan(target.name, target.episode, {});
      sessionStorage.removeItem(draftKey);
      navigate(productionPath(target.name, "breakdown", target.episode));
    } catch (e) { setError(errMsg(e)); }
    finally { setBusy(false); }
  };
  const upload = async (file: File) => {
    if (busy || extracting || disabled) return;
    setExtracting(true); setError(null);
    try {
      const preview = await API.previewProductionSource(file);
      setValue((p) => ({ ...p, text: preview.text })); setPendingFile(file); setConfirmed(false);
    } catch (e) { setError(errMsg(e)); }
    finally { setExtracting(false); }
  };
  return <form id="production-action" onSubmit={(e) => { e.preventDefault(); void create(); }} className="flex max-w-240 flex-col gap-6">
    {!projectName && <><div className="flex flex-wrap gap-4">
      <div className="flex min-w-0 flex-1 flex-col gap-2"><Label htmlFor="work-title">{t("production_work_name")}</Label>
        <Input id="work-title" required disabled={busy || extracting || disabled} value={value.title} onChange={(e) => setValue((p) => ({ ...p, title: e.target.value }))} /></div>
      <div className="flex flex-col gap-2"><Label>{t("aspect_ratio")}</Label><div className="flex gap-2">
        {(["9:16", "16:9"] as const).map((aspect) => <Button key={aspect} variant={value.aspect === aspect ? "default" : "outline"} aria-pressed={value.aspect === aspect} type="button" onClick={() => setValue((p) => ({ ...p, aspect }))}>{aspect}</Button>)}
      </div></div>
    </div>
    <div className="flex flex-col gap-3">
      <Label htmlFor="work-style">{t("production_style_template")}</Label>
      <select id="work-style" value={value.templateId} disabled={busy || !!createdName}
        className="h-9 w-full rounded-lg border border-input bg-background px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
        onChange={(event) => setValue((previous) => ({ ...previous, templateId: event.target.value }))}>
        {(["live", "anim"] as const).map((category) => <optgroup key={category} label={templateText(`category.${category}`)}>
          {STYLE_TEMPLATES.filter((template) => template.category === category).map((template) => <option key={template.id} value={template.id}>{templateText(`name.${template.id}`)}</option>)}
        </optgroup>)}
      </select>
      {selectedTemplate && <div className="flex items-center gap-3 rounded-lg border border-border p-3">
        <img src={selectedTemplate.thumbnail} alt={templateText(`name.${selectedTemplate.id}`)} width={72} height={96} className="h-24 w-18 shrink-0 rounded-md object-cover" />
        <div className="min-w-0"><p className="text-sm font-medium">{templateText(`name.${selectedTemplate.id}`)}</p><p className="mt-1 text-sm text-muted-foreground">{templateText(`tagline.${selectedTemplate.id}`)}</p></div>
      </div>}
    </div>
    </>}
    {!createOnly && <div className="flex flex-col gap-2"><Label htmlFor="work-script">{t("production_script")}</Label>
      <Textarea readOnly={readOnly} id="work-script" className="min-h-80" required disabled={busy || extracting || disabled} value={value.text} onChange={(e) => setValue((p) => ({ ...p, text: e.target.value }))} placeholder={t("production_script_hint")} />
      <Label htmlFor="script-upload">{t("production_upload")}</Label>
      <Input id="script-upload" type="file" disabled={busy || extracting || disabled || readOnly} accept=".docx,.txt,.md,text/plain,text/markdown,application/vnd.openxmlformats-officedocument.wordprocessingml.document" onChange={(e) => {
        const file = e.target.files?.[0]; e.target.value = ""; if (file) void upload(file);
      }} />
      {extracting && <p role="status">{t("production_source_extracting")}</p>}
      {pendingFile && <p className="text-sm text-muted-foreground">{t("production_source_preview", { filename: pendingFile.name })}</p>}
      {needsConfirmation && <label className="flex items-start gap-2 text-sm"><input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />{t("production_source_confirm")}</label>}
      {projectName && !readOnly && <Button type="button" variant="outline" className="self-start" disabled={busy || extracting || disabled || needsConfirmation && !confirmed} onClick={() => void persist()}>{t("production_source_save")}</Button>}
    </div>
    }
    {readOnly && <p className="text-sm text-muted-foreground">{t("whole_script_source_readonly")}</p>}
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    <p className="text-sm text-muted-foreground">{busy ? t("common:loading") : createOnly ? t("management_create_hint") : t("production_text_cost_unknown")}</p>
  </form>;
}
