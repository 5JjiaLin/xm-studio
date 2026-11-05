import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "@/api";
import type { CostSummary, ProductionCost, ProjectCosts } from "@/types/production";
import { Button } from "@/components/ui/button";
import { errMsg } from "@/utils/async";

export function useProductionCosts(project: string, revision: string | number) {
  const [result, setResult] = useState<{ project: string; data: ProjectCosts } | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const c = new AbortController();
    void API.getProductionCosts(project, { signal: c.signal }).then((data) => {
      if (!c.signal.aborted) { setResult({ project, data }); setError(null); }
    }).catch((cause: unknown) => { if (!c.signal.aborted) setError(errMsg(cause)); });
    return () => c.abort();
  }, [project, revision]);
  return { data: result?.project === project ? result.data : null, error };
}

function amounts(values: Record<string, number>): string {
  return Object.entries(values).map(([currency, amount]) => `${currency} ${amount.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 6 })}`).join(" · ");
}

function CostValue({ cost }: { cost: CostSummary }) {
  const { t } = useTranslation("dashboard");
  if (!cost.call_count) return <span>{t("production_cost_empty")}</span>;
  return <span className="wrap-break-word">{cost.known_call_count > 0 ? `${cost.unpriced_count || cost.pending_count ? t("production_cost_subtotal") + " · " : ""}${amounts(cost.amounts)}` : t("production_cost_unknown")}</span>;
}

export function ProductionCosts({ cost, unassigned = false }: { cost: ProductionCost; unassigned?: boolean }) {
  const { t } = useTranslation("dashboard");
  return <section aria-label={t(unassigned ? "production_cost_unassigned" : "production_cost_title")} className="flex flex-col gap-2 rounded-lg border border-border bg-card p-4 text-sm">
    <p className="font-medium">{t(unassigned ? "production_cost_unassigned" : "production_cost_title")}</p>
    <p><CostValue cost={cost} /> <span className="text-muted-foreground">{t("production_cost_includes_rerolls")}</span></p>
    {!!cost.unpriced_count && <p className="text-muted-foreground">{t("production_cost_unpriced", { count: cost.unpriced_count })}</p>}
    {!!cost.pending_count && <p className="text-muted-foreground">{t("production_cost_pending", { count: cost.pending_count })}</p>}
    {unassigned && <p className="text-muted-foreground">{t("production_cost_unassigned_hint")}</p>}
    {cost.call_count > 0 && <details><summary className="cursor-pointer">{t("production_cost_details")}</summary><div className="mt-3 flex flex-col gap-3">
      {(["text", "image", "video", "audio"] as const).map((kind) => {
        const entry = cost.categories[kind];
        return entry && <div key={kind} className="flex flex-col gap-1"><p>{t(`production_cost_${kind}`)} · <CostValue cost={entry} /> · {t("production_cost_calls", { count: entry.call_count })}</p>
          <p className="text-muted-foreground">{t("production_cost_rerolls", { count: entry.reroll_count })}{Object.keys(entry.reroll_amounts).length > 0 ? ` · ${amounts(entry.reroll_amounts)}` : ""}</p></div>;
      })}
      {!!cost.unidentified_target_count && <p className="text-muted-foreground">{t("production_cost_unidentified", { count: cost.unidentified_target_count })}</p>}
    </div></details>}
  </section>;
}

export function EpisodeCost({ project, episode, revision }: { project: string; episode: number; revision: string }) {
  const { t } = useTranslation("dashboard");
  const [refresh, setRefresh] = useState(0);
  const { data, error } = useProductionCosts(project, `${revision}/${refresh}`);
  const cost = data?.episodes.find((item) => item.episode === episode);
  return <div className="flex flex-col gap-2">
    {cost ? <ProductionCosts cost={cost} /> : <p role="status" className="text-sm text-muted-foreground">{t(error ? "production_cost_load_failed" : "common:loading")}</p>}
    {data && data.unassigned.call_count > 0 && <ProductionCosts cost={data.unassigned} unassigned />}
    <Button variant="ghost" className="self-start" onClick={() => setRefresh((v) => v + 1)}>{t("production_cost_refresh")}</Button>
  </div>;
}
