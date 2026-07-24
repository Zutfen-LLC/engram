import type { NextConfig } from "next";

/**
 * Portal is a backend-for-frontend. The Control Plane client is server-only,
 * so its package is treated as external to the server bundle (it uses Node APIs
 * and must never be shipped to the browser).
 *
 * No NEXT_PUBLIC_* variable references the internal Control Plane URL, and no
 * Core API key or database URL is ever exposed to the browser.
 */
const nextConfig: NextConfig = {
  reactStrictMode: true,
  serverExternalPackages: ["engram-portal-sdk"],
  // The standalone output is used by the lean production Docker image.
  output: "standalone",
};

export default nextConfig;
