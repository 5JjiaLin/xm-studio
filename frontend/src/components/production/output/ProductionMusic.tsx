import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "@/api";
import { enqueueMusic, recoverMusic } from "@/actions/generation";
import { useEditUnit } from "@/components/shared/edit-unit/useEditUnit";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import type { BeatReport, MusicDraft, MusicReview, MusicTask, ProductionBgmClip, ProductionEdit } from "@/types/production";
import { errMsg } from "@/utils/async";

const seconds = (value: number) => Math.round(value / 1000) / 1000;
const micros = (value: string) => Math.round(Number(value) * 1e6);

export function ProductionMusic({ project, episode, data, locked, onChanged, onReviewCut, onPendingChange }: {
  project: string; episode: number; data: ProductionEdit; locked: boolean;
  onPendingChange: (value: boolean) => void; onChanged: (value: ProductionEdit) => void; onReviewCut: (timeUs: number) => void;
}) {
  const { t } = useTranslation("dashboard");
  const [working, setWorking] = useState(false);
  const gate = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const [chosen, setChosen] = useState("");
  const [tasks, setTasks] = useState<MusicTask[]>([]);
  const [report, setReport] = useState<BeatReport | null>(null);
  const [undoRevision, setUndoRevision] = useState<number | null>(null);
  const [draft, setDraft] = useState<MusicDraft>({ summary: Object.values(data.subtitle_drafts).filter(Boolean).join("\n").slice(0, 4000), mood: "", target_seconds: Math.max(5, Math.min(1200, Math.round(data.duration))), prompt: "" });
  const [review, setReview] = useState<(MusicReview & { candidate: string; revision: number }) | null>(null);
  const [sentCandidate, setSentCandidate] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [dirtyPlacements, setDirtyPlacements] = useState<Set<string>>(new Set());
  const placementPending = useCallback((id: string, dirty: boolean) => {
    setDirtyPlacements((current) => {
      if (current.has(id) === dirty) return current;
      const next = new Set(current); if (dirty) next.add(id); else next.delete(id); return next;
    });
  }, []);
  useEffect(() => { onPendingChange(working || dirtyPlacements.size > 0); }, [working, dirtyPlacements, onPendingChange]);
  const disabled = locked || working || dirtyPlacements.size > 0;
  const tracks = data.bgm_library ?? [];
  const track = tracks.find((item) => item.id === chosen) ?? tracks[0];
  const activeReport = report?.revision === data.revision ? report : null;
  const activeReview = review?.revision === data.revision ? review : null;
  // Poll reads only. Reopening or refreshing never submits music.
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const value = await API.getProductionMusic(project, episode, { signal: controller.signal });
        if (!controller.signal.aborted) { setTasks(value.items); timer = setTimeout(() => void poll(), 3000); }
      } catch (cause) {
        if (!controller.signal.aborted) { setError(errMsg(cause)); timer = setTimeout(() => void poll(), 3000); }
      }
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [project, episode, refresh]);
  const run = useCallback(async (action: () => Promise<void>) => {
    if (locked || gate.current) return;
    gate.current = true; setWorking(true); setError(null);
    try { await action(); } catch (cause) { setError(errMsg(cause)); }
    finally { gate.current = false; setWorking(false); }
  }, [locked]);
  const accept = (value: ProductionEdit, undo = false) => {
    setReport(null); setReview(null); setUndoRevision(undo ? value.revision : null); onChanged(value);
  };
  const save = async (operations: Record<string, unknown>[]) => accept(await API.saveProductionEdit(project, episode, data.timeline_id, data.revision, operations));
  const apply = (ids: string[]) => run(async () => { if (activeReport) accept(await API.applyProductionBeats(project, episode, activeReport.basis, ids), true); });
  const editDraft = (change: Partial<MusicDraft>) => { setDraft((previous) => ({ ...previous, ...change })); setReview(null); setSentCandidate(null); };
  return <section className="flex flex-col gap-3 rounded-md border border-border p-3" aria-label={t("music_title")}>
    <h3 className="font-medium">{t("music_title")}</h3>
    <p className="text-xs text-muted-foreground">{t("music_hint")}</p>
    {locked && <p role="status" className="text-xs text-muted-foreground">{t("music_save_first")}</p>}
    <div className="flex flex-wrap items-end gap-2">
      <Label className="flex-col items-stretch">{t("music_track")}<select aria-label={t("music_track")} disabled={disabled} value={track?.id ?? ""} className="h-9 rounded-md border border-input bg-background px-2 text-sm" onChange={(e) => setChosen(e.target.value)}><option value="" disabled>{t("music_choose")}</option>{tracks.map((item) => <option key={item.id} value={item.id}>{item.name} · {seconds(item.duration_us)}s</option>)}</select></Label>
      <Button variant="outline" disabled={disabled || !track} onClick={() => void run(async () => { if (track) await save([{ op: "insert_bgm", bgm_id: track.id, start: 0 }]); })}>{t("music_add")}</Button>
      <Label className="flex-col items-stretch">{t("music_upload")}<Input aria-label={t("music_upload")} type="file" accept=".mp3,.wav,.m4a,.aac,.ogg,.flac" disabled={disabled} onChange={(e) => { const file = e.target.files?.[0]; if (file) void run(async () => { await API.uploadBgm(project, file); accept(await API.getProductionEdit(project, episode), false); }); e.target.value = ""; }} /></Label>
      <Button variant="ghost" disabled={disabled} onClick={() => void run(async () => { accept(await API.getProductionEdit(project, episode), false); setRefresh((v) => v + 1); })}>{t("music_refresh")}</Button>
    </div>
    {track && <audio key={track.file} controls preload="none" src={API.getFileUrl(project, track.file, null)}><track kind="captions" /></audio>}
    {(data.bgm ?? []).map((clip) => <MusicPlacement key={`${clip.id}/${data.revision}`} clip={clip} name={tracks.find((item) => item.id === clip.bgm_id)?.name ?? clip.bgm_id} disabled={locked || working || (dirtyPlacements.size > 0 && !dirtyPlacements.has(clip.id))} onPendingChange={placementPending} onSave={async (op) => {
      if (gate.current || locked) throw new Error(t("music_save_first"));
      gate.current = true; setWorking(true);
      try { await save([op]); } finally { gate.current = false; setWorking(false); }
    }} />)}
    <details><summary className="cursor-pointer text-sm font-medium">{t("music_generate")}</summary><div className="mt-3 flex flex-col gap-3">
      <p className="text-xs text-muted-foreground">{t("music_provider_hint")}</p>
      <Label className="flex-col items-stretch">{t("music_summary")}<Textarea aria-label={t("music_summary")} value={draft.summary} maxLength={4000} disabled={working} onChange={(e) => editDraft({ summary: e.target.value })} /></Label>
      <Label className="flex-col items-stretch">{t("music_mood")}<Textarea aria-label={t("music_mood")} value={draft.mood} maxLength={1000} disabled={working} onChange={(e) => editDraft({ mood: e.target.value })} /></Label>
      <Label className="flex-col items-stretch">{t("music_target")}<Input aria-label={t("music_target")} type="number" min={5} max={1200} value={draft.target_seconds} disabled={working} onChange={(e) => editDraft({ target_seconds: Number(e.target.value) })} /></Label>
      <Label className="flex-col items-stretch">{t("music_prompt")}<Textarea aria-label={t("music_prompt")} value={draft.prompt} maxLength={2000} disabled={working} onChange={(e) => editDraft({ prompt: e.target.value })} /></Label>
      <Button variant="outline" disabled={disabled || !draft.mood.trim() || !draft.prompt.trim()} onClick={() => void run(async () => {
        const next = await API.previewProductionMusic(project, episode, draft);
        setReview({ ...next, candidate: crypto.randomUUID(), revision: data.revision }); setSentCandidate(null);
      })}>{t("music_review")}</Button>
      {activeReview && <div className="flex flex-col gap-2 rounded-md border border-border p-3">
        <p className="text-sm">MiniMax · {activeReview.model} · {activeReview.draft.target_seconds}s</p>
        <p className="whitespace-pre-wrap text-sm">{activeReview.draft.summary}</p><p className="whitespace-pre-wrap text-sm">{activeReview.draft.mood}</p><p className="whitespace-pre-wrap text-sm">{activeReview.submitted_prompt}</p>
        {!activeReview.configured && <p role="alert">{t("music_configure")}</p>}
        <Button disabled={disabled || !activeReview.configured || sentCandidate === activeReview.candidate} onClick={() => void run(async () => {
          // Fence before awaiting: a lost response must not offer a second paid submission.
          setSentCandidate(activeReview.candidate);
          await enqueueMusic(project, episode, activeReview, activeReview.candidate);
          setRefresh((v) => v + 1);
        })}>{t(sentCandidate === activeReview.candidate ? "music_submitted" : "music_confirm")}</Button>
      </div>}
      {tasks.map((task) => <div key={task.task_id} className="rounded-md border border-border p-2 text-sm"><p>{task.candidate_id.slice(0, 8)} · {t(`music_task_${task.status}`, { defaultValue: task.status })}</p>
        {task.status === "unknown" && <p>{t("music_unknown")}</p>}
        {task.error && <details><summary>{t("music_error_details")}</summary><p className="break-words">{task.error}</p></details>}
        {task.recoverable && <Button variant="outline" disabled={disabled} onClick={() => void run(async () => { await recoverMusic(project, episode, task.task_id, task.candidate_id); setRefresh((v) => v + 1); })}>{t("music_recover")}</Button>}
      </div>)}
    </div></details>
    <div className="flex flex-wrap gap-2"><Button variant="outline" disabled={disabled || !data.bgm?.length} onClick={() => void run(async () => setReport(await API.analyzeProductionBeats(project, episode)))}>{t("music_analyze")}</Button>
      <Button disabled={disabled || !activeReport?.suggestions.some((item) => item.status === "near")} onClick={() => void apply(activeReport?.suggestions.filter((item) => item.status === "near").map((item) => item.id) ?? [])}>{t("music_apply_safe")}</Button>
      <Button variant="ghost" disabled={disabled || undoRevision !== data.revision} onClick={() => void run(async () => accept(await API.undoProductionMusic(project, episode, data.revision), false))}>{t("music_undo")}</Button>
    </div>
    {activeReport && <>
      {activeReport.tracks.map((item) => <div key={item.clip_id} className="flex flex-col gap-1"><p className="text-xs">{tracks.find((track) => track.id === item.bgm_id)?.name} · {item.bpm ? `${item.bpm} BPM` : t("music_no_bpm")} · {t("music_confidence", { value: Math.round(item.confidence * 100) })}</p><svg role="img" aria-label={t("music_waveform")} viewBox="0 0 1000 80" className="h-20 w-full rounded bg-muted">
        {item.waveform.map((height, index) => {
          const time = index / Math.max(1, item.waveform.length - 1) * item.duration_us;
          const x = (time - item.source_in_us) / Math.max(1, item.end_us - item.start_us) * 1000;
          return x >= 0 && x <= 1000 ? <line key={index} x1={x} x2={x} y1={40 - height * 30} y2={40 + height * 30} stroke="currentColor" opacity=".3" /> : null;
        })}
        {item.beats_us.map((time) => <line key={time} x1={(time - item.start_us) / (item.end_us - item.start_us) * 1000} x2={(time - item.start_us) / (item.end_us - item.start_us) * 1000} y1={0} y2={80} stroke="currentColor" opacity=".5" />)}
        {activeReport.suggestions.filter((cut) => cut.cut_us >= item.start_us && cut.cut_us < item.end_us).map((cut) => <line key={cut.id} x1={(cut.cut_us - item.start_us) / (item.end_us - item.start_us) * 1000} x2={(cut.cut_us - item.start_us) / (item.end_us - item.start_us) * 1000} y1={0} y2={80} stroke="currentColor" strokeWidth={3} />)}
      </svg></div>)}
      <p className="text-xs text-muted-foreground">{t("music_legend")}</p>
      <ol className="flex flex-col gap-2">{activeReport.suggestions.map((item) => <li key={item.id} className="flex flex-wrap items-center gap-2 text-sm"><span>{seconds(item.cut_us)}s → {seconds(item.beat_us)}s · {Math.round(item.offset_us / 1000)}ms · {t(`music_beat_${item.status}`)}</span>{item.reason && <span className="text-muted-foreground">{t(`music_reason_${item.reason}`)}</span>}
        <Button size="sm" variant="outline" disabled={disabled} onClick={() => item.status === "near" ? void apply([item.id]) : onReviewCut(item.cut_us)}>{t(item.status === "near" ? "music_apply" : "music_review_cut")}</Button>
      </li>)}</ol>
      <details><summary className="cursor-pointer text-sm">{t("music_events")}</summary>{activeReport.events.map((event) => <p key={event.clip_id} className="text-xs">{seconds(event.start_us)}～{seconds(event.end_us)}s · {event.unit_id} · {event.text || t("music_silent_event")}</p>)}</details>
    </>}
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
  </section>;
}

