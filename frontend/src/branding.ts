// Brand configuration — single source of truth for product naming.
// Override at build time via Vite env vars
// (VITE_BRAND_NAME / VITE_BRAND_TAGLINE / VITE_BRAND_DESCRIPTION).
//
// Source code references BRAND.name (or the [[brand]] placeholder in i18n
// resources) so the displayed product name is not hardcoded across files.
// XM Studio defaults can be overridden via frontend/.env.

const env = import.meta.env as Record<string, string | undefined>;

function fallback(value: string | undefined, defaultValue: string): string {
  // Trim + empty check so VITE_BRAND_NAME="" (or whitespace) falls back to the
  // configured default.
  if (typeof value !== "string") return defaultValue;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : defaultValue;
}

export const BRAND = {
  name: fallback(env.VITE_BRAND_NAME, "XM Studio"),
  tagline: fallback(env.VITE_BRAND_TAGLINE, "单集短剧制作"),
  description: fallback(
    env.VITE_BRAND_DESCRIPTION,
    "从一集剧本、资产和视频到可下载的成片。",
  ),
} as const;

export const BRAND_DOCUMENT_TITLE = `${BRAND.name} · ${BRAND.tagline}`;

