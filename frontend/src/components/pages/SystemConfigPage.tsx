import { useSearch, useLocation, Link } from "wouter";
import { useTranslation } from "react-i18next";
import { PageShell } from "@/components/shared/page-shell/PageShell";
import { PageHeader } from "@/components/shared/page-shell/PageHeader";
import { buttonVariants } from "@/components/ui/button";
import { ProviderSection } from "./ProviderSection";
import { MediaModelSection } from "./settings/MediaModelSection";
import { EndpointsSection } from "./settings/endpoints/EndpointsSection";
import { PRODUCTION_PATH } from "@/components/production/production-paths";

const SECTIONS = ["default-models", "providers", "endpoints"] as const;
export function SystemConfigPage() {
  const { t } = useTranslation("dashboard");
  const search = useSearch();
  const [, navigate] = useLocation();
  const value = new URLSearchParams(search).get("section");
  const section = SECTIONS.find((s) => s === value) ?? "default-models";
  return <PageShell bottom={<footer className="shrink-0 border-t border-border px-4 py-2 text-xs text-muted-foreground">Copyright © 2026 Pollo3470 and ArcReel contributors · Powered by <a className="underline underline-offset-4" href="https://github.com/ArcReel/ArcReel" target="_blank" rel="noreferrer">ArcReel</a></footer>} tier={section === "default-models" ? "full" : "bleed"} header={<div className="shrink-0">
    <PageHeader title={t("production_models")} back={{ label: t("production_return"), onClick: () => {
      const target = sessionStorage.getItem("xm:production-return");
      navigate(target?.startsWith(PRODUCTION_PATH + "/") ? target : PRODUCTION_PATH);
    } }} />
    <nav aria-label={t("production_models")} className="flex flex-wrap gap-2 border-b border-border px-4 py-2">{SECTIONS.map((s) => <Link key={s} href={`/app/settings?section=${s}`} replace aria-current={s === section ? "page" : undefined} className={buttonVariants({ variant: s === section ? "secondary" : "ghost" })}>{t(s === "default-models" ? "settings_default_models" : s === "providers" ? "providers" : "production_advanced")}</Link>)}</nav>
  </div>}>
    {section === "default-models" ? <div className="flex max-w-240 flex-col gap-8"><MediaModelSection compact /></div> : section === "providers" ? <ProviderSection /> : <EndpointsSection localOnly />}
  </PageShell>;
}
