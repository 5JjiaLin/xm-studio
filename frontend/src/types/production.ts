export interface ProductionState {
  episode: number;
  status: "idle" | "active" | "submitting" | "unknown" | "failed" | "completed";
  action?: "assets" | "videos";
  stage: "confirm" | "prompts" | "assets" | "videos" | "render" | null;
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


export interface ProductionSubtitle {
  id: string; text: string; start_us: number; end_us: number; basis_version: number;
  font_size: number; color: string; position: "top" | "center" | "bottom";
}
export interface ProductionEditClip {
  id: string; unit_id: string; included?: boolean;
  trim: { in_us: number; out_us: number; basis_version: number } | null;
  source_volume: number; hold_us: number;
  subtitles?: ProductionSubtitle[] | null;
}
export interface ProductionEdit {
  timeline_id: string; revision: number; duration: number;
  clips: ProductionEditClip[];
  media: Record<string, { version: number | null; duration_us: number | null }>;
  subtitle_drafts: Record<string, string>;
  issues: { clip_id: string; field: string; subtitle_id?: string; message: string }[];
}
