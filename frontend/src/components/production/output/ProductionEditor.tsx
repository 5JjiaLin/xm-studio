import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import { useTranslation } from "react-i18next";
import { API } from "@/api";
import type { ProductionEdit, ProductionEditClip, ProductionSubtitle } from "@/types/production";
import type { ReferenceVideoUnit } from "@/types/reference-video";
import { useEditUnit } from "@/components/shared/edit-unit/useEditUnit";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { errMsg } from "@/utils/async";

const seconds = (us: number) => Math.round(us / 1000) / 1000;
const us = (seconds: string) => Math.round(Number(seconds) * 1e6);
const included = (clip: ProductionEditClip) => clip.included !== false;

export function ProductionEditor({ project, episode, units, busy, onChanged, onRender }: { project: string; episode: number; units: ReferenceVideoUnit[]; busy: boolean; onChanged: () => void; onRender: () => Promise<void> }) {
  const { t } = useTranslation("dashboard");
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<ProductionEdit | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    void API.getProductionEdit(project, episode, { signal: controller.signal }).then(setData).catch((e) => { if (!controller.signal.aborted) setError(errMsg(e)); });
    return () => controller.abort();
  }, [project, episode, open]);
  return <section className="flex flex-col gap-4">
    <Button variant="outline" className="self-start" disabled={busy} onClick={() => setOpen(true)}>{t("production_edit_output")}</Button>
    {open && !data && !error && <p role="status">{t("common:loading")}</p>}
    {open && data && <Editor key={`${project}/${episode}`} project={project} episode={episode} data={data} units={units} busy={busy} onChanged={onChanged} onRender={onRender} />}
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
  </section>;
}

