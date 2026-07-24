/**
 * Minimal design tokens for the Engram Portal (ENG-PORTAL-001A).
 *
 * Intentionally small: colors, spacing, radii, and a focus ring. No theme
 * editor, no charting, no component vendor (non-goals). Status is conveyed with
 * text + icon, never by color alone.
 */
export const tokens = {
  color: {
    bg: "#ffffff",
    surface: "#f7f8fa",
    border: "#d0d7de",
    text: "#1f2328",
    textMuted: "#656d76",
    focus: "#0969da",
    ok: "#1a7f37",
    warn: "#9a6700",
    err: "#cf222e",
  },
  space: {
    xs: "4px",
    sm: "8px",
    md: "16px",
    lg: "24px",
    xl: "40px",
  },
  radius: {
    sm: "4px",
    md: "8px",
    pill: "9999px",
  },
  focusRing: "0 0 0 3px rgba(9, 105, 218, 0.35)",
} as const;

export type Tokens = typeof tokens;
