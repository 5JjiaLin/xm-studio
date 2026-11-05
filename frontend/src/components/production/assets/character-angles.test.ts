import { expect, it } from "vitest";
import { characterAngleMentions, cropPixels, replaceCharacterAngle } from "./character-angles";

it("switches only the visible reference, preserving voice identity and literal dialogue", () => {
  const source = "@[林清]看向门口。\r\n@[林清]：{我说的是@[林清]！}\n@[房间]";
  expect(replaceCharacterAngle(source, "林清", "林清/视角-正面特写")).toBe("@[林清/视角-正面特写]看向门口。\r\n@[林清]：{我说的是@[林清]！}\n@[房间]");
});
it("keeps costume references and voice-only characters out of base-angle switching", () => {
  expect(characterAngleMentions("@[林清/战损]站着，@[林清]{你好}", { 林清: { description: "", derivatives: { 战损: { description: "" } } } })).toEqual([]);
  expect(characterAngleMentions("@[林清/视角-背面全身]", { 林清: { description: "" } })).toEqual([{ owner: "林清", reference: "林清/视角-背面全身" }]);
});
it("crops real pixels with adjustable bounds and rejects invalid or empty rectangles", () => {
  expect(cropPixels({ x: 75, y: 0, width: 25, height: 100 }, 1600, 900)).toEqual([1200, 0, 400, 900]);
  expect(cropPixels({ x: 10, y: 10, width: 30, height: 50 }, 1000, 800)).toEqual([100, 80, 300, 400]);
  expect(cropPixels({ x: 80, y: 0, width: 25, height: 100 }, 1600, 900)).toBeNull();
  expect(cropPixels({ x: 0, y: 0, width: 0.01, height: 100 }, 10, 10)).toBeNull();
  expect(cropPixels({ x: NaN, y: 0, width: 25, height: 100 }, 1600, 900)).toBeNull();
});
