# Optionaler Updater-Dienst (Compose-Profil "updater"). Er enthält Docker-CLI
# und Compose, damit das Webinterface ein Update auslösen kann, ohne dass das
# Backend je den Docker-Socket sieht (siehe server.py und Handbuch).
FROM python:3.12-alpine

RUN apk add --no-cache bash docker-cli docker-cli-compose

# cosign prüft vor einem Update die Signatur der neuen Images (cosign.pub liegt im gemounteten
# Projektverzeichnis, siehe scripts/opnexus). Version und Prüfsummen sind fest eingetragen.
ARG COSIGN_VERSION=v3.1.3
ARG TARGETARCH
RUN set -e; arch="${TARGETARCH:-amd64}"; \
    case "$arch" in \
      amd64) sha=4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71 ;; \
      arm64) sha=c5d324e091826b0d7a78eb16fef316450b4eb9aaec045611c08ba06f5e73220a ;; \
      *) echo "Architektur $arch nicht unterstützt" >&2; exit 1 ;; \
    esac; \
    python -c "import urllib.request; urllib.request.urlretrieve('https://github.com/sigstore/cosign/releases/download/${COSIGN_VERSION}/cosign-linux-${arch}', '/usr/local/bin/cosign')"; \
    echo "$sha  /usr/local/bin/cosign" | sha256sum -c -; \
    chmod 0755 /usr/local/bin/cosign

COPY opnexus /usr/local/bin/opnexus
COPY server.py /app/server.py
RUN chmod 0755 /usr/local/bin/opnexus

WORKDIR /app
ENV PYTHONUNBUFFERED=1 OPNEXUS_DIR=/project UPDATER_STATE_DIR=/state

HEALTHCHECK --interval=10s --timeout=5s --retries=5 --start-period=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"

CMD ["python", "/app/server.py"]
