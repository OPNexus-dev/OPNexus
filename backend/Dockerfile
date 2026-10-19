FROM python:3.12-slim

WORKDIR /app

# pg_dump/pg_restore für das automatische Sicherheits-Backup vor Schema-
# Migrationen (app/migrate.py) — bewusst in der Hauptversion des Servers
# (siehe das Skript für das Warum).
COPY scripts/install_pg_client.sh /tmp/install_pg_client.sh
RUN /tmp/install_pg_client.sh 16 && rm /tmp/install_pg_client.sh

# Management-Overlay (nur der wg-hub-Container nutzt das, siehe app/overlay_hub.py).
RUN apt-get update && apt-get install -y --no-install-recommends wireguard-tools iproute2 iptables \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
