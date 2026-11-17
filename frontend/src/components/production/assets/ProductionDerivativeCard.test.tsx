import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { API } from "@/api";
import * as generation from "@/actions/generation";
import { LeaveGuardProvider } from "@/components/shared/edit-unit/LeaveGuard";
import { ProductionDerivativeCard } from "./ProductionDerivativeCard";

afterEach(() => vi.restoreAllMocks());
it("repairs a legacy empty derivative, saves before generating and previews the saved instruction", async () => {
  vi.spyOn(API, "previewAssetPrompt").mockImplementation(async (_project, _kind, _name, description) => ({ text: `完整编辑框架：${description}`, unavailable: null }) as never);
  const save = vi.spyOn(API, "updateCharacterDerivative").mockResolvedValue({} as never);
  const generate = vi.spyOn(generation, "enqueueCharacterDerivative").mockResolvedValue({} as never);
  const changed = vi.fn();
  const card = (description: string) => <LeaveGuardProvider><ProductionDerivativeCard project="demo" owner="小桃" name="雨衣" description={description} ownerPath="characters/base.png" busy={false} onChanged={changed} /></LeaveGuardProvider>;
  const { rerender } = render(card(""));
  const button = screen.getByRole("button", { name: "确认提示词并生成图片" });
  expect(button).toBeDisabled();
  fireEvent.change(screen.getByRole("textbox", { name: "外观变化" }), { target: { value: "穿上红色雨衣" } });
  expect(await screen.findByDisplayValue("完整编辑框架：穿上红色雨衣")).toBeInTheDocument();
  expect(button).toBeDisabled();
  expect(generate).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(save).toHaveBeenCalledWith("demo", "小桃", "雨衣", "穿上红色雨衣"));
  rerender(card("穿上红色雨衣"));
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
  await waitFor(() => expect(generate).toHaveBeenCalledExactlyOnceWith("demo", "小桃", "雨衣"));
});
