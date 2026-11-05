import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "@/api";
import { EditableAssetName } from "@/components/canvas/lorebook/EditableAssetName";
import { isAssetBusy, useAssetBusyNames } from "@/components/canvas/lorebook/assetBusyGuard";
import { useTrackWrite } from "@/components/canvas/lorebook/useAssetWrites";
import { useEditUnit } from "@/components/shared/edit-unit/useEditUnit";
import { Button } from "@/components/ui/button";
import { AssetPromptEditor } from "../assets/AssetPromptEditor";

export function BreakdownExistingAsset({ project, kind, name, description, generationPrompt, disabled, tableDirty, onDirty, onChanged, onRemove }: {
  project: string; kind: "character" | "scene" | "prop"; name: string; description: string; generationPrompt: string;
  disabled: boolean; tableDirty: boolean; onDirty: (key: string, dirty: boolean) => void; onChanged: () => void; onRemove: () => void;
}) {
  const { t } = useTranslation("dashboard");
  const [open, setOpen] = useState(false);
  const track = useTrackWrite();
  const occupied = useAssetBusyNames(kind, project).has(name);
  const save = useCallback(async (value: { generation_prompt: string }) => {
    if (isAssetBusy(kind, project, name)) throw new Error(t("production_running"));
    await track(API.saveProductionAssetPrompt(project, kind, name, value));
    onChanged();
  }, [kind, project, name, track, onChanged, t]);
  const edit = useEditUnit({ source: { generation_prompt: generationPrompt }, save });
  useEffect(() => {
    const key = `${kind}/${name}`;
    onDirty(key, edit.dirty);
    return () => onDirty(key, false);
  }, [kind, name, edit.dirty, onDirty]);
  const locked = disabled || occupied || edit.status === "saving";
  return <article aria-label={name} className="flex flex-col gap-2 rounded-lg border border-border p-3">
    <EditableAssetName projectName={project} assetType={kind} name={name} busy={locked || tableDirty || edit.dirty} onRenamed={onChanged} renderTitle={(hidden) => <h4 className={hidden ? "sr-only" : "font-medium"}>{name}</h4>} />
    {(tableDirty || edit.dirty) && <p className="text-xs text-muted-foreground">{t("production_asset_rename_save_first")}</p>}
    <details onToggle={(event) => setOpen(event.currentTarget.open)}><summary className="cursor-pointer text-sm">{t("production_edit_prompt")}</summary>
      <div className="mt-3 flex flex-col gap-2">
        {open && <AssetPromptEditor project={project} kind={kind} name={name} description={description} prompt={edit.value.generation_prompt} disabled={locked} onChange={(prompt) => edit.setValue((value) => ({ ...value, generation_prompt: prompt }))} />}
        <Button type="button" variant="outline" disabled={locked || !edit.dirty} onClick={() => void edit.save()}>{t("common:save")}</Button>
      </div>
    </details>
    {edit.error && <p role="alert" className="text-sm text-destructive">{edit.error}</p>}
    <Button type="button" variant="outline" className="self-start" disabled={locked || edit.dirty} onClick={onRemove}>{t("production_asset_remove")}</Button>
  </article>;
}
