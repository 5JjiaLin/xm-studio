import { create } from "zustand";
import { getToken, setToken as saveToken, clearToken } from "@/utils/auth";

interface AuthState {
  token: string | null;
  username: string | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  initialize: () => void;
  login: (token: string, username: string) => void;
  logout: () => void;
  setLoading: (loading: boolean) => void;
}

// A delayed bootstrap must never restore a session after logout or a new login.
let initializationVersion = 0;

export const useAuthStore = create<AuthState>((set) => ({
  token: null,
  username: null,
  isAuthenticated: false,
  isLoading: true,

  initialize: () => {
    const version = ++initializationVersion;
    set({ isLoading: true });
    const token = getToken();
    if (token) {
      // Restore the HttpOnly media credential before rendering native media.
      // The server keeps the original Bearer expiry and rechecks revocation.
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 5000);
      fetch("/api/v1/auth/media-session", {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        signal: controller.signal,
      })
        .then((res) => {
          if (version !== initializationVersion) return;
          if (!res.ok) {
            if (res.status === 401) clearToken();
            throw new Error(`media session ${res.status}`);
          }
          set({ token, isAuthenticated: true });
        })
        .catch((err) => {
          if (version !== initializationVersion) return;
          console.warn("[auth] media session bootstrap failed", err);
          set({ token: null, isAuthenticated: false });
        })
        .finally(() => {
          clearTimeout(timeoutId);
          if (version === initializationVersion) set({ isLoading: false });
        });
      return;
    }
    // 无 token 时先问后端是否启用了鉴权。`AUTH_ENABLED=false` 时后端全链路
    // bypass，前端也应该跳过登录页直接进主界面。超时 / 网络异常 / 响应 shape
    // 异常时 fail-closed 退回到登录页，避免误把损坏响应当成"无需鉴权"放行。
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 5000);
    fetch("/api/v1/auth/status", { signal: controller.signal })
      .then(async (res) => {
        if (!res.ok) throw new Error(`status ${res.status}`);
        const payload: unknown = await res.json();
        if (version !== initializationVersion) return;
        if (
          typeof payload !== "object" ||
          payload === null ||
          typeof (payload as { enabled?: unknown }).enabled !== "boolean"
        ) {
          throw new Error("invalid /auth/status payload");
        }
        const { enabled } = payload as { enabled: boolean };
        if (!enabled) {
          set({ isAuthenticated: true });
        }
      })
      .catch((err) => {
        console.warn("[auth] /auth/status fetch failed; defaulting to login", err);
      })
      .finally(() => {
        clearTimeout(timeoutId);
        if (version === initializationVersion) set({ isLoading: false });
      });
  },

  login: (token, username) => {
    ++initializationVersion;
    saveToken(token);
    set({ token, username, isAuthenticated: true, isLoading: false });
  },

  logout: () => {
    ++initializationVersion;
    // Cookie removal is best-effort on network failure; the server still checks
    // account/session revocation and membership on every subsequent media GET.
    fetch("/api/v1/auth/logout", { method: "POST", signal: AbortSignal.timeout(5000) })
      .catch((err) => console.warn("[auth] media logout failed", err));
    clearToken();
    set({ token: null, username: null, isAuthenticated: false, isLoading: false });
  },

  setLoading: (isLoading) => set({ isLoading }),
}));
