# Optionaler Updater-Dienst (Compose-Profil "updater"). Er enthält Docker-CLI
# und Compose, damit das Webinterface ein Update auslösen kann, ohne dass das
# Backend je den Docker-Socket sieht (siehe server.py und Handbuch).
FROM python:3.12-alpine

RUN apk add --no-cache bash docker-cli docker-cli-compose

COPY opnexus /usr/local/bin/opnexus
COPY server.py /app/server.py
RUN chmod 0755 /usr/local/bin/opnexus

WORKDIR /app
ENV PYTHONUNBUFFERED=1 OPNEXUS_DIR=/project UPDATER_STATE_DIR=/state

HEALTHCHECK --interval=10s --timeout=5s --retries=5 --start-period=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"

CMD ["python", "/app/server.py"]
