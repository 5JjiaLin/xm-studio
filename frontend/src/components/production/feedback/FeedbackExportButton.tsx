import { useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { Download, Loader2 } from "lucide-react";
import { API } from "@/api";
import type { ProjectSummary } from "@/types/project";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { errMsg } from "@/utils/async";

type ExportProject = Pick<ProjectSummary, "name" | "title">;

export function FeedbackExportButton({ projects, disabled = false }: { projects: ExportProject[]; disabled?: boolean }) {
  const { t } = useTranslation("dashboard");
  const [open, setOpen] = useState(false);
  return <>
    <Button type="button" variant="ghost" disabled={disabled || projects.length === 0} onClick={() => setOpen(true)}>
      <Download aria-hidden data-icon="inline-start" />{t("production_feedback_export_records")}
    </Button>
    {open && <FeedbackExportDialog projects={projects} onClose={() => setOpen(false)} />}
  </>;
}

function FeedbackExportDialog({ projects, onClose }: { projects: ExportProject[]; onClose: () => void }) {
  const { t } = useTranslation("dashboard");
  const id = useId();
  const [selected, setSelected] = useState(projects[0]?.name ?? "");
  const [includeMedia, setIncludeMedia] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const hasSelection = projects.some((project) => project.name === selected);
  const download = async () => {
    if (exporting || !hasSelection) return;
    setExporting(true); setError(null);
    try {
      await API.downloadProductionFeedback(selected, includeMedia);
      onClose();
    } catch (cause) { setError(errMsg(cause)); }
    finally { setExporting(false); }
  };
  return <Dialog open onOpenChange={(open) => { if (!open && !exporting) onClose(); }}>
    <DialogContent showCloseButton={false}>
      <DialogHeader><DialogTitle>{t("production_feedback_export_records")}</DialogTitle></DialogHeader>
      <DialogBody><div className="flex flex-col gap-4">
        <DialogDescription>{t("production_feedback_export_hint")}</DialogDescription>
        <div className="flex min-w-0 flex-col gap-2">
          <Label htmlFor={`${id}-project`}>{t("production_feedback_export_project")}</Label>
          <select id={`${id}-project`} value={selected} disabled={exporting}
            className="h-9 w-full min-w-0 rounded-lg border border-input bg-background px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
            onChange={(event) => { setSelected(event.target.value); setError(null); }}>
            {projects.map((project) => <option key={project.name} value={project.name}>{project.title || t("management_untitled_project")}</option>)}
          </select>
        </div>
        <div className="flex items-center gap-2">
          <Checkbox id={`${id}-media`} checked={includeMedia} disabled={exporting} onCheckedChange={setIncludeMedia} />
          <Label htmlFor={`${id}-media`}>{t("production_feedback_include_media")}</Label>
        </div>
        {error && <p role="alert" className="text-sm wrap-break-word text-destructive">{error}</p>}
      </div></DialogBody>
      <DialogFooter>
        <Button type="button" variant="outline" disabled={exporting} onClick={onClose}>{t("common:close")}</Button>
        <Button type="button" disabled={exporting || !hasSelection} onClick={() => void download()}>
          {exporting ? <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" /> : <Download aria-hidden data-icon="inline-start" />}
          {t(exporting ? "production_feedback_exporting" : "production_feedback_download")}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>;
}
