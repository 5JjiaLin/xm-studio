import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "@/api";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { errMsg } from "@/utils/async";

export function AssetPromptEditor({ project, kind, name, description, prompt, disabled, onChange }: { project: string; kind: "character" | "scene" | "prop"; name: string; description: string; prompt: string; disabled: boolean; onChange: (text: string) => void }) {
  const { t } = useTranslation("dashboard");
  const [preview, setPreview] = useState<{ text: string; aspect_ratio: string; references: string[]; system_message: string | null; affected_episodes?: number[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    if (!name.trim() || !description.trim()) {
      void Promise.resolve().then(() => { if (!controller.signal.aborted) { setPreview(null); setLoading(false); setError(null); } });
      return () => controller.abort();
    }
    void Promise.resolve().then(() => { if (!controller.signal.aborted) { setLoading(true); setError(null); } });
    void API.previewProductionAssetPrompt(project, kind, name, description, "", { signal: controller.signal }).then((value) => { if (!controller.signal.aborted) setPreview(value); }).catch((e) => { if (!controller.signal.aborted) setError(errMsg(e)); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [project, kind, name, description]);
  const text = prompt || preview?.text || "";
  return <div className="flex flex-col gap-2">
    <Label>{t("production_full_image_prompt")}</Label>
    <Textarea aria-label={t("production_full_image_prompt")} className="min-h-48" value={text} disabled={disabled} onChange={(e) => onChange(e.target.value)} />
    {prompt && <p className="text-xs text-muted-foreground">{t("production_prompt_manual")}</p>}
    <div className="flex flex-wrap gap-2"><Button type="button" variant="outline" disabled={!text} onClick={() => { void navigator.clipboard.writeText(text).catch((e) => setError(errMsg(e))); }}>{t("production_prompt_copy")}</Button>{description.trim() && preview && <Button type="button" variant="ghost" disabled={disabled || !prompt} onClick={() => onChange("")}>{t("production_prompt_default")}</Button>}</div>
    {loading && <p role="status">{t("common:loading")}</p>}
    {preview && <p className="text-xs text-muted-foreground">{t("production_prompt_parameters", { aspect: preview.aspect_ratio, count: preview.references.length })}</p>}
    {!!preview?.affected_episodes?.length && <p className="text-xs text-muted-foreground">{t("production_asset_shared_impact", { episodes: preview.affected_episodes.join("、") })}</p>}
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
  </div>;
}
