import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "@/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useEditUnit } from "@/components/shared/edit-unit/useEditUnit";
import { errMsg } from "@/utils/async";

type Settings = { installation_id: string; tester_id: string; test_batch_id: string; test_case_id: string };
export function FeedbackSettings({ project }: { project?: string }) {
  const { t } = useTranslation("dashboard");
  const [settings, setSettings] = useState<Settings | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    void API.request<Settings>("/production/feedback/settings", { signal: controller.signal }).then((value) => { if (!controller.signal.aborted) setSettings(value); }).catch((cause) => { if (!controller.signal.aborted) setError(errMsg(cause)); });
    return () => controller.abort();
  }, []);
  return <details className="rounded-md border border-border p-3"><summary className="cursor-pointer text-sm">{t("production_feedback_settings")}</summary>{settings && <SettingsForm project={project} settings={settings} onSaved={setSettings} />}{error && <p role="alert">{error}</p>}</details>;
}
function SettingsForm({ project, settings, onSaved }: { project?: string; settings: Settings; onSaved: (value: Settings) => void }) {
  const { t } = useTranslation("dashboard");
  const [includeMedia, setIncludeMedia] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const save = useCallback(async (value: { tester_id: string; test_batch_id: string; test_case_id: string }) => {
    const result = await API.request<Settings>("/production/feedback/settings", { method: "PUT", body: JSON.stringify(value) });
    onSaved(result);
  }, [onSaved]);
  const edit = useEditUnit({ source: { tester_id: settings.tester_id, test_batch_id: settings.test_batch_id, test_case_id: settings.test_case_id }, save });
  const download = async () => {
    if (!project || edit.dirty) return;
    setExporting(true); setError(null);
    try { await API.downloadProductionFeedback(project, includeMedia); }
    catch (cause) { setError(errMsg(cause)); }
    finally { setExporting(false); }
  };
  return <div className="mt-3 flex flex-col gap-3">
    <p className="text-xs text-muted-foreground">{t("production_feedback_hint")}</p>
    {(["tester_id", "test_batch_id", "test_case_id"] as const).map((key) => <div key={key} className="flex flex-col gap-1"><Label htmlFor={`feedback-${key}`}>{t(`production_feedback_${key}`)}</Label><Input id={`feedback-${key}`} value={edit.value[key]} disabled={edit.status === "saving"} onChange={(event) => edit.setValue((old) => ({ ...old, [key]: event.target.value }))} /></div>)}
    <p className="text-xs wrap-break-word">{t("production_feedback_installation")}：{settings.installation_id}</p>
    <div className="flex flex-wrap items-center gap-2"><Button type="button" variant="outline" disabled={!edit.dirty || edit.status === "saving"} onClick={() => void edit.save()}>{t("common:save")}</Button>
      <Label><input type="checkbox" checked={includeMedia} onChange={(event) => setIncludeMedia(event.target.checked)} /> {t("production_feedback_include_media")}</Label>
      <Button type="button" variant="outline" disabled={!project || edit.dirty || exporting} onClick={() => void download()}>{t("production_feedback_export")}</Button>
    </div>
    {(edit.error || error) && <p role="alert" className="text-sm text-destructive">{edit.error || error}</p>}
  </div>;
}
