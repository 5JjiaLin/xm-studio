import { useRef, useState, type CSSProperties } from "react";
import { useTranslation } from "react-i18next";
import { API } from "@/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { errMsg } from "@/utils/async";
import { isResourceBusy, useTasksStore, selectActiveResourceIds } from "@/stores/tasks-store";
import { useProjectsStore } from "@/stores/projects-store";
import { CHARACTER_ANGLES, cropPixels, type CropRect } from "./character-angles";

/** Crops are author-approved independent images, saved through the existing derivative upload/version path. */
export function CharacterAngleCropper({ project, name, path, disabled, onBusy, onChanged }: { project: string; name: string; path: string; disabled: boolean; onBusy: (busy: boolean) => void; onChanged: () => void }) {
  const { t } = useTranslation("dashboard");
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState(0);
  const [rect, setRect] = useState<CropRect>({ x: 0, y: 0, width: 25, height: 100 });
  const [dimensions, setDimensions] = useState<[number, number]>([0, 0]);
  const loaded = dimensions[0] > 0;
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const image = useRef<HTMLImageElement>(null);
  const inFlight = useRef(false);
  const fingerprints = useProjectsStore((s) => s.assetFingerprints);
  const occupied = useTasksStore((s) => selectActiveResourceIds(s.tasks, "character_derivative", project, s.optimisticActive).has(`${name}/${CHARACTER_ANGLES[selected].name}`));
  const locked = disabled || occupied;
  const source = API.getFileUrl(project, path, fingerprints[path] ?? null);
  const valid = cropPixels(rect, ...dimensions);
  const save = async () => {
    if (locked || inFlight.current || !image.current || !loaded) return;
    const angle = CHARACTER_ANGLES[selected];
    if (isResourceBusy("character", project, name) || isResourceBusy("character_derivative", project, `${name}/${angle.name}`)) return;
    const pixels = cropPixels(rect, image.current.naturalWidth, image.current.naturalHeight);
    if (!pixels) return;
    inFlight.current = true; onBusy(true); setError(null); setSaved(false);
    try {
      const canvas = document.createElement("canvas");
      canvas.width = pixels[2]; canvas.height = pixels[3];
      const context = canvas.getContext("2d");
      if (!context) throw new Error(t("production_angle_crop_failed"));
      context.drawImage(image.current, ...pixels, 0, 0, pixels[2], pixels[3]);
      const blob = await new Promise<Blob>((resolve, reject) => canvas.toBlob((value) => value ? resolve(value) : reject(new Error(t("production_angle_crop_failed"))), "image/png"));
      // Re-read after cropping: a previous interrupted attempt may already have registered this angle.
      const current = await API.getCharacterDerivativeSheets(project, name);
      if (!current.derivatives[angle.name]) await API.addCharacterDerivative(project, name, angle.name, `同一人物、同一服装的${angle.name.slice(3)}参考图；只改变取景，不改变身份。`);
      const result = await API.uploadCharacterDerivativeSheet(project, name, angle.name, new File([blob], `${angle.name}.png`, { type: "image/png" }), { parent_path: path, parent_fingerprint: fingerprints[path] ?? null, rect: pixels, dimensions });
      useProjectsStore.getState().updateAssetFingerprints(result.asset_fingerprints ?? {});
      setSaved(true); onChanged();
    } catch (cause) { setError(errMsg(cause)); }
    finally { inFlight.current = false; onBusy(false); }
  };
  return <section className="flex flex-col gap-2">
    <Button type="button" variant="outline" disabled={locked} onClick={() => setOpen(!open)}>{t("production_angle_crop_open")}</Button>
    {open && <div className="flex flex-col gap-3">
      <p className="text-xs text-muted-foreground">{t("production_angle_crop_hint")}</p>
      <label className="flex flex-col gap-1 text-sm">{t("production_angle_target")}<select className="h-9 w-full rounded-md border border-input bg-background px-2" disabled={locked} value={selected} onChange={(e) => { const index = Number(e.target.value); setSelected(index); setRect({ x: index * 25, y: 0, width: 25, height: 100 }); setSaved(false); }}>
        {CHARACTER_ANGLES.map((a, i) => <option key={a.name} value={i}>{t(`production_angle_${a.key}`)}</option>)}
      </select></label>
      <div className="relative">
        <img ref={image} src={source} alt={t("production_angle_crop_source")} className="block w-full" onLoad={(e) => setDimensions([e.currentTarget.naturalWidth, e.currentTarget.naturalHeight])} onError={() => { setDimensions([0, 0]); setError(t("production_angle_crop_failed")); }} />
        {valid && <div aria-hidden="true" className="pointer-events-none absolute left-(--crop-left) top-(--crop-top) h-(--crop-height) w-(--crop-width) border-2 border-primary bg-primary/10" style={{ "--crop-left": `${rect.x}%`, "--crop-top": `${rect.y}%`, "--crop-width": `${rect.width}%`, "--crop-height": `${rect.height}%` } as CSSProperties} />}
      </div>
      <div className="grid grid-cols-2 gap-2">{(["x", "y", "width", "height"] as const).map((key) => <label key={key} className="flex flex-col gap-1 text-xs">{t(`production_angle_crop_${key}`)}<Input type="number" min={0} max={100} step={0.1} value={rect[key]} disabled={locked} onChange={(e) => { setRect((old) => ({ ...old, [key]: Number(e.target.value) })); setSaved(false); }} /></label>)}</div>
      {!valid && loaded && <p role="alert" className="text-xs text-destructive">{t("production_angle_crop_invalid")}</p>}
      <Button type="button" variant="outline" disabled={locked || !loaded || !valid} onClick={() => void save()}>{t("production_angle_crop_save")}</Button>
      {saved && <p role="status" className="text-xs">{t("production_angle_crop_saved")}</p>}
      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
    </div>}
  </section>;
}
