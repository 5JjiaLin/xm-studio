import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "./auth-store";
import { clearToken, getToken, setToken } from "@/utils/auth";

async function settle() {
  // Drain the fetch, body parsing and finally callbacks without polling timers.
  for (let i = 0; i < 8; i++) await Promise.resolve();
}

function deferredResponse() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((complete) => { resolve = complete; });
  return { promise, resolve };
}

describe("browser authentication bootstrap", () => {
  beforeEach(() => {
    clearToken();
    useAuthStore.setState(useAuthStore.getInitialState(), true);
    vi.spyOn(console, "warn").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    clearToken();
  });

  it("waits for the media cookie before authenticating a saved Bearer", async () => {
    setToken("saved-token");
    const pending = deferredResponse();
    const fetchMock = vi.fn().mockReturnValue(pending.promise);
    vi.stubGlobal("fetch", fetchMock);
    useAuthStore.getState().initialize();
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(useAuthStore.getState().isLoading).toBe(true);
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/auth/media-session", expect.objectContaining({
      method: "POST", headers: { Authorization: "Bearer saved-token" },
    }));
    pending.resolve(new Response(null, { status: 204 }));
    await settle();
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
    expect(useAuthStore.getState().isLoading).toBe(false);
  });

  it("removes a revoked saved session", async () => {
    setToken("revoked");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 401 })));
    useAuthStore.getState().initialize();
    await settle();
    expect(getToken()).toBeNull();
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(useAuthStore.getState().isLoading).toBe(false);
  });

  it("keeps the token for retry after a temporary network failure", async () => {
    setToken("saved");
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
    useAuthStore.getState().initialize();
    await settle();
    expect(getToken()).toBe("saved");
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(useAuthStore.getState().isLoading).toBe(false);
  });

  it("does not restore a delayed session after logout", async () => {
    setToken("old");
    const pending = deferredResponse();
    const fetchMock = vi.fn().mockReturnValueOnce(pending.promise)
      .mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    useAuthStore.getState().initialize();
    useAuthStore.getState().logout();
    pending.resolve(new Response(null, { status: 204 }));
    await settle();
    expect(getToken()).toBeNull();
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(useAuthStore.getState().isLoading).toBe(false);
    expect(fetchMock).toHaveBeenLastCalledWith("/api/v1/auth/logout", expect.objectContaining({ method: "POST" }));
  });

  it("does not erase a new login when an old session returns 401", async () => {
    setToken("old");
    const pending = deferredResponse();
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(pending.promise));
    useAuthStore.getState().initialize();
    useAuthStore.getState().login("new", "alice");
    pending.resolve(new Response(null, { status: 401 }));
    await settle();
    expect(getToken()).toBe("new");
    expect(useAuthStore.getState().username).toBe("alice");
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
  });

  it("permits anonymous development only after an explicit disabled response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true, json: async () => ({ enabled: false }),
    }));
    useAuthStore.getState().initialize();
    await settle();
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
    expect(useAuthStore.getState().isLoading).toBe(false);
  });

  it("keeps login required for a malformed authentication status", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true, json: async () => ({ enabled: "false" }),
    }));
    useAuthStore.getState().initialize();
    await settle();
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(useAuthStore.getState().isLoading).toBe(false);
  });
});
