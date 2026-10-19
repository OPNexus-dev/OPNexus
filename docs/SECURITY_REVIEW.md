# Opnexus security review — 2026-10-07

## Instructions for Claude Code

This is a remediation handoff, not a record of applied fixes. Work from the finding IDs below. Recheck each finding against the current source before modifying it, preserve unrelated user changes, and add the specified regression coverage. Address SEC-01 through SEC-05 first. Do not deploy to real firewalls to reproduce these issues: use mocked OPNsense responses and a disposable test database. Never include actual credentials, backups, or private keys in tests, logs, or commits.

The review covers commit `ee7b35953a326e848d0bc9e5b19bf7755f78a937` plus the working tree as inspected. Existing user changes were present in `frontend/app/aliases/page.tsx`, `frontend/app/rules/page.tsx`, and the OneToOnePanel, OutboundNatPanel, and PortForwardPanel components. Those files were not modified by this review. All locations below are repository-relative; line references describe this snapshot and can move during remediation.

## Scope and evidence

Reviewed the authentication and authorization boundary; account/password/TOTP flows; all router registrations; firewall rule, alias, NAT, hierarchy, VPN, certificate and ACME operation paths; maintenance and restore controls; secret stores; outbound HTTP and SMTP; frontend API calls and rendering sinks; website worker and scripts; Docker/Compose/proxy configuration; migration and update scripts; CI and release configuration; dependency manifests and lockfile.

The backend inventory contains 164 Python application files and 187 route decorators. This was a broad static review with targeted tracing and isolated probes, not line-by-line proof of every possible behavior or a complete penetration test.

Evidence is in [security-review-2026-10-07](security-review-2026-10-07/):

- `probe-results.json`: 13 synthetic probes, including positive controls. These report current vulnerable behavior, not passing remediation tests.
- `reproduce.py`: reusable, mocked reproduction harness. It does not invoke application lifespan, connect to a database, or contact a firewall. Authentication, database, backup and relevant secret reads are replaced with synthetic fixtures.
- `npm-audit.json`: npm registry audit of the frontend lockfile, reporting 12 affected package entries (10 high, 2 moderate). Dependency-chain entries are not separate exploitable vulnerabilities.
- `pip-audit.json`: resolved requirements audit reporting no known vulnerabilities. This is not a scan of every installed production package or container OS package.
- `route-inventory.tsv`: route source locations, methods, local paths and function names. Apply router prefixes from `main.py` and router declarations to obtain public paths.
- `source-manifest.json`: hashes of tracked source/deployment/website files for identifying the reviewed snapshot. A hash entry does not mean every line was manually examined.

The probes were rerun in a disposable backend-image container with no network and no volume mounts. Source hashes of 12 central authentication, routing, schema and SMTP modules were also compared with the existing backend container and matched. Runtime read-only checks found the existing backend and frontend processes running as UID 0 and the Compose PostgreSQL user marked as a superuser. No real backup contents or secret files were read, no live firewall operations were performed, and no application database rows were changed.

Reproduce using an image built from the reviewed source:

```sh
docker run --rm -i --network none --entrypoint python \
  local/opnexus-backend:latest - \
  < docs/security-review-2026-10-07/reproduce.py
```

Run the harness only against this snapshot or after reviewing changes to the functions it mocks. The standalone harness has no pytest fixtures and does not drop/create tables. The normal backend pytest suite has database setup/teardown fixtures; it was not run as part of this report. No load tests, exploit payloads for dependency vulnerabilities, browser exploitation, image vulnerability scan, full git-history secret scan, or real-firewall integration tests were performed.

## Findings and priority

Severity is contextual to a firewall-fleet management product. High findings can expose credentials, preserve unauthorized administrative access, or defeat safeguards for live firewall changes. Medium findings require more prerequisites or concern integrity, monitoring or supply-chain exposure. Conditional findings state the necessary deployment conditions. No anonymous remote-code-execution exploit was demonstrated.

| ID | Severity | Finding | Evidence level |
|---|---|---|---|
| SEC-01 | High | Read-only users can download secret-bearing firewall backups | Mocked HTTP reproduction |
| SEC-02 | High | Password changes/resets do not revoke all account sessions | Source + mocked reset |
| SEC-03 | High | SMTP TLS does not verify certificates or hostname | Source + runtime library inspection |
| SEC-04 | High | Ambiguous/live deployment failures disable automatic rollback | Source + mocked deploy |
| SEC-05 | High, conditional | HTTP firewall URLs send API credentials without TLS | Schema probe + client source |
| SEC-06 | Medium | Concurrent TOTP/recovery-code consumption is not atomic | Concurrent mocked reproduction |
| SEC-07 | Medium | Login throttle leaks records and password verification blocks the event loop | Source + expiration probe |
| SEC-08 | Medium, conditional | Raw upstream errors reach readers and persisted records | Source + synthetic error probe |
| SEC-09 | Medium | Host deletion omits VPN/certificate/ACME deployment guards | Source tracing |
| SEC-10 | Medium | Security-sensitive operations are absent from durable audit history | Source tracing |
| SEC-11 | Medium | Notification secrets are ephemeral and missing signing keys fail open | Compose + source tracing |
| SEC-12 | Medium, reachability unverified | Known vulnerable frontend dependencies remain in the lockfile | Registry audit + advisory verification |
| SEC-13 | Medium | MFA self-service endpoints lack attempt limits | Source tracing |
| SEC-14 | Medium | VPN approval can be omitted and tokens do not bind secret rotations | Source + synthetic token probe |
| SEC-15 | Low | Validation errors echo submitted secret values | Mocked HTTP reproduction |
| SEC-16 | Medium, conditional | Read-only notification settings expose capability-bearing webhook URLs | Schema + route tracing |

