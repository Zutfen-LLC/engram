"use client";
/**
 * PageContainer — semantic landmark wrapper for the Portal.
 *
 * Establishes header/main/footer landmarks for accessible navigation.
 */
import type { CSSProperties, ReactNode } from "react";
import { tokens } from "./tokens";

export interface PageContainerProps {
  header?: ReactNode;
  footer?: ReactNode;
  children: ReactNode;
}

export function PageContainer({ header, footer, children }: PageContainerProps): ReactNode {
  const wrapStyle: CSSProperties = {
    minHeight: "100vh",
    background: tokens.color.bg,
    color: tokens.color.text,
    display: "flex",
    flexDirection: "column",
  };
  const mainStyle: CSSProperties = {
    flex: 1,
    width: "100%",
    maxWidth: "960px",
    margin: "0 auto",
    padding: `${tokens.space.lg} ${tokens.space.md}`,
  };
  return (
    <div style={wrapStyle}>
      {header ? <header>{header}</header> : null}
      <main id="main" style={mainStyle}>
        {children}
      </main>
      {footer ? <footer>{footer}</footer> : null}
    </div>
  );
}
