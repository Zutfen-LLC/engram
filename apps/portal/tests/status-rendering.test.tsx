import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StatusCard } from "engram-ui";

/**
 * The home page renders Control Plane reachable/unreachable states. We assert
 * the StatusCard contract directly (the page composes these) so the test stays
 * free of the Next server runtime.
 */
describe("Portal status rendering", () => {
  it("renders a reachable Control Plane state", () => {
    render(<StatusCard title="Control Plane" tone="ok" detail="reachable" />);
    expect(screen.getByText("Available")).toBeInTheDocument();
    expect(screen.getByText("reachable")).toBeInTheDocument();
  });

  it("renders a bounded unavailable Control Plane state", () => {
    render(<StatusCard title="Control Plane" tone="err" detail="unreachable" />);
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
    expect(screen.getByText("unreachable")).toBeInTheDocument();
  });

  it("marks identity/delegation/billing as unavailable/not-configured", () => {
    render(
      <StatusCard title="Identity" tone="unknown" label="not configured" />,
    );
    expect(screen.getByText("not configured")).toBeInTheDocument();
  });

  it("does not render fake login or management actions", () => {
    const { container } = render(
      <StatusCard title="Control Plane" tone="ok" detail="reachable" />,
    );
    const html = container.textContent ?? "";
    for (const forbidden of ["Sign in", "Log in", "Sign up", "Create agent", "Billing", "Manage"]) {
      expect(html).not.toContain(forbidden);
    }
  });
});