### SEC-01 — Read-only backup access exposes firewall secrets

**Locations:** `backend/app/auth_middleware.py:55-58`; `backend/app/routers/hosts.py:242-258`; `backend/app/services/backup_store.py:48-55`; `backend/app/services/opnsense_client.py`, `download_backup()`.

**Cause:** The read-only role is implemented as “allow every GET.” Backup listing and download do not enforce a separate permission. Download returns the original snapshot bytes, not the redacted restore manifest. The normal snapshot flow downloads full OPNsense `config.xml` revisions.

**Prerequisites/impact:** Any valid read-only session and an existing backup. Full configurations can contain password hashes, VPN PSKs, private keys and other credentials, depending on the firewall configuration. Exposure can enable offline password attacks and access outside Opnexus. No anonymous access was found. The role's documented allowance for all GETs explains the behavior but does not make secret export least-privileged.

**Reproduce:** Create a synthetic firewall backup fixture. Authenticate as read-only; list `/api/hosts/{host_id}/backups`, then GET `/api/hosts/{host_id}/backups/{backup_id}`. The probe returned HTTP 200 with a synthetic secret marker; a read-only POST control returned 403.

**Fix:** Add server-enforced admin-only or explicit `export_firewall_secrets` authorization for raw downloads, consistently for GET and HEAD. Keep any reader-visible backup metadata separate from export permission. Update the frontend accordingly. Audit successful and denied secret exports without storing their contents.

**Acceptance:** Reader gets 403 for raw download/HEAD, admin gets the exact synthetic bytes, anonymous gets 401. Ordinary reader pages continue working. A path traversal string cannot retrieve another host's backup.

### SEC-02 — Password rotation leaves compromised sessions valid

**Locations:** `backend/app/routers/auth.py:221-241`; `backend/app/routers/users.py:71-78`; `backend/app/services/auth_service.py:151-194`; `backend/app/config.py`, `session_lifetime`.

**Cause:** Self-service password change updates the hash and revokes only the caller's token before creating a replacement. Admin password reset updates only the hash. Session resolution uses token hash, expiry and user existence; it does not check a credential generation. Sessions can remain valid for the configured 30-day default lifetime. MFA enable/disable/reset likewise do not invalidate existing sessions.

**Prerequisites/impact:** Attacker already possesses a session. Changing or administratively resetting the password does not reliably contain the incident; the old session can continue reading secrets or changing firewall policy according to the current account role. This does not let an expired or unknown token authenticate.

**Reproduce:** Log in twice in a disposable database. Change the password in session A or reset it through another admin. Call `/api/auth/me` and a protected route using session B. Source predicts continued access; the mocked reset probe recorded one commit and zero revocation SQL statements. Full two-session database reproduction was not performed.

**Fix:** Revoke every target-account session atomically with password reset/change; for self-service change issue a replacement only after revocation. Consider a credential/session generation for robust concurrency. Define explicit MFA-change session policy, and require reauthentication where appropriate. Do not rely on password hash comparisons at request time.

**Acceptance:** All preexisting tokens fail on protected routes after reset/change. A replacement self-service session works. Failure rolls back both credential change and revocation. A concurrent session-creation attempt cannot bypass the chosen revocation/generation semantics.

### SEC-03 — SMTP encryption lacks peer authentication

**Locations:** `backend/app/services/notification_delivery.py:82-102`, particularly SMTP_SSL construction at line 93 and `client.starttls()` at line 98.

**Cause:** Both TLS modes omit an explicit verifying SSL context. Inspection of the deployed Python 3.12 standard library showed both falling back to `ssl._create_stdlib_context()`, with `verify_mode=0` (`CERT_NONE`) and `check_hostname=False`.

**Prerequisites/impact:** An active network attacker, malicious DNS/route or incorrectly configured SMTP endpoint. The attacker can impersonate the mail server and obtain SMTP credentials and operational notification content even when the UI selects `ssl` or `starttls`. Mode `none` intentionally has no TLS and should not be confused with this defect.

**Fix:** Pass `ssl.create_default_context()` to SMTP_SSL and starttls. Support an explicit custom trust store if needed, without silently disabling verification. Preserve STARTTLS fail-closed behavior when unsupported. Reject authenticated plaintext SMTP by default or require an explicit, documented insecure override.

**Acceptance:** Both modes reject self-signed/untrusted certificates and hostname mismatches; a trusted, matching local test certificate succeeds. Assert the passed context has CERT_REQUIRED and hostname checks. On handshake failure `login()` and `send_message()` must not run.

