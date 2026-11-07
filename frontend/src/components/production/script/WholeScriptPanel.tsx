import { useCallback, useEffect, useState } from "react";
import { Link } from "wouter";
import { useTranslation } from "react-i18next";
import { API } from "@/api";
import type { ScriptBatchState, ScriptBoundary, WholeScriptPreview } from "@/types/production";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { useLeaveGuard } from "@/components/shared/edit-unit/LeaveGuard";
import { errMsg } from "@/utils/async";
import { productionPath } from "../production-paths";

const ACTIVE = ["active", "submitting", "unknown", "stopping"];

export function WholeScriptPanel({ project, disabled, onImported, onActive }: {
  project: string; disabled: boolean; onImported: () => void; onActive: (active: boolean) => void;
}) {
  const { t } = useTranslation("dashboard");
  const [expanded, setExpanded] = useState(false);
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<WholeScriptPreview | null>(null);
  const [boundaries, setBoundaries] = useState<ScriptBoundary[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [batch, setBatch] = useState<ScriptBatchState | null>(null);
  const [selectedImport, setSelectedImport] = useState("");
  const active = batch != null && ACTIVE.includes(batch.status);
  const locked = busy || active || disabled;
  useEffect(() => { onActive(active); }, [active, onActive]);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let lastStatus = "";
    const poll = async () => {
      try {
        const result = await API.getScriptBatch(project, controller.signal);
        if (controller.signal.aborted) return;
        setBatch(result);
        if (lastStatus && lastStatus !== result.items.map((i) => i.status).join(",")) onImported();
        lastStatus = result.items.map((i) => i.status).join(",");
      } catch (cause) { if (!controller.signal.aborted) setError(errMsg(cause)); }
      if (!controller.signal.aborted) timer = setTimeout(() => { void poll(); }, 2500);
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [project, onImported]);
  const perform = async (work: () => Promise<void>) => {
    if (busy) return;
    setBusy(true); setError(null);
    try { await work(); } catch (cause) { setError(errMsg(cause)); } finally { setBusy(false); }
  };
  const recognize = async (manual = false) => {
    const result = await API.previewWholeScript(project, text, manual ? boundaries : undefined);
    setText(result.text); setPreview(result);
    setBoundaries(result.episodes.map(({ start_line, title, number }) => ({ start_line, title, number })));
  };
  const persist = useCallback(async () => {
    if (!preview || preview.errors.length || locked) return false;
    setBusy(true); setError(null);
    try {
      const result = await API.importWholeScript(project, preview, file);
      setSelectedImport(result.import_id); setBatch(await API.getScriptBatch(project));
      setText(""); setFile(null); setPreview(null); setBoundaries([]); setExpanded(false); onImported();
      return true;
    } catch (cause) { setError(errMsg(cause)); return false; } finally { setBusy(false); }
  }, [preview, locked, project, file, onImported]);
  useLeaveGuard({ dirty: !!text, saving: busy, save: persist, discard: () => { setText(""); setFile(null); setPreview(null); setBoundaries([]); } });
  const updateBoundary = (index: number, value: Partial<ScriptBoundary>) => {
    setBoundaries((all) => all.map((b, i) => i === index ? { ...b, ...value } : b)); setPreview(null);
  };
  const imports = batch?.imports ?? [];
  const importId = selectedImport || batch?.import_id || imports.at(-1)?.import_id;
  const complete = batch?.items.filter((i) => i.status === "completed").length ?? 0;
  return <section className="flex flex-col gap-4 rounded-lg border border-border bg-card p-4">
    <div className="flex flex-wrap items-center gap-3"><h2 className="flex-1 font-semibold">{t("whole_script_title")}</h2><Button variant="outline" disabled={locked} onClick={() => setExpanded((v) => !v)}>{t("whole_script_upload")}</Button></div>
    <p className="text-sm text-muted-foreground">{t("whole_script_hint")}</p>
    {expanded && <div className="flex flex-col gap-3">
      <Label htmlFor="whole-script-file">{t("production_upload_source")}</Label><Input id="whole-script-file" type="file" accept=".docx,.txt,.md" disabled={locked} onChange={(event) => {
        const next = event.target.files?.[0]; event.target.value = "";
        if (next) void perform(async () => { const result = await API.previewProductionSource(next); setText(result.text); setFile(next); setPreview(null); setBoundaries([]); });
      }} />
      <Label htmlFor="whole-script-text">{t("whole_script_text")}</Label><Textarea id="whole-script-text" className="h-64" disabled={locked} value={text} onChange={(event) => { setText(event.target.value); setPreview(null); setBoundaries([]); }} />
      <Button className="self-start" variant="outline" disabled={locked || !text.trim()} onClick={() => void perform(() => recognize())}>{t("whole_script_recognize")}</Button>
      {boundaries.length > 0 && <><p className="text-sm text-muted-foreground">{t("whole_script_boundary_hint")}</p>{boundaries.map((boundary, index) => <div key={index} className="flex flex-wrap items-end gap-2 rounded-md border border-border p-3">
        <div><Label htmlFor={`boundary-line-${index}`}>{t("whole_script_line")}</Label><Input id={`boundary-line-${index}`} className="w-24" type="number" min={1} disabled={locked || index === 0} value={boundary.start_line} onChange={(e) => updateBoundary(index, { start_line: Number(e.target.value) })} /></div>
        <div><Label htmlFor={`boundary-number-${index}`}>{t("whole_script_number")}</Label><Input id={`boundary-number-${index}`} className="w-24" type="number" min={1} disabled={locked} value={boundary.number} onChange={(e) => updateBoundary(index, { number: Number(e.target.value) })} /></div>
        <div className="min-w-0 flex-1"><Label htmlFor={`boundary-title-${index}`}>{t("whole_script_episode_title")}</Label><Input id={`boundary-title-${index}`} disabled={locked} value={boundary.title} onChange={(e) => updateBoundary(index, { title: e.target.value })} /></div>
        {index > 0 && <Button variant="ghost" disabled={locked} onClick={() => { setBoundaries((all) => all.filter((_, i) => i !== index)); setPreview(null); }}>{t("whole_script_remove_boundary")}</Button>}
        {preview?.episodes[index] && <div className="w-full text-sm text-muted-foreground"><p>{t("whole_script_characters", { count: preview.episodes[index].characters })}</p><p className="whitespace-pre-wrap wrap-break-word">{preview.episodes[index].head} … {preview.episodes[index].tail}</p></div>}
      </div>)}<div className="flex flex-wrap gap-2"><Button variant="outline" disabled={locked} onClick={() => { setBoundaries((all) => [...all, { start_line: (all.at(-1)?.start_line ?? 1) + 1, number: (all.at(-1)?.number ?? 0) + 1, title: t("whole_script_new_title") }]); setPreview(null); }}>{t("whole_script_add_boundary")}</Button><Button variant="outline" disabled={locked} onClick={() => void perform(() => recognize(true))}>{t("whole_script_verify")}</Button></div></>}
      {preview?.warnings.map((warning) => <p key={warning} className="text-sm text-muted-foreground">{warning}</p>)}
      {preview?.errors.map((problem) => <p key={problem} role="alert" className="text-sm text-destructive">{problem}</p>)}
      <Button className="self-start" disabled={locked || !preview || preview.errors.length > 0} onClick={() => { void persist(); }}>{t("whole_script_confirm")}</Button>
    </div>}
    {imports.length > 0 && !active && <div className="flex flex-wrap items-end gap-3"><div className="min-w-0 flex-1"><Label htmlFor="whole-script-import">{t("whole_script_imported")}</Label><select id="whole-script-import" className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm" value={importId} disabled={busy || disabled} onChange={(e) => setSelectedImport(e.target.value)}>{imports.map((entry, index) => <option key={entry.import_id} value={entry.import_id}>{t("whole_script_import_label", { index: index + 1, count: entry.episodes.length })}</option>)}</select></div><Button disabled={busy || disabled || !importId || batch?.import_id === importId} onClick={() => void perform(async () => { await API.startScriptBatch(project, importId!); setBatch(await API.getScriptBatch(project)); onImported(); })}>{t("whole_script_start")}</Button></div>}
    {importId && batch?.import_id && importId !== batch.import_id && ["stopped", "failed"].includes(batch.status) && <p className="text-sm text-muted-foreground">{t("whole_script_switch_hint")}</p>}
    {batch && batch.status !== "idle" && <div className="flex flex-col gap-3" aria-live="polite">
      <p>{t(`whole_script_status_${batch.status}`)} · {complete}/{batch.items.length}</p>
      {batch.error && <p role="alert" className="text-sm text-destructive">{batch.error}</p>}
      {batch.input_changed && <p className="text-sm text-destructive">{t("whole_script_changed")}</p>}
      <div className="flex flex-wrap gap-3">{batch.items.map((item, index) => <Link key={item.episode} href={productionPath(project, "breakdown", item.episode)} className="text-sm text-primary">{t("whole_script_item", { episode: item.position ?? index + 1 })}{item.title ? ` · ${item.title}` : ""} · {t(`whole_script_item_${item.status}`)}</Link>)}</div>
      {active && batch.status !== "stopping" && <Button variant="outline" className="self-start" disabled={busy} onClick={() => void perform(async () => { await API.controlScriptBatch(project, "stop"); setBatch(await API.getScriptBatch(project)); })}>{t("whole_script_stop")}</Button>}
      {["stopped", "failed"].includes(batch.status) && <Button className="self-start" disabled={busy || disabled} onClick={() => void perform(async () => { await API.controlScriptBatch(project, "continue", batch.input_changed ? batch.input_revision : undefined); setBatch(await API.getScriptBatch(project)); })}>{t(batch.input_changed ? "whole_script_accept_continue" : "whole_script_continue")}</Button>}
    </div>}
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
  </section>;
}
