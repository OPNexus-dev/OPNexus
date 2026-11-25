# Changelog

All notable changes to OPNexus, with version and date. For the full feature descriptions
(screenshots, verification records, v1 scope limits) see the dated sections in the
[handbook](docs/HANDBUCH.md) — this document is the short overview of versions and names, not a
replacement for it.

> **Language.** From 1.38.0 on this changelog is written in English. The entries from 1.36.0 onwards
> are translated below; the complete German history (every release up to 1.37.8) is in
> [CHANGELOG.de.md](CHANGELOG.de.md).

## Versioning

OPNexus follows [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`):

- **MAJOR** — breaking changes (e.g. incompatible API/schema changes, removed features).
- **MINOR** — new features, backwards compatible (the normal case for a project that is still
  growing).
- **PATCH** — bug fixes and small corrections without a new feature.

In addition, **every MINOR and MAJOR version gets a codename** — birds of prey up to 1.38.0, and from 1.39.0 on the names of
cities, in ascending alphabetical order (Amsterdam, Berlin, Copenhagen, Dublin, …). A PATCH release keeps the codename of its
MINOR version. The current version is in `frontend/lib/version.ts` (`APP_VERSION`/`APP_CODENAME`) and `backend/app/version.py`, both
maintained by hand — no build pipeline, no git-hash tagging. It is shown in the app on the login page,
in the footer of every page, on `/about` and in the settings.

Next codenames (in order, when the next MINOR version is due): Berlin, Copenhagen, Dublin, Edinburgh, Florence.

## [1.39.1] "Amsterdam" — 2026-11-25

- **Rule links are repaired after an OPNsense config sync.** When the primary of an HA pair pushes its rules to the secondary (System › High Availability, item "Firewall rules"), the secondary holds the primary's rule IDs instead of the ones OPNexus recorded when it deployed there. The rules were all in place, but the live view called them "local" on that node and editing them for that firewall failed. OPNexus now notices this: the observe loop re-links a deployment by itself when the live rule carries the partner's rule ID and has the same content, and for everything less certain the live view shows a notice with "Check and repair" (a preview of what changes; only OPNexus's own records are changed, never a firewall; every link repaired is an audit entry). New API: `GET /api/rule-order/hosts/{id}/links` and `POST …/links/repair`.
- **Clearer tags in the live rules view.** One column "Managed by" with one calm tag per rule (Central blue, Local only neutral grey instead of warning orange, Hierarchy, System) and a tooltip that says what each means; the cryptic "not everywhere central" tag is replaced by a warning icon that names the firewall and offers the repair; a disabled rule is shown greyed with a small "off" tag instead of struck through plus a word. Rows are one line again.
- **CI.** The language check of the release gate (`scripts/check_server_texts.py`) no longer needs `git` (it failed in the GitHub CI container with "dubious ownership").

## [1.39.0] "Amsterdam" — 2026-11-25

- **Deploy several rules at once.** Tick rules in the list and use "Review and deploy": one dialog with the preview of every rule, then every rule is deployed one after the other exactly like a single deployment (its own approval, checks, confirmation window and automatic rollback). A refused rule does not stop the others, and "Confirm all deployed" confirms them together (at most 50 rules per run; operators need the right to deploy). New API: `POST /api/rules/bulk/deploy` and `/bulk/confirm`.
- **Rules as a compact table, with usage.** The rules page has a table view (the new default; cards stay available): action colours, source, destination, status per target, sortable columns, and **Last hit** from the observed pf counters, with a "usage" filter (in use, unused, collecting data, unknown). On a CARP pair the newest hit of any node counts. New API: `GET /api/rules/usage`.
- **Overlap warning before deploying.** The preview, and the dialog for several rules, now say when a rule would be shadowed by an earlier rule or duplicate one on a target firewall, and which later rules it would make pointless. It is a read-only hint: it never blocks a deployment. New API: `GET /api/rules/{id}/overlap`.
- **The whole product speaks both languages.** What was still German-only is now translated: the explanations the server adds to deploy previews, maintenance and audit entries and error texts (about 240 sentences, shown in English when the interface is English), the console tools behind `opnexus export`, `opnexus apply`, `opnexus backup` and `opnexus restore` (they follow `OPNEXUS_LANG` or your locale, and the `opnexus` command passes its language on), the start-up hints of the backend (database password, volume, schema revision), and the English confirmation sentence "old primary is off" for a forced takeover on the standby page (the German one still works). Server logs are English from this version on. A new check (`scripts/check_server_texts.py`, part of the release gate and CI) fails when a German sentence, log line or console text without an English counterpart is added. Only texts that OPNsense itself returns stay as the firewall wrote them.
- **Configuration as code in English.** The header of an exported file and the title of `opnexus-config-v1.schema.json` are English now ("OPNexus configuration (schema v1)").
- **Notification texts in English.** Email, webhook text, ntfy, Gotify, Telegram, Teams and Matrix can now be sent in English: Settings › Notifications has a new field "Language of the messages" (Automatic / Deutsch / English). Automatic follows the language of the oldest administrator account, so existing installations keep their German texts, and English is used when nobody ever chose a language (migration 0038). The JSON object `context` of the webhook is not translated, so receivers that parse it keep working; only `subject` and `message` change. Prose that comes from other parts of OPNexus (for example the reason a rollout stopped) is still German.
- **Updater and HA agent in English.** The log lines and errors of the updater container and the HA pairing (for example "The partner rejects the code") follow the same language rule as the `opnexus` command: English unless `OPNEXUS_LANG=de` or a German locale is set.

## [1.38.0] "Lammergeier" — 2026-11-21

- **OPNexus speaks English by default — international edition.** New installations and users without a saved language now follow the browser: a German browser gets German, every other browser gets English. A language chosen in the settings (on the account) or earlier in this browser always wins, so existing installations look exactly as before. Under the hood the per-user language can now be "not chosen" (migration 0037; nothing changes for existing values), and changing the colour scheme no longer pins the language by accident.
- **Command line in English.** `opnexus` prints its messages, prompts and help in English unless your locale is German (`LANG=de_DE.UTF-8`) or `OPNEXUS_LANG=de` is set; `OPNEXUS_LANG=en` forces English. Confirmations accept `y` as well as `j`, and the typed confirmations of the HA commands accept both the German and the English sentence ("alter Primaer ist aus" / "old primary is off"). Note: the log of one-click updates in the web interface is English now.
- **English handbook.** `docs/HANDBOOK.md` is the complete handbook in English (the German `docs/HANDBUCH.md` stays); README, the `INSTALL.txt` in the release bundle and the website link to it. The changelog is English from this version on; the German history is in `CHANGELOG.de.md`.
- **Website:** German pages show a dismissible hint with a link to the English version for visitors whose browser is not German.
- **GitHub:** issue templates for bug reports and feature requests.
- Known gap: notification texts (email, webhook, ntfy …) are still sent in German.

## [1.37.8] "Shikra" — 2026-11-14

- **API users with restricted privileges work (issue #8, reported by ccesario).** OPNsense checks an API user's privileges against the URL exactly as it is written, and the privilege definitions (ACL) use `snake_case`. OPNexus, however, called several diagnostic endpoints in `camelCase` (e.g. `getVipStatus`); with "All pages" this went unnoticed, but with a restricted user even adding the firewall failed with HTTP 403. OPNexus now uses the ACL spelling: `get_vip_status`, `system_swap`, `pf_statistics/rules`, `filter_util/rule_stats` and `pre_shared_keys`. For `system_resources`, `system_time`, `system_disk` and `get_interface_statistics` OPNsense changed the spelling between versions (`camelCase` up to 25.1, `snake_case` from 25.7): the current one is tried first and, on 403/404, the older one; the one that works is remembered per firewall. A missing privilege remains a 403 (the privilege alarm stays active). The same applies to the Unbound host overrides: the narrow privilege "Services: Unbound DNS: Edit Host and Domain Override" only knows `search_host_override` & co. and (unlike the broad "Services: Unbound") only with a trailing slash before the query string. Verified against a real OPNsense 26.7 with one test user per privilege (57 privileges, 16,815 requests, all as predicted by the privilege definitions).
- **New: [OPNsense privileges for the API user](docs/API-RECHTE.md) (issue #7, suggested by ccesario).** A table of which OPNexus function needs which OPNsense privileges, generated from the verified data; the handbook links to it.

## [1.37.7] "Shikra" — 2026-11-11

- **One-click updates work with rootless Docker.** Until now the updater always mounted `/var/run/docker.sock`; under rootless Docker the socket lives at `/run/user/<UID>/docker.sock`, and the updater ran with an empty directory instead of a socket (healthy according to the health check, but without access to Docker).
  `opnexus updater enable` now detects the socket itself (`DOCKER_HOST`, Docker context, classic path, `$XDG_RUNTIME_DIR` or `/run/user/<UID>`) and writes it to the `.env` as `DOCKER_HOST_SOCKET`; a value set by hand takes precedence and is never overwritten. `opnexus updater disable` switches the updater off again and leaves the rest of the stack untouched.
  The helper container of the self-update also uses the real host path (it reads it from the updater's mounts). Nothing changes with classic Docker. This rootless support (mount, detection, `enable`/`disable`) comes from [@electronicm](https://github.com/electronicm) (pull request #3), verified on a real rootless installation; they also found the cause (issue #2).
  Added by the project: the HA path, which also starts the updater, now uses the detection as well; it no longer runs during `opnexus update`, because it touches the `.env` even when the updater is not active.
- **Updater: clearer error diagnosis.** @electronicm reported three gaps in issue #4; fixed by the project. The signature check now distinguishes an unreachable registry (DNS, network, TLS) from a missing or invalid signature and shows cosign's own message. The self-update reports "started" instead of "renewed" as long as only the start is confirmed; if the helper container fails, exit code and output appear in the log.
  Log and result of the last updater job remain visible in the web interface after completion and after a reload; a failed partial step in an otherwise successful job is highlighted.

## [1.37.5] "Shikra" — 2026-11-05

- **Image build survives occasional outages of the PostgreSQL mirrors.** The install script for the database client now downloads keys and packages with retries; before, the build aborted when one of the `postgresql.org` mirrors closed the connection. Affects only building the images, not operation.
- **Installation: one single guide, the same everywhere.** README (German/English), handbook, `INSTALL.txt` in the bundle and the website now describe the same path: download the release bundle, create the `.env`, generate your own database password (`openssl rand -hex 24`), `docker compose up -d`, `./opnexus password`. Building from source is its own section "for development only". The handbook now explains that `POSTGRES_PASSWORD` only applies on the first start with an empty volume and that the Compose project name is `opnexus` for every folder (containers and volumes are therefore shared between folders; set `COMPOSE_PROJECT_NAME` for a second instance).
- **Restart loop of backend and database backup fixed.** If a database volume from an earlier attempt with a different password was still present (`docker compose up -d` in a new folder uses the same volumes because of the fixed project name), backend and `postgres-backup` restarted endlessly without anyone learning why. The backend now stops with a clear message (cause and the two ways out), and `postgres-backup` reports the error, waits ten minutes and tries again instead of crashing.
- **Passwords with special characters** (`@ / # ? :` and others) in `POSTGRES_PASSWORD` led to "database host cannot be resolved" and a restart loop of the backend. The backend now encodes such passwords itself; passwords without special characters stay unchanged.
- **Backend health check waits 3 minutes on the first start** (before: 10 seconds). On slow machines (Raspberry Pi, small VM) creating the schema takes longer; `docker compose up -d` used to abort with "dependency failed to start" although the backend was still starting.

## [1.37.3] "Shikra" — 2026-11-05

- **Rule form: "Position" only for new rules.** For an already deployed rule it said "At the end" although the rule sits somewhere else by now. The form now shows a note there with a link to the view "Live across all hosts", where the position is changed by arrow or drag. New rules keep the field (correct place on the first deployment, on all target hosts).

## [1.37.2] "Shikra" — 2026-11-05

- **Rules: "Live across all hosts" now shows the real order and lets you edit and move rules.** Per firewall (an HA pair together, single firewalls on their own) all rules appear in the order in which the firewall evaluates them:
  central ones (badge "Central"), those created only on the firewall ("Local only"), those set by the hierarchy and, on request, system rules, with a checkbox per firewall of the pair. **Moving** works with arrows (one place, to the very top, to the end) or by dragging;
  for a pair the move applies to both firewalls. A new order applies only **on probation**: without confirmation within the time window OPNexus restores the old one (several moves in a row are confirmed together or reverted together,
  newest first); if reverting fails, a notification is sent, and further moves and deployments on that host are blocked until it is resolved. **Editing**: central rules open the normal editor; a local-only rule is first adopted centrally
  (only an entry in OPNexus, nothing is written; on the second host of the pair it is linked) and then changed as usual with preview, confirmation and automatic rollback. Rules set by the hierarchy and system rules cannot be moved here.
  New API `/api/rule-order/…` (moving and reverting require deploy permission, confirming only in the browser). Migration 0036.
- **Position "Very top" applies when creating and when changing the position, not on every deployment.** Whoever moves a rule later keeps that position after a new deployment (a retry after an error sets the position again).
- **Rules: overview of rules that exist only on individual firewalls.** Below the list of central rules there is the collapsible section "Only on individual firewalls": the rules created by hand directly on a firewall,
  grouped by firewall (an HA pair counts once), with the note "Only here" or "Also on: …" if the same rule exists on another firewall. The firewalls are queried live only when the section is opened;
  one that does not respond shows only its own error message. "Adopt …" opens the existing adopt dialog directly on that firewall (manage centrally or link with a central rule). None of this writes to the firewalls.

## [1.37.1] "Shikra" — 2026-11-04

- **Rules: position in the rule list is selectable.** OPNsense appends new rules to the end of their priority group; a block rule behind an "allow all" rule therefore never matched. The rule form now has
  **Position**: "At the end" (as before, default) or "Very top" (on deployment placed before the first rule of the group via `moveRuleBefore`; rules from the policy hierarchy stay above).
  New block/reject rules suggest "Very top". The position appears in preview, history and configuration as code (`placement: top`). If the move is rejected, the push counts as failed
  and the automatic-rollback window stays armed, instead of reporting a wrongly placed block rule as a success. Verified live on a test firewall. Migration 0035.
- **Protocol selection: TCP/UDP always available.** In rules, port forwards and outbound NAT, `any`, TCP, UDP, **TCP/UDP** and ICMP are always at the top, even if the firewall's live list has not loaded yet; a saved "tcp" is shown as the entry "TCP" instead of a duplicate. The hierarchy rules use the same selection instead of a free-text field.

## [1.37.0] "Shikra" — 2026-11-03

- **HA: hardening after the review of the UI.** Taking over on the standby also works for accounts with spaces or umlauts in the name (the updater now only receives the account ID); for a pair that still runs over SSH,
  the standby page does not offer a forced takeover without fencing. "Set up HA" rejects the wrong direction (the partner has firewalls, this server has none). The HA gate only listens on the node's address and no longer runs
  as root. `ha migrate-agent` rolls the partner back on a local error. Fixed: with several addresses on one network card, `ha` considered the VIP not set (SIGPIPE on `ip … | grep -q`).
- **Set up and operate HA in the UI.** Settings › System shows the HA card even without a pair: "Set up HA" (enter the partner's pairing code and the VIP, "Check prerequisites" changes nothing,
  "Set up now" shows progress) and "Prepare this server as standby" (shows the code). With a pair: Sync now, Rebuild standby, Switch to partner and **Dissolve HA** (the partner
  becomes an empty single installation); every action asks for the logged-in administrator's password. New in the terminal: `./opnexus ha dissolve` and `./opnexus ha migrate-agent` (switches an existing SSH pair to the agent
  over the existing SSH connection, removes the cron of both nodes and rolls everything back on any error). New notification **HA agent not responding** (`ha_agent_down`). Found in the lab:
  a stale ARP line counted as "VIP in use" (now only a confirmed entry counts).
