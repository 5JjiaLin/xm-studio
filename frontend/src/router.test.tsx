import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import { useAuthStore } from "@/stores/auth-store";
import { AppRoutes } from "./router";

const tourMounted = vi.hoisted(() => vi.fn());
vi.mock("@/onboarding/OnboardingTour", () => ({ OnboardingTour: () => { tourMounted(); return null; } }));
vi.mock("@/components/production/ProductionPage", () => ({ ProductionPage: () => <h1>production</h1> }));
vi.mock("@/components/production/management/ProjectsPage", () => ({ ProjectsPage: () => <h1>projects</h1> }));
vi.mock("@/components/pages/SystemConfigPage", () => ({ SystemConfigPage: () => <h1>models</h1> }));
vi.mock("@/components/pages/LoginPage", () => ({ LoginPage: () => <h1>login</h1> }));
function at(path: string) {
  const location = memoryLocation({ path, record: true });
  render(<Router hook={location.hook}><AppRoutes /></Router>);
  return location;
}
describe("minimal production routes", () => {
  beforeEach(() => { useAuthStore.setState({ isAuthenticated: true, isLoading: false }); });
  it.each(["/", "/app", "/app/assets", "/app/projects/demo/settings", "/app/projects/demo/episodes/1", "/app/make/demo/files"])("retires platform URL %s", async (path) => {
    const location = at(path);
    expect(await screen.findByRole("heading", { name: "production" })).toBeInTheDocument();
    await waitFor(() => expect(location.history?.at(-1)).toBe("/app/make"));
  });
  it.each(["/app/make", "/app/make/work-1?step=assets"])("opens production %s", async (path) => {
    at(path); expect(await screen.findByRole("heading", { name: "production" })).toBeInTheDocument();
  });
  it.each(["/app/projects", "/app/projects/demo"])("opens project management %s", async (path) => { at(path); expect(await screen.findByRole("heading", { name: "projects" })).toBeInTheDocument(); });
  it("never mounts onboarding", () => { at("/app/make"); expect(tourMounted).not.toHaveBeenCalled(); });
  it("keeps model settings available", async () => {
    at("/app/settings?section=providers"); expect(await screen.findByRole("heading", { name: "models" })).toBeInTheDocument();
  });
  it("uses inherited authentication when explicitly enabled", async () => {
    useAuthStore.setState({ isAuthenticated: false, isLoading: false });
    at("/app/make"); expect(await screen.findByRole("heading", { name: "login" })).toBeInTheDocument();
  });
});
