import type { Character } from "@/types";
import { extractMentions, LINE_BREAK_RE, MENTION_RE, mentionNameFromMatch, normalizeAssetName, splitSpeechLine } from "@/utils/reference-mentions";

export const CHARACTER_ANGLES = [
  { name: "视角-正面全身", key: "front", index: 0 },
  { name: "视角-侧面全身", key: "side", index: 1 },
  { name: "视角-背面全身", key: "back", index: 2 },
  { name: "视角-正面特写", key: "face", index: 3 },
] as const;

export function characterAngleMentions(text: string, characters: Record<string, Character>): { reference: string; owner: string }[] {
  return extractMentions(text).flatMap((reference) => {
    const [base, angle] = reference.split("/");
    const owner = Object.keys(characters).find((name) => normalizeAssetName(name) === base);
    // Costume/other forms retain their own appearance; never silently switch them to a base angle.
    return owner && (!angle || CHARACTER_ANGLES.some((item) => item.name === angle)) ? [{ reference, owner }] : [];
  });
}

/** Change only visual mentions. Speaker marks and literal dialogue stay byte-for-byte intact. */
export function replaceCharacterAngle(text: string, source: string, target: string): string {
  return text.split(LINE_BREAK_RE).map((line, index) => index % 2 ? line : splitSpeechLine(line).map((part) => {
    if (typeof part !== "string") return part.raw;
    return part.replace(MENTION_RE, (...args: unknown[]) => {
      const match = args.slice(0, 3) as unknown as RegExpMatchArray;
      return normalizeAssetName(mentionNameFromMatch(match)) === normalizeAssetName(source) ? `@[${target}]` : String(args[0]);
    });
  }).join("")).join("");
}

export type CropRect = { x: number; y: number; width: number; height: number };
export function cropPixels(rect: CropRect, width: number, height: number): [number, number, number, number] | null {
  if (![rect.x, rect.y, rect.width, rect.height, width, height].every(Number.isFinite) || rect.x < 0 || rect.y < 0 || rect.width <= 0 || rect.height <= 0 || rect.x + rect.width > 100 || rect.y + rect.height > 100 || width < 1 || height < 1) return null;
  const left = Math.round(width * rect.x / 100), top = Math.round(height * rect.y / 100);
  const w = Math.round(width * (rect.x + rect.width) / 100) - left;
  const h = Math.round(height * (rect.y + rect.height) / 100) - top;
  return w > 0 && h > 0 ? [left, top, w, h] : null;
}
