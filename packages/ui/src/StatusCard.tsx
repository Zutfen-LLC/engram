"use client";
/**
 * Accessible StatusCard (ENG-PORTAL-001A).
 *
 * Status is conveyed by an explicit text label AND a symbol — never by color
 * alone (WCAG 1.4.1). The card is a labelled live region so assistive tech
 * announces status changes.
 */
import type { CSSProperties, ReactNode } from "react";
import { tokens } from "./tokens";

export type StatusTone = "ok" | "warn" | "err" | "unknown";

const TONE_TEXT: Record<StatusTone, string> = {
  ok: "Available",
  warn: "Degraded",
  err: "Unavailable",
  unknown: "Unknown",
};

const TONE_SYMBOL: Record<StatusTone, string> = {
  ok: "\u2713", // check mark
  warn: "!",
  err: "\u2717", // ballot X
  unknown: "?",
};

export interface StatusCardProps {
  /** Short title, e.g. "Control Plane". */
  title: string;
  /** Tone drives the text label + symbol; color is secondary. */
  tone: StatusTone;
  /** Optional human-readable detail shown under the label. */
  detail?: ReactNode;
  /** Override the auto-derived text label if needed. */
  label?: string;
}

export function StatusCard({ title, tone, detail, label }: StatusCardProps): ReactNode {
  const text = label ?? TONE_TEXT[tone];
  const color =
    tone === "ok"
      ? tokens.color.ok
      : tone === "warn"
        ? tokens.color.warn
        : tone === "err"
          ? tokens.color.err
          : tokens.color.textMuted;

  const cardStyle: CSSProperties = {
    background: tokens.color.surface,
    border: `1px solid ${tokens.color.border}`,
    borderRadius: tokens.radius.md,
    padding: tokens.space.md,
  };
  const titleStyle: CSSProperties = {
    fontWeight: 600,
    margin: 0,
    marginBottom: tokens.space.xs,
  };
  const statusStyle: CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    gap: tokens.space.xs,
    color,
    fontWeight: 600,
  };

  return (
    <div role="status" aria-live="polite" style={cardStyle}>
      <h3 style={titleStyle}>{title}</h3>
      <p style={statusStyle}>
        {/* aria-hidden symbol: the text label carries the meaning */}
        <span aria-hidden="true">{TONE_SYMBOL[tone]}</span>
        <span>{text}</span>
      </p>
      {detail ? (
        <p style={{ margin: 0, marginTop: tokens.space.xs, color: tokens.color.textMuted }}>
          {detail}
        </p>
      ) : null}
    </div>
  );
}
