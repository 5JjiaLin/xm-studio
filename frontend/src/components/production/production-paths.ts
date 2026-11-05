export const PRODUCTION_PATH = "/app/make";
export const PROJECTS_PATH = "/app/projects";
export const PRODUCTION_STEPS = ["script", "breakdown", "assets", "videos", "output"] as const;
export type ProductionStep = (typeof PRODUCTION_STEPS)[number];
export function projectPath(project?: string) {
  return `${PROJECTS_PATH}${project ? `/${encodeURIComponent(project)}` : ""}`;
}
export function productionPath(project?: string | null, step: ProductionStep = "script", episode?: number | null) {
  return `${PRODUCTION_PATH}${project ? `/${encodeURIComponent(project)}?step=${step}${episode ? `&episode=${episode}` : ""}` : ""}`;
}