function Editor({ project, episode, data, units, busy, onChanged, onRender }: { project: string; episode: number; data: ProductionEdit; units: ReferenceVideoUnit[]; busy: boolean; onChanged: () => void; onRender: () => Promise<void> }) {
  const { t } = useTranslation("dashboard");
  const [metadata, setMetadata] = useState(data);
  const [selectedId, setSelectedId] = useState(data.clips.find(included)?.id || data.clips[0]?.id || "");
  const [timeUs, setTimeUs] = useState(0);
  const [playerHeight, setPlayerHeight] = useState(400);
  const [rendering, setRendering] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const player = useRef<HTMLVideoElement>(null);
  const [continuous, setContinuous] = useState(false);
  const save = useCallback(async (clips: ProductionEditClip[], saved: ProductionEditClip[]) => {
    if (busy) throw new Error(t("production_running"));
    const operations: Record<string, unknown>[] = [];
    const previous = new Map(saved.map((clip) => [clip.id, clip]));
    const order = saved.map((clip) => clip.id);
    for (const [index, clip] of clips.entries()) {
      const old = previous.get(clip.id)!;
      if (order[index] !== clip.id) {
        operations.push({ op: "move", clip: clip.id, after: index ? clips[index - 1].id : null });
        order.splice(order.indexOf(clip.id), 1); order.splice(index, 0, clip.id);
      }
      if (included(old) !== included(clip)) operations.push({ op: "set_included", clip: clip.id, included: included(clip) });
      if (old.source_volume !== clip.source_volume) operations.push({ op: "set_volume", clip: clip.id, volume: clip.source_volume });
      if (JSON.stringify(old.trim) !== JSON.stringify(clip.trim)) operations.push({ op: "set_trim", clip: clip.id, trim: clip.trim ? { source_in: seconds(clip.trim.in_us), source_out: seconds(clip.trim.out_us) } : null });
      if (JSON.stringify(old.subtitles) !== JSON.stringify(clip.subtitles)) operations.push({ op: "set_subtitles", clip: clip.id, subtitles: clip.subtitles ?? null });
    }
    if (!operations.length) return clips;
    const result = await API.saveProductionEdit(project, episode, metadata.timeline_id, metadata.revision, operations);
    setMetadata(result); onChanged(); return result.clips;
  }, [busy, episode, metadata.revision, metadata.timeline_id, onChanged, project, t]);
  const editor = useEditUnit({ source: data.clips, save });
  const locked = busy || rendering || editor.status === "saving";
  const clip = editor.value.find((item) => item.id === selectedId);
  const media = clip ? metadata.media[clip.unit_id] : null;
  const source = clip ? units.find((unit) => unit.unit_id === clip.unit_id)?.generated_assets?.video_clip : null;
  const low = clip?.trim?.in_us ?? 0;
  const high = clip?.trim?.out_us ?? media?.duration_us ?? 0;
  const duration = editor.value.filter(included).reduce((total, item) => total + Math.max(0, (item.trim?.out_us ?? metadata.media[item.unit_id]?.duration_us ?? 0) - (item.trim?.in_us ?? 0)) + item.hold_us, 0);
  const generatedCues: ProductionSubtitle[] = clip && media?.version && metadata.subtitle_drafts[clip.unit_id] ? [{ id: `auto-${clip.id}`, text: metadata.subtitle_drafts[clip.unit_id], start_us: low, end_us: high, basis_version: media.version, font_size: 48, color: "#FFFFFF", position: "bottom" }] : [];
  const cues = clip?.subtitles ?? generatedCues;
  const visibleCues = cues.filter((cue) => cue.start_us <= timeUs && timeUs < cue.end_us);
  const changeClip = (change: Partial<ProductionEditClip>) => editor.setValue((items) => items.map((item) => item.id === selectedId ? { ...item, ...change } : item));
  const updateCue = (id: string, change: Partial<ProductionSubtitle>) => changeClip({ subtitles: cues.map((cue) => cue.id === id ? { ...cue, ...change } : cue) });
  const invalid = editor.value.filter(included).flatMap((item) => {
    const m = metadata.media[item.unit_id];
    const min = item.trim?.in_us ?? 0, max = item.trim?.out_us ?? m?.duration_us ?? 0;
    const result: string[] = [];
    if (!Number.isFinite(item.source_volume) || item.source_volume < 0 || item.source_volume > 1) result.push(`${item.id}: ${t("production_edit_volume")}`);
    if (item.trim && (item.trim.basis_version !== m?.version || max > (m?.duration_us ?? 0) || min >= max)) result.push(`${item.id}: ${t("production_edit_trim_invalid")}`);
    if (item.subtitles?.some((cue) => cue.basis_version !== m?.version || cue.start_us < min || cue.end_us > max || cue.end_us <= cue.start_us || !cue.text.trim())) result.push(`${item.id}: ${t("production_edit_subtitle_invalid")}`);
    return result;
  });
  const playNext = () => {
    if (!continuous || !clip) return;
    const current = editor.value.findIndex((item) => item.id === clip.id);
    const next = editor.value.slice(current + 1).find(included);
    if (next) { setSelectedId(next.id); setTimeUs(next.trim?.in_us ?? 0); } else { setContinuous(false); player.current?.pause(); }
  };
  useEffect(() => { const video = player.current; if (video && clip) video.volume = Math.max(0, Math.min(1, clip.source_volume)); }, [clip]);
  const render = async () => {
    if (locked || invalid.length) return;
    setRendering(true); setError(null);
    try { await editor.saveAndGenerate(onRender); }
    catch (e) { setError(errMsg(e)); } finally { setRendering(false); }
  };
  return <div className="flex flex-col gap-4 rounded-lg border border-border p-4">
    <h2 className="font-medium">{t("production_edit_output")}</h2><p className="text-sm text-muted-foreground">{t("production_edit_hint")}</p>
    <p className="text-sm">{t("production_edit_duration", { seconds: seconds(duration), revision: metadata.revision })}</p>
    {source && clip && <div className="relative mx-auto w-full max-w-180 overflow-hidden rounded-md bg-black">
      <video key={`${clip.id}/${source}`} ref={player} controls className="max-h-[55vh] w-full" src={API.getFileUrl(project, source, media?.version ?? null)} onLoadedMetadata={() => { if (player.current) { setPlayerHeight(player.current.clientHeight); player.current.currentTime = seconds(low); player.current.volume = clip.source_volume; if (continuous) void player.current.play().catch((e) => setError(errMsg(e))); } }} onTimeUpdate={(e) => { const current = e.currentTarget.currentTime * 1e6; setTimeUs(current); if (current >= high) { e.currentTarget.pause(); playNext(); } }} onEnded={playNext}><track kind="captions" /></video>
      {visibleCues.map((cue) => <div key={cue.id} className="pointer-events-none absolute inset-x-[5%] whitespace-pre-wrap text-center font-bold text-shadow-sm text-shadow-black text-(length:--caption-size) text-(--caption-color) translate-y-(--caption-shift) top-(--caption-top) bottom-(--caption-bottom)" style={{ "--caption-shift": cue.position === "center" ? "-50%" : "0%", "--caption-color": cue.color, "--caption-size": `${Math.max(12, cue.font_size * playerHeight / 1080)}px`, "--caption-top": cue.position === "top" ? "8%" : cue.position === "center" ? "45%" : "auto", "--caption-bottom": cue.position === "bottom" ? "12%" : "auto" } as CSSProperties}>{cue.text}</div>)}
    </div>}
    <Button type="button" variant="outline" className="self-start" disabled={!source} onClick={() => { setContinuous(true); if (player.current) void player.current.play().catch((e) => setError(errMsg(e))); }}>{t("production_edit_play")}</Button>
    <h3 className="font-medium">{t("production_edit_clips")}</h3>
    <ol className="flex flex-col gap-2">{editor.value.map((item, index) => <li key={item.id} className="flex flex-wrap items-center gap-2 rounded-md border border-border p-2">
      <Button type="button" variant={item.id === selectedId ? "default" : "ghost"} onClick={() => { setSelectedId(item.id); setContinuous(false); }}>{item.unit_id}</Button><span className="text-xs text-muted-foreground">{seconds(Math.max(0, (item.trim?.out_us ?? metadata.media[item.unit_id]?.duration_us ?? 0) - (item.trim?.in_us ?? 0)))}s</span>
      {([-1, 1] as const).map((direction) => <Button key={direction} type="button" variant="outline" disabled={locked || index + direction < 0 || index + direction >= editor.value.length} onClick={() => editor.setValue((items) => { const next = [...items]; [next[index], next[index + direction]] = [next[index + direction], next[index]]; return next; })}>{t(direction === -1 ? "production_edit_up" : "production_edit_down")}</Button>)}
      <Button type="button" variant="outline" disabled={locked} onClick={() => editor.setValue((items) => items.map((c) => c.id === item.id ? { ...c, included: !included(c) } : c))}>{t(included(item) ? "production_edit_exclude" : "production_edit_include")}</Button>
    </li>)}</ol>
    {clip && <><div className="grid grid-cols-2 gap-3">
      <div className="flex flex-col gap-2"><Label htmlFor="clip-in">{t("production_edit_in")}</Label><Input id="clip-in" type="number" step="0.1" min={0} disabled={locked} value={seconds(low)} onChange={(e) => changeClip({ trim: { in_us: us(e.target.value), out_us: high, basis_version: media?.version || 1 } })} /></div>
      <div className="flex flex-col gap-2"><Label htmlFor="clip-out">{t("production_edit_out")}</Label><Input id="clip-out" type="number" step="0.1" min={0.1} max={seconds(media?.duration_us ?? 0)} disabled={locked} value={seconds(high)} onChange={(e) => changeClip({ trim: { in_us: low, out_us: us(e.target.value), basis_version: media?.version || 1 } })} /></div>
      <div className="flex flex-col gap-2"><Label htmlFor="clip-volume">{t("production_edit_volume")}</Label><Input id="clip-volume" type="number" min={0} max={1} step="0.1" value={clip.source_volume} disabled={locked} onChange={(e) => changeClip({ source_volume: Number(e.target.value) })} /></div>
      <div className="flex flex-wrap items-end gap-2"><Button type="button" variant="outline" disabled={locked} onClick={() => changeClip({ source_volume: clip.source_volume ? 0 : 1 })}>{t(clip.source_volume ? "production_edit_mute" : "production_edit_unmute")}</Button><Button type="button" variant="ghost" disabled={locked} onClick={() => changeClip({ trim: null })}>{t("production_edit_full_clip")}</Button></div>
    </div>
    <h3 className="font-medium">{t("production_edit_subtitles")}</h3><p className="text-xs text-muted-foreground">{t("production_edit_subtitle_hint")}</p>
    {cues.map((cue, i) => <div key={cue.id} className="flex flex-col gap-2 rounded-md border border-border p-3"><Label htmlFor={`cue-${cue.id}`}>{t("production_edit_caption_number", { number: i + 1 })}</Label><Textarea id={`cue-${cue.id}`} disabled={locked} value={cue.text} onChange={(e) => updateCue(cue.id, { text: e.target.value })} />
      <div className="grid grid-cols-2 gap-3"><Label className="flex-col items-stretch">{t("production_edit_start")}<Input aria-label={`${t("production_edit_start")} ${i + 1}`} type="number" min={0} step="0.1" value={seconds(cue.start_us)} disabled={locked} onChange={(e) => updateCue(cue.id, { start_us: us(e.target.value), basis_version: media?.version || 1 })} /></Label><Label className="flex-col items-stretch">{t("production_edit_end")}<Input aria-label={`${t("production_edit_end")} ${i + 1}`} type="number" min={0.1} step="0.1" value={seconds(cue.end_us)} disabled={locked} onChange={(e) => updateCue(cue.id, { end_us: us(e.target.value), basis_version: media?.version || 1 })} /></Label>
      <Label className="flex-col items-stretch">{t("production_edit_font")}<Input aria-label={`${t("production_edit_font")} ${i + 1}`} type="number" min={12} max={160} value={cue.font_size} disabled={locked} onChange={(e) => updateCue(cue.id, { font_size: Number(e.target.value) })} /></Label><Label className="flex-col items-stretch">{t("production_edit_color")}<Input aria-label={`${t("production_edit_color")} ${i + 1}`} type="color" value={cue.color} disabled={locked} onChange={(e) => updateCue(cue.id, { color: e.target.value })} /></Label>
      <Label className="flex-col items-stretch">{t("production_edit_position")}<select aria-label={`${t("production_edit_position")} ${i + 1}`} className="h-9 w-full rounded-md border border-input bg-background px-2 text-sm" disabled={locked} value={cue.position} onChange={(e) => updateCue(cue.id, { position: e.target.value as ProductionSubtitle["position"] })}>{(["top", "center", "bottom"] as const).map((position) => <option key={position} value={position}>{t(`production_edit_${position}`)}</option>)}</select></Label></div>
      <div className="flex flex-wrap gap-2"><Button type="button" variant="outline" disabled={locked} onClick={() => changeClip({ subtitles: cues.filter((item) => item.id !== cue.id) })}>{t("production_edit_delete_caption")}</Button><Button type="button" variant="ghost" disabled={locked} onClick={() => updateCue(cue.id, { start_us: Math.max(low, cue.start_us), end_us: Math.min(high, cue.end_us), basis_version: media?.version || 1 })}>{t("production_edit_fit_caption")}</Button></div>
    </div>)}
    <Button type="button" variant="outline" className="self-start" disabled={locked || !media?.version || high <= low} onClick={() => changeClip({ subtitles: [...cues, { id: crypto.randomUUID(), text: t("production_edit_new_caption"), start_us: low, end_us: high, basis_version: media?.version || 1, font_size: 48, color: "#FFFFFF", position: "bottom" }] })}>{t("production_edit_add_caption")}</Button>
    </>}
    {!!invalid.length && <div role="alert" className="text-sm text-destructive">{invalid.map((issue) => <p key={issue}>{issue}</p>)}</div>}
    {(editor.error || error) && <p role="alert" className="text-sm text-destructive">{editor.error || error}</p>}
    <div className="flex flex-wrap gap-2"><Button type="button" variant="outline" disabled={locked || !editor.dirty} onClick={() => void editor.save()}>{t("production_edit_save")}</Button><Button type="button" disabled={locked || !!invalid.length || !editor.value.some(included)} onClick={() => void render()}>{t("production_edit_export")}</Button></div>
  </div>;
}
