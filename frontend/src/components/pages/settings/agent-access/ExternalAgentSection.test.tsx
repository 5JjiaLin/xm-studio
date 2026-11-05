import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAppStore } from "@/stores/app-store";

import { ExternalAgentSection } from "./ExternalAgentSection";

function stubClipboard(writeText: () => Promise<void> = () => Promise.resolve()) {
  const fn = vi.fn(writeText);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: fn } });
  return fn;
}

describe("ExternalAgentSection", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    useAppStore.setState(useAppStore.getInitialState(), true);
  });

  afterEach(() => {
    Reflect.deleteProperty(navigator, "clipboard");
  });

  it("按步骤给出安装命令、MCP 端点与给 AI Agent 的提示词，复制的是原样内容", async () => {
    const user = userEvent.setup();
    const writeText = stubClipboard();
    render(<ExternalAgentSection />);

    const steps = within(screen.getByRole("list")).getAllByRole("listitem");
    expect(steps.map((step) => within(step).getByRole("heading").textContent)).toEqual([
      "安装公开 skills",
      "配置远程 MCP",
      "准备访问令牌",
    ]);
    // setup skill 明文保存令牌，第三步提醒把配置文件排除在版本控制之外
    expect(within(steps[2]).getByText(/加入 \.gitignore/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "复制安装命令" }));
    expect(writeText).toHaveBeenLastCalledWith("npx skills add ArcReel/skills");
    await user.click(screen.getByRole("button", { name: "复制 MCP 端点" }));
    expect(writeText).toHaveBeenLastCalledWith(`${window.location.origin}/mcp`);
    await user.click(screen.getByRole("button", { name: "复制提示词" }));
    // 提示词只给安装指引地址，不夹带任何令牌
    expect(writeText).toHaveBeenLastCalledWith(
      `帮我接入 ArcReel。请阅读并执行 ${window.location.origin}/agent-installation-guide.md`,
    );
  });

  it("剪贴板不可用时提示手动复制", async () => {
    const user = userEvent.setup();
    stubClipboard(() => Promise.reject(new Error("denied")));
    render(<ExternalAgentSection />);

    await user.click(screen.getByRole("button", { name: "复制安装命令" }));

    await waitFor(() => expect(useAppStore.getState().toast?.text).toBe("复制失败，请手动选择并复制内容。"));
    expect(screen.getByRole("button", { name: "复制安装命令" })).toBeInTheDocument();
  });

});
