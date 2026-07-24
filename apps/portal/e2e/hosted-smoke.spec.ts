import { expect, test } from "@playwright/test";

/**
 * Hosted-stack browser smoke (ENG-PORTAL-001A).
 *
 * Boots nothing itself — expects the hosted Compose overlay to be running with
 * Portal on :3000 and the Control Plane on :8100. Run from the CI runner.
 */
test.describe("Portal hosted smoke", () => {
  test("status page loads and renders honest unavailability", async ({ page }) => {
    await page.goto("/");
    // Branding present.
    await expect(page.getByRole("heading", { name: "Engram Portal" })).toBeVisible();

    // The scaffold label is visible. Use body text (the label is split across a
    // <strong> tag, so getByText is fragile here).
    await expect(page.locator("body")).toContainText(/scaffolding/i);

    // Identity/delegation/billing are visibly unavailable, never by color alone.
    await expect(page.getByText("not configured").first()).toBeVisible();
    await expect(page.getByText("not implemented").first()).toBeVisible();

    // No fake login/management actions.
    const body = await page.locator("body").innerText();
    for (const forbidden of ["Sign in", "Log in", "Sign up", "Create agent"]) {
      expect(body).not.toContain(forbidden);
    }
  });

  test("no secret or internal database address appears in rendered HTML", async ({ page }) => {
    const responses: string[] = [];
    page.on("response", (r) => responses.push(r.url()));
    await page.goto("/");
    const html = await page.content();
    // The internal control-plane host and any DB URL must never leak to HTML.
    expect(html).not.toMatch(/control-plane:8100/i);
    expect(html).not.toMatch(/postgresql:\/\//i);
    expect(html).not.toMatch(/engram_control_app/i);
    expect(html).not.toMatch(/ENGRAM_OPENAI_API_KEY/i);
    // No Core API key material.
    expect(html).not.toMatch(/eng_[a-z0-9]{20,}/i);
  });

  test("is keyboard accessible", async ({ page }) => {
    await page.goto("/");
    // The skip link becomes focusable and reachable.
    await page.keyboard.press("Tab");
    const active = await page.evaluate(() => document.activeElement?.textContent ?? "");
    expect(active.toLowerCase()).toContain("skip");
  });
});