- **HA: take over with one click on the standby.** The standby now runs the full stack (proxy, frontend, backend). Every page requested there leads to `/standby`:
  a login **without write access** (the database there is a replica) for local administrator accounts: password against the replicated hash, second factor from the synchronised
  TOTP file (replay protection in memory; recovery codes do not work, consuming one would be a write; if the file is still missing, the account cannot get in without a factor), cookie
  `__Host-opnexus-standby` (15 minutes, signed, stored nowhere). The page shows the state of the pair (partner and its agent, epoch, replication, last applied change,
  sync, missing secret files) and offers **Take over**: normal if the partner's agent responds (it is stopped first); with a completely silent partner only after typing the sentence
  "alter Primaer ist aus" ("old primary is off"); **refused** as long as the partner still answers as leader. The server decides the mode, not the browser. The backend triggers `ha promote` through the node's
  updater; who took over is recorded afterwards in the new primary's audit log. Everything else on a standby's API answers 503 `NODE_STANDBY` (exceptions: health, metrics).
  `ha sync` restarts the standby's proxy after a new proxy CA.
- **Set up HA without SSH and without cron (agent).** The updater container becomes the agent of the pair: `./opnexus ha prepare` (on the future standby) switches on the updater and the new
  **HA gate** (profile `ha`, proxy image with baked-in configuration, port 8445, TLS 1.3, no Docker socket and no secrets except its key) and prints a
  **pairing code** (valid for 30 minutes, single use, consumed after 5 failed attempts; it carries address, port and the pin of the gate certificate). `./opnexus ha init --code CODE --vip IP/LEN`
  on the future primary checks beforehand (version, release mode, space, VIP free, partner's proxy reachable), pairs (the pin is verified **before** the secret is sent;
  the partner calls back through the gate) and then sets everything up; in the lab this took 80 s. Afterwards fencing, takeover, resync, sync and update run through fixed verbs
  (`/ha/peer/…`, no path for free-form commands; cluster secret plus sender address) instead of SSH. The agent syncs the secrets every 5 minutes and follows up the VIP
  (only on the primary, never while the partner is leader); the cron is no longer needed. Fencing stops the services but not the updater and gate; the VIP is set in a short-lived helper container
  (`--network host`, only `NET_ADMIN`/`NET_RAW`). Existing SSH pairs keep running unchanged. Design: `docs/DESIGN-HA-UI.md`.
