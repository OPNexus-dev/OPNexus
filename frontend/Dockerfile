# deps und builder laufen immer auf der Build-Plattform (nativ schnell, `next build` unter QEMU-Emulation
# hängt); das Ergebnis (.next/standalone) ist reines JavaScript und gilt für jede Zielarchitektur.
FROM --platform=$BUILDPLATFORM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM --platform=$BUILDPLATFORM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:20-alpine AS runner
ARG TARGETARCH
ARG BUILDARCH
WORKDIR /app
ENV NODE_ENV=production
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
# Beim Cross-Build liegen die nativen sharp-Binärdateien der Build-Architektur im Standalone-Ordner; die App nutzt
# next/image nicht, sie wären nur totes Gewicht.
RUN if [ "$TARGETARCH" != "$BUILDARCH" ]; then rm -rf node_modules/@img node_modules/sharp; fi

EXPOSE 3000
CMD ["node", "server.js"]