function MusicPlacement({ clip, name, disabled, onSave, onPendingChange }: { clip: ProductionBgmClip; name: string; disabled: boolean; onSave: (operation: Record<string, unknown>) => Promise<void>; onPendingChange: (id: string, dirty: boolean) => void }) {
  const { t } = useTranslation("dashboard");
  const edit = useEditUnit({ source: clip, save: async (value) => {
    await onSave({ op: "set_bgm", clip: clip.id, start: seconds(value.start_us), source_in: seconds(value.in_us), source_out: seconds(value.out_us), volume: value.volume, fade_in: seconds(value.fade_in_us), fade_out: seconds(value.fade_out_us) });
    return value;
  } });
  const [removeError, setRemoveError] = useState<string | null>(null);
  const { value, setValue } = edit;
  useEffect(() => { onPendingChange(clip.id, edit.dirty || edit.status === "saving"); return () => onPendingChange(clip.id, false); }, [clip.id, edit.dirty, edit.status, onPendingChange]);
  const fields = ["start_us", "in_us", "out_us", "volume", "fade_in_us", "fade_out_us"] as const;
  const valid = fields.every((field) => Number.isFinite(value[field])) && value.start_us >= 0 && value.in_us >= 0 && value.out_us > value.in_us && value.volume >= 0 && value.volume <= 1 && value.fade_in_us >= 0 && value.fade_out_us >= 0;
  return <div className="@container flex flex-col gap-2 rounded-md border border-border p-2"><p className="text-sm">{name}</p><div className="grid grid-cols-2 gap-2 @sm:grid-cols-3">{fields.map((field) => <Label key={field} className="flex-col items-stretch">{t(`music_field_${field}`)}<Input aria-label={`${t(`music_field_${field}`)} ${clip.id}`} type="number" min={0} step={field === "volume" ? .05 : .1} max={field === "volume" ? 1 : undefined} value={field === "volume" ? value[field] : seconds(value[field])} disabled={disabled} onChange={(e) => setValue({ ...value, [field]: field === "volume" ? Number(e.target.value) : micros(e.target.value) })} /></Label>)}</div><div className="flex gap-2"><Button variant="outline" disabled={disabled || !valid || JSON.stringify(clip) === JSON.stringify(value)} onClick={() => void edit.save()}>{t("music_save_placement")}</Button><Button variant="ghost" disabled={disabled} onClick={() => void onSave({ op: "delete_bgm", clip: clip.id }).catch((cause) => setRemoveError(errMsg(cause)))}>{t("music_remove")}</Button>{edit.dirty && <Button variant="ghost" disabled={disabled} onClick={edit.discard}>{t("common:cancel")}</Button>}</div>{(edit.error || removeError) && <p role="alert" className="text-sm text-destructive">{edit.error || removeError}</p>}</div>;
}
