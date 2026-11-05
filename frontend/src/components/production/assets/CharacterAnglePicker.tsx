import { useTranslation } from "react-i18next";
import { API } from "@/api";
import type { Character } from "@/types";
import { useProjectsStore } from "@/stores/projects-store";
import { CHARACTER_ANGLES, characterAngleMentions, replaceCharacterAngle } from "./character-angles";

export function CharacterAnglePicker({ project, characters, text, disabled, onChange }: { project: string; characters: Record<string, Character>; text: string; disabled: boolean; onChange: (text: string) => void }) {
  const { t } = useTranslation("dashboard");
  const fingerprints = useProjectsStore((s) => s.assetFingerprints);
  const mentions = characterAngleMentions(text, characters);
  if (!mentions.length) return null;
  return <div className="flex flex-col gap-2">
    <p className="text-xs text-muted-foreground">{t("production_angle_picker_hint")}</p>
    {mentions.map(({ reference, owner }) => {
      const character = characters[owner];
      const angle = reference.split("/")[1];
      const path = angle ? character.derivatives?.[angle]?.character_sheet : character.character_sheet;
      const available = CHARACTER_ANGLES.filter((a) => character.derivatives?.[a.name]?.character_sheet);
      return <div key={reference} className="flex flex-wrap items-center gap-2">
        <label className="flex min-w-0 flex-1 flex-col gap-1 text-xs">
          {t("production_angle_for", { name: owner })}
          <select className="h-9 w-full min-w-0 rounded-md border border-input bg-background px-2 text-sm" aria-label={t("production_angle_for", { name: owner })} value={angle || ""} disabled={disabled} onChange={(e) => onChange(replaceCharacterAngle(text, reference, e.target.value ? `${owner}/${e.target.value}` : owner))}>
            <option value="">{t("production_angle_sheet")}</option>
            {angle && !available.some((a) => a.name === angle) && <option value={angle}>{angle} · {t("production_angle_missing")}</option>}
            {available.map((a) => <option key={a.name} value={a.name}>{t(`production_angle_${a.key}`)}</option>)}
          </select>
        </label>
        {path && <a href={API.getFileUrl(project, path, fingerprints[path] ?? null)} target="_blank" rel="noreferrer"><img src={API.getFileUrl(project, path, fingerprints[path] ?? null)} alt={t("production_angle_reference", { name: owner })} className="h-16 w-16 object-contain" /></a>}
      </div>;
    })}
  </div>;
}
