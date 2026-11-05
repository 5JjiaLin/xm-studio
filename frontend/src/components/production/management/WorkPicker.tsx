import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "wouter";
import { API } from "@/api";
import type { ProjectSummary } from "@/types/project";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogBody } from "@/components/ui/dialog";
import { buttonVariants } from "@/components/ui/button";
import { projectPath } from "../production-paths";
import { errMsg } from "@/utils/async";

export function WorkPicker({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation("dashboard");
  const [works, setWorks] = useState<ProjectSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const c = new AbortController();
    void API.listProjects({ signal: c.signal }).then((r) => { if (!c.signal.aborted) setWorks(r.projects.filter((p) => !p.production_management?.archived)); })
      .catch((e) => { if (!c.signal.aborted) setError(errMsg(e)); }).finally(() => { if (!c.signal.aborted) setLoading(false); });
    return () => c.abort();
  }, []);
  return <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}><DialogContent>
    <DialogHeader><DialogTitle>{t("production_open")}</DialogTitle></DialogHeader>
    <DialogBody><div className="flex flex-col gap-2">
      {error && <p role="alert">{error}</p>}
      {works.map((w) => <Link key={w.name} href={projectPath(w.name)} onClick={onClose}
        className={buttonVariants({ variant: "outline", className: "h-auto justify-start whitespace-normal py-3 text-left" })}>{w.title}</Link>)}
      {loading && <p className="text-sm text-muted-foreground">{t("common:loading")}</p>}
      {!loading && !error && works.length === 0 && <p className="text-sm text-muted-foreground">{t("production_no_works")}</p>}
    </div></DialogBody>
  </DialogContent></Dialog>;
}
