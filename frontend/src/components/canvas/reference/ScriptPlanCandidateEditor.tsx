import { useCallback } from "react";
import { useTranslation } from "react-i18next";
import { API } from "@/api";
import type { ScriptReviewQuarantine } from "@/types";
import { useEditUnit } from "@/components/shared/edit-unit/useEditUnit";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

/** 结构损坏的隔离候选仍可人工修正；保存只走既有草稿 CAS 和全量重判。 */
export function ScriptPlanCandidateEditor({ project, episode, draft, onChanged }: {
  project: string; episode: number; draft: ScriptReviewQuarantine; onChanged: () => void;
}) {
  const { t } = useTranslation("common");
  const source = { text: JSON.stringify(draft.content, null, 2), revision: draft.revision };
  const save = useCallback(async (value: typeof source, baseline: typeof source) => {
    const parsed: unknown = JSON.parse(value.text);
    // JSON.parse 会吞掉重复键；在提交对象前保留这一结构错误，避免悄悄丢稿。
    const tokens = value.text.match(/"(?:\\.|[^"\\])*"|[{}[\]:,]/g) ?? [];
    const objects: (Set<string> | null)[] = [];
    for (let index = 0; index < tokens.length; index++) {
      const token = tokens[index];
      if (token === "{") objects.push(new Set());
      else if (token === "[") objects.push(null);
      else if (token === "}" || token === "]") objects.pop();
      else if (token.startsWith('"') && tokens[index + 1] === ":") {
        const key = JSON.parse(token) as string;
        const keys = objects.at(-1);
        if (keys?.has(key)) throw new Error(t("dashboard:ce_duplicate_key"));
        keys?.add(key);
      }
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error(t("dashboard:ce_json_parse_error"));
    if (!baseline.revision) throw new Error(t("adopt_new_content"));
    const result = await API.saveEpisodeDraft(project, episode, "reference_script_plan", parsed as Record<string, unknown>, baseline.revision);
    onChanged();
    return { text: JSON.stringify(result.draft?.content ?? parsed, null, 2), revision: result.draft?.revision ?? baseline.revision };
  }, [project, episode, onChanged, t]);
  const unit = useEditUnit({ source, save, preserveDirtyBaseline: true });
  return <section className="flex flex-col gap-2">
    <label>{t("edit")} JSON<Textarea mono aria-label="JSON" value={unit.value.text} disabled={unit.status === "saving"} onChange={(e) => unit.setValue((old) => ({ ...old, text: e.target.value }))} /></label>
    {unit.error && <p role="alert" className="text-sm text-destructive">{unit.error}</p>}
    {unit.externallyUpdated && <Button variant="outline" onClick={unit.discard}>{t("adopt_new_content")}</Button>}
    <Button variant="outline" disabled={!unit.dirty || unit.status === "saving"} onClick={() => void unit.save()}>{t("save")}</Button>
  </section>;
}
