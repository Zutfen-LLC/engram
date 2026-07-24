# syntax=docker/dockerfile:1
#
# Portal production image (ENG-PORTAL-001A).
#
# Lean: multi-stage Next.js standalone build. Browsers and Playwright OS deps
# are NEVER installed here (alteration 7) — browser tests run from the CI runner.
# No Core API key or database URL is present; only the server-only
# CONTROL_PLANE_URL is expected at runtime.

# ---- deps ----
FROM node:22-bookworm-slim AS deps
WORKDIR /app

# Provision the EXACT pinned pnpm (do not assume corepack). Matches root
# package.json `packageManager`.
RUN npm install -g pnpm@9.12.3

# Copy workspace manifests first for layer caching.
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/portal/package.json apps/portal/package.json
COPY packages/portal-sdk/package.json packages/portal-sdk/package.json
COPY packages/ui/package.json packages/ui/package.json

RUN pnpm install --frozen-lockfile

# ---- builder ----
FROM node:22-bookworm-slim AS builder
WORKDIR /app
RUN npm install -g pnpm@9.12.3
COPY --from=deps /app/node_modules ./node_modules
COPY --from=deps /app/apps/portal/node_modules ./apps/portal/node_modules
COPY --from=deps /app/packages ./packages
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/portal ./apps/portal
COPY packages/portal-sdk ./packages/portal-sdk
COPY packages/ui ./packages/ui

WORKDIR /app/apps/portal
# Build the SDK and UI are consumed as workspace source; Next resolves them.
ENV NEXT_TELEMETRY_DISABLED=1
RUN pnpm exec next build

# ---- runner ----
FROM node:22-bookworm-slim AS runner
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# Non-root user.
RUN addgroup --system --gid 1001 nodejs && \
    adduser --system --uid 1001 --ingroup nodejs nextjs

# Copy the standalone server output + static assets.
COPY --from=builder --chown=nextjs:nodejs /app/apps/portal/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/apps/portal/.next/static ./apps/portal/.next/static
COPY --from=builder --chown=nextjs:nodejs /app/apps/portal/public ./apps/portal/public

USER nextjs
EXPOSE 3000

CMD ["node", "apps/portal/server.js"]