- **HA: `ha resync --from-peer` also brings the old primary back after a forced switch.** After `ha promote --force` the `.env` of the
  failed node could no longer be set to `fenced` and stayed `HA_ROLE=primary`; `ha resync` refused there ("This node is primary") although the handbook names exactly this way back. With `--from-peer` the node rebuilds itself as a standby, but only if the partner demonstrably took over later
  (database is primary, **higher epoch**, and it does not list this node as primary); otherwise it aborts without touching anything.
- **HA: `ha promote --force` does not take over while the partner still answers as leader.** If only SSH is disturbed but the partner is running (`/api/health`
  reports `leader`), the switch would result in two primaries. With `--yes` this is refused; without `--yes` the sentence "Partner laeuft noch" ("partner is still running") must be typed.
- **HA: `ha init` checks whether the VIP is already in use** (ping, otherwise ARP entry) before anything is changed.
- **HA: the state no longer lives in the `.env` but in `.opnexus-ha/state`** (role, node, addresses, VIP, replication password; file 0600, directory 0700,
  written atomically, never executed). Existing pairs are migrated automatically on the first HA command; the `.env` keeps only `COMPOSE_FILE` and a derived
  line `HA_ROLE`, which exists solely so that an older updater image or an old CLI (e.g. after a rollback) keeps blocking one-click updates. The file is always authoritative.
  The updater container reads the role there as well. `HA_VIP` and `HA_REPLICATION_PASSWORD` are no longer synchronised as `.env` keys.
