import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import { API, ApiRequestError } from "@/api";
import { CharacterAnglePicker } from "../assets/CharacterAnglePicker";
import { AssetPromptEditor } from "../assets/AssetPromptEditor";
import { BreakdownExistingAsset } from "./BreakdownExistingAsset";
import { AssetWriteProvider, useAssetWrites } from "@/components/canvas/lorebook/useAssetWrites";
import { belongsToAsset, replaceAssetReferences } from "../asset-references";
import { dialogueSpeakers, extractMentions } from "@/utils/reference-mentions";
import type { ProjectData } from "@/types";
import type { ScriptOverwrite, ScriptReviewState } from "@/types";
import type { ReferenceScriptPlanDraft } from "@/types/reference-video";
import { useEditUnit } from "@/components/shared/edit-unit/useEditUnit";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { ScriptOverwriteConfirmDialog } from "@/components/shared/ScriptOverwriteConfirmDialog";

export function BreakdownEditor({ project, episode, projectData, review, busy, onStarted, onChanged }: { project: string; episode: number; projectData: ProjectData; review: ScriptReviewState; busy: boolean; onStarted: () => void; onChanged: () => void }) {
  const { t } = useTranslation("dashboard");
  const { writing, track } = useAssetWrites();
  const [dirtyAssets, setDirtyAssets] = useState<ReadonlySet<string>>(new Set());
  const assetDirty = useCallback((key: string, dirty: boolean) => setDirtyAssets((old) => {
    if (old.has(key) === dirty) return old;
    const next = new Set(old);
    if (dirty) next.add(key); else next.delete(key);
    return next;
  }), []);
  const [error, setError] = useState<string | null>(null);
  const [removeIndex, setRemoveIndex] = useState<number | null>(null);
  const [existingRemoval, setExistingRemoval] = useState<{ name: string; type: "character" | "scene" | "prop" } | null>(null);
  const [adoptName, setAdoptName] = useState<Record<string, string>>({});
  const [adoptUnit, setAdoptUnit] = useState<Record<string, string>>({});
  const [replacement, setReplacement] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [overwrite, setOverwrite] = useState<ScriptOverwrite | null>(null);
  const content = review.content && "units" in review.content ? review.content : null;
  const save = useCallback(async (value: ReferenceScriptPlanDraft | null) => {
    if (!value) return;
    const saved = await API.saveScriptReviewContent(project, episode, value, review.fingerprint);
    return saved.content && "units" in saved.content ? saved.content : value;
  }, [project, episode, review.fingerprint]);
  const unit = useEditUnit({ source: content, save });
  const locked = busy || writing || submitting || unit.status === "saving" || review.status === "confirmed";
  const submit = async (overwriteRevision?: string) => {
    if (locked || review.quarantine) return;
    if (dirtyAssets.size) { setError(t("common:unsaved_changes")); return; }
    setSubmitting(true); setError(null);
    try {
      // Ask before saving edits; cancelling the coverage confirmation changes nothing.
      const current = await API.getScriptReview(project, episode);
      if (!current.fingerprint) throw new Error(t("production_no_breakdown"));
      if (current.status !== "confirmed" && current.script_overwrite && current.script_overwrite.revision !== overwriteRevision) {
        setOverwrite(current.script_overwrite); return;
      }
      await unit.saveAndGenerate(async () => {
        if (current.status !== "confirmed") await API.confirmScriptReview(project, episode, { overwriteRevision });
        setOverwrite(null); onStarted();
      });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      if (cause instanceof ApiRequestError && cause.status === 409) {
        // A concurrent formal-script change requires a new explicit decision.
        setOverwrite(null);
        try { setOverwrite((await API.getScriptReview(project, episode)).script_overwrite); }
        catch (refreshError) { setError(refreshError instanceof Error ? refreshError.message : String(refreshError)); }
      }
    } finally { setSubmitting(false); }
  };
  return <AssetWriteProvider value={track}><div className="flex flex-col gap-4">
    {review.quarantine && <p role="alert" className="text-sm text-destructive">{review.quarantine.violations.map((v) => v.message).join("；")}</p>}
    <p className="text-sm text-muted-foreground">{t("production_asset_cost_notice")}</p>
    <div className="relative overflow-x-auto scroll-fade-x"><table className="w-full table-fixed text-sm"><caption className="sr-only">{t("production_breakdown_table")}</caption>
      <colgroup><col className="w-20" /><col className="w-80" /><col className="w-80" /><col className="w-24" /></colgroup>
      <thead><tr>{["production_shot", "production_content_refs", "production_dialogue_source", "production_duration"].map((k) => <th key={k} scope="col" className="p-2 text-left font-medium">{t(k)}</th>)}</tr></thead>
      <tbody>{unit.value?.units.map((u, i) => <tr key={u.unit_id} className="border-t border-border"><td className="p-2 align-top">{i + 1}</td><td className="p-2"><Textarea aria-label={t("production_content_for", { number: i + 1 })} disabled={locked} value={u.text} onChange={(e) => unit.setValue((old) => old && ({ ...old, units: old.units.map((v, j) => j === i ? { ...v, text: e.target.value } : v) }))} /><CharacterAnglePicker project={project} characters={projectData.characters ?? {}} text={u.text} disabled={locked} onChange={(text) => unit.setValue((old) => old && ({ ...old, units: old.units.map((v, j) => j === i ? { ...v, text } : v) }))} /></td><td className="whitespace-pre-wrap wrap-break-word p-2 align-top">{u.source_text}</td><td className="p-2 align-top"><Input type="number" min={1} aria-label={t("production_duration_for", { number: i + 1 })} disabled={locked} value={u.duration_seconds} onChange={(e) => unit.setValue((old) => old && ({ ...old, units: old.units.map((v, j) => j === i ? { ...v, duration_seconds: Number(e.target.value) } : v) }))} /></td></tr>)}</tbody>
    </table></div>
    <p className="text-sm text-muted-foreground">{t("production_content_pending")}</p>
    <section className="flex flex-col gap-3"><h2 className="font-medium">{t("production_asset_suggestions")}</h2>
      {(["character", "scene", "prop"] as const).map((kind) => {
        const existing = kind === "character" ? projectData.characters : kind === "scene" ? projectData.scenes : projectData.props;
        return <section key={kind} className="flex flex-col gap-3"><h3>{t(kind === "character" ? "characters" : kind === "scene" ? "scenes" : "props")}</h3>
          {Object.entries((existing ?? {}) as Record<string, { description: string; generation_prompt?: string }>).filter(([name]) => unit.value?.units.some((u) => [...extractMentions(u.text), ...dialogueSpeakers(u.text)].some((ref) => belongsToAsset(ref, name))) && !unit.value?.new_assets?.some((a) => a.name === name)).map(([name, asset]) => <BreakdownExistingAsset key={name} project={project} kind={kind} name={name} description={asset.description} generationPrompt={asset.generation_prompt || ""} disabled={locked} tableDirty={unit.dirty} onDirty={assetDirty} onChanged={onChanged} onRemove={() => { setRemoveIndex(null); setExistingRemoval({ name, type: kind }); setReplacement(""); }} />)}
          {!!Object.keys(existing ?? {}).length && <div className="flex flex-wrap gap-2"><select aria-label={`${t("production_asset_adopt")} ${kind}`} className="h-9 max-w-full rounded-md border border-input bg-background px-2 text-sm" disabled={locked} value={adoptName[kind] ?? ""} onChange={(e) => setAdoptName((v) => ({ ...v, [kind]: e.target.value }))}><option value="">{t("production_asset_adopt")}</option>{Object.keys(existing ?? {}).map((name) => <option key={name} value={name}>{name}</option>)}</select><select aria-label={`${t("production_shot")} ${kind}`} className="h-9 rounded-md border border-input bg-background px-2 text-sm" disabled={locked} value={adoptUnit[kind] || unit.value?.units[0]?.unit_id || ""} onChange={(e) => setAdoptUnit((v) => ({ ...v, [kind]: e.target.value }))}>{unit.value?.units.map((u) => <option key={u.unit_id} value={u.unit_id}>{u.unit_id}</option>)}</select><Button type="button" variant="outline" disabled={locked || !adoptName[kind]} onClick={() => unit.setValue((old) => old && ({ ...old, units: old.units.map((u) => u.unit_id === (adoptUnit[kind] || old.units[0]?.unit_id) ? { ...u, text: `${u.text}\n@[${adoptName[kind]}]` } : u) }))}>{t("production_asset_add")}</Button></div>}
          {unit.value?.new_assets?.map((asset, i) => asset.type !== kind ? null : <div key={i} className="flex flex-col gap-2 rounded-lg border border-border p-3">
            <Label htmlFor={`asset-name-${i}`}>{t("production_asset_name")}</Label><Input id={`asset-name-${i}`} value={asset.asset_name || asset.name} disabled={locked} onChange={(e) => unit.setValue((old) => old && ({ ...old, new_assets: old.new_assets?.map((a, j) => j === i ? { ...a, asset_name: e.target.value } : a) }))} />
            <Label htmlFor={`adopt-${i}`}>{t("production_asset_adopt")}</Label><select id={`adopt-${i}`} className="h-9 rounded-md border border-input bg-background px-2 text-sm" disabled={locked} value={asset.decision === "merge" ? asset.target : asset.decision === "skip" ? "__skip" : ""} onChange={(e) => unit.setValue((old) => old && ({ ...old, new_assets: old.new_assets?.map((a, j) => j === i ? { ...a, decision: e.target.value === "__skip" ? "skip" : e.target.value ? "merge" : "register", target: e.target.value === "__skip" ? "" : e.target.value } : a) }))}>
              <option value="">{t("production_asset_new")}</option><option value="__skip">{t("production_asset_text_only")}</option>{Object.keys(existing ?? {}).map((name) => <option key={name} value={name}>{name}</option>)}
            </select>
            {asset.decision === "register" && <AssetPromptEditor project={project} kind={kind} name={asset.asset_name || asset.name} description={asset.description} prompt={asset.generation_prompt || ""} disabled={locked} onChange={(prompt) => unit.setValue((old) => old && ({ ...old, new_assets: old.new_assets?.map((a, j) => j === i ? { ...a, generation_prompt: prompt } : a) }))} />}
            <Button type="button" variant="outline" className="self-start" disabled={locked} onClick={() => { setExistingRemoval(null); setRemoveIndex(i); setReplacement(""); }}>{t("production_asset_remove")}</Button>
          </div>)}
          <Button type="button" variant="outline" className="self-start" disabled={locked} onClick={() => unit.setValue((old) => old && ({ ...old, units: old.units.map((u, i) => i === 0 ? { ...u, text: `${u.text}\n@[${t("production_asset_new")}${(old.new_assets?.length || 0) + 1}]` } : u), new_assets: [...old.new_assets || [], { type: kind, name: `${t("production_asset_new")}${(old.new_assets?.length || 0) + 1}`, decision: "register", reason: "", description: "", aliases: [], target: "", asset_name: "" }] }))}>{t("production_asset_add")}</Button>
        </section>;
      })}
    </section>
    {unit.value && (existingRemoval || (removeIndex !== null && unit.value.new_assets?.[removeIndex])) && (() => {
      const asset = existingRemoval || unit.value.new_assets![removeIndex!];
      const references = unit.value.units.filter((u) => [...extractMentions(u.text), ...dialogueSpeakers(u.text)].some((ref) => belongsToAsset(ref, asset.name)));
      return <section className="flex flex-col gap-3 rounded-lg border border-border p-4"><p>{t("production_asset_remove_hint", { name: asset.name, units: references.map((u) => u.unit_id).join("、") || "—" })}</p>
        <Label htmlFor="replacement-asset">{t("production_asset_replacement")}</Label><select id="replacement-asset" className="h-9 rounded-md border border-input bg-background px-2 text-sm" value={replacement} onChange={(e) => setReplacement(e.target.value)}><option value="">{t("production_asset_text_only")}</option>{[...new Set([...Object.keys(asset.type === "character" ? projectData.characters ?? {} : asset.type === "scene" ? projectData.scenes ?? {} : projectData.props ?? {}), ...(unit.value.new_assets || []).filter((a, i) => i !== removeIndex && a.type === asset.type && a.decision !== "skip").map((a) => a.name)])].filter((name) => name !== asset.name).map((name) => <option key={name} value={name}>{name}</option>)}</select>
        <div className="flex gap-2"><Button type="button" disabled={locked} onClick={() => {
          unit.setValue((old) => old && ({ ...old, units: old.units.map((u) => ({ ...u, text: replaceAssetReferences(u.text, asset.name, replacement) })), new_assets: old.new_assets?.filter((_, i) => i !== removeIndex) })); setRemoveIndex(null); setExistingRemoval(null);
        }}>{t("production_asset_remove")}</Button><Button type="button" variant="ghost" onClick={() => { setRemoveIndex(null); setExistingRemoval(null); }}>{t("common:cancel")}</Button></div>
      </section>;
    })()}
    {unit.error && <p role="alert" className="text-sm text-destructive">{unit.error}</p>}
    {review.status !== "confirmed" && <Button variant="outline" className="self-start" disabled={locked || !unit.dirty} onClick={() => void unit.save()}>{t("common:save")}</Button>}
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    <form id="production-action" onSubmit={(e) => { e.preventDefault(); void submit(); }} />
    {overwrite && <ScriptOverwriteConfirmDialog open overwrite={overwrite} loading={submitting} confirmDisabled={busy || writing || unit.status === "saving" || !!review.quarantine} onConfirm={() => submit(overwrite.revision)} onCancel={() => setOverwrite(null)} />}

  </div></AssetWriteProvider>;
}
