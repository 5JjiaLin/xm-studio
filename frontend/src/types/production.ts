export interface ProductionState {
  episode: number;
  status: "idle" | "active" | "submitting" | "unknown" | "failed" | "completed";
  action?: "assets" | "videos" | "finish";
  stage: "confirm" | "prompts" | "assets" | "videos" | "prepare" | "music" | "apply_music" | "render" | "draft" | null;
  batch_id?: string | null;
  timeline_id?: string;
  error?: string | null;
  members?: Array<{ unit_id: string; task_id?: string | null; status: string; problem?: { detail?: string | null } | null }>;
  output?: { status: string; artifact_path: string; version: number | null };
}

export interface CostSummary {
  call_count: number;
  known_call_count: number;
  amounts: Record<string, number>;
  unpriced_count: number;
  pending_count: number;
  reroll_count: number;
  reroll_amounts: Record<string, number>;
  unidentified_target_count: number;
}
export interface ProductionCost extends CostSummary {
  categories: Partial<Record<"text" | "image" | "video" | "audio", CostSummary>>;
}
export interface ProjectCosts {
  episodes: (ProductionCost & { episode: number })[];
  unassigned: ProductionCost;
}


export interface ScriptBoundary { start_line: number; title: string; number: number }
export interface WholeScriptPreview {
  text: string; fingerprint: string; project_basis: string;
  episodes: Array<ScriptBoundary & { start: number; end: number; characters: number; head: string; tail: string }>;
  warnings: string[]; errors: string[];
}
export interface ScriptImport { import_id: string; episodes: number[]; fingerprint: string }
export interface ScriptBatchState {
  status: "idle" | "active" | "submitting" | "unknown" | "stopping" | "stopped" | "failed" | "completed";
  import_id?: string; error?: string | null; input_changed?: boolean; input_revision?: string;
    items: Array<{ episode: number; position?: number | null; title?: string; requires_provider_check?: boolean; status: "pending" | "submitting" | "active" | "unknown" | "failed" | "completed" }>;
  imports: ScriptImport[];
}