- **HA: `ha sync` only transfers explicitly allowed `.env` keys** (allow list instead of block list). `COMPOSE_PROFILES`, `OPNEXUS_IMAGE_REPO`, ports, bind addresses and
  `OPNEXUS_RUN_AS_ROOT` remain the business of the respective node; before, ports and bind addresses were sent along.

## [1.36.1] "Harpy" — 2026-11-02

- **Fix: the backend images of 1.36.0 do not start.** When the release images were built, all files in the image were readable only by root (mode 600/700); the
  backend runs as an unprivileged user and therefore could not find its own code ("Could not import module app.main"). Frontend and updater each had one
  unreadable file. **Please do not use 1.36.0**: if you already pulled it, stay on 1.35.1 (the update procedure would have rolled back automatically anyway, because the backend does not start),
  everyone else goes directly to 1.36.1. The content is otherwise unchanged (the interface roles from 1.36.0).
  Cause: the build context was created with `git archive | tar -x` under the restrictive `umask 077` of the release pipeline, and the gate checks a different context (checkout).
  The pipeline now creates the context with `umask 022` and refuses to build when entries are unreadable; in addition, the build host pulls every finished image
  and starts it as the operating user (files readable, backend imports as uid 10001) before anything is mirrored or published. Both protection layers have tests.

