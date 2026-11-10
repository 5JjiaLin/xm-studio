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
  aspect_ratio?: string | { storyboard?: string };
  content_mode: "narration" | "drama" | "ad";
  timeline_id: string; revision: number; duration: number;
  bgm?: ProductionBgmClip[]; bgm_library?: ProductionBgmTrack[];
  clips: ProductionEditClip[];
  media: Record<string, { version: number | null; duration_us: number | null }>;
  subtitle_drafts: Record<string, string>;
  issues: { clip_id: string; field: string; subtitle_id?: string; message: string }[];
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
  items: Array<{ episode: number; position?: number | null; title?: string; status: "pending" | "submitting" | "active" | "unknown" | "failed" | "completed" }>;
  imports: ScriptImport[];
}

export interface ProductionBgmTrack { id: string; name: string; file: string; duration_us: number; gain_db: number; generation_id?: string | null }
export interface ProductionBgmClip { id: string; bgm_id: string; start_us: number; in_us: number; out_us: number; volume: number; fade_in_us: number; fade_out_us: number }
export interface MusicDraft { summary: string; mood: string; target_seconds: number; prompt: string }
export interface MusicReview { basis: string; model: string; configured: boolean; submitted_prompt: string; draft: MusicDraft }
export interface MusicTask { task_id: string; candidate_id: string; status: string; recoverable: boolean; error?: string | null }
export interface BeatReport {
  basis: string; revision: number; duration_us: number;
  tracks: Array<{ clip_id: string; bgm_id: string; start_us: number; end_us: number; source_in_us: number; duration_us: number; confidence: number; bpm: number | null; waveform: number[]; beats_us: number[] }>;
  events: Array<{ clip_id: string; unit_id: string; start_us: number; end_us: number; text: string; speech: string | null }>;
  suggestions: Array<{ id: string; cut_us: number; beat_us: number; offset_us: number; status: "aligned" | "near" | "conflict" | "unconfirmed"; reason: string | null }>;
}
