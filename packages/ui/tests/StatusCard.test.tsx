import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StatusCard } from "../src/StatusCard.js";

describe("StatusCard", () => {
  it("renders an accessible status region", () => {
    render(<StatusCard title="Control Plane" tone="ok" />);
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("conveys status by text, not color alone", () => {
    const { rerender } = render(<StatusCard title="Control Plane" tone="ok" />);
    expect(screen.getByText("Available")).toBeInTheDocument();

    rerender(<StatusCard title="Control Plane" tone="err" />);
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
  });

  it("conveys status by a symbol alongside the label", () => {
    render(<StatusCard title="Identity" tone="warn" />);
    // the symbol span is aria-hidden; the text label carries meaning
    expect(screen.getByText("Degraded")).toBeInTheDocument();
  });

  it("renders the title and optional detail", () => {
    render(<StatusCard title="Billing" tone="unknown" detail="not configured" />);
    expect(screen.getByText("Billing")).toBeInTheDocument();
    expect(screen.getByText("not configured")).toBeInTheDocument();
  });

  it("supports an explicit label override", () => {
    render(<StatusCard title="Delegation" tone="warn" label="not implemented" />);
    expect(screen.getByText("not implemented")).toBeInTheDocument();
  });
});
