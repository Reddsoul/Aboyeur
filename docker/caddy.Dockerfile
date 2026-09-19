# Caddy image with the built SPA baked in at /srv. Multi-stage: the node
# build never reaches the runtime image.
FROM node:22-bookworm-slim AS frontend
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend ./
ARG VITE_SENTRY_DSN=
ENV VITE_SENTRY_DSN=$VITE_SENTRY_DSN
RUN npm run build

FROM caddy:2-alpine
COPY --from=frontend /src/dist /srv
