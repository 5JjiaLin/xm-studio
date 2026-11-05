import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "@/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { errMsg } from "@/utils/async";

type Evidence = { objects: Record<string, { revision: string }>; reviews: Record<string, { revision: string; usable: boolean }> };
export function FeedbackReview({ project, resource, layer, versionKey }: { project: string; resource: string; layer: "text" | "media"; versionKey: string }) {
  const { t } = useTranslation("dashboard");
  const requestKey = `${project}:${resource}:${versionKey}`;
  const [result, setResult] = useState<{ key: string; evidence: Evidence } | null>(null);
  const evidence = result?.key === requestKey ? result.evidence : null;
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    void API.request<Evidence>(`/projects/${encodeURIComponent(project)}/production/feedback/objects`, { signal: controller.signal }).then((value) => { if (!controller.signal.aborted) { setResult({ key: requestKey, evidence: value }); setError(null); } }).catch((cause) => { if (!controller.signal.aborted) setError(errMsg(cause)); });
    return () => controller.abort();
  }, [project, requestKey]);
  const revision = evidence?.objects[resource]?.revision;
  const review = evidence?.reviews[`${resource}:${layer}`];
  const judge = async (usable: boolean) => {
    if (!revision || busy) return;
    setBusy(true); setError(null);
    try {
      await API.request(`/projects/${encodeURIComponent(project)}/production/feedback/review`, { method: "POST", body: JSON.stringify({ resource, revision, usable, layer, reason: reason || null }) });
      setResult((old) => old?.key === requestKey ? ({ ...old, evidence: { ...old.evidence, reviews: { ...old.evidence.reviews, [`${resource}:${layer}`]: { revision, usable } } } }) : old);
    } catch (cause) { setError(errMsg(cause)); }
    finally { setBusy(false); }
  };
  return <div className="flex flex-col gap-2 border-t border-border pt-2">
    <p className="text-xs text-muted-foreground">{t(layer === "media" ? "production_feedback_media_review" : "production_feedback_text_review")}</p>
    <div className="flex flex-wrap gap-2"><Button type="button" size="xs" variant="outline" disabled={busy || !revision} onClick={() => void judge(true)}>{t("production_feedback_usable")}</Button><Button type="button" size="xs" variant="outline" disabled={busy || !revision} onClick={() => void judge(false)}>{t("production_feedback_needs_changes")}</Button></div>
    <Input aria-label={t("production_feedback_reason")} placeholder={t("production_feedback_reason")} value={reason} onChange={(event) => setReason(event.target.value)} />
    {review && review.revision === revision && <p role="status" className="text-xs">{t(review.usable ? "production_feedback_usable" : "production_feedback_needs_changes")}</p>}
    {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
  </div>;
}
