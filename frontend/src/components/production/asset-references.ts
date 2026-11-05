import { LINE_BREAK_RE, MENTION_RE, mentionNameFromMatch, normalizeAssetName, splitSpeechLine } from "@/utils/reference-mentions";

/** A character angle remains part of its parent asset for adoption/removal. */
export function belongsToAsset(reference: string, name: string): boolean {
  const ref = normalizeAssetName(reference), key = normalizeAssetName(name);
  return ref === key || ref.startsWith(`${key}/`);
}

/** Remove visual references without turning character dialogue into narration. */
export function replaceAssetReferences(text: string, name: string, replacement: string): string {
  const key = normalizeAssetName(name);
  const rewrite = (part: string) => part.replace(MENTION_RE, (...args: unknown[]) => {
    const match = args.slice(0, 3) as unknown as RegExpMatchArray;
    return belongsToAsset(mentionNameFromMatch(match), key) ? replacement ? `@[${replacement}]` : name : String(args[0]);
  });
  return text.split(LINE_BREAK_RE).map((line, index) => index % 2 ? line : splitSpeechLine(line).map((part) => {
    if (typeof part === "string") return rewrite(part);
    // Ordinary-description removal preserves the speaker and all spoken words.
    return replacement && belongsToAsset(part.speaker, key) ? rewrite(part.raw.slice(0, part.raw.indexOf("{"))) + part.raw.slice(part.raw.indexOf("{")) : part.raw;
  }).join("")).join("");
}
