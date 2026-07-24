"use client";
/** Small typography primitives. No charting/design-system (non-goals). */
import type { CSSProperties, ReactNode } from "react";
import { tokens } from "./tokens";

export function Heading({
  level = 1,
  children,
}: {
  level?: 1 | 2 | 3;
  children: ReactNode;
}): ReactNode {
  const Tag = (`h${level}` as "h1" | "h2" | "h3");
  const sizes: Record<number, CSSProperties> = {
    1: { fontSize: "2rem", margin: 0 },
    2: { fontSize: "1.5rem", margin: 0 },
    3: { fontSize: "1.2rem", margin: 0 },
  };
  return <Tag style={sizes[level]}>{children}</Tag>;
}

export function Muted({ children }: { children: ReactNode }): ReactNode {
  return <span style={{ color: tokens.color.textMuted }}>{children}</span>;
}
