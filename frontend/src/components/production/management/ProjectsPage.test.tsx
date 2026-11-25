import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { API } from "@/api";
import type { ProjectSummary } from "@/types/project";
import { ProjectsPage } from "./ProjectsPage";

vi.mock("../feedback/FeedbackExportButton", () => ({ FeedbackExportButton: () => null }));
vi.mock("../script/ScriptInput", () => ({ ScriptInput: () => null }));

const project = { name: "archived-demo", title: "归档测试项目", production_management: { archived: true } } as ProjectSummary;

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(API, "listProjects").mockResolvedValue({ projects: [project] });
  vi.spyOn(API, "deleteProject").mockResolvedValue({ success: true });
});

async function openDelete() {
  const user = userEvent.setup();
  render(<ProjectsPage />);
  await user.click(await screen.findByRole("button", { name: "查看归档" }));
  await user.click(await screen.findByRole("button", { name: "删除项目" }));
  const dialog = await screen.findByRole("alertdialog", { name: "删除「归档测试项目」？" });
  return { user, dialog };
}

it("does not delete an archived project when the confirmation is cancelled", async () => {
  const { user, dialog } = await openDelete();
  expect(API.deleteProject).not.toHaveBeenCalled();
  await user.click(within(dialog).getByRole("button", { name: "取消" }));
  expect(API.deleteProject).not.toHaveBeenCalled();
  expect(screen.getByRole("heading", { name: "归档测试项目" })).toBeInTheDocument();
});

it("deletes the selected archived project after confirmation and refreshes the list", async () => {
  const { user, dialog } = await openDelete();
  vi.mocked(API.listProjects).mockResolvedValue({ projects: [] });
  await user.click(within(dialog).getByRole("button", { name: "删除项目" }));
  expect(API.deleteProject).toHaveBeenCalledWith("archived-demo");
  await waitFor(() => expect(screen.queryByRole("heading", { name: "归档测试项目" })).not.toBeInTheDocument());
});

it("retains the project and shows a failed delete so the user can retry", async () => {
  vi.mocked(API.deleteProject).mockRejectedValue(new Error("删除失败，请重试"));
  const { user, dialog } = await openDelete();
  await user.click(within(dialog).getByRole("button", { name: "删除项目" }));
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("删除失败，请重试");
  expect(screen.getByRole("heading", { name: "归档测试项目", hidden: true })).toBeInTheDocument();
});

it("exposes the existing deleted-projects recovery dialog", async () => {
  vi.spyOn(API, "listDeletedProjects").mockResolvedValue({ projects: [], retention_days: 30 });
  const user = userEvent.setup();
  render(<ProjectsPage />);
  await user.click(await screen.findByRole("button", { name: "已删除项目" }));
  expect(await screen.findByRole("dialog", { name: "已删除项目" })).toBeInTheDocument();
  expect(API.listDeletedProjects).toHaveBeenCalled();
});