## [1.36.0] "Harpy" — 2026-11-02 — withdrawn, see 1.36.1

- **Interface roles: central rules for firewalls with different interfaces.** Until now a central rule stored the
  OPNsense identifier of the interface (`opt3`), and that has to mean the same network on all targets. Anyone running firewalls with differing numbers of
  interfaces had to create dummy VLANs to make the numbers match. There are now **roles**: on the host page
  each firewall assigns its own interface to a role such as `GUEST` (`opt3` here, `vlan0.20` there), and rules, port forwards,
  1:1 and outbound NAT choose `{{IF_GUEST}}` in the interface field. A firewall without that role is skipped for the object, without an error
  (preview: "Role not assigned · skipped"); if all targets are skipped, nothing is deployed. The assigned identifier is checked live against the firewall
  when it is set and before deployment. Roles are optional; plain identifiers (`lan`, `opt3`) apply unchanged, including the previous check "same meaning on all targets". Linking and configuration as code understand roles (the
  resolution per host decides whether a rule matches exactly). **Adopt as role:** the dialog offers "Adopt as role GUEST" if the
  firewall has assigned the role to this interface; the second firewall then links the equivalent rule with the central one. **Hierarchy rules** carry roles as well (order per firewall on the actual interface). Design: `docs/DESIGN-1.36-INTERFACE-ROLES.md`.

---

Releases before 1.36.0 (1.0.0 – 1.35.1, "Bateleur" and earlier): see [CHANGELOG.de.md](CHANGELOG.de.md) (German).
