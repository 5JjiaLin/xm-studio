import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Loader2 } from "lucide-react";
import { API } from "@/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useHasUnsavedChanges } from "@/components/shared/edit-unit/LeaveGuard";
import { errMsg } from "@/utils/async";
import type { JianyingDelivery, JianyingLocalSettings, JianyingSettingsResponse } from "@/types/jianying-delivery";

export function JianyingDeliveryCard({ project, episode, busy, revision }: { project: string; episode: number; busy: boolean; revision: string }) {
  const { t } = useTranslation("dashboard");
  const [settings, setSettings] = useState<JianyingSettingsResponse | null>(null);
  const [form, setForm] = useState<JianyingLocalSettings | null>(null);
  const [delivery, setDelivery] = useState<JianyingDelivery | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const unsaved = useHasUnsavedChanges();
  const refresh = useCallback(async () => {
    const result = await API.getJianyingDelivery(project, episode);
    setDelivery(result.delivery); setLoaded(true);
  }, [project, episode]);
  useEffect(() => {
    let active = true;
    void API.getJianyingSettings().then((value) => { if (active) { setSettings(value); setForm(value.settings); } }).catch((e) => { if (active) setError(errMsg(e)); });
    return () => { active = false; };
  }, []);
  useEffect(() => {
    let active = true;
    void API.getJianyingDelivery(project, episode).then((result) => { if (active) { setDelivery(result.delivery); setLoaded(true); } }).catch((e) => { if (active) setError(errMsg(e)); });
    return () => { active = false; };
  }, [project, episode, revision]);
  useEffect(() => {
    if (delivery?.state !== "preparing") return;
    const timer = window.setInterval(() => { void refresh().catch((e) => setError(errMsg(e))); }, 2500);
    return () => window.clearInterval(timer);
  }, [delivery?.state, refresh]);
  const run = async (action: () => Promise<void>) => {
    setWorking(true); setError(null);
    try { await action(); } catch (e) { setError(errMsg(e)); } finally { setWorking(false); }
  };
  const submit = (newVersion = false) => run(async () => {
    if (unsaved()) throw new Error(t("jy_save_first"));
    const result = await API.submitJianyingDelivery(project, episode, { local: settings?.local ?? false, new_version: newVersion, previous_id: delivery?.delivery_id });
    setDelivery(result.delivery);
  });
  const download = () => run(async () => {
    const blob = await API.downloadJianyingDelivery(project, episode);
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a"); link.href = url; link.download = `${delivery?.folder ?? "jianying"}.zip`; link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  const pending = delivery?.state === "preparing";
  const delivered = delivery?.state === "delivered" || delivery?.state === "package_ready";
  const configured = settings && (!settings.local || settings.settings.enabled && settings.compatibility.ready);
  const disabled = busy || working || pending || !loaded || !configured;
  return <section aria-label={t("jy_title")} className="flex flex-col gap-3 rounded-lg border border-border bg-card p-4">
    <h2 className="font-medium">{t("jy_title")}</h2>
    <p className="text-sm text-muted-foreground">{t("jy_hint")}</p>
    <p role="status" className="flex items-center gap-2 text-sm">{pending && <Loader2 aria-hidden className="size-4 animate-spin" />}{t(delivery ? `jy_state_${delivery.state}` : "jy_state_none")}</p>
    {delivery && <p className="break-all text-sm text-muted-foreground">{delivery.folder} · {t("jy_revision", { revision: delivery.revision })}</p>}
    {delivery?.stale && <p className="text-sm">{t("jy_stale")}</p>}
    {delivery?.error && <p role="alert" className="text-sm text-destructive">{delivery.error}</p>}
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    <div className="flex flex-wrap gap-2">
      {!delivered && delivery?.state !== "removed" && <Button disabled={!!disabled} onClick={() => void submit()}>{t(delivery ? "jy_continue" : settings?.local === false ? "jy_prepare_package" : "jy_create")}</Button>}
      {(delivered || delivery?.state === "removed" || delivery?.state === "failed" || delivery?.stale) && <Button variant="outline" disabled={!!disabled} onClick={() => void submit(true)}>{t("jy_new_version")}</Button>}
      {delivery?.state === "delivered" && settings?.local && <><Button variant="outline" disabled={working} onClick={() => void run(() => API.openJianyingDelivery(project, episode, "app"))}>{t("jy_open")}</Button><Button variant="ghost" disabled={working} onClick={() => void run(() => API.openJianyingDelivery(project, episode, "folder"))}>{t("jy_folder")}</Button></>}
      {delivered && <Button variant="ghost" disabled={working} onClick={() => void download()}>{t("jy_download")}</Button>}
      <Button variant="ghost" disabled={working} onClick={() => void run(refresh)}>{t("jy_refresh")}</Button>
    </div>
    {settings?.local === false && <p className="text-sm text-muted-foreground">{t("jy_remote")}</p>}
    {settings?.local && form && <details><summary className="cursor-pointer text-sm text-muted-foreground">{t("jy_settings")}</summary><div className="mt-3 flex flex-col gap-3">
      <p className="text-sm">{settings.compatibility.system} {settings.compatibility.architecture} · {settings.compatibility.version || t("jy_unverified")}</p>
      {settings.compatibility.verification === "unverified" && <p className="text-sm text-muted-foreground">{t("jy_unverified")}</p>}
      <Label htmlFor="jy-app">{t("jy_app_path")}</Label><Input id="jy-app" value={form.app_path} onChange={(e) => setForm({ ...form, app_path: e.target.value })} />
      <Label htmlFor="jy-root">{t("jy_draft_root")}</Label><Input id="jy-root" value={form.draft_root} onChange={(e) => setForm({ ...form, draft_root: e.target.value })} />
      <div className="flex flex-wrap gap-2"><Button variant="outline" disabled={working} onClick={() => void run(async () => { const result = await API.saveJianyingSettings({ ...form, enabled: true }); setSettings(result); setForm(result.settings); })}>{t("jy_enable")}</Button>{settings.settings.enabled && <Button variant="ghost" disabled={working} onClick={() => void run(async () => { const result = await API.saveJianyingSettings({ ...form, enabled: false }); setSettings(result); setForm(result.settings); })}>{t("jy_disable")}</Button>}</div>
    </div></details>}
    {settings?.local && !settings.settings.enabled && <p className="text-sm text-muted-foreground">{t("jy_setup_hint")}</p>}
  </section>;
}
