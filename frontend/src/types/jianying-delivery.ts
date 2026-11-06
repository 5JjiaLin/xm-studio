export interface JianyingLocalSettings { enabled: boolean; app_path: string; draft_root: string }
export interface JianyingCompatibility {
  system: string; architecture: string; version: string; build: string; ready: boolean;
  process: "running" | "stopped" | "unknown"; verification: "mac_11_7_sample" | "unverified";
}
export interface JianyingSettingsResponse { settings: JianyingLocalSettings; compatibility: JianyingCompatibility; local: boolean }
export interface JianyingDelivery {
  delivery_id: string; draft_id: string; folder: string; revision: number; local: boolean; stale?: boolean;
  state: "preparing" | "ready" | "waiting_for_exit" | "process_unknown" | "delivered" | "package_ready" | "failed" | "removed";
  error: string | null;
}
export interface JianyingDeliveryResponse { delivery: JianyingDelivery | null }
