# OPNexus – Handbook

OPNexus manages several [OPNsense](https://opnsense.org) firewalls in one place: rules, aliases, NAT, VPN, certificates, network, logs and updates. All
changes go through the OPNsense API and follow the same flow: **preview,
deploy, confirm**. If the confirmation does not arrive, OPNexus restores the
previous state.

This handbook is for everyone who installs and operates OPNexus.
A short version is in the [README](../README.md), the changes per version in the
[changelog](../CHANGELOG.md). A German edition is available as [HANDBUCH.md](HANDBUCH.md); the English handbook is the reference.

## Contents

1. [Getting started](#getting-started)
2. [Network access and TLS](#network-access-and-tls)
3. [Configuration (`.env`)](#configuration-env)
4. [Users, roles and login](#users-roles-and-login)
5. [Connecting firewalls](#connecting-firewalls)
6. [Deploying rules safely](#deploying-rules-safely)
7. [Device groups and hierarchical policies](#device-groups-and-hierarchical-policies)
8. [Aliases and NAT](#aliases-and-nat)
9. [Network and host system](#network-and-host-system)
10. [VPN: IPsec, OpenVPN, WireGuard](#vpn-ipsec-openvpn-wireguard)
11. [Certificates and ACME](#certificates-and-acme)
12. [HA clusters and CARP](#ha-clusters-and-carp)
13. [Maintenance and firewall updates](#maintenance-and-firewall-updates)
14. [Monitoring: dashboard, logs, audit, notifications](#monitoring-dashboard-logs-audit-notifications)
15. [Automation: API tokens, Prometheus, AI assistants](#automation-api-tokens-prometheus-ai-assistants)
16. [Operating OPNexus](#operating-opnexus)
17. [Security](#security)
18. [Troubleshooting](#troubleshooting)
19. [Limits](#limits)
20. [Usage, language, accessibility](#usage-language-accessibility)
21. [License and privacy](#license-and-privacy)

---

## Getting started

### Requirements

- A server with **Docker and the Compose plugin**. OPNexus runs as a
  Compose stack (database, backend, frontend, TLS proxy).
- One or more **OPNsense firewalls** whose web interface is reachable over HTTPS from the OPNexus server.
  OPNexus uses the REST API of the OPNsense core; nothing needs to be installed on
  the firewalls. Only ACME issuance needs the `os-acme-client` plugin there.
- One **API key** per firewall (see [Connecting firewalls](#connecting-firewalls)).

### Installation from the release bundle

This is the recommended way, on Linux, macOS and Windows (WSL). The bundle
(`opnexus-<version>.tar.gz` on the release page) contains a ready-made Compose file with the
signed release images (default registry: `ghcr.io/opnexus-dev`), a template for the
`.env`, the `opnexus` tool and a short guide (`INSTALL.txt`). Nothing is built on your machine;
`docker compose up -d` pulls the images itself the first time.

```bash
curl -LO https://github.com/OPNexus-dev/OPNexus/releases/download/v<version>/opnexus-<version>.tar.gz
tar xzf opnexus-<version>.tar.gz && cd opnexus-<version>
install -m 600 .env.example .env     # readable only by you
sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -hex 24)|" .env   # your own database password
docker compose up -d
./opnexus password                   # shows the admin password if ADMIN_PASSWORD was empty
```

Then open `https://<server-ip>` (locally `https://localhost`); the setup assistant guides you through the first steps.
You do not have to remember the database password, it is in the `.env`. More settings (addresses, TLS) are described under
[Configuration (`.env`)](#configuration-env).

**Good to know**

- `POSTGRES_PASSWORD` (likewise `POSTGRES_DB` and `POSTGRES_USER`) applies **only on the very first start** with an empty
  database volume. The volume keeps its password. If the password in the `.env` changes later, the database
  rejects the backend, and backend and `postgres-backup` restart in a loop (the backend log names the reason). Either put the
  original password back or, if there is no data yet, remove the volumes with `docker compose down -v`
  (**deletes the database and all credentials**).
- The Compose project name is always `opnexus`, **no matter which folder** you install in. A second folder (for example a freshly
  unpacked bundle next to the old one) therefore uses the same containers and volumes. For a truly separate second instance
  see [Second instance on the same machine](#second-instance-on-the-same-machine).
- The first start creates the database schema; on slow machines (Raspberry Pi, small VM) this can take several minutes.
  `docker compose ps` shows "starting" until the backend is healthy.

### Installation from source

Only for development or if you want to build the images yourself. Building the frontend needs several GB of free
memory and takes long on small machines.

```bash
git clone https://git.opnexus.dev/opnexus/opnexus.git && cd opnexus
install -m 600 .env.example .env
sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -hex 24)|" .env
docker compose up -d --build
scripts/opnexus password
```

Interface: `http://localhost:3100`. You can also use the ready-made images from a checkout: in
the `.env` set `OPNEXUS_IMAGE_REPO=ghcr.io/opnexus-dev` and `OPNEXUS_VERSION=<version>`,
then `docker compose pull && docker compose up -d --no-build`.

### Installation on Windows and macOS

On Windows and macOS the same stack runs in **Docker Desktop** (Windows with WSL 2): install Docker Desktop,
unpack the release bundle, create the `.env` (enter `POSTGRES_PASSWORD` and `ADMIN_PASSWORD`)
and run `docker compose up -d`; the interface opens at
`https://localhost`. The detailed guides with certificate import, access from the network
(Windows firewall), updates and troubleshooting are on the website:
<https://opnexus.dev/en/docs/windows/> and <https://opnexus.dev/en/docs/macos/>. OPNexus is developed and tested on Linux; for
permanent operation a Linux server or a Linux VM is better, because Docker Desktop only starts after
login and pauses when the machine sleeps.

### Verifying images (signature)

The release images are signed with [cosign](https://github.com/sigstore/cosign) (from 1.23.9).
This lets you check that an image really comes from OPNexus and has not been
modified since it was built. The public key is in the repository and in the
release bundle as `cosign.pub`:

```bash
cosign verify --key cosign.pub --insecure-ignore-tlog=true \
  ghcr.io/opnexus-dev/opnexus-backend:<version>
```

The images are available in **two registries**: `ghcr.io/opnexus-dev` (default for new installations)
and `git.opnexus.dev/opnexus` (Gitea, fully supported). They are built only once and copied,
digest-identical and including the signature, into both registries; the same check with
`git.opnexus.dev/opnexus/opnexus-backend:<version>` leads to the same digest. The check applies to the
digest, not the tag, and is the same in both registries.

The same applies to `opnexus-frontend`, `opnexus-proxy` and `opnexus-updater`. A
successful check prints the signature as JSON; with a foreign or missing
key the command ends with an error. `--insecure-ignore-tlog=true` is intended here:
OPNexus does not upload signatures to the public transparency log, so that the check also works in
air-gapped networks. The signature covers the content (digest) of the image, not
the tag.

`opnexus update` verifies the signatures itself before it switches anything, as soon as `cosign` is
installed and `cosign.pub` sits next to the `docker-compose.yml` (`cosign.pub` is already in the bundle,
you have to install `cosign` yourself). The updater container for one-click updates
ships `cosign` and therefore also verifies, as long as `cosign.pub` is in the project directory. If the check fails, the
update aborts and nothing was changed. If `cosign` is missing, `opnexus update` says so and
continues without verification.

### First start and password

Without `ADMIN_PASSWORD` in the `.env`, OPNexus generates a
random password for the user `ADMIN_USERNAME` (default `admin`) on the first start and
writes it **once** to the backend log. Simpler:

```bash
./opnexus password        # in a repo checkout: scripts/opnexus password
```

The command sets a new random password, shows it once and ends all
sessions of the account. It also works later, if the password was forgotten.
It only asks for confirmation if someone has already logged in
(`--yes` skips the question, a user name as argument selects a different account).
An old password cannot be displayed; only an
Argon2id hash is stored.

Then open the interface: `https://<server-ip>` (locally also
`http://localhost:3100`). At first the browser shows a certificate warning, see
[Network access and TLS](#network-access-and-tls).

### The setup assistant

After the first login the **setup assistant** opens by itself,
as long as no firewall is connected yet. It guides you through the essentials in four
steps:

1. **Welcome:** what the assistant does, language (German/English). It only reads
   from the firewall; nothing is changed there until you deploy something yourself later.
2. **Account:** set your own password and set up two-factor login.
   Both are optional but recommended.
3. **Firewall:** instructions for the API key in OPNsense, then enter name, address,
   key and secret, fetch the certificate and confirm the fingerprint. After creation the step
   immediately shows whether the firewall responds and, if "offline", what to check. You add further firewalls, including the
   nodes of an HA pair, one after another in the same step.
4. **Done:** summary and hints on what makes sense next.

"Skip assistant" is remembered for your user. You can reopen it at any time via **System › Setup**, the settings or the
quick search. Read-only users are not offered it.

---

## Network access and TLS

The bundled **Caddy proxy** is the only service listening on the network
(ports 80 and 443; port 80 redirects to HTTPS). Backend and frontend are bound to
`127.0.0.1` only. Browser and API talk to the same host, so
IP address and host name work equally well.

**Certificates without DNS.** The proxy issues certificates from its own internal
CA, also for plain IP addresses. Download the root CA once from
`https://<address>/opnexus-ca.crt` and import it into the browser or
system (Firefox: Settings › Certificates › Authorities;
Windows/macOS: install as a trusted root certification authority).
Until then the browser shows a warning; the connection is encrypted nonetheless.

**Verify the root CA before importing.** Someone sitting between browser and server could
slip in their own CA. Therefore compare the fingerprint on the server with that of the downloaded file before importing:

```bash
# on the server
docker compose exec -T proxy cat /data/caddy/pki/authorities/local/root.crt \
  | openssl x509 -noout -fingerprint -sha256
# on the client, for the downloaded file
openssl x509 -in opnexus-root-ca.crt -noout -fingerprint -sha256
```

Import only if the SHA-256 fingerprints are identical, or distribute the CA through
your company/MDM tooling.

**Setting addresses.** In the `.env` you enter all addresses under which
OPNexus should be reachable:

```bash
OPNEXUS_SITES=10.0.0.5, opnexus.lan     # all addresses (IP and/or names)
OPNEXUS_DEFAULT_SNI=10.0.0.5            # the primary one; browsers send no name for a plain IP
FRONTEND_ORIGIN=https://10.0.0.5,https://opnexus.lan
COOKIE_SECURE=true
TRUST_PROXY_HEADERS=true
```

Then `docker compose up -d`. A name without DNS needs an entry in the hosts file on the client. `TRUST_PROXY_HEADERS=true` may only be set if
a proxy sits in front that sets `X-Forwarded-For` itself; the bundled proxy does.
Otherwise any client could bypass the login lockout with a forged header.

**Back up:** the key of the internal CA lives in the volume `caddy_data`. If it is
lost, a new CA is created and all clients have to import it again.

**Your own reverse proxy.** Instead of the bundled proxy you can use your own
(e.g. Caddy with a Let's Encrypt certificate). Backend (`127.0.0.1:8100`)
and frontend (`127.0.0.1:3100`) then stay behind it:

```
opnexus.example.com {
    reverse_proxy /api/* localhost:8100
    reverse_proxy localhost:3100
}
```

In the `.env`: `FRONTEND_ORIGIN=https://opnexus.example.com`, `COOKIE_SECURE=true`,
`TRUST_PROXY_HEADERS=true`.

**Without TLS (only for trusted test networks).** `BIND_ADDRESS=0.0.0.0` in the
`.env` publishes frontend (3100) and backend (8100) on the network,
`FRONTEND_ORIGIN=http://<ip>:3100` allows access. The session cookie then
travels unencrypted over the network; this is not suitable for an untrusted network or the
internet.

---

## Operating settings in the web interface

Under **Settings › Operating settings** you configure what used to require the server's `.env`:

| Area | Settings |
|---|---|
| Login and sessions | Session lifetime, failed attempts per account and per sender, time window, lockout duration |
| Deployment and firewall access | Confirmation window when deploying, time limit for firewall requests, backups before updates |
| Measurements and retention | Measurement interval for system load, retention of measurements, rule-history interval, interval of the version check |
| External monitoring and mail | Heartbeat address, CA of the mail server (paste PEM), time limit for sending mail |

Each field shows its default and allowed range; "Reset to default" undoes a change. Changes
apply immediately (in a second backend after 15 seconds at the latest) and appear in the audit log; the heartbeat
address and the CA only as "set". Only admins may change them.

If a value is **in the `.env`**, that value still applies; the field is then locked and shows "from the server". This keeps
automatically provisioned servers controllable. Deliberately **only** in the `.env`: the addresses and TLS of OPNexus
itself (a wrong value would lock you out of exactly this interface), database access, the first admin account,
the additional services (`COMPOSE_PROFILES`) and the escape hatches (`ALLOW_INSECURE_*`, `OPNEXUS_RUN_AS_ROOT`).

## Configuration (`.env`)

All settings are in the `.env` next to the `docker-compose.yml`. After a
change run `docker compose up -d`.

| Variable | Meaning |
| --- | --- |
| `POSTGRES_PASSWORD` | **Required.** Database password. Startup refuses without a value. |
| `POSTGRES_DB`, `POSTGRES_USER` | Database and user name (default `opnexus`). Only effective on the very first start of an empty volume, see [Database names](#database-names). |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | First account. Without a password one is generated. Only applies when the first account is created. |
| `OPNEXUS_SITES`, `OPNEXUS_DEFAULT_SNI` | Addresses for the TLS proxy, see above. |
| `PROXY_BIND_ADDRESS`, `PROXY_HTTPS_PORT`, `PROXY_HTTP_PORT` | Interface and ports of the proxy (default `0.0.0.0`, 443, 80). |
| `FRONTEND_ORIGIN` | Exact origins (`https://…`, comma-separated) from which API requests may come. Without a value the backend accepts only `localhost`. |
| `COOKIE_SECURE` | `true` as soon as OPNexus is used only over HTTPS. |
| `TRUST_PROXY_HEADERS` | `true` only behind a proxy that sets `X-Forwarded-For`. |
| `BIND_ADDRESS` | Interface for backend and frontend (default `127.0.0.1`). |
| `BACKEND_PORT`, `FRONTEND_PORT` | Host ports (default 8100 and 3100), e.g. for a second instance. |
| `OPNEXUS_IMAGE_REPO`, `OPNEXUS_VERSION` | Registry and version of the release images. `OPNEXUS_VERSION` is the "track", see [Updates](#updating-opnexus). |
| `UPDATE_CHECK_URL`, `UPDATE_CHECK_INTERVAL_HOURS` | Notice about new versions, **off** by default. Both are also available in the web interface (version check and operating settings). The address in the `.env` takes precedence; the interval there is only the default that the web interface can override. |
| `COMPOSE_PROFILES=updater` | Enables the optional updater for one-click updates. |
| `HEARTBEAT_URL` | Heartbeat address, see [Noticing an OPNexus outage](#noticing-an-opnexus-outage-heartbeat). Easier in the web interface (operating settings); a value here takes precedence. |
| `OPNEXUS_RUN_AS_ROOT` | `1` lets the backend run as root as in older versions. Normally it runs as an unprivileged user (uid 10001) and switches its data volumes to that user on start. Only for special cases, such as data directories that cannot be switched. |
| `ALLOW_INSECURE_FIREWALL_URLS` | Allows `http://` addresses for firewalls. Only for isolated labs. |
| `SMTP_CA_FILE`, `ALLOW_INSECURE_SMTP_AUTH` | Own CA for the mail server (simpler: paste the PEM in the operating settings; the file here takes precedence) or SMTP login without encryption (stays blocked by default). |

---

## Users, roles and login

**Roles.** There are three:

| Role | May |
|---|---|
| `admin` | everything, including users, settings, creating/deleting hosts, OPNexus update, raw download of configuration backups |
| `operator` | deploy and operate: rules, aliases, NAT, VPN, certificates, maintenance, backups, security check. **Not:** users, settings (notifications, backups, audit export, logs), creating/changing/deleting hosts, OPNexus update, downloading raw backups |
| `read_only` | sees everything, changes nothing |

A read-only user cannot change anything: all write actions are disabled in the
interface and rejected by the backend with 403. An operator also gets 403 on the
admin actions ("requires the Admin role"); the attempt is recorded in the
audit log. Only your own password as well as colour scheme and language remain changeable for every role.

**Single sign-on (OIDC).** Under Settings › Single sign-on (OIDC) you connect an
identity provider (Authentik, Keycloak, Entra ID, Google, …). The login page then shows an
additional button ("Sign in with …"). At the provider you need a client with the
*Authorization Code* flow (OPNexus additionally uses PKCE) and enter the displayed
redirect URI there (`https://<your-opnexus>/api/auth/oidc/callback`). In OPNexus you enter
issuer URL, client ID, client secret and the **group names** that map to the roles Admin, Operator
and Read-only. "Test provider" checks discovery and signing keys.

- **Role from groups, at every login:** the highest matching role applies; whoever is in none of the groups
  does not get in (and no account is created). If someone is removed from the group, access ends
  at the next login (running SSO sessions end after 12 hours at the latest; people blocked at the provider
  should therefore also be deleted in the user list).
- **Accounts:** SSO users are created at their first login (marked "SSO"). Role and password
  change only at the provider; the provider handles two-factor login. An SSO account is never linked with
  a local account of the same name: in case of a name conflict it gets a suffix (`~sso`).
- **Local accounts remain:** login with user name and password keeps working, e.g. for an
  emergency if the provider fails.
- **Verified** are the signature (asymmetric algorithms only, never `none` or HMAC), issuer, audience, expiry
  and nonce of the ID token; every login attempt (state) is valid exactly once and for 10 minutes and is bound by a cookie (with HTTPS `__Host-opnexus-oidc-state`) to the browser that started it; a call without this cookie always fails. Groups come from the
  ID token, alternatively from the userinfo endpoint. Issuer and endpoints must be `https://`. This is
  tested against an imitated provider (including forged tokens) and against real servers of Keycloak 26 and
  Authelia 4.39 (login, roles from groups, role removal, intercepted callback); other providers: report what does not work with yours.
  The default scopes are `openid profile email`; the groups must arrive as a claim in the ID token or userinfo
  (Keycloak: "Group Membership" mapper on the client; Entra ID: optional groups claim). Providers that only send groups
  on request (e.g. Okta, Authelia) additionally need the scope `groups`.

**Separation of duties.** Settings › Separation of duties: if you switch off "Operators may deploy changes
themselves", operators only prepare changes (create, edit, preview), and **only an admin
deploys**: deploying, removing or rolling back a deployment, running or scheduling maintenance,
applying network changes. The rejection ("administrator only") appears in the audit log. This is a two-person step
(preparing and deploying by different people), not an approval queue.

**User management** (System › Users, admins only): create users, change the role,
reset the password, reset another user's two-factor login,
delete. You cannot delete yourself and cannot demote yourself from being the last admin. **Only admins see the user list** (`GET /api/users`); every other role,
including a token, sees only its own account.

**Login.**
- Several failed attempts (default: 5 in 5 minutes, per user and IP) lock the
  login for five minutes.
- A session lasts 30 days. Changing the password ends all sessions of the account.
- After login a notice shows time and IP of the **previous**
  login, failed attempts since then and the end of the current session.
  If something looks wrong, change the password.

**Two-factor login** (optional, per user): Settings › Two-factor
login. Scan the QR code with an authenticator app, confirm the code and keep the
10 single-use recovery codes safe. The password is requested again during
setup. A code can be used only once; wrong
codes count towards the login lockout.

**Lost access?**
- Password: `./opnexus password [user]`, see [First start](#first-start-and-password).
- Two-factor, if the recovery codes are gone too: another admin
  resets it in the user list; for the last admin, on the server run
  `docker compose exec backend python -m app.reset_2fa <username>`.

---

## Connecting firewalls

### Creating an API key in OPNsense

1. In OPNsense open **System › Access › Users**.
2. Create a dedicated user (e.g. `opnexus`) or edit an existing one.
3. On the user, click the plus under **API keys**. The browser downloads a file
   `apikey.txt`. The key is behind `key=` and the secret
   behind `secret=`. The secret exists only in this file.
4. Give the user sufficient privileges, most simply via the group `admin`.
   Restricted privileges work as well: which privileges each function needs is listed in
   [OPNsense privileges for the API user](API-RECHTE.md). Without a required privilege exactly that function fails.

### Adding a host

Under **Overview › Hosts** (or in the setup assistant) you create a
firewall: name, address (`https://…`, with the port if it differs), API key,
API secret, optionally cluster name and policy group.

- **HA pairs:** enter both nodes with their **own management addresses**,
  not with the shared CARP address. Rules have to reach each node individually, and through the CARP address only the currently active
  master would be reachable.
  Give both nodes the same **cluster name**, then OPNexus groups them.
- **API secrets** are never stored in the database but as a file (mode `0600`)
  in a dedicated volume.
- `http://` addresses are rejected because the credentials are sent via HTTP basic auth
  (exception: `ALLOW_INSECURE_FIREWALL_URLS=true` for labs).
- **One address, one host:** OPNexus rejects entering the same address twice
  (upper/lower case and a trailing `/` do not count); it would double every rollout,
  alert and backup.
- **Connection check:** the hosts page and the setup assistant query the
  firewall once with the entered credentials when creating it. If that fails,
  **nothing is saved**, and the message names the reason (key rejected,
  insufficient privileges, certificate mismatch, no connection). **Create anyway** saves
  the host without the check, for example for a firewall behind NAT whose tunnel is not
  up yet. Through the API the check can be enabled with `"verify_connection": true`
  (off by default so that scripts can also register unreachable firewalls).

### Pinning the certificate

Firewalls usually have a self-signed certificate whose name does not match the
address. Instead of switching off verification, OPNexus pins the certificate:

1. When creating, or under Hosts › Edit, click **Fetch certificate**.
2. Compare the displayed SHA-256 fingerprint with the one in OPNsense
   (System › Trust › Certificates).
3. If it matches, confirm.

From then on OPNexus trusts **only** this certificate before credentials are
sent, regardless of whether the firewall is reached by IP or name.
If the firewall renews its certificate, the host reports "Certificate does not match the
pinned one". Then fetch and confirm again. The fingerprint comparison
is the step that secures pinning.

### Managing

- **Host list:** status (online/offline/disabled), CARP role, cluster,
  policy group. **Open in OPNsense** jumps to the firewall's web interface.
- **Host detail page** (click the name): status, centrally managed and
  **live-read from the firewall** rules (including hand-made ones),
  configuration backups, services, system load and maintenance.
- **Deleting** requires the **host name**: in the interface in the confirmation dialog, through
  the API as `DELETE /api/hosts/{id}?confirm_name=<name>` (otherwise `409`). When deleting,
  the stored credentials are lost immediately.
- **Disabling** takes a host out of rollouts and queries without deleting it.
  A host to which rules, aliases or NAT are still deployed cannot be
  deleted: otherwise OPNexus would lose track of what is there. Remove
  the deployments first (delete the rule or clean up the former target under "Preview & push").

### Firewalls behind NAT (management tunnel)

OPNexus must be able to reach a firewall's API. If the firewall has no public address (behind NAT,
CGNAT, mobile router), you build a WireGuard hub on the OPNexus server with the **management tunnel** (System › Management tunnel):
the firewall dials in **outwards** there, and OPNexus reaches
its API through the tunnel. Nothing on the firewall has to be reachable from the internet. The tunnel is
meant only for management; user traffic does not run through it.

**Prerequisites:** the hub is an optional container. In `.env` set `COMPOSE_PROFILES=overlay` (comma-separated with
several profiles), run `docker compose up -d`, and the UDP port `OVERLAY_PORT` (default 51820)
of the server must be reachable from the internet (port forwarding on the router of the OPNexus server). The hub
runs with the backend's image and needs only the `NET_ADMIN` capability for the WireGuard interface;
the Linux kernel module `wireguard` must be present on the server (any current kernel).

**Setting up:** switch it on in the management tunnel, enter the server's public address (DNS name or IP),
save. Then per firewall **"Add firewall"**: OPNexus assigns a tunnel address (e.g.
`10.99.0.2`) and a pre-shared key and shows instructions with copyable values:

1. In OPNsense (VPN › WireGuard) create a **local instance**, let it generate a key pair, enter the
   tunnel address.
2. Enter the **hub as peer** (public key, pre-shared key, endpoint, allowed IP
   `10.99.0.1/32`, keepalive 25).
3. A **firewall rule** on the WireGuard interface group: TCP from the hub address to "This
   firewall", port of the web interface (443).
4. Enter the new instance's **public key** under "Key / host" in OPNexus.
5. Create the firewall as a **host** as usual, but with the tunnel address (`https://10.99.0.2`). The
   status in the management tunnel changes to "connected".

From then on everything works as with any other firewall (rules, maintenance, logs, backups,
security posture). Certificate pinning also works through the tunnel.

**Security:** the hub accepts **no new connections** from the firewalls (iptables `INPUT … NEW`
dropped), so a compromised remote site reaches neither OPNexus nor other remote sites. The
proxy through which OPNexus reaches the firewalls is reachable only in the Compose network and connects
only into the tunnel network. Keys are stored as files in the volume `overlay_state` (including the hub's
private key; the full backup contains it). Each firewall has its own
pre-shared key; "Delete" removes it from the hub immediately. Hosts in the tunnel network are addressed only by IP addresses
(no DNS names). Not included: OPNexus does not perform the setup on the firewall
(for a firewall behind NAT it cannot reach it, after all); for a reachable firewall you can
alternatively distribute a tunnel with the same values under VPN › WireGuard.

### Firewall interface through OPNexus

With **Open interface** on the host page you open a firewall's web interface in a new
browser tab, **through OPNexus**. This is mainly worthwhile for firewalls behind NAT that are reachable only through the
[management tunnel](#firewalls-behind-nat-management-tunnel): your browser needs no access of its own
to the tunnel network. You log in to the firewall as usual with its user and password; OPNexus
neither knows nor stores these credentials and does not log in automatically.

**The function is off by default** and secured twice: it needs a setup on the server and
a switch in the settings.

**Setting up** (in the `.env` in the OPNexus directory):

1. **HTTPS and `COOKIE_SECURE=true`** (see [Network access and TLS](#network-access-and-tls)).
   Without secure cookies the function cannot be switched on.
2. Activate the **Compose profile `gui`**: `COMPOSE_PROFILES=gui` (comma-separated with other profiles, e.g.
   `overlay,gui`). This starts the `gui-gateway` container.
3. Set the **slot addresses**: `OPNEXUS_GUI_SITES=https://10.0.0.5:9441, https://10.0.0.5:9442, https://10.0.0.5:9443, https://10.0.0.5:9444`.
   It is the same address as in `OPNEXUS_SITES`, one port per slot. These origins must **not** be in
   `FRONTEND_ORIGIN` (otherwise OPNexus does not start).
4. **Shared secret** `GUI_GATEWAY_TOKEN` (at least 32 characters, e.g. `openssl rand -hex 32`).
5. **Ports:** `GUI_PORTS=9441-9444` (the ports from `OPNEXUS_GUI_SITES`) are the slot ports on the proxy. Set the
   entry explicitly: without it Docker assigns random free ports, so that an update does not fail on ports
   that are already taken (e.g. Portainer on 9443). `GUI_BIND_ADDRESS` determines where
   they listen; the default is `127.0.0.1` (local only). For access from the network, enter the address of the
   OPNexus machine and open the ports in the machine's firewall only for administrators.
6. `docker compose up -d`, then switch it on under **Settings › Firewall interface through OPNexus**.
   If something is missing, the settings page names exactly what.

In the settings you also define: **inactivity** (default 15 minutes, 1 to 240), **total duration**
(default 60 minutes, 5 to 720) and the number of **simultaneous slots** (default 4, 1 to 4). Switching it off
ends all open sessions (the gateway notices at the latest after 20 seconds).

**How it is secured:**

- **Own origin:** every firewall interface runs on its own port (a "slot") and thus in its
  own browser origin, separate from OPNexus. Scripts of the firewall interface cannot
  address OPNexus; OPNexus also rejects requests carrying the login cookie from foreign origins.
- **Cookies stay on the server:** the gateway never passes the OPNexus session on to the firewall and
  never the firewall's cookies to your browser. The firewall session exists only in the gateway's memory.
- **Admins only**, only with a logged-in browser session (never with an API token). The gateway re-checks every 20
  seconds: logging out, losing the admin role or switching the function off ends the session.
- **One-time ticket:** the click creates a ticket that is valid for only **30 seconds** and **exactly once** and is bound to your
  session and the slot. Afterwards it disappears from the address bar.
- **Secure firewalls only:** https address and verified or pinned certificate (see pinning); otherwise
  the button stays disabled and names the reason.
- **Time limits** (inactivity and total duration) and **audit:** under **Monitoring › Audit** (kind "GUI access")
  ticket, start and end of every session appear with user, firewall, slot and client IP. What you do in the
  interface is logged by the firewall itself, but there with the address of OPNexus; the OPNexus audit
  shows who was behind it.
- **Login protection:** the gateway allows at most three failed logins per firewall in five minutes.

**Important: enter the OPNexus address in the firewall's login protection.** Through the gateway all
logins come from OPNexus's address (for firewalls in the tunnel, from the hub's). OPNsense's login lockout
(sshguard) blocks this address after several failed attempts, and with it also **OPNexus's API access**.
Therefore add it on every firewall under **System › Settings › Administration** (Login
Protection) to the whitelist before you use the function.

**Limitations:**

- **No WebSocket** (functions that need one do not work) and **no automatic login**.
- **At most 4 slots at the same time.** A slot stays with the same firewall per OPNexus session; if all are
  taken, close an interface or wait until it expires.
- **Cool-down:** a freed slot goes to a **different** firewall only **5 minutes** after the end of the
  last session there (same user; a still-open session counts as taken). The same firewall gets
  it back immediately. Reason: a tab of the old firewall interface may still be open and shares its cookies with the
  slot. If all slots are taken or still cooling down, OPNexus names the reason; close the
  old tabs after your work.
- **Change of the firewall address:** if the address, pinned certificate or tunnel path of a firewall changes
  while its interface is open, the session ends at the next check at the latest (20 seconds).
- **At most 64 sessions** run at the same time. A running session is never displaced by a new one; once the
  limit is reached, the new one is rejected until one ends.
- After a **restart of the wg-hub** the management tunnel needs 2 to 4 minutes until the firewalls
  have reconnected; until then the interface cannot be opened.
- With `COOKIE_SECURE=false` the function is not available.

**Note for operators with their own scripts:** since this function the session cookie is called
`__Host-opnexus-session` with HTTPS, and requests to `/api` with a cookie
and `Sec-Fetch-Site: same-site` or `cross-site` are rejected. Since 1.29 the old cookie `opnexus-session`
is **no longer accepted** with HTTPS: whoever has not logged in again since the change has to do so once
(scripts with a raw session cookie must use the new name; without HTTPS `opnexus-session` remains the
normal name). Scripts with an API token (`Authorization: Bearer`)
are not affected.

**Alternative without this function:** you set up your own WireGuard access into the firewalls' network
(e.g. at the site or on the firewall itself) and open the interface directly. This is independent of
OPNexus, but has no role check and no audit by OPNexus.

---

## Deploying rules safely

### The flow

Centrally maintained rules (**Rulebase › Rules**) always follow the same path:

1. **Save draft.** This changes no firewall.
2. **Preview:** per target host the preview shows new, changed and unchanged
   rules with a field comparison. The basis is the state last saved by OPNexus,
   **no live comparison**. The comparison with the firewall is shown by the
   live view.
3. **Deploy (push):** OPNexus writes the rule and applies it. It takes effect
   immediately, but only **on probation**.
4. **Confirm:** within the window (default 180 seconds) you click
   **Confirm**. Then the change is permanent.
5. **Automatic rollback:** if it is not confirmed, OPNexus reverts the change:
   a newly created rule is removed again, a changed one is set back to its
   previous state. Whoever locks themselves out with a rule only has to
   wait.

Further rules:

- Pushing a confirmed rule again with **unchanged** content makes
  no firewall calls. While a deployment is open, pushing again and
  editing are blocked.
- **After an error while applying** (configuration written, but applying not confirmed) you may
  deploy again. The way back remains the **original** state before the first attempt: a new rule is
  removed when the window expires, a changed one is reset to the confirmed state.
- If the rollback itself fails (host not reachable), the rule is shown with the
  status "Rollback failed" and needs attention. In the preview
  it can be retried specifically. A notification reports this as well.
- **Deleting** is a deliberate, immediately effective action without automatic rollback.
- If someone changes the draft, the targets or a
  deployment between preview and push, OPNexus rejects the push and demands a new preview. Simultaneous
  editing of the same rule is detected (409).
- Every rule has a **history**: every change is an immutable state
  that can be loaded into the form and saved as a new draft.

### Table view, usage and overlaps

The rule list has two views (switch at the top right, your choice is remembered in the browser): **Cards** and the compact
**Table** (the default). The table shows one line per rule: action (green pass, red block, amber reject), name with
interface, direction, protocol and number of targets, source, destination, status per target and **Last hit**. Click a column header to sort;
the **Usage** filter limits the list to rules in use, unused, still collecting data or unknown.

- **Last hit** comes from the pf counters that OPNexus samples regularly (see "Rule usage"). Times are shown, not counters: every apply
  resets pf's counters. A rule that was never hit is **Collecting data** until it has been watched for 30 days, and only then **Unused**.
  For a rule on both firewalls of a pair the newest hit of any node counts, because the CARP master sees the traffic; if one node has
  no data, the rule is never called unused. Rules that are not deployed yet show "—".
- **Overlap check:** the preview (and the dialog for several rules, see below) asks the target firewalls whether the rule would be
  **shadowed** by an earlier rule (it would never take effect) or **duplicate** one, and which later rules it would **make pointless** (for example
  a block-all on top). The check is a hint: it reads the firewalls, writes nothing, never blocks the push and does not change the
  preview token. A rule that is not deployed yet is judged by the position it would land in (end of its priority group, or the front for
  "Very top") and is therefore at most "probably"; if a firewall cannot be read the preview says so.

### Deploying several rules at once

Tick the rules in the list (or **Select all shown**) and click **Review and deploy (N)**, at most 50 at a time. The dialog loads
the preview of every rule; rules with nothing to deploy or with an open confirmation are listed as not deployable. **Deploy N rules** then
deploys them **one after the other, each exactly like a single deployment**: with the approval shown in the dialog (a changed rule is refused),
with the same checks, its own confirmation window and its own automatic rollback. A refused rule does not stop the others; the result lists
every rule as deployed, needs attention (written, but a target reported an error), not deployed or skipped.
Afterwards **Confirm all deployed** (or **Confirm all pending** in the bar above the list) confirms every open deployment at once.

Deploying in bulk is a deployment in the sense of the separation of duties: operators may only do it if they may deploy at all. Every rule is
applied on its own, so a firewall reloads its filter once per rule.

### Position in the rule list

Firewalls evaluate rules from top to bottom; the first match wins. OPNsense puts new rules **at the end**; a block rule behind a matching allow rule therefore never takes effect.
In the rule form you choose the **position** for a **new** (not yet deployed) rule; for an already deployed rule the field does not appear, you change its position in the "Live across all hosts" view (see below):

- **At the end** (default, the previous behaviour): behind all existing rules.
- **Very top**: on deployment OPNexus moves the rule before the first rule of its priority group (OPNsense only knows "before rule X" for this). Rules from the
  policy hierarchy (pre-rules) stay above. The position is set on creation and when you change it, not on every deployment: a rule that you move later (see below) stays there.

A new block or reject rule suggests "Very top"; if it is set to "At the end", the form points this out. If only the position changes, this counts as a change in the preview (field "Position").
If the firewall rejects the move, OPNexus reports an error; the window for the automatic rollback stays armed. The rollback restores the content of the rule, **not** its position.
In configuration as code the field is called `placement` (`end` or `top`).

**Protocol "TCP/UDP":** for services that need both (e.g. DNS on port 53), choose **TCP/UDP** in the protocol field. The option is always available in rules, port forwards, outbound NAT and hierarchy rules.

### Viewing the order, moving and editing rules

The **Live across all hosts** tab shows per firewall (an HA pair together, single firewalls on their own) all rules **in the order in which the firewall evaluates them**. A badge says where the rule comes from:
**Central** (managed by OPNexus), **Local only** (created by hand on the firewall), **Hierarchy** (set by the policy hierarchy) or **System** (built in, visible only on request). For a pair, checkmarks show on which firewall the rule exists.

- **Moving:** with the arrows (to the very top, one place up/down, to the end) or by dragging onto another rule (it lands before it; the "Drop here" area at the end of the table puts it at the end). Moving happens within a priority group
  (floating rules, rules per interface, interface groups); hierarchy rules and system rules cannot be moved. For a pair the move is applied on both firewalls.
- **On probation:** the new order is live immediately but applies only on probation. A yellow notice shows the remaining time; **Confirm** makes it permanent (in the browser only, never via API token), **Revert** restores the old one immediately. Without confirmation
  OPNexus reverts it itself after the window (default 180 seconds, as with deploying). Several moves in a row count as one series: each new one renews the window, and the whole series is confirmed or reverted together, newest first.
  If reverting fails (e.g. firewall not reachable), OPNexus reports it as a notification; until it is resolved (revert again), further moves and deploying on this host are blocked.
- **Editing:** a central rule opens the normal editor (preview, confirm, automatic rollback). A **local-only** rule is first adopted centrally for this (only an entry in OPNexus, nothing is written; for a pair it is linked on the second firewall)
  and then edited in the same way.
- Identical rules on the firewalls of a pair are recognised by their content (description, interface, direction, protocol, source, destination, action), not by their ID.

### Rules that exist only on individual firewalls

Below the list of central rules, the collapsible section **Only on individual firewalls** shows the rules that were created by hand directly on a firewall and are not centrally managed
(built-in system rules do not appear). They are grouped by firewall; an HA pair appears once because both firewalls have the same configuration. For each rule a note shows whether it exists **only there** or
**also on** other firewalls (then a central rule is usually worthwhile). The firewalls are queried live when the section is opened, not with the page; if one does not respond, the rest stays visible.
**Adopt …** opens the adopt dialog (see "Adopting hand-made rules") on that firewall. The section is read-only: OPNexus does not change these rules.

### States of a rule

The backend computes the state from targets and deployments: *targets missing*,
*target hosts disabled*, *changes pending*, *confirmation pending*, *check
error*, *check former targets* and *deployed*. "Deployed" refers to
the state saved by OPNexus; manual changes on the firewall are shown by the
live view.

### Cleaning up targets

Under "Preview & push" **former targets** can be removed individually: OPNexus
deletes the rule only on that host; the central rule and its other
deployments remain. This takes effect immediately and without automatic rollback. Taking a target out of the
target list does not delete anything on the firewall.

### Live view and drift

- **Rules › Live across all hosts:** per cluster a table of the rules that are not present
  identically on all nodes, with a checkmark per host. The comparison uses
  a fingerprint of description, interface, direction, protocol, source,
  destination and action; a difference in another field is not noticed.
- The cluster view and the dashboard point out drift; optionally a
  notification reports it.

### Adopting hand-made rules

**Rules › Adopt from firewall** captures rules that were created by hand in OPNsense
as a managed rule with its existing UUID. Adopting only reads and writes nothing to the firewall; only the first change runs through
the normal flow with confirmation and automatic rollback. Fields that OPNexus does not
know (gateway, state options, schedules) remain unchanged.

Rules that the model would distort are not adopted: inverted
interface, source or destination, several or no interface, "received on",
non-quick rule, direction neither in nor out, IPv4+IPv6 in one rule, missing
description and system rules. The interface names the reason per rule.

> **Order:** OPNexus sets every rule with `quick`, so the firewall evaluates
> strictly "first match wins". The order is therefore
> security-relevant, see [hierarchical policies](#device-groups-and-hierarchical-policies).

### Adopting aliases and NAT from the firewall

Like rules, **aliases** and **NAT rules** (port forward, 1:1, outbound) that were created by hand
in OPNsense can be adopted: on the respective page **Adopt from firewall**, choose the firewall, then either an
object with **Adopt** or all of them at once with **Adopt all adoptable**. This brings an existing firewall
under central management without retyping.

Adopting **writes nothing to the firewall**. OPNexus reads the object fresh from the firewall, creates it centrally
(target: exactly this firewall) and remembers the UUID from OPNsense. That is why the preview shows "no change" right afterwards,
and the next rollout after a change updates the same object instead of creating a second one. Adopting appears in the
audit log; operators may adopt (it only creates central entries), read-only users may not.

What would distort the model is not adopted. The interface names the reason per object:

| Object | Not adoptable because … |
|---|---|
| Aliases | OPNsense-internal (`bogons`, `sshlockout`, `__lan_network` …); type unknown to OPNexus (URL, URL table, GeoIP, dynamic IPv6 hosts, OpenVPN group, "External"); the name already exists in OPNexus; name with special characters; BGP ASN with IPv4/IPv6 restriction; reference to an alias that has not been adopted |
| Port forward | several or no interface; IP version neither IPv4 nor IPv6; protocol "any"; "No redirect"; no target; linked filter rule; inverted source or destination; unknown NAT reflection value; description missing |
| 1:1 NAT | several or no interface; unknown type or NAT reflection value; mandatory fields or description missing |
| Outbound NAT | several or no interface; "No NAT" rule (`nonat`); IP version neither IPv4 nor IPv6; description missing |

Automatically generated NAT rules (anti-lockout, "Auto created rule") never appear in the list. **Nested aliases:**
an alias that contains another, not yet adopted alias waits for it; "Adopt all adoptable"
adopts both in the right order, individually the contained alias first. If it refers to an alias that can never be
adopted (e.g. of type URL), it stays behind with that reason. **Outbound NAT** can only be adopted
if the firewall's outbound mode is hybrid or manual; in automatic mode OPNsense does not show manual rules at all,
OPNexus then points this out and does not change the mode.

Settings that OPNexus does not manage (logging and pool options on port forwards, tags, categories, static
NAT port) are never overwritten and are shown in the dialog as "Stays unchanged on the firewall".

#### Same object on several firewalls: link instead of adopting twice

If the same rule, alias or NAT rule exists on several firewalls (typically: both nodes of an
HA cluster, several sites with the same policy), you adopt it **once** and **link** it on the other
firewalls to the central object. This applies to rules, aliases, port forwards, 1:1 NAT and outbound NAT.

1. Adopt normally on the first firewall.
2. In the dialog choose the second firewall. For every entry that matches a central object **exactly**, it says
   "Already exists centrally" and offers the action **Link** (next to **Adopt as separate object**). Linking adds the
   firewall as a further target to the object and remembers the UUID there. Nothing is changed on the firewall.
3. The preview then shows "no change" for this firewall. You deploy a later change to the object as usual for
   all targets together; no duplicate is created.

**What "exactly" means:** all fields managed by OPNexus are equal after the `{{VARIABLES}}` of the central object have been replaced with
the values of **this** firewall (so an alias `{{LAN_NET}}` matches `192.168.2.0/24` if that is the variable
of this firewall). For aliases, name, type, content including references to other aliases, description and
active status must match. If a variable value is missing for the firewall, the object does not match; the dialog names the variable.

**Deviations:** if there is an object with the same name (alias) or the same description (rules, NAT) whose content
differs, the dialog shows "Similar" with the differing fields (central versus firewall). It is then **never** linked,
not even automatically. Either align the firewall or the central object, or adopt the object separately (for aliases
this works only after a rename, because the name is unique centrally).

**Adopt all adoptable:** the dialog shows beforehand what will happen ("3 link, 2 adopt as new, 1 not adoptable"), and you
confirm. Linking happens only if exactly **one** central object matches exactly; if there are several identical ones (legacy from earlier
individual adoptions), you decide at the entry itself, and "All" adopts it as a new object. A central object is linked at most once
per run.

**Safety:** when linking, the firewall is read fresh and compared exactly once more; if something changed between display and click,
nothing is linked and the message names the fields. A second click changes nothing. The action appears in the
audit log and in the object's revisions ("Linked with <firewall>"). Roles as for adopting: operators may, read-only users
may not. If the central object has an open confirmation or an open rollback, linking happens only after it is resolved.
"Adopt all adoptable" then skips such entries (note in the result). **Important:** a linked object is treated like any managed object: if the central object is deleted or the firewall is removed from its targets, the next rollout deletes the object on this firewall as well.

---

## Device groups and hierarchical policies

### Device groups

**Rulebase › Groups** bundle hosts for targeted deployment. A host can belong to
several groups; groups are independent of HA clusters. A rule
can have several groups **and** individual hosts as targets. On push the
current members are merged and deduplicated, disabled hosts
skipped. New members receive a rule only at the next push.

- Before saving an existing group, the interface shows the effects:
  new and removed members and targets per rule.
- Groups that are still in use (by rules, aliases, NAT, **VPN objects** or
  rollout waves) cannot be deleted.
- "Create rule for this group" opens the rule form with the group
  preselected.

### Hierarchical policies

**Rulebase › Policy hierarchy** is a group tree with **pre** and
**post rulebases**. A rule attached to a parent group is
automatically rolled out to all hosts of the group and its subgroups, in
the correct order:

- **Pre rules:** the top group first. An organisation-wide pre rule
  wins over anything more specific.
- **Post rules:** the most specific group first, close to the host-local default deny.

A host belongs to **at most one** hierarchy group (set in the host form). Aliases and all NAT kinds can likewise target a subtree,
but deliberately have no pre/post. The order is changed by drag and drop or
with the arrows. **Restore order** then brings already deployed rules
onto the firewall; after normal rule, alias and NAT deploys
this happens automatically. OPNexus rejects cycles in the tree and the deletion of referenced groups.

If sorting fails after a successful push, the deployment shows
**error** instead of "open": a rule that is live but sorted wrongly is
wrong in a security-relevant way, not just unconfirmed. OPNsense does not move legacy and automatic rules,
and OPNexus does not touch them.

---

## Aliases and NAT

**Aliases** (host, network, port, group, MAC, ASN) are maintained centrally and
follow the same flow with preview, confirmation and rollback as rules.

**NAT** (Rulebase › NAT) comprises **port forward**, **1:1 NAT** and **outbound NAT**
with separate forms. All three have draft, preview, push, confirmation,
history, duplicate, targets (groups, hosts, hierarchy), rollback retry and
a live view of the entries active on the firewall, separated into
automatic and configured ones.

- **Port forward** does not create a filter rule: a matching rule must exist
  separately.
- **Outbound mode:** OPNexus reads the global mode and checks it before every push.
  Only `hybrid` or `advanced` (manual) allow manual rules; with `automatic`,
  `disabled` or an unknown mode the push aborts with
  `OUTBOUND_MODE_BLOCKS_PUSH`. OPNexus never changes the global mode. Set it
  in OPNsense under Firewall › NAT › Outbound to "Hybrid".
- NAT drift detection compares the nodes of a cluster and reports
  deviations.

### Site variables

With **variables** you maintain an object once and deploy it to firewalls with different networks.
Write `{{NAME}}` (capital letters, digits and `_`, starting with a letter) in a field of a
**rule**, an **alias**, a **port forward**, a **1:1 NAT** or **outbound NAT rule**, for example as
source `{{LAN_NET}}` or as description `Access {{SITE}}`. You set the value per firewall on the host page
under **Site variables** (`192.168.7.0/24`, `Branch North`, …). The page automatically lists all
placeholders used by the objects destined for this host and shows which ones still have no value.

- **Resolved per host:** preview, deployment, the change counters and the audit see the real values of the
  respective host; if a value changes, a change appears only for that host. The centrally stored
  object keeps its placeholders.
- **If a value is missing, nothing is sent:** the preview marks the host with "Variable missing", the deployment
  is rejected before any host has been touched, and as a last safeguard OPNexus sends no request
  to a firewall that still contains a placeholder.
- **Values** may contain letters, digits and `. : / , @ + - _` as well as spaces (at most 255 characters);
  they are inserted literally, never evaluated as a pattern or as a further placeholder.
- **Not** supported: placeholders in VPN tunnels and in the policy hierarchy (there the values go directly into the
  object); variables for device groups (default values) do not exist, every host has its own. Operators may
  maintain variables; whether they may also deploy is governed by [separation of duties](#users-roles-and-login).

### Interface roles

Two firewalls do not always have the same interfaces: one has a guest VLAN as `opt3`, the other none at all, or the same VLAN
is called `opt5` there. Because the OPNsense identifier (`opt3`) is only a number from the assignment order, a central rule with
`opt3` could hit the wrong network on the second firewall. OPNexus rejects this ("Interface … does not mean the same
network on all targets"). With **roles** you do not need dummy VLANs for this.

1. On the host page under **Interface roles** create a role (name e.g. `GUEST`) and choose the interface of **this** firewall.
   Each firewall does this for itself: `fw-1` assigns `opt3`, `fw-2` assigns `vlan0.20`, `fw-3` assigns nothing.
2. In the rule (or port forward, 1:1, outbound NAT) choose the role in the **Interface** field. `{{IF_GUEST}}` is stored.
3. The preview shows the real interface per firewall. `fw-3` appears as **"Role not assigned · skipped"** and is not
   touched; this is not an error.

- **Nothing is guessed:** a role only takes effect where you assigned it. Without an assignment the firewall does not get the rule.
  If all targets lack the role, nothing is deployed (message "Role … is not assigned on any target").
- **Checked live:** when saving the assignment and once more before deploying, the interface must exist on the firewall.
  If it has been removed there in the meantime, the request is rejected before anything is written.
- **Adopting and linking:** this is how you gather existing, hand-made rules centrally: first assign the
  role on every firewall. When **adopting** the rule on the first firewall, the dialog offers, next to "Adopt", the button **"Adopt as role GUEST"**
  (only if this firewall has assigned the role to exactly this interface; rules, port forwards, 1:1 and
  outbound NAT). The central rule then carries `{{IF_GUEST}}`. On the further firewalls the equivalent rule appears as
  **"Link"**: it matches if `GUEST` points there to the interface on which the rule sits (exact comparison after resolution,
  as with [site variables](#site-variables)). Without an assignment or with a different interface only the difference is
  shown, never linked. Adopting and linking write nothing to the firewall.
- **Optional:** plain identifiers (`lan`, `wan`, `opt3`) work as before, including the check for the same meaning.
  The interface field contains either an identifier or exactly one role, never a mixture.
- **Config as code:** roles are stored like other variables in the `variables` section (`IF_GUEST: opt3`) and as `{{IF_GUEST}}` in the
  interface of the objects.
- **Policy hierarchy:** hierarchy rules can also carry a role as interface. The order of inherited rules (pre/post) is established per
  firewall on the actual interface; a host without the role does not get this rule and does not carry it in
  its order. Other fields of a hierarchy rule remain without placeholders.
- **Not** supported: roles for device groups (each firewall assigns for itself).

---

## Network and host system

### Network (per firewall)

The **Network** area manages **interfaces**, **VLANs**, **virtual IPs**,
**gateways** (including gateway groups), **routes**, **DNS** (Unbound entries
and forwards), **DHCP** (dnsmasq hosts and ranges, Kea subnets and
reservations) and **devices** (LAGG, bridge, loopback, GRE, GIF, VXLAN). These
objects are firewall-specific, so there is no central copy and no
multi-host push: the firewall is the source of truth.

**Flow of a change:**

1. **Preview:** OPNexus reads the live state and shows differences, risks,
   warnings and blocks.
2. **Execute:** OPNexus reads the state again and refuses if it has changed
   in the meantime. Risky changes additionally require the
   entry of the firewall name. Then: save a configuration backup (if this
   fails, nothing is changed), write, apply, read back and compare with the
   desired state.
3. Every step ends up in the audit log.

**Protection against cutting your own connection.** OPNexus determines through which
interface the firewall is reachable at the stored address. These
changes require explicit confirmation: changing or deleting this
interface, its VLAN or gateway, changing the default gateway, a
default route or a route that covers the firewall address, and any change to
existing objects if the connection path cannot be determined.

> **No automatic rollback.** Unlike with rules, OPNexus cannot revert a change
> that cuts its own connection. The way back is the
> backup taken before the change. If the firewall is then not reachable at the old address
> ("applied, cannot be verified"), enter the
> new address under Hosts › Edit.

Further notes:

- **Interface assignment** is two-stage: changes are first staged,
  then applied. OPNexus blocks new changes while something is already
  staged there, so that nothing foreign goes along. When deleting, OPNsense removes all
  rules and NAT entries of the interface; the preview lists them.
- IP configuration of interfaces is possible via API **only from OPNsense 26.7.6**.
  On older versions OPNexus blocks this change and gives the reason.
- Secrets (CARP password, DDNS key) are never displayed, stored or
  audited.
- **Transfer to the partner:** after creating an object on one
  cluster node, OPNexus offers to transfer it to the partner with its own preview.
  Recommended: the backup node first, then the master.
- **Comparison** (Network › Comparison) compares the nodes of a cluster. Addresses,
  CARP advskew and password as well as the active default gateway differ per node
  and are not compared. A deviation optionally triggers the alert
  "Network deviation in the cluster", which resolves itself after the correction.

### Host system

- **Tunables:** every change needs confirmation. Built-in defaults can
  only be overridden with an entry of your own, not edited.
- **Cron:** scheduled tasks of the firewall.
- **Syslog:** remote log targets.
- **Snapshots:** create and delete ZFS snapshots. The active boot environment cannot
  be deleted; activating with a reboot is deliberately not offered.
- **Diagnostics:** ping, traceroute, TCP port check and DNS query, executed **from
  the firewall**. Admins only, with strict input validation; every run
  appears in the audit log, results are not stored, and the port check never passes on
  the banner of the remote side. A test against a closed or blocked
  port takes about 10 seconds on OPNsense. Hops that do not answer traceroute
  appear as a line "* no response" instead of a gap. Packet capture is deliberately not offered,
  because captures contain other people's payload data and credentials.

---

## VPN: IPsec, OpenVPN, WireGuard

VPN objects (VPN › IPsec VPN, OpenVPN, WireGuard) are defined centrally and deployed to
hosts or device groups, for example the same tunnel on both nodes of an
HA pair. The flow is draft → preview → push; history and deleting work
as with rules.

> **No automatic rollback for VPN.** Reloading the VPN service can cut
> exactly the connection through which OPNexus reaches the firewall, and an
> automatic rollback could then no longer reach it. Instead of a deceptive
> safety net, OPNexus shows a clear warning before **every** push and
> requires two steps ("Warning understood", then "Yes, deploy now").
> If a step fails, the partial state already written remains, and the
> deployment shows "Error" with a plain-text message. If only the
> reload fails, the message reads: "Configuration was written, but service reload was
> not confirmed — check reachability manually."

Key material (pre-shared keys, static keys, WireGuard keys) is never stored in
the database but as a file in dedicated volumes. When editing, leave the
field empty to keep the existing key.

| | IPsec | OpenVPN | WireGuard |
| --- | --- | --- | --- |
| Scope | site-to-site tunnel | server and client instances | one server and one peer per tunnel |
| Authentication | pre-shared key | certificate and CA per target host, optional TLS auth/crypt key | key pair, optional peer PSK |
| Not included | certificate/EAP, several child SAs, pools | `crypt-v2`, authentication servers | multi-peer hub, key generator, live status |

Notes:

- **OpenVPN:** you choose certificate and CA per target host from its own
  trust store (the UUIDs are host-local). If the choice is missing for a target, the
  push aborts without writing anything.
- **WireGuard:** you supply the server private key, server public key and peer public key
  yourself. In OPNsense enable **VPN › WireGuard ›
  General** once before you rely on pushed tunnels.

Test VPN rollouts first on a non-production firewall.

If a deployment breaks off midway (network gone, OPNexus restart), an entry with the status "Error" and
the note "Deployment interrupted" remains, together with all identifiers that the firewall had assigned up to then. Deploying
again continues from there instead of creating objects twice. Per VPN only one deployment or
removal runs at a time; a second one is rejected with "already running".

---

## Certificates and ACME

**Certificates** shows live the certificates of all firewalls (expiry, status) and
manages the trust store as well as ACME issuance. Everything is modelled **per host**,
because certificate and CA UUIDs are host-local; there is no
multi-host push. **The target host is fixed after the first push** (certificate, ACME account and
ACME request): for a different host you delete the entry and create it anew. An ACME account that
certificate requests use cannot be moved to another host beforehand either.

**Trust store** (certificates and CAs): create (import, issue internally,
reissue), edit, push, delete. This is a single,
immediately effective step without a service restart and without lockout risk. The
certificate of the firewall's web interface cannot be switched via the API, and therefore
not by accident either. An imported private key exists only as a file
in a dedicated volume. OPNsense does not allow deleting a certificate that is still in use;
OPNexus passes the message through.

**ACME / Let's Encrypt** (plugin `os-acme-client` on the firewall): create and push accounts and
certificate requests. Creating, changing and pushing are pure
configuration. Two actions **really** contact the certificate authority over
the internet, count against its rate limits and are therefore behind a
two-step warning: **Register** (account) and **Sign** (certificate).

- **Signing runs asynchronously.** The message "triggered" is **not** a success. You fetch the
  result with **Status**.
- Use the staging CA (`letsencrypt_test`) for first attempts.
- **Renewal interval** counts days since the last issuance, not days before
  expiry.
- You set up DNS providers and validation methods in OPNsense itself;
  OPNexus references them via the UUID. OPNsense handles automatic renewal,
  OPNexus offers the manual immediate trigger.
- The EAB HMAC key of an ACME account exists only as a file in a dedicated volume.

---

## HA clusters and CARP

Hosts with the same **cluster name** form a cluster. **Overview › Cluster**
shows the nodes side by side with MASTER/BACKUP role, rule count, rule and
NAT drift and a collapsible **CARP history**.

- **CARP history:** role changes are detected at the pace of the notification check
  (Settings › Notifications). A change that is reversed between two
  runs does not appear; the first run after
  creation only records the initial state.
- **Alerts:** *split brain* (several MASTERs) and *no master* report through the
  configured channels and resolve themselves.
- **Maintenance mode:** a node can be put into CARP maintenance mode in a controlled way
  so that the partner takes over. Optionally with an **auto-revert timer**
  (15, 30, 60 minutes or permanent). The timer restores normal operation
  by itself; if that fails, the alert
  "CARP auto-revert failed" reports it.
- **Lock against double demotion:** if a node is already in maintenance mode,
  OPNexus blocks demoting the partner and names the host that first
  needs "Restore normal operation".
- If a role swap is not triggered despite maintenance mode (a known
  OPNsense bug), **Cancel operation** helps: it closes the operation with a
  reason, without claiming a success.

---

## Maintenance and firewall updates

### Host maintenance

On the host detail page: update status, update check, **reboot**, **stable
updates** and **major upgrades**. Every action runs through the same protective steps:

- A **preview**, valid for five minutes and usable only once.
- Entering the **host name** and confirming the possible **downtime**.
- A **local configuration backup**, at most 24 hours old.
- Blocked as long as deployments, rollbacks, restores or other
  maintenance are open.

**Way-back point:** before every stable update and major upgrade (manual, scheduled, in a
rollout and during CARP handover) OPNexus creates a **way-back point** on the firewall:
a ZFS boot environment of the running system, named
`opnexus-pre-<version>-<time>`. If this fails, the update does **not** start. If the
firewall has no ZFS (UFS installation), the update runs, and the message says that there is
no way back. Per firewall the two newest points created by OPNexus are kept;
boot environments created by hand are never touched. Snapshots you create manually must not carry the
prefix `opnexus-pre-`.

"Handed over to the firewall" only confirms acceptance. After a timeout the
outcome remains unclear and is **never** retried automatically. Check reachability,
version and services and document the completion. If an operation hangs,
**Cancel operation** closes it with a reason. Open maintenance blocks new pushes on the
affected host or cluster.

### Back to the previous firmware

**To previous firmware** (host maintenance) undoes an update: OPNexus
activates the newest way-back point and reboots the firewall. The action runs through
the same protective steps as a reboot (preview, host name, downtime confirmation, in a
cluster only on the BACKUP, blocked when operations are open). The preview shows the
target version, the point and **what will be lost**.

**Important:** a boot environment also contains the configuration (`/conf`). The way back
therefore resets firmware **and all configuration changes since the point**, including
those made by hand. Therefore:

- Before the way back, OPNexus saves the current configuration locally (if that fails, it does not
  start).
- All OPNexus deployments to this firewall since the point (rules, aliases,
  NAT, VPN) are marked as "unclear". The next deployment really rewrites them
  instead of assuming "unchanged". Afterwards redeploy the central objects via preview → deploy
  → confirm.
- OPNexus activates the point, checks that it is staged for the next boot, and
  only then follows the reboot. If one of these fails, OPNexus re-enables the previous
  boot environment so that no old state waits for the next unplanned reboot.
- After startup check the version and close the maintenance. A rollout **never**
  performs a way back by itself; on a failed host it stops, and you decide.
- The abandoned boot environment (the state after the update) is kept so that you can also
  go forward again. If you no longer need it, delete it under Host system › Snapshots.
  The firewall then runs from the boot environment named after the way-back point.
- In a cluster the same applies as for a reboot: only on the BACKUP. For the MASTER, run the
  CARP handover first.

### Update Center

**System › Update Center** shows the whole fleet: installed and available
versions, stable updates, major upgrades, reboot requirement, age of the local
backup, package changes, check log and open jobs. When it is opened, an update check starts once on all suitable hosts
for admins.

- **HA updates** run guided: updates only on the BACKUP node (on the MASTER
  OPNexus refuses them). The MASTER is put into maintenance mode after its own preview and
  double confirmation so that the updated partner
  takes over, and returns to normal operation afterwards. Every step is completed and audited only
  after healthy roles have been observed. There are no
  unattended update chains and no automatic role switches.
- **Major upgrades** (e.g. 25.7 → 26.1) are enabled only if OPNsense
  itself reports an upgrade, so never while a stable package update
  is still pending. They have an additional red warning (takes longer, no
  automatic rollback in OPNexus).

### Scheduled maintenance

Reboot, update or major upgrade can be set for a **one-time**
future point in time. This is explicitly "runs unattended" and
requires confirmation. At the trigger time OPNexus checks the locks and the
fresh backup again. A blocked action is marked "failed"
and **not** retried. It appears in the status of the scheduled maintenance and additionally reports
via notification ("Scheduled maintenance not executed", can be switched
individually), so that you do not only notice it the next morning when you look.
Recurring appointments and whole fleets run via [rollouts](#rollouts-in-waves).

**Check and complete the result automatically** (preselected in the dialog): after the job
OPNexus waits until the firmware job has finished and then checks itself: firmware
ready for at least one minute in a row, an expected reboot really took place
(host seen offline or new boot time), version readable. Only then is the maintenance action
marked completed and no longer blocks host and cluster. If this does not succeed within
20 minutes, it stays "unclear" (blocked, with notification). Without the option
it stays "handed over", as before, until someone completes it by hand.

In a cluster, unattended maintenance runs by default **only on the
CARP BACKUP**. If the host is currently MASTER when scheduling, OPNexus rejects this immediately with
a reason instead of failing only at the scheduled time.

**With automatic CARP switchover** (option in the scheduling dialog, only for cluster nodes,
off by default) the MASTER may be scheduled as well. At the time the following runs:

1. Check prerequisites (partner reachable and BACKUP, no open maintenance, for
   updates a fresh backup). If something is not met, **nothing** is changed.
2. Switch on CARP maintenance mode on the host. OPNexus waits up to 3 minutes until the
   partner is actually MASTER. If it does not become so, OPNexus switches back and
   updates nothing.
3. Only then the action starts (reboot/update/major upgrade).
4. OPNexus waits until the host is reachable again and the firmware job has finished
   (for a reboot the host must first have been observed as down).
5. Leave maintenance mode and check that both nodes are reachable again and have exactly
   one MASTER. Depending on the CARP settings the host takes the MASTER role
   back (a brief second switch).

If something fails after step 3 (timeout, unclear response, host does not come
back), the host **deliberately stays in maintenance mode** and the partner remains MASTER;
the plan shows "failed", the maintenance action "unclear", and the
notification is sent. The CARP switch is never blindly repeated. If you end the
maintenance action by hand, the flow stops and touches nothing further. On success
OPNexus completes the maintenance action itself. It also continues through a restart of the OPNexus backend
in the middle of the flow (the state is in the database). With "Check result
automatically" the check comes **before** leaving maintenance mode: the host only gets
traffic back once it is demonstrably finished.

### Rollouts in waves

**System › Rollouts** distributes a firmware update (stable update or major upgrade) in
waves across the fleet. The waves consist of [device groups](#device-groups), which you create
as rings, for example `ring-canary` (one unimportant firewall), `ring-pilot`
(a few) and `ring-prod` (the rest). If a host is in several waves, the earliest applies.

**Flow per wave:**

1. **Pre-check:** update check on the hosts of the wave, then take inventory (version,
   gateways, running services, security score). Hosts without a matching update are
   skipped. The version installed in the first wave is the
   **target version**; if a host of a later wave offers a different one, the rollout stops
   (OPNsense always installs the newest, otherwise "prod" would get a version that never ran in the
   canary ring).
2. **Hosts one after another:** every host runs as [scheduled maintenance](#scheduled-maintenance) with
   automatic checking (backup before the update, locks, audit). Always only one host,
   always only one rollout at a time. In a cluster first the BACKUP, then the MASTER;
   the MASTER only if the wave allows CARP handover, otherwise the rollout stops.
   Before the second node of a cluster, the cluster (all nodes reachable,
   exactly one MASTER per VHID, no maintenance mode) and the already updated node are
   checked again.
3. **Observation time** (adjustable per wave), then the **health check** of the
   updated hosts: reachable and firmware ready, target version installed, CARP
   healthy, gateways and services that ran before are running again (those already faulty before
   only as a note), no open locks, optionally security score not dropped
   (note or stop) and optionally the plan's **reachability probes**. If something is red, OPNexus checks again two minutes later
   (slow start after the reboot) and then stops.
4. **Release** (optional per wave): the wave only begins when someone clicks "Release
   wave".

**Reachability probes:** in the plan you can enter up to ten probes (ping or TCP
to a port, target as IP or name), for example the uplink, a DNS server or an
internal server. The **firewall itself** runs them: once before the wave (baseline)
and after the observation time. A probe that worked before and no longer does stops the
rollout; one that already failed before is only a note. To be honest: this
checks reachability **from the firewall**, not the traffic that passes
through it. Every probe appears in the audit log (actor `system:rollout`). Before every update
OPNexus also creates the [way-back point](#host-maintenance) here.

**It stops** at any uncertainty, and nothing is ever repeated by itself: gate red,
host failed or unusually long in progress, host not ready in the pre-check,
differing target version, MASTER without handover, a foreign open maintenance in the cluster for longer
than 15 minutes, the responsible person is no longer allowed to deploy, the window was
deleted, the run is older than 14 days. The notification "Rollout
stopped" is sent. **Resume** first re-checks all hosts updated so far; whoever
resumes is responsible from then on. A failed host must be explicitly
**skipped** beforehand (its maintenance action stays "unclear" until you complete it on the
host page). **Cancel** discards everything that has not begun yet; an
action that is already running on a firewall is brought to an end in a guided way. **Stopping**
is allowed to anyone with write permission, everything else needs the permission to deploy.

**Maintenance windows** (tab "Maintenance windows"): weekly on certain days or
monthly (first…fourth or last weekday, or a fixed day 1–28), start in a
time zone, duration and cut-off time: after the first N minutes no new host begins,
running updates finish, and the rollout waits for the next slot (then
what was done so far is checked again first). Daylight saving time: a time that does not exist on that day
moves forward (02:30 → 03:30); a duplicate time counts the first time. Individual slots can be
skipped or postponed. A window that a plan uses cannot be deleted.

**Automatic start:** a plan with a window and stable update can start by itself at every slot.
For this someone with deploy permission must **activate** it after the preview.
OPNexus remembers a fingerprint of the plan, resolved hosts and window
(**including skipped or postponed slots**); if any of it changes
(including the members of a group), nothing starts, and "Rollout not
started automatically" is sent. Every edit of the plan deactivates it. This way nobody can
change the targets of an activated plan unnoticed. Whoever skips or
postpones a slot must activate an automatic plan again afterwards. The activating person
is additionally identified by their account ID: if their account is deleted and a new one with the
same name is created, the plan no longer starts. If two slots of a
window overlap (e.g. a postponed one with the regular one), the one begun last counts.

The **Update Center** shows the running rollout as a banner; hosts it is still working on
are blocked there for manual firmware actions. Restarting or updating OPNexus
during a rollout is safe (the state is in the database), but pauses it for
that time.

### Services and system load

- **Services** (host detail page): all services reported by OPNsense with status and
  start/stop/restart. There is no selection list, all services are controllable,
  therefore a general warning precedes them.
- **System load:** time series of the last 24 hours for CPU load,
  memory and disk usage per host. Sampling happens every 60 seconds, retention
  is 30 days.

### Configuration backups of the firewalls

On the host detail page you back up a firewall's configuration
(**Back up now**) and see the locally stored backups. This is not
the backup of OPNexus itself, see [Backup and restore](#backup-and-restore).

**Scheduled and offsite** (Settings › Backups): OPNexus backs up every firewall
automatically (default: daily) and keeps the newest ones (default 30 per firewall). Optionally every
backup is **encrypted on your server with a passphrase** (AES-256-GCM, key derived with scrypt
from the passphrase) and uploaded to **S3-compatible storage** (AWS S3, MinIO, Backblaze B2,
Wasabi, Cloudflare R2, Hetzner Object Storage …; endpoint must be `https://`). "Test target"
writes, reads and deletes a test object. Each backup is uploaded only once, and nothing is ever deleted
offsite (use the bucket's lifecycle rules for that). **Without the passphrase an
offsite copy is unreadable** – keep it outside OPNexus. Decrypting:

```bash
docker compose exec -T -e BACKUP_PASSPHRASE='…' backend python -m app.backup_decrypt < file.opxbak > config.xml
```

The notification "No current backup" fires when a host has been without a backup for three intervals (at least
6 hours), "Offsite backup failed" on problems with the target. The
offsite integration is tested against the official AWS signature examples and against running S3 servers with real
signature verification (Garage, MinIO, RustFS and Versity S3 Gateway: upload, download, delete, 20 MB object, special characters in the
key, wrong secret is rejected); for AWS itself, Backblaze, Wasabi & co. "Test target" shows you immediately whether your provider works.

**Viewing changes:** the "Changes" button next to a backup shows what changed compared with the
previous one. Secrets (passwords, keys, tokens, certificates) appear only as a
hash: a changed secret shows up as a changed hash, the value never leaves the server.

**Restoring** checks a stored backup as a preview (format,
metadata, secret fields are masked) and then hands over to the
OPNsense web interface. **OPNexus writes nothing to the firewall in the process.** You
import the backup into OPNsense yourself. While the operation is open,
OPNexus blocks rule, alias, NAT and maintenance pushes on this host; confirm
or cancel the operation. In case of deviations (such as a different host name in the
backup), the preview shows a warning.

---

## Monitoring: dashboard, logs, audit, notifications

### Rule usage

On the **Security posture** page the "Rule usage" table shows when each rule
last matched traffic. For this OPNexus reads the firewall's packet counters. Because these
start from zero on every change and every reboot, OPNexus remembers the time at which
a counter last grew (resolution: one observation, every 15 minutes). Immediately before every apply that OPNexus triggers
on a firewall (rules, NAT, aliases, VPN, network), OPNexus additionally reads the counters, because every apply resets them;
this way no hits are lost. The counters come from the firewall's `ruleStats` (already summed per rule; on older OPNsense versions from the loaded rule set). A rule counts
as **unused** if OPNexus has observed it for at least 30 days without seeing a hit;
until then it shows "Collecting data" (the observation period restarts when the rule is enabled or changed, and after a pf reload with a long gap in the counters), so that a new installation does not raise a false
alarm. Rules from the rule editor or the API are recorded, as well as not yet migrated rules in the legacy format (OPNsense's
automatic rules are not). Unused rules feed into the security posture as the finding "Rules in use".

### Dashboard

The start page (**Overview › Dashboard**) shows: open actions (pending
confirmations, errors, hanging maintenance, running CARP maintenance windows),
fleet and CARP health, security and expiry warnings (certificates, rule
and NAT drift, available updates) and an activity feed of the latest
audit events.

### Logs

OPNexus regularly fetches the firewall logs of all hosts itself and stores them
in its own database. **Monitoring › Logs** searches them server-side:
individual hosts, whole clusters or all, free text, filters (action, interface,
protocol), time range (last hour/24 hours/7 days/all/custom range) and
a statistics panel (distribution by action and interface, most-blocked sources,
most frequent destination ports).

Settings › **Log collection**:

| Setting | Default | Meaning |
| --- | --- | --- |
| Collection active | on | switches fetching on or off |
| Fetch interval | 30 s | minimum 10 s |
| Retention | 7 days | older entries are removed |
| Max. storage | 10 GB | `0` = unlimited; when reached, the oldest entries are deleted |

The storage figure is an **estimate** from the table size: Postgres does not return deleted
space to the file system, but reuses it for new logs. On
heavily loaded firewalls even a few days can occupy several GB; set
retention and limit accordingly. The timestamps are the firewall's clock time,
**uncorrected**: if the firewall is not set to UTC, time filters are shifted by that
offset.

### Audit

**Monitoring › Audit** logs without gaps requests, results per host,
confirmations, deletions, automatic and manual rollbacks, maintenance and
network actions, diagnostic runs and update requests, each with user,
time, correlation ID and content. Filters, paging and JSON export are
available. Entries remain even after a rule or a host is deleted;
a database trigger prevents changes and deletion. Passwords,
API secrets and configuration files are not logged.

### Audit export

Under **Settings › Audit export** you forward the audit (who changed what, when, with which
result) to a **syslog server** (RFC 5424 over UDP, TCP or TLS; facility local0,
severity "warning" for rejected or failed actions) and/or a **webhook**
(JSON batch, optionally with an HMAC-SHA256 signature in the header `X-OPNexus-Signature`) so that it also
exists outside OPNexus. Only new events from the moment it is switched on are sent; the
content of firewall objects (before/after data) is never included. Delivery is "at least
once": if the target fails, OPNexus tries the same events again, each carrying its ID for
deduplication. The status of the last delivery appears in the settings.

### Notifications

Settings › **Notifications**. Channels:

- **Webhook** with optional signing secret.
- **Email (SMTP)** with host, port, encryption (STARTTLS/SSL), credentials,
  sender and recipients. Certificate and host name are verified; a mail server
  with a private CA needs `SMTP_CA_FILE`. Login without encryption stays
  blocked. With **test** you check each channel.
- **Further channels** (section "Further channels", any number per type, each with **Test**):

  | Type | Settings | Credentials |
  |---|---|---|
  | **ntfy** | server address (default `https://ntfy.sh`), topic, priority 1–5 | access token, optional |
  | **Gotify** | server address, priority 0–10 | app token |
  | **Telegram** | chat ID (number or `@channel`), optionally your own API address | bot token (from @BotFather) |
  | **Microsoft Teams** | – | webhook address of a workflow ("Post to a channel when a webhook request is received") or of an incoming webhook |
  | **Matrix** | homeserver address, room ID (`!abc:example.org`, not the alias) | access token of a user who is in the room |

  The credentials are kept like the SMTP password in the server's secret store, never in the
  database, are never displayed again (only "set") and do not appear in error messages or logs.
  Because for Telegram and Teams the address itself is the secret, it is treated like a token.
  Targets must be `http(s)`, without credentials in the address; local addresses (`localhost`, loopback,
  link-local, metadata service) are rejected, targets in your own network are allowed. The check also applies
  when the connection is established: OPNexus connects exactly to the address it checked (protection against DNS rebinding,
  message "Target address not allowed"); the same applies to the addresses of the SSO provider. Redirects are
  not followed. Webhook, email and all channels are served **simultaneously**, each with its own
  time limit (20 seconds) and at most 90 seconds in total per check run: a failed or
  hanging channel stops neither the others nor the check (the log says "timed out"). Delivery happens
  once per event, without retry.

Events (each can be switched individually): host not reachable, certificate expiring,
rule drift in the cluster, NAT drift in the cluster, network deviation in the cluster,
failed rollback, maintenance result unclear, CARP state critical
(split brain/no master), CARP auto-revert failed, scheduled maintenance not
executed, gateway disturbed, resource threshold exceeded, service down,
API privileges missing (the last four are described below). Checking happens at the configured interval (default 300 s). A problem
reports once and resolves itself as soon as it is fixed; the
dashboard card "Warnings" shows the open ones. Exception "scheduled maintenance not
executed": it stays open for three days after the scheduled time and then closes
without a "resolved" message, because a missed appointment does not fix itself. The
language of the texts is set under Settings › Notifications › "Language of the messages" (Automatic, Deutsch or
English; Automatic follows the language of the oldest administrator account, and English if nobody ever chose one).
The JSON object `context` of the webhook is never translated, so receivers that parse it keep working.

#### Gateway, resource and service alarms

These three events read the **minute measurement** (the same one as the load charts), so they do not query the
firewalls additionally. So that single spikes and flapping connections report nothing,
an alarm opens only when the last *N* measurements are all bad, and closes only when the last
*N* are all good again; in between the state stays as it is. *N* is adjustable (default 3 for
gateways and services, 5 for resources; the measurement runs every minute). If a firewall is currently not reporting,
OPNexus does not assess it (that is reported by "Host not reachable") and leaves open alarms standing.

- **Gateway disturbed** – **on by default**, because a WAN outage is what everyone wants to know about. One alarm per gateway if OPNsense lists it as
  *down*, or if packet loss (default from 20 %) or latency (default from 500 ms) is above the threshold (0 switches the respective threshold off).
  OPNsense itself decides on "down" according to the limits in the gateway ("Gateway monitoring" setting). Gateways switched off by hand
  ("Disable gateway/forced down") and gateways without monitoring (no monitor IP, values "~") report nothing.
  The status is also on the host page (section "Gateways", read live), as a tile on the dashboard and in Prometheus.
  **Switching off individual gateways:** a dormant second WAN or a dead IPv6 gateway permanently report "offline". On the host page the
  bell behind the gateway switches the alarm off only for that gateway (tooltip "Alarm for this gateway"); the gateway then carries the badge "Alarm off",
  an open alarm resolves, and a click on the bell switches it on again. Only administrators may do this (like the other
  notification settings), and the change appears in the audit log. At most 200 gateways can be switched off.
- **API privileges missing** – **on by default**. OPNexus's API user needs read privileges in OPNsense for the measurements on which the alarms
  are based: *System: Gateways* (gateway status), *Status: Services* (services) and *Lobby: Dashboard* (CPU, swap; the same privilege as the
  base values). If the firewall explicitly denies one of these accesses (HTTP 403), the associated alarm would never fire. Then the
  host page and the dashboard show the warning badge "API privileges missing: …" (tooltip names the missing privilege), there is one alarm per firewall and
  access, and it resolves as soon as the access works again. Timeouts and server errors count only as "unknown" and neither trigger
  anything nor close the alarm. Remedy: in OPNsense under System › Access add the named
  privilege to the user or group of the API key. Prometheus: `opnexus_host_api_rights_missing{host,read}` (1, only while it is missing).
- **Resource threshold** – **off** by default: on an existing fleet switching it on would otherwise immediately report everything that has been full for a long time. Thresholds: disk from 90 % (one alarm per ZFS pool or UFS device), memory from 95 %, swap from 50 %, CPU load from 2.0 per thread
  (1-minute load divided by the number of CPU threads). Every threshold can be switched off with 0.
- **Service down** – **off** by default. OPNsense only delivers "running / not running" and "locked", but not whether someone stopped a service
  deliberately. Therefore a **list of monitored services** applies (default: unbound, dnsmasq, dhcpd, dhcpd6, kea-dhcp4, kea-dhcp6, ntpd,
  openvpn, wireguard, strongswan), and only a service that ran during roughly the last two hours and no longer does is reported. A service that
  never ran (such as DHCPv6 without a prefix) stays silent; you take a deliberately stopped service off the list. A firewall lists only services that are
  enabled in its configuration, so a service that is not set up does not appear at all.

### Rule analysis

Under **Rulebase › Rule analysis** you ask which rule applies to a connection: source and destination IP,
protocol, destination port and the ingress interface ("automatic" guesses it from the source IP and marks this as a
note). With "Check egress side" the rules for outgoing traffic are checked as well. The first packet of a new connection is checked against
the rule set actually loaded on the firewall; it is read-only.
Per firewall a card appears with the verdict (Allowed, Blocked, Rejected), the **certainty** (certain,
probable, unknown; tooltip with the reasons) and the deciding rule. If the result is "unknown"
(for example the destination port is missing or an alias cannot be resolved), the card deliberately shows no verdict but
the open points. Under "Why?" every checked rule is listed in evaluation order with ✓/✗/? per field.
The query is in the address bar and can be shared as a link.

#### Cleanup

The **Cleanup** tab (Rulebase › Rule analysis › Cleanup) lists per firewall the rules worth a look. You choose the firewall
and the **threshold** (7, 30, 90 or 180 days); the selection is in the address bar (`?tab=cleanup&host=…&days=…`). The list itself is only read; changing goes through the selection (see below):

| Finding | Meaning |
|---|---|
| **Unused** | OPNexus has observed the rule for at least the threshold and has seen no hit during that time (see "Rule usage"). A new rule appears only after the observation period. |
| **Shadowed** | An earlier quick rule already matches everything this rule matches; the rule never decides. The shadowing rule is shown next to it. |
| **Duplicate** | Like shadowed, but the earlier rule has the same action and matches exactly the same. |
| **Disabled for a long time** | The rule is disabled and OPNexus has seen it unchanged for at least the threshold (from the change history). |

Every finding carries a **certainty**. **Certain** exists only with complete, fresh evidence: for "unused" the rule must be loaded in pf,
without a schedule, and the counters must be fresh; for "shadowed/duplicate" the order comes from the firewall, the shadowing rule is loaded
and the comparison is based on the rules themselves. **Probable** means: it rests on an assumption (the content of aliases, an estimated
order, a rule with a schedule, a missing pf rule set). **Unknown** means: there is a hint without evidence, for example because the rule is not loaded
in pf or the counters are outdated. The tooltip explains each. What is not proven is never "certain".

The **Action** column says what selecting the rule would do (**Disable** or, for an already disabled rule, **Delete**) or why
the rule is not offered: automatic rules (generated by OPNsense), rules in the legacy format (migrate in OPNsense first), rules deployed by OPNexus
(you remove those through the normal flow of the rules page, otherwise OPNexus recreates them) and rules with the finding "unknown". The security posture links
its findings "unused", "shadowed" and "duplicate" directly into this list. Via API: `GET /api/hosts/{id}/rule-analysis/cleanup?unused_days=30`, via MCP: `rule_cleanup_candidates`.

##### Management sources (prerequisite)

Before the cleanup may change anything, you enter the **management sources**: the IP addresses or networks (CIDR, no host names) from which your firewalls are managed
(OPNexus itself, your workstation, a jump host). Until then the tab shows a notice and (for administrators only) a small form; the same is under
**Settings › Rule cleanup**, where you also find the **limit per run** (1 to 25, default 10) and the **waiting time before deletion** (1 to 365 days, default 7).
For every selected rule OPNexus checks whether it can match a packet from a management source to an address of the firewall on a management port (API port from
the host address, 443, 80, 22 and the **further management ports** that you enter under Settings › Rule cleanup, e.g. SSH on 2222 or the web interface
on its own port; up to 20 ports). This is checked on every interface, also for packets over unassigned devices (WireGuard, OpenVPN, IPsec). If the rule matches or
this cannot be ruled out, it is never disabled. In addition there are cluster rules (CARP, pfsync), which are never offered. Re-enabling a block rule
that could hit management access requires explicit confirmation per rule.

> **OPNexus itself is protected automatically.** For every firewall the cleanup asks when planning from which address the firewall sees OPNexus coming (its own
> API connection in the state table, via `diagnostics/firewall/query_states`), and additionally protects that address. The preview shows it per firewall
> ("additionally protected" or "already in the management sources"). If it cannot be determined (the API key lacks the privilege **Diagnostics: States**, or the
> firewall is attached to the management tunnel), the preview says so. Then enter the address yourself (with NAT the sender address after translation, via the
> management overlay the overlay address). If it is missing, a cleanup could disable exactly the rule through which OPNexus reaches the firewall; then the
> automatic reset fails as well. Under Settings › Rule cleanup OPNexus also suggests your browser's address (as OPNexus sees it; the
> firewalls may see it differently). A "savepoint" with reversal by the firewall itself does not exist in OPNsense 26.7 (checked in the source code and live:
> `firewall/filter/savepoint` answers 404); resetting remains OPNexus's job.
> Sources, ports and firewall addresses are visible only to administrators; everyone else only sees whether something is entered.

##### Disable, confirm, automatic reset

1. **Select.** Per run either only disable or only delete, at most as many rules as configured. For "probable" findings OPNexus asks before the preview, per rule
   with a fixed text, whether you have checked that the rule is not needed (schedule, alias content, short observation window, block rule without hits). "Unused" is at most
   "probable" under 30 days of observation and for block rules.
2. **Preview.** OPNexus computes everything fresh from the firewall (in a cluster from all nodes; the rule must have the same UUID on every node and there must be exactly one
   master). Per rule the preview shows action, finding, certainty, the protection check for management access and notes (e.g. "changed externally"). The preview is valid for 5 minutes
   and belongs to whoever created it. Open maintenance, deployments or another cleanup in the cluster block it.
3. **Deploy** (administrators only, with entry of the firewall name). OPNexus checks the state again and writes nothing if it deviates from the preview. Then the rules are
   disabled (in a cluster the backup first) and applied.
4. **Confirm or reset.** The box at the top of the tab shows the countdown. **Confirm** (operator or administrator, **only in a browser session, never via API token**) reads
   every firewall once more beforehand and refuses if it does not respond: that is the signal that you may have locked yourself out. If you do not confirm
   in time, OPNexus resets automatically; with **Reset now** this happens immediately (administrator, or operator if this switch is allowed in the settings). If
   a rule failed or is unclear (status *Partial*), confirming is blocked. If the reset itself fails, **Retry reset** helps. After a reset the
   rule's observation window starts anew.

##### Deleting and restoring

Only rules that are already disabled are deleted: a rule disabled and confirmed, after the waiting time (and only if it has been unchanged and not changed externally since)
or a rule disabled by hand with the finding "disabled for a long time" (certain). A disabled rule is not loaded in the firewall, so deleting changes nothing there
and therefore has no confirmation window. Beforehand OPNexus saves the complete rule content including position; without a backup nothing is deleted. Under **Cleanup history**
(at the bottom of the tab) every row shows its steps; for deleted rules there is **Restore** (administrators only, with the firewall name): the rule comes back from the backup, **disabled**,
with a new UUID (in a cluster on all nodes) and, if possible, directly before its former successor. If the position cannot be restored, the rule is placed at the end of its group and
OPNexus points this out. If the firewall in the meantime lacks an alias or a category of the rule, it refuses and its message appears unchanged in the result. To switch it on there is
**Re-enable** (also for rules disabled and confirmed): a separate run with a confirmation window, because switching on a block rule can lock you out.

Permissions at a glance: preview, cancel and confirm are allowed to operators and administrators; deploy, retry and restore only to administrators; read-only accounts see the list and the history
but no buttons. Every step appears per rule in the audit log and in the rule's change history (origin "via OPNexus").

#### History of a rule

Every 15 minutes OPNexus reads the rules of each firewall (two read accesses) and compares them with the last seen state – including rules that do not
come from OPNexus. Every change becomes an event: **created, changed** (with before/after of the affected fields), **moved** (only order or group), **enabled, disabled, removed**. The **origin**
is "via OPNexus" if the audit knows of a deployment or reset of this rule on this firewall since the previous observation (and not yet for any other change) (with the user), otherwise "external" (in the
firewall's interface, by script or through another access). The first observation of a firewall only stores the initial state, no
"created" events arise for existing rules; an empty or incomplete response from the firewall is discarded, and if more than half of the rules disappear at once, only the second identical observation counts; a change between two observations appears at the time of the second. Events are kept for 365 days.

The history is visible in the **live view** (Rulebase › Live across all hosts) via the row action **History**, in the cleanup list at the icon next to the rule,
and via API (`GET /api/hosts/{id}/live-rules/{uuid}/history`). For a rule managed by OPNexus, the stored versions and the audit entries of the
rule are shown as well. Automatic rules are not tracked.

### Security posture

Under **Monitoring › Security posture** OPNexus checks every firewall for typical
weaknesses and assigns a score from 0 to 100 (from 90 good, from 70 medium, below that
poor). The check downloads the firewall's configuration once, read-only,
and evaluates it in memory; nothing on the firewall is changed and the
configuration is not stored. The check runs by itself every six hours;
"Check all" and "Check again" start it immediately.

| Check | Weight | When a finding arises |
|---|---|---|
| No allow-all rule on the WAN | critical | An active rule on the WAN (or on an interface with an upstream gateway) allows all traffic from anywhere to anywhere |
| Web interface only over HTTPS | high | The management interface runs over HTTP |
| SSH secured | high / medium | SSH is on and allows password login (high if root may also log in by password) |
| SNMP community not "public" | high | SNMP is on and uses "public" |
| No allow-all rule with any source (internal) | medium | An internal rule allows any source to any destination (the default rule "LAN to any" does not count) |
| No shadowed rules | medium | A rule is never reached because an earlier one already covers it |
| Firmware up to date | medium | Updates are available |
| Recent backup | medium | The newest local backup is 7 days old or older |
| No duplicate rules, rules labelled, time server, external logs | low | Duplicate rule, rule without description, no time server, no syslog target |
| Intrusion detection active | note | IDS/IPS is off (costs no points) |

A critical finding costs 30 points, a high one 15, a medium one 8, a low one 3.
Checks that cannot be decided (such as the update status of a currently unreachable
firewall) cost nothing. OPNexus reports shadowed or duplicate only when it can be
proven: rules are compared field by field in the firewall's order (floating, interface groups, interface rules),
networks and port ranges as sets; only an earlier quick rule can shadow a later one, and
rules with a schedule, tag or limits shadow nothing. Aliases count as equal only if they have the same name; this way
no false findings arise, but an overlap across different aliases remains undetected here (the cleanup also compares
static alias contents, there as "probable"). Both rule formats (OPNsense 26.x and older installations) are read.
The values are also available as Prometheus metrics (see below).

The **dashboard** shows the firewalls with the lowest score. The **Report** button produces a printable overview
of all checked firewalls with their open findings and hints for remediation; in the browser choose "Print / Save as PDF". In Settings › Notifications you can switch on **"Critical or high security finding"**
(off by default, so that an existing fleet does not suddenly report all old findings at once): per
firewall and check a message is sent when it occurs, and a "resolved" message when it has disappeared after the next
check.

### About OPNexus

A click on the logo opens **About**: all hosts with status, CARP role, cluster,
installed OPNsense version and rule count. The values load per host; an
unreachable firewall shows "not available". The own version appears in the footer
of every page and in the settings.

---

## Automation: API tokens, Prometheus, AI assistants

Under **System › API tokens** you create personal tokens. With a token,
scripts, CI jobs, Prometheus and AI assistants talk to the OPNexus API without
logging in:

```
curl -H "Authorization: Bearer opx_…" https://opnexus.example/api/hosts
```

A token belongs to your account and never has more privileges than you. It is displayed
only once and stored only as a hash. You need your password to create one. The default
lifetime is 90 days; you can revoke any token at any time, and a
password reset of the account revokes all its tokens. At most 50 tokens per user.

| Scope | May |
|---|---|
| **Read** | View everything (GET), except things with secrets: raw download of the configuration backups and the notification settings. For dashboards, Prometheus, AI assistants. |
| **Deploy** | Additionally deploy and maintain: rules, aliases, NAT, VPN, certificates, maintenance, security check. Not: users, settings, notifications, creating or deleting hosts, OPNexus update. |
| **Admin** | Everything your account may do, including the things above. |

Plan and export of [configuration as code](#configuration-as-code-yaml-git-ci) already work
with "Read"; applying only with "Admin".

For every scope: managing tokens and your own account (password, two-factor)
works only in a normal login, never with a token. A read-only account can
create only "Read" tokens; if the account is demoted, all its tokens
immediately become read-only. In the audit log, changes made via token appear as "User (API: name)".

**API description:** the OpenAPI schema is at `/api/openapi.json` (retrievable with
login or token) and can be loaded into Postman, a code generator or
an assistant. There is no interactive page. Errors arrive as
`{"detail": {"code": "…", "params": {…}}}` with a stable `code`.

### Configuration as code (YAML, Git, CI)

Under **System › Configuration as code** (or via `opnexus export` / `opnexus apply`)
all **centrally maintained objects** can be **exported** as a single YAML file and
**imported** again: device groups, hierarchy groups, aliases, filter rules,
port forwards, 1:1 NAT, outbound NAT, hierarchy rules and the values of the
[site variables](#site-variables). For Git management, reviews, CI checks and
restoring onto a new instance.

**What is not included:** hosts themselves (address, API key, pinning), users,
tokens, SSO, settings, notifications, maintenance windows and rollouts,
network objects, VPN and certificates. The file contains no credentials, keys or
passwords; hosts appear only with their name.

**The import changes only the drafts in OPNexus.** Nothing reaches firewalls
without preview → deploy → confirm. The import code calls no firewall command
(a test enforces this) and never deletes an object that has ever been deployed.

**Flow:**

1. **Plan:** OPNexus compares the file with the current state and shows per object
   *new*, *changed* (with fields before/after), *removed* or *unchanged*,
   plus notes. The plan writes nothing and may also be retrieved by read-only accounts
   and with a "Read" token (for example for a check in a pull request).
2. **Apply:** only **administrators** (session or admin token). Applying
   writes everything in **one** transaction or nothing. It requires the token of exactly this plan: if something changed
   since the plan (even just a group member or a new object in the interface),
   OPNexus answers "outdated" and you plan again. Finally OPNexus verifies that
   the database corresponds exactly to the plan; otherwise everything is reverted.
3. **Afterwards** the changes appear as drafts and are deployed per object
   as usual. Changed objects that are already deployed are named by the plan ("deployed to N
   firewalls").

**Partial files:** if a section is missing in the file, that kind remains untouched. If a
section is present, even an empty one, and **prune** is on, that kind is fully
reconciled: what is missing in the file is removed from OPNexus. Without prune, objects are only
created and changed. Prune is powerful and therefore an explicit switch
(`--prune`, in the interface with confirmation).

**Blocked** (the plan cannot be applied, nothing is changed):

- An object with an **open deployment** (waiting for confirmation or rollback) is to be
  changed or removed.
- Prune of an object that has **already been deployed** (also rolled back or
  failed). Remove it in the interface first, where there is a preview and a way back.
- Prune of a group, an alias name or a hierarchy group that is **still in use** after the
  import (by rules, VPN, rollout waves, hosts, child groups).
- Errors in the file itself (unknown host, group or hierarchy group, duplicate
  key, cycle in the hierarchy tree, a host in two hierarchy groups, placeholders
  `{{…}}` in hierarchy rules) reject the plan already, with line and path.

**Keys:** aliases, groups and hierarchy groups are recognised by their **name**,
rules, NAT objects and hierarchy rules by a **`key`**
(lowercase letters, digits, `.` `_` `-`, at most 64 characters, unique per kind). Existing
objects automatically received one on update (`r-…`, `pf-…`, `nat1-…`, `out-…`, `ph-…`);
in the file you may assign your own (`allow-dns-lan`), and the import stores them. A
**rename** counts as removing and recreating; a deployed object then blocks
with prune. The order of hierarchy rules is the order in the file.

**Notes in the plan:** a value that looks like an alias but is not a central alias in OPNexus (aliases created by hand on the firewall are fine; OPNexus queries no firewall when planning); a variable that is removed
although an object uses it (deployment to this host then fails safely);
a disabled target host; new objects with targets without a link to objects already present on the
firewalls. The latter is important for an import into a **new
instance** with the same firewalls: deploying creates duplicates there. Better restore a
full backup (`opnexus backup --full`) or assign the objects beforehand via
"Link with existing central object".

**Format:** one file `opnexus-config.yaml`, `opnexus_config: 1`, fixed order of the
sections, objects sorted by key, all fields always, no timestamp: Git
shows only real changes. The JSON schema is in
`backend/app/config_code/opnexus-config-v1.schema.json` (for editor validation). Descriptions
may be multi-line, names and keys may not; ports may be a number or text.
The file may be at most 1 MiB (5000 objects per kind, 200 variables per host).

**Strict validation:** the file is read only as data. Anchors and aliases (`&`, `*`),
merge keys (`<<`), explicit tags (`!!python/…`, `!!binary`), directives, multiple
documents, duplicate keys, control characters and invalid UTF-8 are rejected.
`on`, `yes`, dates and `1:30` remain text (no "YAML 1.1" behaviour). Errors
name line and path (`rules[3].destination_port`).

**On the server (CLI):**

```
opnexus export [--out DIRECTORY] [--kinds rules,aliases]
opnexus apply FILE [--dry-run] [--prune] [--yes] [--json]
```

`apply` shows the plan first and asks `[y/N]` (without a terminal only with `--yes`). With
`--dry-run` nothing is applied. Exit codes for CI: `0` applied or no
differences, `2` differences (`--dry-run` only), `3` file invalid, `4`
blocked or outdated, `1` other error. Whoever operates the server console needs
no token; the audit log shows `cli (host console)`.

**Git recipe** (cron or systemd timer on the OPNexus server):

```
opnexus export --out /srv/opnexus-config \
  && git -C /srv/opnexus-config add -A \
  && git -C /srv/opnexus-config commit -m "OPNexus state $(date -u +%F)" \
  && git -C /srv/opnexus-config push
```

OPNexus itself never connects to a Git server and needs no credentials for it.

**CI without access to the server:** via API with a token. The plan is enough for
checks in pull requests (token "Read"), applying needs "Admin":

```
curl -s -H "Authorization: Bearer opx_…" -H "Content-Type: application/json" \
  -d "$(jq -n --rawfile y opnexus-config.yaml '{yaml:$y, prune:false}')" \
  https://opnexus.example/api/config/import/plan
```

The response contains `summary`, `items`, `warnings`, `applicable` and `token`; this
token belongs to `POST /api/config/import/apply` (`{"yaml", "prune", "plan_token"}`). Export:
`GET /api/config/export` (`?kinds=rules,aliases`). In the audit log every apply leaves a
summary entry (SHA-256 of the file, counters, prune) and one entry per changed object; the content
of the file is not in it.

### Prometheus

`GET /api/metrics` delivers the state in Prometheus text format. The response comes
only from the OPNexus database (no requests to the firewalls), so frequent scraping
puts no load on anything. With a "Read" token:

```yaml
scrape_configs:
  - job_name: opnexus
    scheme: https
    metrics_path: /api/metrics
    authorization: { credentials: "opx_…" }
    static_configs: [{ targets: ["opnexus.example"] }]
```

| Metric | Meaning |
|---|---|
| `opnexus_info{version}` | Version, always 1 |
| `opnexus_hosts` | Number of firewalls |
| `opnexus_host_up{host,cluster}` | 1 if the host responded within the last 3 minutes |
| `opnexus_host_enabled`, `…_last_sample_timestamp_seconds` | Monitoring on; time of the last measurement |
| `opnexus_host_memory_used_percent`, `opnexus_host_load1`, `opnexus_host_disk_used_percent` | Last measurement |
| `opnexus_host_carp_master` | 1 = MASTER, 0 = BACKUP (missing if unknown) |
| `opnexus_host_swap_used_percent` | Swap usage of the last measurement (missing without swap) |
| `opnexus_host_gateway_up{gateway}` | 1 as long as OPNsense does not list the gateway as down |
| `opnexus_host_gateway_loss_percent`, `…_delay_seconds`, `…_stddev_seconds` `{gateway}` | Packet loss, round-trip time and deviation to the monitor (missing without measurement) |
| `opnexus_host_api_rights_missing{read}` | 1 as long as the firewall denies the read access `read` (gateways, services, swap, cpu) because of missing API privileges; otherwise the line is absent |
| `opnexus_host_service_running{service,instance}` | 1 = running, only for services on the monitoring list |
| `opnexus_host_compliance_score`, `opnexus_host_compliance_findings{severity}` | Security posture |
| `opnexus_pending_actions{kind,status}` | Open actions as on the dashboard |
| `opnexus_problems_open{type}` | Open problems of the notifications |
| `opnexus_scheduled_maintenance_pending` | Scheduled maintenance not yet run |
| `opnexus_rollout_runs{status}` | Fleet rollouts by status (waiting, running, paused, successful, …) |

A ready-made **Grafana dashboard** is at [`docs/grafana-opnexus.json`](grafana-opnexus.json)
(Grafana › Dashboards › Import, choose the Prometheus data source): reachable firewalls, open actions
and problems, security score and findings, load, memory, swap, disk and CARP role as well as gateways (reachable, packet loss, latency),
stopped services, missing API privileges and rollouts by status.

### AI assistants (MCP)

OPNexus speaks the Model Context Protocol so that an AI assistant can look at the fleet,
**read-only**: it cannot change anything. Example for Claude Code:

```
claude mcp add --transport http opnexus https://opnexus.example/api/mcp \
  --header "Authorization: Bearer opx_…"
```

The tools: `fleet_overview`, `list_hosts`, `host_resources`, `carp_events`,
`recent_audit`, `search` and `api_get` (reads any other GET endpoint). Everything runs
with the privileges of the token: a "Read" token sees no secrets here either.

---

## Operating OPNexus

### Data and volumes

| Volume | Content |
| --- | --- |
| `postgres_data` | the database (hosts, rules, history, audit, users) |
| `postgres_backups` | automatic and manual database backups |
| `secrets` | API secrets of the firewalls |
| `backups` | configuration backups of the firewalls (XML) |
| `ipsec_psk_secrets`, `openvpn_key_secrets`, `wireguard_key_secrets` | VPN keys |
| `cert_key_secrets`, `acme_secrets` | private certificate keys, ACME account data |
| `totp_secrets`, `notification_secrets` | two-factor secrets, webhook/SMTP credentials |
| `caddy_data`, `caddy_config` | key of the internal CA and proxy configuration |
| `updater_state` | token of the updater (only with an active updater) |

### Noticing an OPNexus outage (heartbeat)

If OPNexus fails, every alarm it would send falls silent too. An external monitor helps against this: enter
under **Settings › Operating settings › External monitoring and mail** the ping address of such a
service (healthchecks.io or self-hosted, a push monitor in Uptime Kuma, an ntfy topic). OPNexus calls
it with `GET` after **every successful check cycle** of the notifications, so about once a minute.
If the call stops, OPNexus has failed or can no longer reach its database; the monitor then raises the alarm. Set
its grace period to a multiple of the check interval (e.g. 5 minutes).

Only `https://` is allowed; local addresses, link-local and metadata services are excluded, as with the notifications.
The address usually contains a secret key: after saving it is no longer displayed
and never logged. Alternatively use `HEARTBEAT_URL` in the `.env` (which then takes precedence).

**Disk of the OPNexus server.** If the disk that OPNexus runs on fills up, the database stops and with it
every alarm. The alarm "OPNexus server disk almost full" (Settings › Notifications, on by default,
from 90 %) warns beforehand; the metric `opnexus_server_disk_used_percent` shows the fill level. It measures the
file system of the Docker volumes (in a standard installation the same disk as the database).

### Several backends on one database

The background services (reverting unconfirmed changes, notifications, log fetching, measurements, scheduled
maintenance and rollouts, security posture, audit export, scheduled backups, rule history) always run in only
**one** backend process, the *leader*. It holds a lock in the database for this. A second backend on the
same database (for example during a migration or later as a reserve) answers the API normally but waits with the
background services in *standby*. If the leader fails or loses its connection to the database,
it stops its services immediately; a waiting backend takes over within half a minute. On a database that
is currently a replica, a leader never starts.

Which role a backend has is shown by `GET /api/health` (`"role": "leader"`, `"standby"`, `"takeover"`, `"replica"`)
and the metric `opnexus_leader`. For a single installation nothing changes: its only backend is the
leader; the background services start after boot with a delay of about 12 seconds.

### Backup and restore

**Automatic:** the service `postgres-backup` writes a dump to `postgres_backups` on startup and then every
24 hours and keeps 14 days. In addition a dump is created before every schema migration (the last 5 are kept).
**Manual:** `./opnexus backup` immediately writes `manual-<time>.dump`.

**Full backup (recommended):** the dump alone is not enough to rebuild an OPNexus server,
because the secrets live in separate volumes. `./opnexus backup --full [FILE]` packs the database
and all secret volumes (firewall credentials, two-factor, VPN and certificate keys,
configuration backups) into **one file encrypted with a passphrase** (`.opxfull`). The passphrase
is asked for (or `OPNEXUS_BACKUP_PASSPHRASE`). On a fresh installation (same or
newer version) `./opnexus restore FILE` restores everything: it checks the file, asks for confirmation, stops the
services, creates a safety copy of the current state, restores database and volumes and
restarts. Not included: log collection and measurements. Keep file and passphrase apart from the server.

With `--with-env` and `--with-ca` the full backup also includes the `.env` and the proxy's internal CA
(`caddy_data`); `./opnexus restore FILE --with-env --with-ca` restores them. From the `.env` the
restore takes everything **except** the database credentials, the version and the addresses of the target server (`POSTGRES_*`,
`OPNEXUS_VERSION`, `OPNEXUS_IMAGE_REPO`, `OPNEXUS_SITES`, `OPNEXUS_DEFAULT_SNI`, `FRONTEND_ORIGIN`, `*_BIND_ADDRESS`,
`OPNEXUS_GUI_SITES`); it shows the adopted names and asks. The saved file then lies completely
next to it as `.env.from-backup`, the previous one as `.env.before-restore`. With the CA, browsers that already
trust it also trust the new server. `./opnexus status` shows when the last full backup was made.

### Migration and emergency (cold standby)

If the OPNexus server fails, the firewalls keep running unchanged; only management, alarms and scheduled
maintenance stand still. For this case you keep a **cold standby**: a full backup with `.env` and CA and
a second server on which OPNexus can be installed.

**Emergency (the old server is gone):** set up a new server following [Installation from the release bundle](#installation-from-the-release-bundle)
with the same or a newer version, then `./opnexus restore FILE --with-env --with-ca`. Afterwards point the address
(virtual IP, DNS name) to the new server. Unconfirmed changes whose window
expired during the outage are reverted by the new server immediately: when in doubt, the old state applies.

**Planned migration (both servers running):** on the old server

```bash
./opnexus migrate-host root@new-server            # --dir PATH if OPNexus is located elsewhere there
```

Prerequisites: SSH key login to the target, OPNexus installed and started there (same or newer
version). The tool checks the target, stops the old server (no changes from then on), creates a
full backup with `.env` and CA (random passphrase that only travels over the SSH connection), restores it on the target,
compares the number of firewalls and reports the duration. In the lab a migration with three firewalls took
**90 seconds** from the pre-check to a healthy target; the way back to the old server a good 30 seconds. The old server stays stopped, not deleted: going back works with `docker compose stop` on the
target and `docker compose up -d` on the old server.

**To check afterwards:**
- Firewalls that open their API only to certain senders must allow the address of the new server.
- With the [management overlay](#firewalls-behind-nat-management-tunnel) firewalls dial in through a public
  address; it must point to the new server (the keys come with the backup).
- Open windows of the firewall interface through OPNexus break off and are reopened.

### High availability (warm standby)

For a switchover in minutes instead of hours you run **two nodes**: the **primary** (database writable, full
stack, holds the shared address, the **VIP**) and the **standby** (database as a streaming replica, only `postgres` and
`backend` run). The database is replicated over TLS and also contains the logs. Switching happens **only by hand** on the
standby (`opnexus ha promote`); there is deliberately no automatic failover, because two nodes without a third witness cannot
tell an outage from a network partition, and two primaries with access to the same firewalls do more
harm than a failed OPNexus (the firewalls keep running unchanged without OPNexus).

**Prerequisites:** two Linux servers in the same network (L2), on both OPNexus of the **same version** from the release bundle,
SSH key login from the primary to the standby (as root), `openssl`, NTP on both, a free address as VIP (`ha init` checks that nobody uses it). On the
standby enough space for the database (database plus reserve; the server's disk warning applies on both nodes).
The HA commands run on the host, not in the updater container (VIP and cron need the host).

**Setting up without SSH (recommended, from 1.37):** on the **standby** run `./opnexus ha prepare`. This switches on the updater and the HA gate
(port 8445) and prints a **pairing code**, valid for 30 minutes and single use. On the **primary**:

```bash
./opnexus ha init --code 'OPNX1-…' --vip 192.168.2.22/24     # --node a --peer-node b --node-ip --vip-dev --yes
```

The command checks beforehand version, release mode, space on the partner, whether the VIP is free and whether the partner's proxy answers, then pairs both nodes (the
partner's certificate must match the pin in the code before anything is sent) and sets up the pair. Prerequisites: same version on both nodes, NTP,
port 8445 open between the nodes (access lists), `openssl` on the primary (for the replication certificate). SSH keys, root access between the nodes and the cron are no longer needed:
fencing, takeover, resync, sync and update run through fixed verbs of the agent, the secrets sync and the VIP follow-up through its timer.
The gate listens only on the node's address (line `HA_GATE_BIND` in the `.env`, set by the tool) and runs without root. It forwards only the paths for pairing and the partner verbs to the updater; everything else answers 404, without the secret 401. The updater thus has more tasks than
updating and is still reachable only from the Compose network (backend) and through the gate (partner). If setup fails after pairing, a new code is enough
(`ha prepare` on the standby); the half-finished pairing is discarded.

**Setting up and operating in the interface (from 1.37):** Settings › System › High availability. On the future standby first log in and choose "Prepare this server as standby": the card shows the pairing code
(30 minutes, single use). On the future primary "Set up HA": paste the code, enter the shared address (VIP) with the prefix length (name, address and interface of the node are suggested and can be adjusted under "change"),
"Check prerequisites" (changes nothing: version, space, VIP free, partner reachable), then "Set up now" (the page shows the log; the backend restarts in the process, the dialog stays open). With a pair the card offers
**Sync now**, **Rebuild standby** (after a switch or if the standby is behind), **Switch to partner** (planned, without data loss) and **Dissolve HA** (the partner is emptied and afterwards is an empty
single installation with setup assistant). Every action asks for the logged-in administrator's password. If the card shows "Agent not reachable", the updater container is not running (profile `updater`). Notification
"HA agent not responding": this node's agent has been silent for over 5 minutes (check the updater container).

**Switching an existing SSH pair to the agent:** bring both nodes to the same version with agent (`./opnexus ha update`), then on one node `./opnexus ha migrate-agent` (port 8445 open between the nodes). The command uses SSH one last time,
pairs both gates, checks them with a ping and only then removes the cron of both nodes; if anything fails, the SSH pairing stays in place. Roles, epoch and data do not change.

**Setting up via SSH (older variant, stays for at most two minor versions):** on the future primary; the standby is overwritten in the process:

```bash
./opnexus ha init root@192.168.1.21 --vip 192.168.1.22/24     # --node-ip, --peer-ip, --vip-dev, --peer-dir, --node, --peer-node
```

`ha init` creates a cluster CA and a server certificate for the database (SAN: both nodes and the VIP), sets up on the
primary the replication role, `pg_hba.conf`, the replication slot and the cap for retained WAL (`max_slot_wal_keep_size`
= 4 GB), publishes the database port on the LAN address (file `docker-compose.ha.yml`, a short restart of the
database), creates a shared SSH key for both directions, builds the standby from a base backup, sets the VIP and
sets up the cron (`/etc/cron.d/opnexus-ha`: sync every 5 minutes, VIP after a reboot). `OPNEXUS_SITES` and
`FRONTEND_ORIGIN` then contain the VIP; the proxy CA is the same on both nodes, so browsers trust both.

**Where the state lives:** `.opnexus-ha/state` in the installation directory (role, node, addresses, VIP, replication password; readable by root only). Only the tool maintains this
file; you never have to touch the `.env` for it. The `.env` contains only `COMPOSE_FILE` (so that a `docker compose up -d` run by hand does not remove the
database port) and a derived line `HA_ROLE`, which older tools need; if they differ, the file wins. Pairs from earlier versions are migrated automatically on the
first HA command. The file does not belong in backups or migrations (`migrate-host`): a restored server is not a pair node.

**Operation:**
- `./opnexus ha status` shows both nodes: role, epoch, version, replication lag, slot fill level, age of the last
  secrets sync, clock offset and whether the secret file exists for every firewall and every two-factor account.
- `./opnexus ha sync` (on the primary, otherwise from cron) transfers the secrets (they are not in the database), the overlay,
  new configuration backups of the firewalls, the proxy CA and a fixed list of shared `.env` settings (login, cookie, update check, certificate check; ports, addresses, registry and profiles stay per node). The
  secrets are therefore up to 5 minutes old; `ha promote` first checks that a file exists for everything in the database.
- **One-click updates in the web interface are blocked** as long as HA is set up, and so is `opnexus update`. Updating is done with
  `./opnexus ha update [VERSION]` on the primary: first the standby (pull images, verify signature, restart backend; it never
  migrates), then the primary as usual (backup, migration, which flows to the standby via replication), finally the check that
  both nodes have the same version and revision and the standby is streaming. An automatic rollback on the primary restores a dump;
  the standby follows by itself, the log history is empty on both afterwards.

**Taking over in the browser (standby):** the full stack runs on the standby. Whoever opens its address (not the VIP) lands on **/standby**. There a local
administrator logs in (password and, if set up, the authenticator app code; recovery codes do not work on the standby; the login is valid for 15 minutes). The page shows the state
of the pair and the button "Take over now". If the partner's agent is reachable, the partner is stopped first (like `ha promote`). If the pair still runs over SSH (without agent pairing), the page cannot stop the partner and does not offer takeover: then `./opnexus ha promote` in the terminal or beforehand `./opnexus ha migrate-agent`. If the partner does not answer at all, the page requires the
typed sentence **alter Primaer ist aus** ("old primary is off"); the prerequisite is that it is really switched off. If it still answers as leader (only the connection to its agent is missing), the page refuses the
takeover. After about a minute this node is the primary; who took over appears in the audit log. The terminal (`./opnexus ha promote`) remains the equivalent way back, even if no
local administrator account with a synchronised second factor exists (pure SSO accounts cannot log in on the standby).

**VIP and old primary:** the agent follows up the VIP every minute. It belongs to the primary only as long as its backend is not blocked by the partner watchdog (`fenced`) and the partner does not answer as
leader with a **higher epoch**; an old primary after `promote --force` therefore releases it by itself as soon as the new one is reachable. A single failed query does not count as
"no leader" (it is asked twice). `ha sync` synchronises the overlay and GUI profile (`COMPOSE_PROFILES`) together with `GUI_PORTS`/`OVERLAY_PORT` to the partner, so that after a switch `wg-hub` and
`gui-gateway` run there; `updater` and `ha` remain the business of the respective node.

**Switching (the primary has failed or is to be serviced):** on the standby

```bash
./opnexus ha promote
```

The tool checks replication state and secrets, **first stops the old primary** (fencing: stop all services, release the VIP,
`HA_ROLE=fenced`), promotes the database, increases the **epoch**, restarts the backend and the other services and
sets the VIP. If the old primary is not reachable, it aborts: then switch it off safely (power, hypervisor), verify that it is
off, and call `./opnexus ha promote --force` (requires the typed confirmation "alter Primaer ist aus" / "old primary is off"). In the lab
the VIP was reachable again after about 12 seconds, the background services (alarms, scheduled maintenance) run after about half a minute.
**The old primary** is brought back as the new standby with `./opnexus ha resync` (run there); there is no need to switch back.
After a forced switch (`--force`) `HA_ROLE=primary` is still set there; then `./opnexus ha resync --from-peer` applies. It runs only if the
partner has a higher epoch, i.e. demonstrably took over later, and otherwise leaves everything untouched. `ha promote --force` refuses as long as the
partner still answers as leader at its address (SSH alone is then merely disturbed): switch it off safely first.

**Alarms and metrics:** the leader checks five things (a switch under Settings › Notifications, on by default): standby
has not been streaming for 5 minutes, replication lag over 64 MB or 60 s, replication slot retains more than 50 % of the cap or
is missing/lost ("Rebuild standby"), last secrets sync older than an hour, partner runs as a second primary
(split brain). On the dashboard they appear among the health problems. For Prometheus there are `opnexus_ha_role`, `opnexus_ha_epoch`,
`opnexus_ha_replication_lag_bytes`, `opnexus_ha_slot_retained_bytes`, `opnexus_ha_sync_age_seconds` and `opnexus_ha_split_brain` (even a
fenced node answers the scrape). Settings › System shows the card "High availability" with the state of the pair.

**Protection against two primaries:** every backend asks the partner (`https://PARTNER/api/health`, with its port for a different `PROXY_HTTPS_PORT`; `ha init` checks the address beforehand) for role and epoch before it starts the
background services, and then every 30 seconds. If the partner reports `leader` with the same or a higher epoch, this
node runs in the state `fenced` without background services and reports `split_brain`, and it rejects all modifying API calls with 503 (reading and logging in continue); an unreachable partner is no objection
(stopping it is the job of fencing). After a restart a primary does not set the VIP as long as the partner is leader.

**Running work during a switch:** unconfirmed changes with an expired window are reverted by the new primary. Overdue
rollout steps are **postponed** in the 10 minutes after a switch instead of being discarded as "not started". Scheduled
maintenance that was in the middle of starting at the time of the outage is closed by the new primary after 15 minutes as "interrupted" (the operation stays
blocked until someone has checked the firewall; a rollout pauses).

**Limits:** IPv4, one network (L2) with a shared VIP, exactly two nodes. Replication is asynchronous: the last seconds before an
outage may be missing (drift detection finds deviations on the firewalls). If the standby is down longer than the
retained WAL lasts (cap 4 GB, about 12 hours on average), it loses its slot; the primary keeps running, and
`ha resync` rebuilds the standby. Firewalls that open their API only to certain senders must allow both node addresses.



**What is not in the dump:**
- The tables of **log collection** and **system load** (schema only).
  After a restore they are empty and fill from the firewalls.
- **Secrets** (API secrets, VPN and certificate keys, two-factor): they
  live in the separate volumes. For a complete backup use `./opnexus backup --full` (see above).

**Restoring** into an **empty** database (not with `--clean` into an
existing one):

```bash
docker compose exec postgres-backup ls /backups                    # list dumps
docker compose cp postgres-backup:/backups/<file>.dump /tmp/restore.dump
docker compose cp /tmp/restore.dump postgres:/tmp/restore.dump
docker compose stop backend frontend
docker compose exec -e PGPASSWORD=<PASSWORD> postgres \
  psql -U opnexus -d postgres -c "DROP DATABASE opnexus; CREATE DATABASE opnexus OWNER opnexus;"
docker compose exec -e PGPASSWORD=<PASSWORD> postgres \
  pg_restore -h localhost -U opnexus -d opnexus /tmp/restore.dump
docker compose up -d backend frontend
```

A dump can only be restored into a Postgres of the same major version
(currently 16). If you roll back after an update, it is best to use `./opnexus rollback`.

### Updating OPNexus

An update replaces only the code; the data stays in the volume, the configuration in
the `.env`.

**With `opnexus`** (a single Bash file, needs only Docker; in the directory with
`docker-compose.yml` and `.env`, or set `OPNEXUS_DIR=…`):

| Command | Effect |
| --- | --- |
| `opnexus status` | version, database revision, state of the containers |
| `opnexus update [VERSION]` | pulls the new images **before** anything is touched, switches, waits until everything is "healthy", otherwise automatic rollback |
| `opnexus rollback` | restores the version and, if the update migrated the database, the database from the backup before the update |
| `opnexus backup` | immediate database backup |
| `opnexus password [USER]` | set and display a new random password |

`status`, `update` and `rollback` need release images (`OPNEXUS_IMAGE_REPO`
set); `backup` and `password` work in any installation.

The output language of the tool follows `OPNEXUS_LANG` (`de` or `en`), otherwise your locale (`LC_ALL`, `LANG`):
German with a German locale, English otherwise.

`OPNEXUS_VERSION` in the `.env` is the **track**: `1.16.0` pins exactly, `1.16` follows
patch releases, `1` follows all 1.x. Without an argument `opnexus update` fetches the newest
version of the track, with an argument the track is switched. After a rollback the
version is pinned to the old state.

**The Compose file comes along (from 1.34).** `opnexus update` also adopts the `docker-compose.yml` of the target release
so that new service settings arrive. It comes from the signed updater image (only with `cosign` and
`cosign.pub`, like the CLI itself). The previous file stays as `docker-compose.yml.bak-<version>`, a
rollback puts it back. If you have **customised the file yourself** (it differs from the one last adopted), it stays
unchanged; the new version then lies next to it as `docker-compose.yml.new`. Your own customisations therefore belong
in a `docker-compose.override.yml` in the same directory: Compose reads it automatically, and updates can
keep replacing the main file. On the first update with this function no checksum is known yet; then
it is replaced and `diff docker-compose.yml.bak-<version> docker-compose.yml` shows what changed.
`OPNEXUS_NO_COMPOSE_REFRESH=1` switches the adoption off.

**Rollback with database restore** means: everything is back to the state before
the update, whatever was written afterwards is gone, and log history and
measurements are empty. Beforehand the tool creates a safety copy of the current
state (`pre-rollback-….dump`), checks that the backup is readable and restores
the copy on an error. If the update did not change the database,
nothing is restored. `OPNEXUS_UPDATE_TIMEOUT` (seconds, default
240) extends the waiting time for long migrations, `OPNEXUS_NO_AUTO_ROLLBACK=1`
switches off the automatic rollback.

**Changing the registry (by hand only).** Existing installations stay with the registry that is in their `.env`
(`OPNEXUS_IMAGE_REPO`); neither `opnexus update` nor a rollback, a restore or a bundle ever changes it.
Whoever wants to switch from `git.opnexus.dev/opnexus` to `ghcr.io/opnexus-dev` (or back) does so separately from a
version update and first verifies the signature of the running version in the new registry:

```bash
opnexus status                                                  # note the running version
cosign verify --key cosign.pub --insecure-ignore-tlog=true \
  ghcr.io/opnexus-dev/opnexus-backend:<running version>         # same digest as in the old registry
sed -i 's|^OPNEXUS_IMAGE_REPO=.*|OPNEXUS_IMAGE_REPO=ghcr.io/opnexus-dev|' .env
docker compose pull && docker compose up -d --no-build         # same images; Compose may recreate backend, frontend and proxy (short interruption)
```

A rollback starts the previous version with `docker compose up --no-build`; if its image is missing locally, Compose pulls it from
the registry in the `.env`. After a switch the previous version must therefore be there too; check this with the same
`cosign verify` for the previous version before you switch. If a tag is missing, an update aborts with "Pull failed —
nothing was changed", a rollback would fail.

**Installations built from source** are updated with
`git pull && docker compose up -d --build`.

**Schema migrations** run when the backend starts: if the database is behind the
code version, a dump is created **first**; if it fails, no migration happens and
the start aborts with a clear message. Then the migration runs in one
transaction, an error rolls everything back. If a **newer** version has already migrated the
database, older code does not start (start the newer version
or restore a backup). There is no schema downgrade, the way back is
the backup. Installations up to 1.8.x go to 1.9.x first.

### Notice about new versions

Optionally OPNexus shows administrators a notice at the top when a newer version
has been published. It is switched on under **Settings › Update OPNexus**
(switch "Enable version check", feed address `https://updates.opnexus.dev/latest.json`)
— no change to the `.env` is needed. Whoever wants to fix the check centrally can
instead write `UPDATE_CHECK_URL=https://updates.opnexus.dev/latest.json` into the `.env`;
then the switch in the web interface is locked.

By default it is **off**, and OPNexus makes no outgoing request. When switched on,
at most every 12 hours (`UPDATE_CHECK_INTERVAL_HOURS`) a single `GET` goes out with
the user agent `OPNexus/<version>`, without instance or user data. A
unreachable feed stays unobtrusive; pre-releases are never suggested. The
notice installs nothing.

### One-click updates (optional)

Instead of `opnexus update`, an admin can under **Settings › Update OPNexus**
view version and release notes, update, follow progress and, if needed,
reset to the previous version. A failed update rolls back automatically.

This is **deliberately off** and must be enabled because it needs Docker access:

```bash
./opnexus updater enable
```

This writes `COMPOSE_PROFILES=updater` into the `.env`, pulls the updater image
and starts the container. Under **rootless Docker** it additionally detects the
socket path and stores it as `DOCKER_HOST_SOCKET` in the `.env` — a value already
there takes precedence and is never overwritten. By hand it still works with
`COMPOSE_PROFILES=updater` in the `.env` and
`docker compose pull && docker compose up -d --no-build`; it is switched off
with `./opnexus updater disable`.

You switch on the version check (feed) in the web interface for this, see above.

**Security model:**
- The **updater** is the only container with the Docker socket, which is equivalent to
  root privileges on the host. That is why it is optional and small.
  The backend never gets the socket.
- It has no published port and is reachable only from the Compose network with a
  self-generated token.
- It can only "update to X.Y.Z", "roll back" and "status", no
  arbitrary command. The target version must be a stable, newer version with the same
  major version and the one that the release feed reports as the newest stable.
- Only admin requests are passed on, and each appears in the audit log (type
  "OPNexus system").
- **Major version jumps** (1.x → 2.0) deliberately cannot be triggered by click:
  read the release notes, then `opnexus update 2.0.0`.

Prerequisites: release images and an active release feed. An update switches the
images, **not** the Compose file; if a version needs new Compose entries,
the release notes say so.

### Database names

New installations create database and user as `opnexus` (the names are set by
`POSTGRES_DB` and `POSTGRES_USER`, only on the very first start of an empty volume).
An **older** installation with different names enters them unchanged in the `.env`,
then everything runs as before (check with
`docker compose exec postgres psql -U <user> -l`). Renaming: stop the services except
`postgres`, run `ALTER DATABASE <old> RENAME TO
opnexus;` and `ALTER ROLE <old> RENAME TO opnexus;` via `psql -U <user> -d postgres`, set or remove `POSTGRES_DB` and
`POSTGRES_USER` to `opnexus` and restart. Back up beforehand with
`opnexus backup`.

### Second instance on the same machine

Put the Compose file in its own directory, with its own `.env`. An own project
name is important (`COMPOSE_PROJECT_NAME=opnexus2`): the Compose file fixes the project name to `opnexus`, and
without an own name the second folder shares containers and volumes with the first. In addition: a different `POSTGRES_PASSWORD` and `ADMIN_PASSWORD`,
different `PROXY_HTTPS_PORT` (e.g. 8443), `PROXY_HTTP_PORT`, `BACKEND_PORT`,
`FRONTEND_PORT` and a matching `FRONTEND_ORIGIN`. Browsers do not separate cookies by
port: open two instances under the same IP in separate profiles or windows.

### Disk space

A full disk makes Postgres crash. Keep an eye on the space, set
retention and limit for log collection appropriately (see [Logs](#logs)) and
occasionally clean up unused Docker images and build caches
(`docker image prune`, `docker builder prune`).

### Versioning

OPNexus uses [Semantic Versioning](https://semver.org/) and gives every
minor version a bird-of-prey codename. A patch release keeps the name. The
running version is in the footer, in the settings and on the About page; all
changes are in the [changelog](../CHANGELOG.md).

---

## Security

- **Encryption:** HTTPS through the proxy; backend and frontend listen only
  locally. Cookie `Secure`/`HttpOnly`/`SameSite=Lax`. The backend rejects write
  requests whose `Origin`/`Referer` is not the same host or listed in `FRONTEND_ORIGIN`.
- **Headers:** HSTS, `Content-Security-Policy` (own origin only),
  `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`, `Permissions-Policy`;
  `Server` and `X-Powered-By` removed; API responses `no-store`.
- **Firewall connections:** HTTPS only, certificate pinnable, secrets
  only as files.
- **Passwords:** Argon2id; login lockout; changing the password ends all sessions.
- **Secrets outside the database:** API secrets, VPN keys,
  certificate keys, ACME account data, two-factor, webhook/SMTP credentials.
  Plain-text secrets never appear in history or audit log.
- **Audit:** without gaps and protected against changes by a trigger.
- **Roles:** the read-only protection applies server-side, not only in the interface.
- **Foreign files:** the import of configuration files reads YAML only as data
  (no anchors, tags, merge keys or multiple documents), limits size and
  depth, validates every field strictly and is reserved for admins.
- **No telemetry.** OPNexus connects only to your firewalls and to the
  notification targets you set up; the version notice is optional.
- **Error details:** raw response texts of the firewalls on connection errors are not
  passed on to users, not even to read-only accounts.

You report security vulnerabilities confidentially to **security@opnexus.dev**
(see [SECURITY.md](../SECURITY.md)).

---

## Troubleshooting

| Symptom | Cause and solution |
| --- | --- |
| Browser warns about the certificate | The internal CA is not imported yet, see [Network access and TLS](#network-access-and-tls). |
| Login fails despite the correct password, the network tab shows a CORS error | `FRONTEND_ORIGIN` does not match the address you opened. Enter the exact origin (`https://…`, without a trailing slash). |
| Forgot password | `./opnexus password` |
| Two-factor device and recovery codes lost | Another admin resets it, otherwise `docker compose exec backend python -m app.reset_2fa <user>`. |
| Host "offline", detail names connection/certificate | Check address, API key and privileges; the firewall must allow the web interface from the OPNexus server. If the host reports "Certificate does not match the pinned one", the firewall renewed its certificate: fetch and confirm again under Hosts › Edit. |
| Firewall online, but firmware info or log collection fail, the log says "corrupted HTTP response" / `malformed chunk footer` | See [kTLS on OPNsense 26.x](#ktls-on-opnsense-26x). |
| Outbound NAT push is rejected with `OUTBOUND_MODE_BLOCKS_PUSH` | Intended: the firewall is set to "Automatic". In OPNsense under Firewall › NAT › Outbound set it to "Hybrid". |
| Change to the interface IP is blocked | IP configuration via API exists only from OPNsense 26.7.6. |
| After a network change the firewall is no longer reachable ("applied, cannot be verified") | Enter the new address under Hosts › Edit. If necessary use the backup from before the change. |
| Action blocked, message names open maintenance, restore or open deployment | First confirm, complete or cancel the open operation. |
| Both nodes are in CARP maintenance mode | On the node that was demoted first, "Restore normal operation"; if the operation hangs, "Cancel operation". |
| Backend does not start, message about the database revision | The database was migrated by a newer version. Start the newer version or restore a backup. |
| Backend does not start, migration backup failed | Check space on `postgres_backups` and the Postgres server version (dump and server need the same major version). |
| `docker compose up -d` aborts ("dependency failed to start"); backend and `postgres-backup` restart in a loop, the log says "password authentication failed" | The database volume comes from an earlier attempt and has a different password (`POSTGRES_PASSWORD` applies only on the first start; the project name `opnexus` is the same for every folder). Enter the original password in the `.env` or, without data, `docker compose down -v` and restart (deletes the database). |
| Backend does not start, log: database host cannot be resolved | `POSTGRES_PASSWORD` contains special characters that interfere in the connection address (newer versions encode the password themselves, older ones do not). Letters and digits are safe, e.g. `openssl rand -hex 24`. |
| Everything becomes slow or Postgres restarts | Disk full: free up space, lower log retention and limit. |
| Time filters in the logs are shifted | The firewall is not set to UTC, the timestamps are uncorrected. |

### kTLS on OPNsense 26.x

**Symptom:** firmware info and log collection fail although the firewall is online
and small queries work; the backend log says `malformed chunk footer`
or "corrupted HTTP response", the interface shows a 502.

**Cause:** OPNsense 26.7 encrypts TLS in the kernel (kTLS,
`kern.ipc.tls.enable=1`). In some environments (e.g. Proxmox/virtio) this corrupts
responses from about 64 KiB when fetched from a *different* machine. This is
not a bug in OPNexus.

**Solution:** on the firewall under *System › Settings › Tunables* create the entry
`kern.ipc.tls.enable` with value `0` and apply it. It takes effect immediately and is kept
after a reboot. Alternatively OPNexus creates it under
*Host system › Tunables*.

---

## Limits

- **No multi-tenancy**, no organisations.
- **No IP allowlisting, no passkeys/WebAuthn;** the session lifetime is 30 days.
- **Rules:** export and import as YAML exist for the central objects
  ([configuration as code](#configuration-as-code-yaml-git-ci)); not included are
  hosts, VPN, network objects and certificates. No configuration templates or
  policy bundles with an atomic commit on the firewalls. The rule drift comparison uses
  a fingerprint and is not a complete field-by-field comparison.
- **Rollback:** only for rules, aliases and NAT (deployments). Deleting, VPN and
  network changes have no automatic rollback.
- **Restoring** a firewall backup is a guided handover to
  OPNsense, not an execution by OPNexus.
- **Logs:** no live streaming (polling in the range of seconds), the free-text search is
  a simple substring search without an index.
- **Scheduled maintenance and rollouts:** rollouts have no target version "from outside"
  (OPNsense always installs the newest offered). The way back to the old version exists
  only on firewalls with ZFS and only guided by hand ([way back](#back-to-the-previous-firmware)),
  and it also resets the configuration; a rollout never does it by itself.
  Major-upgrade rollouts have been tested only against a simulated firewall. The
  health check sees the firewall from the management point of view; the optional
  reachability probes check what the firewall itself reaches, not the traffic
  through it.
- **Services:** no selection list, all services are controllable.
- **VPN:** scope per method see [VPN](#vpn-ipsec-openvpn-wireguard).
- **Certificates:** no push to several hosts, no management of DNS providers
  and post-processing actions, no own renewal scheduler.
- **Network:** no templates across several firewalls, no automatic
  rolling apply on CARP pairs (only transferring to the partner by click),
  no Kea DDNS fields.
- **Adopted rules** apply per firewall individually: whoever additionally targets an adopted rule
  at another host creates a second, duplicate rule there
  (the preview shows "create").
- **Notification texts, server logs and OPNsense responses** are not
  translated.

---

## Usage, language, accessibility

- **Navigation:** eight groups in the header with a submenu; below about
  1150 px only icons with tooltip. On smartphones forms,
  tables and dialogs adapt.
- **Quick search:** `Ctrl`/`⌘` + `K` or the search button. Finds pages, hosts and
  content (rules, aliases, NAT, VPN, …) and jumps straight there.
- **Hints** appear as info icons and tooltips instead of text blocks; they
  show on hover, keyboard focus and tap and close with `Esc`.
- **Appearance:** light, dark or system, saved per account.
- **Language:** German or English, per account (Settings › Language). Without a saved choice OPNexus follows the
  browser language: German for a German browser, English otherwise. The choice
  applies immediately and is adopted on all devices. Everything OPNexus says is available in both languages: pages,
  messages and error texts (including the prose of deploy previews, maintenance and audit entries), notifications (own
  setting, see Notifications), the `opnexus` command and the console tools behind it (set `OPNEXUS_LANG=de|en`; they
  follow your locale otherwise), and the HA agent. Server logs are English. Only texts that OPNsense itself returns
  (firewall error messages) stay as the firewall wrote them.
- **Accessibility:** the goal is WCAG 2.2 AA: full keyboard operation with
  skip link, visible focus, dialogs with a focus trap, changing page titles and
  contrasts in both appearances. A test with a real screen reader is
  still pending.

---

## License and privacy

OPNexus is licensed under the **PolyForm Shield License 1.0.0** (see [LICENSE](../LICENSE)).
It is a *source-available* license and **not** an open-source license in the sense of the
OSI:

- **Allowed:** download for free, view the source code, adapt and use it for your own
  purposes, also commercially, for example in a company for its own
  firewalls.
- **Not allowed:** offering or reselling OPNexus or derivatives as a product or service
  that competes with OPNexus, not even for free.

This summary does not replace the license text.

**Privacy:** OPNexus is operated by you yourself and has no telemetry. All
data lies on your server. Outgoing connections go only to your
firewalls, to configured notification targets and, if enabled, to the
version notice.

```
Required Notice: Copyright 2026 Julian Boldt (https://opnexus.dev)
```