**Reference:** [Python SMTP TLS context documentation](https://docs.python.org/3.12/library/smtplib.html). The vulnerability conclusion also relies on the inspected runtime implementation, not merely the documentation.

### SEC-04 — Rollback is dropped precisely when a deployment outcome is uncertain

**Locations:** `backend/app/services/rule_deploy_service.py:56-137,154-172`; corresponding `_deploy_to_host()` and post-deploy hierarchy handling in `alias_deploy_service.py`, `port_forward_deploy_service.py`, `one_to_one_deploy_service.py`, `outbound_nat_deploy_service.py`; `policy_hierarchy_deploy_service.py:220-278`.

**Cause:** The API may accept a rule write and apply it before the response is lost. On apply error, the code records `status="error"` and no revert deadline. It generally captures rollback payload/deadline only after successful apply. There is a second, stronger case: after confirmed apply, hierarchy reordering failure changes `pending` to `error` and clears the deadline. The sweeper selects only expired `pending` rows. Policy hierarchy positioning failure can return before saving the new payload and rollback baseline.

**Impact:** A restrictive, permissive or misplaced live rule/NAT/alias change can remain indefinitely despite lack of confirmation. Operators expecting automatic rollback may lose connectivity or leave an unintended opening. Requires an authorized deployment and a failure/race; it is a safety-control failure, not an anonymous authorization bypass. Recovery also requires the management path to remain reachable: Opnexus cannot undo a change over a connection that has become permanently inaccessible.

**Reproduce:** Mock a successful add with a returned remote UUID, then make apply raise a timeout representing a lost response after execution. The probe returned `error`, a known remote UUID and null revert deadline. Separately mock successful apply followed by failed hierarchy reassertion; verify the worker excludes the now-live change.

**Fix:** Introduce explicit state for written/applied/unknown outcomes with durable rollback baseline and recovery scheduling. Persist intent/baseline before external mutation where possible. Do not erase the recovery obligation when apply or ordering is ambiguous. Block ordinary retry/edit paths until reconciliation. Avoid blindly retrying non-idempotent remote actions. Apply the same recovery semantics across all affected resource types.

**Acceptance:** Test failed write before mutation, successful write plus apply timeout, successful apply plus ordering failure, and process interruption between phases. Known or potentially live changes retain recovery metadata and cannot be mistaken for harmless failures. Simulate recovery worker execution without a firewall. Confirmed validation rejection before a write does not spuriously remove a remote rule.

### SEC-05 — Firewall connections accept plaintext HTTP

**Locations:** `backend/app/schemas/host.py:10-14`; `backend/app/routers/hosts.py:102-127`; `backend/app/services/opnsense_client.py:28-37`; `backend/app/services/tls_pin_service.py:84-90`.

**Cause:** `base_url` is a plain string. Create accepts `http://...`; httpx receives the API key/secret as Basic authentication. `verify_tls=True` only affects TLS validation and does not upgrade or reject HTTP. The schema probe accepted an HTTP URL while verification remained true. Setting `verify_tls=False` also deliberately allows unauthenticated HTTPS.

**Prerequisites/impact:** Admin configures an HTTP host or disables verification, then a network observer/attacker is on the path. API credentials and firewall responses are exposed or forgeable. Existing correctly pinned HTTPS hosts are not shown vulnerable by this finding.

**Fix:** Validate canonical HTTPS host URLs, rejecting userinfo, fragments, unsuitable paths and unsupported schemes. Enforce the rule in the client too, including existing database rows. If HTTP or disabled verification must remain for development, put it behind an explicit deployment-level insecure override with clear status and audit evidence. Do not ban private IPs: management of internal firewalls is core functionality.

**Acceptance:** HTTP is rejected before credentials are sent; HTTPS hostname/IP with valid pin works. Existing unsafe records cannot silently continue after migration. Tests distinguish URL scheme protection from `verify_tls` certificate protection.

### SEC-06 — TOTP and recovery codes can be used concurrently more than once

**Locations:** `backend/app/routers/auth.py:116-131`; `backend/app/services/totp_service.py:45-58,77-86`; user resolution/authentication before these calls.

**Cause:** Separate sessions load the same `totp_last_step` or recovery hash list, check locally, modify locally and commit without row locking or conditional updates. Two requests can both pass before either committed update is visible. Sequential replay tests do not cover this.

**Impact:** An attacker with the password and a captured second factor can race legitimate use, violating single-use protection. This is not a bypass with only the password and no code.

**Reproduce:** Two independent synthetic user snapshots and a barrier at commit accepted the same current TOTP twice, `[true,true]`. The same concurrent test accepted one recovery code twice. This demonstrates the check/commit race with mocks; database-level concurrent integration remains to be added.

**Fix:** Lock/reload the user row inside the consumption transaction or use an atomic conditional update that only succeeds once. Consume recovery codes atomically too. Ensure login and MFA-disable use the same primitive and avoid commits that release locks before consumption finishes.

**Acceptance:** Concurrent real-test-database requests for the same code yield exactly one acceptance. Sequential replay, drift window, remaining recovery codes and unrelated-account concurrency continue working.

### SEC-07 — Login throttling has unbounded state and synchronous expensive work

**Locations:** `backend/app/services/auth_service.py:55-60,78-139,199-233`; `backend/app/routers/auth.py:74-113`; backend Dockerfile uvicorn invocation.

**Cause:** `_records` and `_failures_since_success` have no TTL eviction or size cap. Expired attempt records remain, and arbitrary nonexistent usernames create counters. A probe created 200 bucket records for 100 synthetic sources; advancing time and checking every bucket left all 200 records and 100 username counters. Password verification is synchronous Argon2 work inside an async request handler. The limiter is per-process, per-IP/account and per-IP; it has no shared/account-wide policy across sources/processes. A correct password on an MFA account with omitted code exits without recording a failure, allowing repeated expensive checks on that path.

**Impact:** Public login requests can consume memory over time and stall the single event loop, also delaying status, deployment and rollback tasks. One IP's existing lockout reduces simple repeated attacks, so sustained abuse generally needs multiple sources/windows; multiworker deployment introduces additional independent buckets. The review did not load-test a running instance.

**Fix:** Bound/expire all throttle maps, including informational username counters. Use a shared limiter if supporting multiple workers and an account-aware policy that avoids trivially locking users out. Offload Argon2 into a bounded worker pool with admission/concurrency limits. Limit request body size before JSON parsing; field max lengths do not bound the raw HTTP body. Count appropriate incomplete/failed MFA attempts.

**Acceptance:** Expired records are removed, capacity remains bounded under many usernames, and throttles work across supported workers. Heartbeat/health requests stay responsive during bounded synthetic password verification. Missing MFA codes cannot create an unlimited costly path. Oversized chunked and Content-Length bodies are rejected before buffering.

### SEC-08 — Untrusted upstream errors cross the reader/secret boundary

**Locations:** `backend/app/services/opnsense_client.py:40-67`; `backend/app/services/host_status_service.py:35-50`; `backend/app/routers/hosts.py:42-57,69-75,131-136`; `backend/app/routers/maintenance.py:53-61`; `backend/app/services/managed_certificate_service.py`, `push_certificate()`; analogous `str(exc)` assignments in VPN/ACME/deploy services.

**Cause:** HTTP error exceptions include the entire upstream response body. Some direct host routes sanitize with `_upstream_error`, but status returns `str(exc)` through `status_detail`, maintenance returns it as `error`, and multiple deploy/certificate/ACME paths persist it as user-visible messages. An upstream response or validation object may contain reflected secret-bearing inputs. `_mutation_result()` also embeds up to 350 characters of unfiltered JSON.

**Prerequisites/impact:** Upstream emits sensitive details or is compromised/misconfigured. Readers can see otherwise privileged error content; messages may additionally enter database backups/audit exports/logs. The synthetic status probe returned the embedded marker. Actual firewall credential reflection was not tested or claimed.

**Fix:** Use structured upstream exceptions with safe code/status/operation fields. Centralize redaction before persistence, logging and response serialization; avoid raw bodies even in generic backend logs. Bound retained diagnostic data. Cover success-shaped-but-invalid mutation responses too.

**Acceptance:** Mock an upstream error/validation response containing API credentials, PSK, private key and arbitrary markers. None appear in reader JSON, database messages, audit details or logs. Safe operation/status diagnostics remain useful.

### SEC-09 — Deleting a host can leave live VPN/certificate state untracked

**Locations:** `backend/app/routers/hosts.py:175-201`; host foreign keys in `backend/app/models/ipsec_tunnel_deployment.py:40`, `openvpn_instance_deployment.py:44`, `wireguard_tunnel_deployment.py:44`, `managed_certificate.py:61`, `acme_account.py:47`, `acme_certificate_request.py:62`.

**Cause:** Host deletion checks rule/alias/NAT/hierarchy deployments and active maintenance operations but omits VPN deployments and certificate/ACME records. Their host FKs cascade deletion. Deleting the host removes local tracking and its API secret without undeploying the remote objects. File-backed entity secrets can also outlive cascaded entity deletion.

**Impact:** Authorized cleanup can leave unintended VPN trust/tunnels/certificates live while destroying their local management records. This is an integrity/lifecycle issue requiring admin access, not a privilege escalation.

**Reproduce:** In a disposable DB, associate a host only with a deployed VPN or managed certificate. DELETE the host; inspect cascaded local records and mocked remote-call history. Do not run this on a real firewall.

**Fix:** Extend guards across every managed remote resource. Require explicit reconciliation/undeployment or a separately acknowledged orphaning workflow. Clean entity secret files only after a safe, successful cleanup; keep identifiers for partial failures. Serialize creation/deletion races with the same operation lock.

**Acceptance:** Each VPN/certificate/ACME case blocks unsafe deletion or performs the documented explicit cleanup. Partial remote failure preserves tracking and secret access needed for recovery. Ordinary undeployed-host deletion succeeds.

### SEC-10 — Durable audit misses MFA, VPN, certificate and notification changes

**Locations:** `backend/app/auth_middleware.py:65-80`; `backend/app/services/audit_service.py:27-50`; `backend/app/routers/auth.py`; `backend/app/routers/ipsec.py`, `openvpn.py`, `wireguard.py`, `certificates.py`, `notifications.py`.

**Cause:** Middleware's resource mapping covers rules, aliases, NAT, hosts and users. The transactional deployment-history hook covers rule/alias/NAT/hierarchy deployment models, not VPN/certificate/ACME. Those routers/services have no compensating durable audit events. MFA changes and password change use ordinary logs or no audit event. Raw backup downloads also are not tracked.

**Impact:** VPN trust, certificate issuance, notification disablement and account security changes can occur without attributable records in `/api/audit`. Some related operations do have audit coverage and should not be duplicated. An account's current role still restricts writes.

**Fix:** Define an explicit security event inventory and record actor, action, target, correlation ID, timestamp and safe result for each missing operation. Include secret exports and failed authorization for sensitive actions. Preserve transactional history and durable pre-operation intent where externally visible operations require it. Never audit submitted secret bodies.

**Acceptance:** Exercise success/failure for each operation class and verify durable, attributable events. Passwords, keys, cookies, webhook tokens and raw backups never appear. Interrupted remote calls have an interpretable incomplete state. Existing audit views/exports remain compatible.

### SEC-11 — Notification secrets disappear on container replacement; signing fails open

**Locations:** `backend/app/config.py:82`; `docker-compose.yml:103-134,193-205`; `backend/app/services/notification_secret_store.py:15-30`; `backend/app/services/notification_delivery.py:67-72,82-100`; `backend/app/services/notification_settings_service.py:35-49`.

**Cause:** Notification secrets default to `/data/notification-secrets`, but Compose mounts sibling secret directories individually and omits this directory. SMTP/signing material remains in the backend writable layer and disappears on recreation/update; DB `*_set` flags survive. When `webhook_signing_secret_set` is true but the file is missing, webhook delivery silently sends without a signature. Missing SMTP password can lead to sending without login or failed delivery.

**Impact:** Routine replacement can disable authenticated alert delivery or drop expected message authentication. A receiver that correctly rejects unsigned messages will reject delivery; a permissive receiver may accept unauthenticated messages. Mere container restart normally preserves the writable layer; recreation is the trigger.

**Fix:** Mount a dedicated persistent notification-secret volume and document backup/restore. Migrate existing secrets before replacing a container. If a configured secret is absent, fail closed with a safe diagnostic and visible unhealthy notification state rather than silently changing authentication behavior.

**Acceptance:** Save synthetic secrets, recreate disposable backend containers with the intended volume, and verify persistence. With signing flag true but missing file, no HTTP request is sent. With SMTP authentication configured but secret missing, no unauthenticated send occurs. Diagnose state without revealing values.

### SEC-12 — Dependency audit is no longer clean

**Locations:** `frontend/package-lock.json:710,1135,1144,3718,4068,4203`; `frontend/package.json`; `frontend/Dockerfile`; `.github/workflows/ci.yml`. Full chain/version data is in `npm-audit.json`.

**Observed versions:** `sharp 0.35.4`, `source-map-js 1.2.1`, `braces 3.0.3`, `brace-expansion 1.1.18` and nested `5.0.9`, `postcss-selector-parser 6.1.4`. npm also flags their dependent chains. Most listed packages are dev dependencies; sharp and source-map-js are not marked dev in the lockfile.

**Impact/limitations:** Registry advisories establish affected versions, not exploitability in this application. The sharp advisory discusses memory corruption under specific runtime conditions, including glibc; the supplied frontend runner is Alpine/musl. The review found no frontend `next/image` usage and demonstrated no attacker-controlled SVG decoding or remote-code execution. Other advisories concern parsing untrusted patterns/selectors/maps and are primarily build/tooling risks here. Check standalone-image contents and actual input reachability before assigning a higher product severity.

**Fix:** Refresh affected dependencies to compatible patched versions, review transitive overrides, and regenerate the lockfile. Do not blindly use `npm audit fix --force`: its suggested framework downgrade/major Tailwind changes require compatibility analysis. Audit the actual runtime image and add recurring dependency auditing to CI. Revise dated documentation that claims no known vulnerabilities without a current scan.

**Acceptance:** Fresh lockfile audit has no unresolved relevant advisories, or documented time-bounded exceptions with reachability evidence. Frontend build/lint succeed. Verify the standalone runtime uses patched native libraries. Backend audit remains clean for the resolved dependency set, and separately scan OS images.

**Verified primary references:** [sharp maintainer advisory](https://github.com/advisories/GHSA-wq5f-xc86-pv6w), [braces advisory](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm). Additional advisory URLs are preserved in npm's JSON evidence. The sharp advisory lists 0.35.5 as patched; confirm compatible current releases before changing dependencies.

### SEC-13 — MFA management bypasses login attempt limiting

**Locations:** `backend/app/routers/auth.py:153-200`; `backend/app/services/auth_service.py:78-139`.

**Cause:** Setup verifies a password without the login throttle; enable checks a six-digit code without a limit; disable checks password plus second factor without a limit. Only `/login` consults/updates the throttle. Pending enrollment has no explicit expiry or session binding.

**Prerequisites/impact:** An authenticated session. A stolen session allows repeated password guesses against setup/disable; with the password, an attacker can repeatedly guess the disable second factor. An attacker with a session and a pending enrollment can try to complete it without a bounded attempt budget. Argon2 cost and network latency reduce throughput but are not an explicit attempt limit.

**Fix:** Add a bounded per-account/per-action rate limiter and short-lived, session-bound enrollment state. Require recent authentication for security-sensitive changes. Avoid resetting the attempt budget merely by recreating pending enrollment. Reuse atomic factor consumption from SEC-06.

**Acceptance:** Repeated invalid password/code requests become 429 or a documented lockout; counters and enrollment survive the supported worker model. Expired or another session's enrollment cannot enable MFA. Normal read-only self-service still works.

### SEC-14 — VPN preview approval is optional and ignores secret-only changes

**Locations:** `backend/app/routers/ipsec.py:152-160`; `backend/app/routers/openvpn.py:155-163`; `backend/app/routers/wireguard.py:164-172`; `backend/app/services/ipsec_service.py:90-100`; analogous token construction in `openvpn_service.py:109-119`, `wireguard_service.py:104-114`.

**Cause:** Deploy accepts `approval=None`, and checks the token only if supplied. Tokens are built from snapshots with secret-present booleans and masked diff rows, not the current secret's version/hash. Rotating only a PSK/private key while keeping flags true can produce the same token. The IPsec probe used an enabled synthetic target and two different PSKs and observed equal tokens.

**Impact:** An admin API call can skip the claimed preview gate, and an existing approval can apply different secret material than reviewed. This concerns change integrity/lockout prevention; admin already has deployment permission. The source explicitly describes some confirmations as UI-only, so implementing a backend gate changes that policy intentionally.

**Fix:** Make approval mandatory when preview is part of the promised safety workflow. Bind it to entity revision, resolved targets, relevant transport trust and secret version/change identifiers. Keep actual secrets and offline-guessable low-entropy secret fingerprints out of user-visible responses; use an opaque server-side approval record or keyed fingerprint where appropriate. Add expiry/single-use semantics if treating approval as an authorization artifact. Correct comparisons of raw payloads versus redacted stored payloads so unchanged secrets do not always appear modified.

**Acceptance:** Missing token fails without a remote call. Secret-only rotation invalidates old approval; metadata/target/transport changes also invalidate it. An unchanged draft permits the intended no-op. Test all three VPN types, not just IPsec. Exposed preview/revision JSON contains no plaintext secret.

### SEC-15 — Validation responses echo secret-bearing input

**Locations:** `backend/app/main.py` (default validation exception handling); `backend/app/schemas/certificates.py:57-79`; other password/key request schemas.

**Cause:** FastAPI's default validation response includes `input`. Model-level validators can include an entire submitted object; field validators can include invalid secret values. The synthetic request to `/api/certificates/managed` returned 422 containing the submitted private-key marker.

**Impact:** The submitting client already knows the value, so this alone is not cross-user disclosure. It unnecessarily puts secrets into response bodies that error telemetry, browser diagnostics or proxies may capture. Classified low rather than claiming account takeover.

**Fix:** Install a safe RequestValidationError response serializer retaining useful field locations, safe messages and codes while removing/redacting inputs and sensitive context. Do not log raw validation objects. `SecretStr` alone is insufficient unless serialization behavior is explicitly tested.

**Acceptance:** Invalid password, private key, PSK and model-level requests return useful 422 responses without markers anywhere in their JSON/logs. Non-secret validation remains understandable.

### SEC-16 — Reader settings expose webhook credentials embedded in URLs

**Locations:** `backend/app/routers/notifications.py:19-21`; `backend/app/schemas/notification.py:18-19`; `backend/app/services/notification_settings_service.py:22-32`; webhook URL column in `backend/app/models/notification.py`.

**Cause:** Settings GET returns the full webhook URL to any authenticated role. Many webhook integrations authenticate possession of a token in the URL path/query; that URL is therefore a credential, unlike an ordinary hostname. Signing-secret and SMTP-password flags correctly avoid returning those secret values but do not protect URL-embedded tokens.

**Prerequisites/impact:** A configured URL contains a capability token, and a reader account accesses settings. The reader can reuse it outside Opnexus to submit forged messages or use whatever access that receiver grants. For a token-free URL protected separately by a signature, this finding's credential impact does not apply. No real configured URLs were inspected.

**Fix:** Treat capability URLs as write-only secrets or return a carefully redacted display value. Keep a `webhook_url_set` indicator; preserve omitted values on updates and provide explicit clear/replace behavior. At minimum require an explicit privileged permission for the unredacted value. Avoid tokens in DB dumps, audit records and logs where the threat model requires secret-store separation.

**Acceptance:** Reader GET never returns a synthetic path/query token. Saving/reloading settings preserves delivery functionality. Masked UI text is not accidentally written back as the real URL. Both signed token-free URLs and capability URLs work with the intended authorization.

## Additional hardening and policy decisions

These are separate from the 16 findings above. Do not call them demonstrated remote exploits.

| ID | Locations | Observation, prerequisites and concrete next step |
|---|---|---|
| HARD-01 | `backend/Dockerfile`; `frontend/Dockerfile:13-23`; `docker-compose.yml:29-39,103` | Backend/frontend run as root; application uses the PostgreSQL bootstrap superuser. This amplifies a future application compromise. Add non-root runtime users, compatible volume ownership, reduced capabilities/no-new-privileges and a least-privileged DB role. Keep migration credentials separate. Verify runtime UID, secret-store access and migrations before rollout. |
| HARD-02 | `backend/app/security_middleware.py:25-31` | Same-origin check compares only netloc to Host, so `http://host` is accepted for an HTTPS host. Probe confirmed cross-scheme acceptance. Exploitability depends on same-site delivery and access to an insecure origin; HSTS and Secure cookies reduce it. Compare a canonical scheme/host/port origin, respecting only explicitly trusted forwarding. Add cross-scheme and default-port tests. Headerless requests being allowed is not by itself proof of browser CSRF; no such exploit was demonstrated. |
| HARD-03 | All `*_secret_store.py`; `totp_service.py:95-99`; `wireguard_key_store.py:39-44,57-62`; `backup_store.py:19-29` | Files are written before chmod 0600. With umask 022 a newly created file can briefly be 0644; writes are not atomic and follow symlinks. Requires local access to the secret directory; no remote path to that access was found. Create restrictive directories and mode-0600 temporary files at open time, avoid symlink following, then atomically replace. Test permissions before writing and crash/partial-write behavior. |
| HARD-04 | `schemas/host.py`; `tls_pin_service.py:93-113`; `notification_delivery.py:55-100`; `update_check_service.py:65-78` | Admin-controlled hosts, TLS probes and notification targets can cause outbound connections. Private targets are necessary for this product, and only admins can set these inputs; this was not classified as anonymous SSRF. Restrict schemes/ports and optionally provide operator-defined management/notification egress policy, protecting metadata services where relevant. Enforce streaming response-size limits; update-feed size is checked only after full buffering. |
| HARD-05 | `docs/HANDBUCH.md:213-217`; `proxy/Caddyfile`, `/opnexus-ca.crt` handler | First-time CA installation is bootstrapped from the same initially untrusted HTTPS endpoint, without an independent fingerprint in those instructions. An attacker on the first-use path could substitute a root users then trust system-wide. Provide an operator-side fingerprint command/out-of-band comparison before import, or use an already-trusted organizational CA. Do not tell users that encryption alone authenticates the initial download. |
| HARD-06 | `docker-compose.yml:115-116`; `.env.example`; `backend/app/config.py`, `cookie_secure` | Bundled proxy is enabled but Secure-cookie defaults remain false. Production handbook correctly says true. Make secure production defaults/validation align with the shipped TLS topology and retain an explicit local-dev mode. HSTS protects established HTTPS clients but does not replace a Secure cookie on first use. Verify cookie flags behind the proxy and HTTP-only dev behavior. |
| HARD-07 | `.github/workflows/ci.yml`, `release.yml`; Dockerfiles; `scripts/opnexus:130-139,163-166` | CI actions/base images are referenced by mutable tags, Python transitives are not fully hash-locked, and release images have no documented verified signature/provenance gate. Rollback sources a local state file as shell code; exploitation requires ability to write that trusted installation file. Pin/verify build inputs, publish/verifiably check provenance, and parse rollback state as data with strict field validation and restrictive permissions. Do not assume a new remote exploit without establishing who can modify these inputs. |
| HARD-08 | `backup_store.py`; `routers/hosts.py:231-258`; `migrate.py`, `_dump_to()`; `docker-compose.yml`, backup service | Firewall snapshots have no retention/deletion/total-size policy; downloads buffer complete responses. Database dumps are not explicitly chmod-hardened and include authentication-related records. Define backup retention/capacity, streaming caps and restrictive file modes, plus encrypted/off-host backup and complete secret-volume recovery procedures. This is availability/confidentiality hardening requiring access/usage conditions, not proof that stored backups were exposed over the network. |

## Controls checked and non-findings

- Session tokens use cryptographic randomness; only their SHA-256 hashes are stored. Passwords use Argon2. Reader write restrictions and anonymous protected-route restrictions are present. No authentication bypass was demonstrated.
- ORM queries and inspected SQL use bound parameters or trusted static statements. No request-driven SQL injection or shell-command injection path was established. Migration subprocesses use argument lists rather than a shell.
- XML restore parsing uses defusedxml, an input size check and an allowlisted metadata manifest. Raw backup export is a distinct path covered by SEC-01.
- Backup filename checks prevent straightforward directory traversal. UUID-based secret filenames do not use request-controlled arbitrary path strings.
- VPN deployment payloads are currently redacted/hashed before persistence, despite stale secret-store docstrings claiming otherwise. The report does not repeat those obsolete plaintext-payload claims as a current vulnerability. Error paths remain covered by SEC-08, and full git-history/legacy database data were not scanned.
- Inspected React content is normally escaped. The layout's `dangerouslySetInnerHTML` contains a static script; the website lightbox `innerHTML` uses static markup and two fixed language labels. No attacker-controlled HTML sink was established. The proxy's `unsafe-inline` CSP remains a defense-in-depth limitation, not proof of XSS.
- Caddy terminates TLS and adds HSTS/CSP/frame/content headers. Compose publishes backend/frontend to loopback by default and does not publish PostgreSQL. Widening `BIND_ADDRESS` can expose HTTP directly; operators should keep the documented proxy topology.
- Firewall TLS pinning uses a verifying trust context; the unauthenticated probe is deliberately only a fingerprint-discovery step. Trusting an uploaded CA intentionally trusts certificates it signs and is not identical to an exact leaf fingerprint pin. Confirm the chosen trust scope in UI/docs.
- Maintenance execute checks bind preview to actor, host, freshness and observed state and consume it before a non-idempotent remote action. VPN approval is weaker and is separately covered by SEC-14.
- Audit UPDATE/DELETE triggers exist. They are useful accidental-tampering controls, not resistance to the application database superuser, which can disable triggers/truncate tables.

## Remediation execution order

1. Implement explicit secret-export authorization, atomic credential/session revocation, verifying SMTP TLS and durable uncertain-deployment recovery (SEC-01–04).
2. Reject plaintext firewall URLs by default and make production transport settings consistent (SEC-05, HARD-06).
3. Make MFA consumption atomic and bound every authentication/security-management attempt path (SEC-06, SEC-07, SEC-13).
4. Remove raw-error/validation secret reflection, protect capability URLs and persist notification secrets with fail-closed delivery (SEC-08, SEC-11, SEC-15, SEC-16).
5. Guard managed-resource lifecycle, complete durable auditing and bind VPN approval to current secret versions (SEC-09, SEC-10, SEC-14).
6. Update dependencies after compatibility/reachability analysis, then apply infrastructure/build/backup hardening (SEC-12, HARD-01–08).

For each ticket, report the final affected files, regression evidence, migration/backward-compatibility consequences and unresolved limitations. Keep deployment approval separate from implementation work. A clean dependency scan or passing mocks alone must not be described as proof the whole product is secure.

## Remediation status (2026-10-07, Claude Code)

Every finding was re-checked against the source before changing it; all 16 SEC findings were confirmed as described (SEC-03 by reading `smtplib` in the Python 3.12 image, SEC-11 by listing `/data` in the live backend container: no `notification-secrets`, and no notification secrets are configured there). Regression tests: `backend/tests/test_security_review.py` (42 tests); full backend suite 475 passed. Nothing was deployed and no firewall was contacted; the frontend lint/typecheck/build passed in a scratch copy.

| ID | Status | What was done / what remains |
|---|---|---|
| SEC-01 | Fixed | Raw backup download is admin-only (403 `READ_ONLY_ACCESS` otherwise), listing stays visible; every attempt audited (`backup/download`, status `accepted`/`denied`); UI hides the button for readers. |
| SEC-02 | Fixed | Password change/reset revoke all sessions atomically with the new hash; admin 2FA reset revokes all; 2FA enable/disable revoke the *other* sessions; login re-checks the password hash under a row lock so a login racing a reset cannot create a session. |
| SEC-03 | Fixed | Verifying `ssl.create_default_context()` for SSL and STARTTLS (optional `SMTP_CA_FILE`); authenticated plaintext SMTP refused unless `ALLOW_INSECURE_SMTP_AUTH`. |
| SEC-04 | Fixed | New shared write path `services/deploy_flow.py` (all six deploy types): a durable intent row is committed before the first remote call (existing deployment: armed pending with previous payload; first deployment: `revert_failed` = "outcome unknown", blocks retries, notifies, "Rollback wiederholen" forgets it); UUID committed right after a confirmed write; failed apply keeps the deadline; timeouts/lost replies/5xx are classed ambiguous (`OpnsenseAmbiguousError`), connect failures and answered refusals are plain errors. Residual: a crash in the few milliseconds between the firewall executing `addRule` and the intent row being the only trace leaves the "unknown" row, which needs a manual look by design. |
| SEC-05 | Fixed, one deliberate gap | `http://`, userinfo, query/fragment rejected on create and in the client (existing rows get "offline", no credentials sent); `ALLOW_INSECURE_FIREWALL_URLS` as explicit override. `verify_tls=false` over HTTPS is **kept**: it is an existing explicit opt-in (pinning is the supported alternative). |
| SEC-06 | Fixed | User row re-read `FOR UPDATE` before consuming a TOTP step or recovery code; concurrent-request tests with the real database. |
| SEC-07 | Mostly fixed | Bounded/expiring throttle state, Argon2 in a bounded thread pool (4 concurrent, 32 queued, then 503/429), 2 MB request-body cap in Caddy. **Not changed:** the missing-MFA-code path is not counted as a failure (counting it would lock out ordinary two-step logins; the password was already correct and the cost is now bounded); the limiter stays per process (the backend runs one uvicorn worker). |
| SEC-08 | Fixed | Upstream error bodies are scrubbed (secret-named JSON fields, PEM blocks, key=value pairs, the host's own key/secret) and cut to 300 characters where the exception is built, so status, maintenance, deploy messages, DB rows and logs all get the safe form; webhook delivery errors no longer contain the URL. |
| SEC-09 | Fixed | Deletion blocked while VPN deployments or deployed certificate/ACME objects exist; secret files of never-deployed drafts are removed with the host. |
| SEC-10 | Fixed | Middleware now records VPN, certificate/ACME, notification, MFA/password and user-admin operations, plus denied writes of read-only accounts. Paths only — no bodies, no query strings. |
| SEC-11 | Fixed | `notification_secrets` volume + `NOTIFICATION_SECRET_DIR` in `docker-compose.yml`; missing signing secret/SMTP password now fails closed. No migration needed on the live instance (no secrets stored there); `docker compose up -d` creates the volume. |
| SEC-12 | Partly fixed | Lockfile refreshed (`sharp` 0.35.5, `source-map-js` 1.2.2, `brace-expansion`); these were the only findings present in the runtime image. Remaining 9 npm findings are build tooling (Tailwind 3/ESLint chain), no patched `braces` exists, `postcss-selector-parser` fix needs a Tailwind major. Re-scan the image after the next build; no recurring audit job added to CI yet. |
| SEC-13 | Fixed, one gap | Attempt budget on 2FA setup/enable/disable and password change (not reset by a new enrolment); pending enrolment expires after 15 minutes. Session binding of the enrolment not added. |
| SEC-14 | Fixed | Approval token mandatory for all three VPN types, bound to the stored secret's version (no secret-derived value in the token); preview now compares in the redacted form, so unchanged secrets no longer show as modified. Expiry/single-use not added. |
| SEC-15 | Fixed | Custom 422 handler returns only `loc`/`msg`/`type`. |
| SEC-16 | Fixed | Non-admins get the webhook URL reduced to scheme/host/port. |
| HARD-02 | Fixed | Same-host origins must be `https` when `COOKIE_SECURE=true`. |
| HARD-03 | Fixed | Secret files are created 0600 under a temp name with `O_EXCL|O_NOFOLLOW`, fsynced and renamed into place. |
| HARD-05 | Documented | Handbook now gives the fingerprint comparison before importing the CA. |
| HARD-06 | Partly | Startup warning when `FRONTEND_ORIGIN` is https but `COOKIE_SECURE` is false; the compose default is unchanged (would break HTTP development). |
| HARD-01, 04, 07, 08 | Open | Non-root containers/least-privilege DB role (volume ownership and migration role need a rollout plan), egress policy and streaming limits, CI pinning/signing and `scripts/opnexus` state-file parsing, backup retention/permissions — separate work items. |

Rollout notes: rebuild backend, frontend **and proxy** images (Caddyfile is baked in); apply the lockfile with the frontend rebuild; no database migration.
