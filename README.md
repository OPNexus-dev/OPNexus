# OPNexus

**All your OPNsense firewalls. One console.**
Central management for multiple [OPNsense](https://opnsense.org) firewalls: rules, aliases, NAT, VPN, certificates, logs and updates — rolled out through the OPNsense API with preview, confirmation and automatic rollback.

[Website](https://opnexus.dev) · [Changelog](CHANGELOG.md) · [Handbook (German)](docs/HANDBUCH.md) · [Deutsch](README.de.md)

![Central firewall rules](website/assets/screenshots/en/rules-light-desktop.webp)

> **Status:** `1.11.0-rc1` is the current public release candidate (it replaces the `1.10.x` candidates). It has been developed against real OPNsense instances, but a fresh install from the published images has not been field-tested yet — please expect rough edges and report them.

## What it does

- **Fleet & high availability** — live status of all hosts, CARP roles side by side, event history, alerts for split-brain or a missing MASTER, maintenance mode with an optional auto-revert timer.
- **Rules, aliases & NAT** — maintained centrally, rolled out to single hosts or device groups; port forward, 1:1 and outbound NAT with drift detection between cluster nodes.
- **Safe changes** — every change goes *preview → deploy → confirm*; without your confirmation inside the time window OPNexus rolls it back.
- **Hierarchical policies** — pre/post rulebases with inheritance across group trees.
- **VPN** — IPsec site-to-site, OpenVPN and WireGuard, defined once and rolled out.
- **Certificates** — trust store for all firewalls and ACME / Let's Encrypt issuance.
- **Logs & audit** — central log search with correlation, dashboards and a complete deploy audit trail.
- **Update center** — firmware updates for the fleet, aware of HA order and maintenance mode.
- **Access** — HTTPS with pinned firewall certificates, optional two-factor sign-in, roles (admin / read-only), keyboard operation, dark mode, German and English UI.

![Cluster view](website/assets/screenshots/en/cluster-light-desktop.webp)

All screenshots show fictitious sample data.

## Quick start

Requires Docker with the Compose plugin.

```bash
git clone https://git.opnexus.dev/opnexus/opnexus.git
cd opnexus
cp .env.example .env        # set POSTGRES_PASSWORD (and ADMIN_PASSWORD)
docker compose up -d --build
```

The UI is then available at `http://localhost:3100`. Without `ADMIN_PASSWORD` a random admin password is generated on first start and printed **once** in the backend log:

```bash
docker compose logs backend | grep -A5 "generated password"
```

For access from other machines and for production use with TLS (bundled Caddy proxy), see the [handbook](docs/HANDBUCH.md#zugriff-von-außerhalb-dieser-vm--produktivbetrieb-mit-tls) (German).

**Prebuilt images** (amd64) are published to this Gitea's registry. In `.env`:

```bash
OPNEXUS_IMAGE_REPO=git.opnexus.dev/opnexus
OPNEXUS_VERSION=1.11.0-rc1
```

```bash
docker compose pull && docker compose up -d --no-build
```

`scripts/opnexus update` / `rollback` then update the installation (pull first, database backup before migrations, automatic rollback if the services do not become healthy).

## Privacy

OPNexus is self-hosted and has no telemetry. It only connects to your firewalls and to notification targets you configure yourself. The optional new-version notice (`UPDATE_CHECK_URL=https://updates.opnexus.dev/latest.json`) is **off** unless you set it; when on, it makes one plain `GET` at most every 12 hours with the user agent `OPNexus/<version>` — no instance or user data.

## License

OPNexus is **source-available** under the [PolyForm Shield License 1.0.0](LICENSE) — it is *not* open source in the OSI sense. You may download, read, modify and use it, including inside a company for its own firewalls. You may not use it to provide a product that competes with OPNexus (for example reselling it or offering it as a hosted service). The license text is authoritative.

## Security

Please report vulnerabilities privately to **security@opnexus.dev**, see [SECURITY.md](SECURITY.md).

---

OPNexus is an independent project and is not affiliated with Deciso B.V. or the OPNsense® project. OPNsense® is a trademark of Deciso B.V.
