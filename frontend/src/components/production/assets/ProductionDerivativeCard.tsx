import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { API, derivativeResourceId } from "@/api";
import { enqueueCharacterDerivative } from "@/actions/generation";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { useActiveResourceIds } from "@/stores/tasks-store";
import { errMsg } from "@/utils/async";

interface ProductionDerivativeCardProps {
  project: string; owner: string; name: string; description: string; path?: string; ownerPath?: string; busy: boolean; onChanged: () => void;
}

export function ProductionDerivativeCard(props: ProductionDerivativeCardProps) {
  const { project, owner, name, description, ownerPath } = props;
  return <ProductionDerivativeCardContent key={JSON.stringify([project, owner, name, description, ownerPath])} {...props} />;
}

function ProductionDerivativeCardContent({ project, owner, name, description, path, ownerPath, busy, onChanged }: ProductionDerivativeCardProps) {
  const { t } = useTranslation("dashboard");
  const [prompt, setPrompt] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const occupied = useActiveResourceIds("character_derivative", project).has(derivativeResourceId(owner, name));
  useEffect(() => {
    const controller = new AbortController();
    void API.previewAssetPrompt(project, "character", owner, description, { derivativeName: name, signal: controller.signal })
      .then((preview) => { if (!controller.signal.aborted) { setPrompt(preview.text || ""); setError(preview.unavailable || ""); } })
      .catch((cause) => { if (!controller.signal.aborted) setError(errMsg(cause)); });
    return () => controller.abort();
  }, [project, owner, name, description, ownerPath]);
  const generate = async () => {
    if (busy || occupied || submitting || !ownerPath) return;
    setSubmitting(true); setError("");
    try { await enqueueCharacterDerivative(project, owner, name); onChanged(); }
    catch (cause) { setError(errMsg(cause)); }
    finally { setSubmitting(false); }
  };
  return <article aria-label={`${owner}/${name}`} className="overflow-hidden rounded-lg border border-border bg-card">
    {path ? <a href={API.getFileUrl(project, path, null)} target="_blank" rel="noreferrer"><img src={API.getFileUrl(project, path, null)} alt={`${owner}/${name}`} className="aspect-video w-full object-contain" /></a> : <div className="flex aspect-video items-center justify-center bg-muted text-sm text-muted-foreground">{t(occupied || submitting ? "production_image_running" : "production_image_waiting")}</div>}
    <div className="flex flex-col gap-3 p-4"><h3 className="text-sm font-medium">{owner} / {name}</h3>
      <p className="text-sm">{t(occupied || submitting ? "production_image_running" : path ? "production_image_ready" : "production_image_waiting")}</p>
      <label className="text-sm">{t("production_full_image_prompt")}<Textarea className="mt-2 min-h-48" readOnly value={prompt} /></label>
      {!ownerPath && <p className="text-sm text-muted-foreground">{t("production_variant_needs_base")}</p>}
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      <Button variant="outline" disabled={busy || occupied || submitting || !ownerPath} onClick={() => void generate()}>{t(path ? "production_regenerate_paid" : "production_generate_image")}</Button>
    </div>
  </article>;
}
