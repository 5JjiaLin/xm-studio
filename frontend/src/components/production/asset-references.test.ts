import { describe, expect, it } from "vitest";
import { replaceAssetReferences } from "./asset-references";

describe("asset reference removal", () => {
  it("removes both reference formats while preserving speech and literal dialogue mentions", () => {
    expect(replaceAssetReferences("@阿明走向@[阿明]\n@[阿明]：{等一下，@阿明！}", "阿明", "")).toBe("@阿明走向阿明\n@[阿明]：{等一下，@阿明！}");
  });
  it("rebinds a replacement speaker without rewriting spoken words", () => {
    expect(replaceAssetReferences("@阿明 @[阿明]：{你好，@[阿明]}", "阿明", "小红")).toBe("@[小红] @[小红]：{你好，@[阿明]}");
  });
});


it("removes a parent character's angle references while preserving all dialogue", () => {
  const text = "@[阿明/视角-正面特写]抬头。@[阿明]{别走} @[阿明二号]";
  expect(replaceAssetReferences(text, "阿明", "")).toBe("阿明抬头。@[阿明]{别走} @[阿明二号]");
  expect(replaceAssetReferences(text, "阿明", "阿红")).toBe("@[阿红]抬头。@[阿红]{别走} @[阿明二号]");
});
