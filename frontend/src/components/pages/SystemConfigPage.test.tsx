import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import { SystemConfigPage } from "./SystemConfigPage";
vi.mock("./ProviderSection", () => ({ ProviderSection: () => <h2>provider form</h2> }));
vi.mock("./settings/MediaModelSection", () => ({ MediaModelSection: ({ compact }: { compact: boolean }) => <h2>{compact ? "three models" : "all models"}</h2> }));
vi.mock("./settings/endpoints/EndpointsSection", () => ({ EndpointsSection: ({ localOnly }: { localOnly: boolean }) => <h2>{localOnly ? "local endpoints" : "market endpoints"}</h2> }));
function at(section: string) {
 const location = memoryLocation({ path: "/app/settings", searchPath: `section=${section}`, record: true });
 render(<Router hook={location.hook}><SystemConfigPage /></Router>); return location;
}
describe("minimal model configuration", () => {
 beforeEach(() => sessionStorage.clear());
 it.each(["arcreel-agent", "market", "usage", "general", "about", "prompt-templates"])("does not expose removed section %s", (section) => {
  at(section); expect(screen.getByRole("heading", { name: "three models" })).toBeInTheDocument();
  expect(screen.getAllByRole("link").map((x) => x.textContent)).toEqual(["默认模型", "供应商", "高级端点", "ArcReel"]);
 });
 it("retains suppliers", () => { at("providers"); expect(screen.getByRole("heading", { name: "provider form" })).toBeInTheDocument(); });
 it("keeps local advanced endpoints", () => { at("endpoints"); expect(screen.getByRole("heading", { name: "local endpoints" })).toBeInTheDocument(); });
 it("returns to the original work and step", async () => {
  sessionStorage.setItem("xm:production-return", "/app/make/work-1?step=assets"); const location = at("providers");
  await userEvent.click(screen.getByRole("button", { name: "返回制作" }));
  expect(location.history?.at(-1)).toBe("/app/make/work-1?step=assets");
 });
});
