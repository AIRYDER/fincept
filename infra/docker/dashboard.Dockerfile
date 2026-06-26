# dashboard.Dockerfile — Fincept Operator Dashboard (TASK-0903)
#
# Production container for the Next.js operator dashboard. Served by ALB
# path "/" (everything not under /api/*). NO server-side state, NO writes
# to the trading bus. Static asset hash + Next.js standalone output keeps
# the image small.
#
# Build:
#   docker build -t fincept-dashboard:v1.0.0 -f infra/docker/dashboard.Dockerfile .
#
# Local run:
#   docker run --rm -p 3000:3000 \
#     -e NEXT_PUBLIC_API_URL=http://host.docker.internal:8010/api \
#     -e API_INTERNAL_URL=http://host.docker.internal:8010 \
#     fincept-dashboard:local

# ---- Stage 1: deps + build -------------------------------------------------
FROM node:20-alpine AS builder

WORKDIR /build

# Install only deps first so the heavy layer is cached when only source changes.
COPY apps/dashboard/package.json apps/dashboard/pnpm-lock.yaml* ./
RUN corepack enable && corepack prepare pnpm@9.15.0 --activate \
    && pnpm install --frozen-lockfile --prod=false

# Copy the dashboard source and build the Next.js standalone bundle.
COPY apps/dashboard ./
ENV NEXT_TELEMETRY_DISABLED=1
RUN pnpm run build

# ---- Stage 2: minimal runtime ---------------------------------------------
FROM node:20-alpine AS runtime

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000

# Non-root user.
RUN addgroup -S -g 1001 fincept \
    && adduser -S -u 1001 -G fincept -h /app fincept

WORKDIR /app

# Copy only the standalone output + public + static. ~50-80MB instead of 300MB.
COPY --from=builder --chown=fincept:fincept /build/.next/standalone ./
COPY --from=builder --chown=fincept:fincept /build/.next/static ./.next/static
COPY --from=builder --chown=fincept:fincept /build/public ./public

# The dashboard talks to the API two ways:
#   - NEXT_PUBLIC_API_URL: baked into the JS bundle for client-side fetches
#   - API_INTERNAL_URL:    used by Next.js API routes (server-side) for SSR
# Both are injected at task start from ECS task env (NOT secrets).
ENV NEXT_PUBLIC_API_URL="http://localhost:8000/api" \
    API_INTERNAL_URL="http://localhost:8000"

USER fincept

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD wget -q --spider http://localhost:3000/ || exit 1

# server.js is the entry point emitted by Next.js standalone builds.
CMD ["node", "server.js"]