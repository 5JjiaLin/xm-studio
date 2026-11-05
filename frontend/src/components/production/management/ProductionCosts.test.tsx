import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import "@/i18n";
import { API } from "@/api";
import type { ProductionCost, ProjectCosts } from "@/types/production";
import { ProductionCosts, useProductionCosts } from "./ProductionCosts";

const empty: ProductionCost = { call_count: 0, known_call_count: 0, amounts: {}, unpriced_count: 0, pending_count: 0, reroll_count: 0, reroll_amounts: {}, unidentified_target_count: 0, categories: {} };
afterEach(() => vi.restoreAllMocks());

it("shows known subtotal separately from pending and unpriced calls without adding rerolls twice", () => {
  render(<ProductionCosts cost={{ ...empty, call_count: 4, known_call_count: 2, amounts: { CNY: 3, USD: 1 }, unpriced_count: 1, pending_count: 1, reroll_count: 1, reroll_amounts: { CNY: 1 }, categories: { image: { ...empty, call_count: 2, known_call_count: 2, amounts: { CNY: 3 }, reroll_count: 1, reroll_amounts: { CNY: 1 } } } }} />);
  expect(screen.getByText("已知小计 · CNY 3.00 · USD 1.00")).toBeInTheDocument();
  expect(screen.getByText("1 次缺少价格，未计入金额")).toBeInTheDocument();
  expect(screen.getByText("1 次尚未结算，未计入金额")).toBeInTheDocument();
  expect(screen.getByText("其中重做/重试 1 次 · CNY 1.00")).toBeInTheDocument();
  expect(screen.queryByText("CNY 4.00")).not.toBeInTheDocument();
});

it("unknown cost is not presented as zero", () => {
  render(<ProductionCosts cost={{ ...empty, call_count: 1, unpriced_count: 1 }} />);
  expect(screen.getByText("待核算")).toBeInTheDocument();
  expect(screen.queryByText(/0\.00/)).not.toBeInTheDocument();
});

it("ignores a late cost response after changing projects", async () => {
  let resolveOld: (value: ProjectCosts) => void = () => {};
  const current: ProjectCosts = { episodes: [{ ...empty, episode: 2 }], unassigned: empty };
  const request = vi.spyOn(API, "getProductionCosts").mockImplementation((name) => name === "old" ? new Promise<ProjectCosts>((resolve) => { resolveOld = resolve; }) : Promise.resolve(current));
  const { result, rerender } = renderHook(({ name }) => useProductionCosts(name, 0), { initialProps: { name: "old" } });
  rerender({ name: "new" });
  await waitFor(() => expect(result.current.data).toEqual(current));
  await act(async () => { resolveOld({ episodes: [{ ...empty, episode: 1 }], unassigned: empty }); await Promise.resolve(); });
  expect(result.current.data).toEqual(current);
  expect(request).toHaveBeenCalledTimes(2);
});
