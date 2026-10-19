from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    @property
    def allowed_origins(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.frontend_origin.split(",") if o.strip()]

    database_url: str = (
        "postgresql+asyncpg://opnexus:opnexus@postgres:5432/opnexus"
    )
    # ── Schema migrations (app/migrate.py) ────────────────────────────
    # Vor jeder Migration an einer bestehenden Datenbank wird per pg_dump
    # ein Sicherheits-Backup abgelegt (dasselbe Volume wie der
    # postgres-backup-Service). Schlägt es fehl, wird NICHT migriert.
    migration_backup_dir: str = "/backups"
    # Nur für Entwicklung/Notfall: Backup überspringen.
    skip_migration_backup: bool = False
    migration_backup_keep: int = 5

    # ── Update check (app/services/update_check_service.py) ───────────
    # Release-Feed im Format von GitHub/Forgejo `releases/latest`, z. B.
    #   https://api.github.com/repos/<owner>/<repo>/releases/latest
    #   https://git.example.com/api/v1/repos/<owner>/<repo>/releases/latest
    # Leer = die Funktion ist aus: OPNexus macht dann KEINE ausgehende
    # Anfrage. Gesendet wird nur ein GET mit User-Agent "OPNexus/<Version>",
    # keine Instanz- oder Nutzerdaten.
    update_check_url: str = ""
    update_check_interval_hours: int = 12

    # ── Updates aus dem Webinterface (app/services/updater_client.py) ─────
    # Optionaler Updater-Container (Compose-Profil "updater"). Das Backend hat
    # nie Zugriff auf den Docker-Socket; es reicht nur "aktualisieren" /
    # "zurückrollen" an diesen Dienst weiter, authentifiziert mit dem Token,
    # den der Updater in sein Zustands-Volume schreibt.
    updater_url: str = "http://updater:8080"
    updater_token_file: str = "/updater/token"

    # Cookies now carry the session, so this can no longer be a bare "*"
    # (the Fetch spec forbids credentialed requests against a wildcard
    # response) — see main.py's allow_origin_regex. Default only matches
    # localhost/127.0.0.1 on the compose-default frontend port, i.e. local
    # dev out of the box. A real deployment reachable under any other
    # hostname MUST set FRONTEND_ORIGIN instead (see below) — a bare
    # port-only regex would accept a credentialed cross-site request from
    # *any* host serving on that port.
    cors_origin_regex: str = r"^https?://(localhost|127\.0\.0\.1):3100$"
    # Exact origin (scheme://host[:port], no trailing slash) of the
    # deployed frontend, e.g. "https://opnexus.example.com". When set,
    # this replaces cors_origin_regex entirely with an exact match — set
    # it for any non-localhost deployment.
    # Several origins may be given comma-separated, e.g. when OPNexus is
    # reachable both by IP and by name: "https://10.0.0.5,https://opnexus.lan".
    frontend_origin: str = ""
    opnsense_secret_dir: str = "/data/opnsense-secrets"
    # Lab only: allow an http:// endpoint for offsite backups (credentials in the clear).
    allow_insecure_offsite: bool = False
    opnsense_backup_dir: str = "/data/opnsense-backups"
    opnsense_timeout: float = 10.0
    # Firewall API credentials are sent as HTTP Basic auth. Plain http://
    # addresses are therefore refused (on create AND on every request) unless
    # this is switched on — only meant for isolated lab setups.
    allow_insecure_firewall_urls: bool = False

    # ── Auth ──────────────────────────────────────────────────────────
    admin_username: str = "admin"
    # Empty → bootstrap_admin() generates one and logs it once.
    admin_password: str = ""
    session_lifetime: int = 60 * 60 * 24 * 30  # 30 days, seconds
    # No TLS termination in front of this tool by default (see README) —
    # a Secure cookie would just never be sent. Set to true once HTTPS is
    # in place.
    cookie_secure: bool = False
    login_max_attempts: int = 5
    login_ip_max_attempts: int = 20
    login_window_seconds: int = 300
    login_lockout_seconds: int = 300
    # Off by default: request.client.host is the actual TCP peer, safe to
    # trust with no proxy in front. Behind a reverse proxy (see README's
    # "Produktivbetrieb"), that peer is the proxy itself, collapsing every
    # real client onto one IP for the login-throttle bucket — set this to
    # true ONLY when the proxy is trusted to always set/overwrite
    # X-Forwarded-For itself (every proxy setup in the README's example
    # does). Never enable this if the app is reachable directly (no
    # proxy) — any client could then spoof the header to dodge the
    # per-IP lockout entirely.
    trust_proxy_headers: bool = False

    # ── Deploy lockout protection ────────────────────────────────────
    deploy_confirm_window_seconds: int = 180

    # ── Notifications ─────────────────────────────────────────────────
    # Infra-only knobs (deployment concerns); delivery config itself
    # (webhook URL, SMTP settings, per-event toggles, poll interval) is
    # admin-editable via NotificationSettings — see app/models/notification.py.
    notification_secret_dir: str = "/data/notification-secrets"
    totp_secret_dir: str = "/data/totp-secrets"
    # Floor against an admin setting an absurdly low poll interval, which
    # would hammer both real firewalls with live status/cert/rule-drift
    # calls every cycle.
    notification_poll_min_interval_seconds: int = 60
    notification_smtp_timeout_seconds: float = 10.0
    # SMTP over SSL/STARTTLS always verifies the server certificate and host
    # name against the container's trust store. A mail server with a private
    # CA needs that CA's PEM file mounted and referenced here.
    smtp_ca_file: str = ""
    # Sending SMTP credentials over an unencrypted connection (security "none")
    # is refused unless this is switched on explicitly.
    allow_insecure_smtp_auth: bool = False

    # ── Log ingest ────────────────────────────────────────────────────
    # Floor against an admin setting an absurdly low poll interval — same
    # rationale as notification_poll_min_interval_seconds, but for the
    # digest-based firewall-log ingest loop (see log_ingest_service.py).
    # A single GET per enabled host per cycle, so a lower floor than
    # notifications' is acceptable — this is the same call the log page
    # already makes on-demand today, just automated.
    log_poll_min_interval_seconds: int = 10

    # ── IPsec VPN management ────────────────────────────────────────────
    # Pre-shared-key secrets, one file per tunnel id — same convention as
    # opnsense_secret_dir above, see ipsec_psk_store.py.
    ipsec_psk_secret_dir: str = "/data/ipsec-psk-secrets"

    # ── OpenVPN instance management ─────────────────────────────────────
    # Static-key (tls-auth/tls-crypt hardening) key material, one file per
    # instance id — same convention as ipsec_psk_secret_dir above, see
    # openvpn_key_store.py.
    openvpn_key_secret_dir: str = "/data/openvpn-key-secrets"

    # ── WireGuard tunnel management ─────────────────────────────────────
    # Server private key + optional per-peer pre-shared key, both stored
    # one-file-per-tunnel-id under this directory — same convention as
    # ipsec_psk_secret_dir/openvpn_key_secret_dir above, see
    # wireguard_key_store.py.
    wireguard_key_secret_dir: str = "/data/wireguard-key-secrets"

    # ── Resource monitoring ──────────────────────────────────────────────
    # Fixed interval, no admin-editable settings row/UI for this — v1
    # deliberately kept minimal (see resource_monitor_service.py).
    resource_poll_interval_seconds: int = 60
    resource_sample_retention_days: int = 30

    # ── Trust-Store + ACME certificate management ────────────────────────
    # Private-key material for action="import" ManagedCertificate rows,
    # one file per entity id — same convention as ipsec_psk_secret_dir
    # above, see cert_key_store.py.
    cert_key_secret_dir: str = "/data/cert-key-secrets"
    # External-Account-Binding HMAC for AcmeAccount rows, one file per
    # entity id — see acme_secret_store.py.
    acme_secret_dir: str = "/data/acme-secrets"

    model_config = SettingsConfigDict(case_sensitive=False)


settings = Settings()
