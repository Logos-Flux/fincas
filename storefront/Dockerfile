# Storefront image (docs/deploy-fly.md, "Storefront"). It builds and serves the
# app the way the staging demo does: `qwik build`, the preview SSR bundle,
# then `vite preview` under bun.
#
# VITE_API_BASE_URL is baked into the browser bundle at build time, so it is a
# build arg (fly.toml [build.args]), not a runtime setting. VITE_DEMO_CATALOG
# stays unset: a production build never shows the offline demo catalog.
FROM oven/bun:1.3 AS build
WORKDIR /app
COPY package.json bun.lock ./
RUN bun install --frozen-lockfile
COPY . .
ARG VITE_API_BASE_URL
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
RUN test -n "$VITE_API_BASE_URL" \
    && bun run build \
    && bunx vite build --ssr src/entry.preview.tsx

FROM oven/bun:1.3-slim
WORKDIR /app
ENV NODE_ENV=production PORT=8080
# `vite preview` serves dist/ and the SSR bundle in server/, and needs vite
# itself, so node_modules ships too (as on staging). Owned by the runtime
# user: vite writes its bundled config under node_modules/.vite-temp at start.
COPY --from=build --chown=bun:bun /app /app
USER bun
EXPOSE 8080
CMD ["bun", "./node_modules/vite/bin/vite.js", "preview", "--port", "8080", "--host", "0.0.0.0", "--strictPort"]
