// NEXT_PUBLIC_* vars are baked in at build time, which breaks as soon as
// this is reached via anything other than the exact hostname used during
// `docker compose build` (e.g. a LAN IP instead of localhost). Since this
// runs client-side in the viewer's own browser, deriving the backend URL
// from window.location at runtime instead adapts automatically — the
// backend is reached on this same origin (proxy) or, in direct dev mode, on port 8100.
import { translate, translateServerParams, translateServerText, type Language } from "@/lib/i18n";
export type { Language } from "@/lib/i18n";

function resolveApiUrl(): string {
  if (process.env.NEXT_PUBLIC_API_URL) return process.env.NEXT_PUBLIC_API_URL;
  if (typeof window !== "undefined") {
    // Behind the TLS proxy the API lives on the same origin (works for the IP and
    // for any name alike). Only the direct dev port 3100 still talks to :8100.
    if (window.location.port === "3100") return `${window.location.protocol}//${window.location.hostname}:8100`;
    return "";
  }
  return "http://localhost:8100";
}

const API_URL = resolveApiUrl();

export class ApiError extends Error {
  status: number;
  code?: string;
  params?: Record<string, string>;
  constructor(status: number, message: string, code?: string, params?: Record<string, string>) {
    super(message);
    this.status = status;
    this.code = code;
    this.params = params;
  }
}

function currentLanguage(): Language {
  return typeof window !== "undefined" && localStorage.getItem("opnexus-language") === "en" ? "en" : "de";
}

function makeApiError(status: number, detail: string, code?: string, params?: Record<string, string>): ApiError {
  const language = currentLanguage();
  if (code) return new ApiError(status, translate(language, `errors.${code}`, translateServerParams(language, params)), code, params);
  return new ApiError(status, translateServerText(language, detail));
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API_URL}/api${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
    cache: "no-store",
    // The session cookie lives on the backend's origin (port 8100), a
    // different origin than this frontend (port 3100) — without this,
    // the browser never sends or stores it. Requires the backend's CORS
    // config to reflect a specific origin (not "*"), which it does.
    credentials: "include",
  });
  if (!res.ok) {
    let detail = res.statusText;
    let code: string | undefined;
    let params: Record<string, string> | undefined;
    try {
      const body = await res.json();
      if (body?.detail && typeof body.detail === "object" && !Array.isArray(body.detail) && typeof body.detail.code === "string") {
        code = body.detail.code;
        params = body.detail.params && typeof body.detail.params === "object" ? body.detail.params : undefined;
      } else if (typeof body?.detail === "string") detail = body.detail;
      else if (Array.isArray(body?.detail)) detail = body.detail.map((item: { msg?: string }) => translateServerText(currentLanguage(), item.msg ?? "Ungültige Eingabe")).join("; ");
    } catch {
      // response had no JSON body
    }
    throw makeApiError(res.status, detail, code, params);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// Separate from request<T>() because a backup download is a raw XML blob,
// not JSON: it must not force Content-Type: application/json on the
// request or call res.json() on the response. The filename comes from
// Content-Disposition, which the backend's CORS config explicitly exposes
// (browsers hide all but a few standard response headers cross-origin by
// default).
async function requestBlob(path: string): Promise<{ blob: Blob; filename: string }> {
  const res = await fetch(`${API_URL}/api${path}`, { cache: "no-store", credentials: "include" });
  if (!res.ok) {
    let detail = res.statusText;
    let code: string | undefined;
    let params: Record<string, string> | undefined;
    try {
      const body = await res.json();
      if (body?.detail && typeof body.detail === "object" && !Array.isArray(body.detail) && typeof body.detail.code === "string") {
        code = body.detail.code;
        params = body.detail.params && typeof body.detail.params === "object" ? body.detail.params : undefined;
      } else if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      // response had no JSON body
    }
    throw makeApiError(res.status, detail, code, params);
  }
  const match = (res.headers.get("Content-Disposition") ?? "").match(/filename="?([^"]+)"?/);
  return { blob: await res.blob(), filename: match?.[1] ?? "backup.xml" };
}

export type HostStatus = "online" | "offline" | "disabled";

export interface Host {
  id: string;
  name: string;
  base_url: string;
  verify_tls: boolean;
  tls_pinned: boolean;
  tls_fingerprint: string | null;
  enabled: boolean;
  cluster_name: string | null;
  policy_hierarchy_group_id: string | null;
  created_at: string;
  status: HostStatus;
  carp_status: string | null;
  status_detail: string | null;
}

export interface HostCreate {
  name: string;
  base_url: string;
  api_key: string;
  api_secret: string;
  verify_tls: boolean;
  tls_ca_pem?: string | null;
  enabled: boolean;
  cluster_name?: string | null;
  policy_hierarchy_group_id?: string | null;
  // Ask the firewall once with these credentials before saving (the host page and the setup assistant do).
  verify_connection?: boolean;
}

export interface TlsProbe {
  pem: string;
  sha256: string;
  subject: string;
  issuer: string;
  self_signed: boolean;
  not_before: string;
  not_after: string;
  expired: boolean;
  san: string[];
}

export interface HostUpdate {
  name?: string;
  base_url?: string;
  verify_tls?: boolean;
  tls_ca_pem?: string | null;
  clear_tls_pin?: boolean;
  enabled?: boolean;
  cluster_name?: string | null;
  clear_cluster_name?: boolean;
  policy_hierarchy_group_id?: string | null;
  clear_policy_hierarchy_group_id?: boolean;
}

export interface NetAddress {
  address: string;
  vhid: string | null;
  carp_status: string | null;
}

export interface NetInterface {
  device: string;
  identifier: string | null;
  description: string;
  status: string;
  enabled: boolean;
  is_physical: boolean;
  mtu: string | null;
  mac: string | null;
  media: string | null;
  vlan_tag: string | null;
  vlan_parent: string | null;
  ipv4_type: string | null;
  ipv6_type: string | null;
  addresses: NetAddress[];
  pending_action: string | null;
  assigned: boolean;
  is_management: boolean;
}

export interface NetVlan {
  uuid: string;
  parent: string;
  tag: string;
  pcp: string | null;
  description: string;
  device: string | null;
}

export interface NetVip {
  uuid: string;
  interface: string;
  interface_label: string;
  mode: string;
  address: string;
  vhid: string | null;
  advbase: string | null;
  advskew: string | null;
  description: string;
}

export interface NetGateway {
  uuid: string;
  name: string;
  description: string;
  interface: string;
  interface_label: string;
  ip_protocol: string;
  address: string;
  is_default: boolean;
  disabled: boolean;
  priority: string | null;
  status: string;
}

export interface NetRoute {
  uuid: string;
  network: string;
  gateway: string;
  description: string;
  disabled: boolean;
}

export type NetworkKind =
  | "vlan" | "vip" | "gateway" | "route" | "interface" | "dns_host" | "dhcp_host" | "kea_subnet" | "kea_reservation"
  | "tunable" | "cron_job" | "syslog_destination" | "gateway_group" | "lagg" | "bridge" | "loopback" | "gre" | "gif" | "vxlan"
  | "dns_forward" | "dhcp_range";

export interface PingResult {
  target: string;
  sent: number;
  received: number;
  loss: string;
  min: number | null;
  avg: number | null;
  max: number | null;
  stddev: number | null;
  error: string | null;
}

export interface TracerouteResult {
  target: string;
  notice: string;
  hops: { ttl: string; address: string; host: string; as: string; probes: string; silent?: boolean }[];
}

export interface PortprobeResult {
  target: string;
  port: number;
  open: boolean;
  message: string;
}

export interface DnsLookupResult {
  name: string;
  records: Record<string, { answers: string[]; query_time: string; server: string }>;
}

export interface NetworkObjects {
  available: boolean;
  columns: string[];
  rows: Record<string, string>[];
}

export interface NetworkDifference {
  category: string;
  key: string;
  status: "missing" | "different";
  present_on: string[];
  missing_from: string[];
  signatures: Record<string, string>;
}

export interface ClusterNetworkDrift {
  name: string;
  hosts: string[];
  compared: string[];
  unreachable: string[];
  differences: NetworkDifference[];
}

export interface HostSnapshot {
  uuid: string;
  name: string;
  active: string;
  size: string;
  created: string;
  mountpoint: string;
}
export type NetworkAction = "add" | "set" | "del";

export interface NetworkChangeRequest {
  kind: NetworkKind;
  action: NetworkAction;
  uuid?: string;
  fields: Record<string, string>;
}

export interface NetworkRisk {
  code: string;
  params: Record<string, string>;
}

export interface NetworkChangePreview {
  token: string;
  kind: NetworkKind;
  action: NetworkAction;
  uuid: string | null;
  host_id: string;
  host_name: string;
  changes: { field: string; before: string | null; after: string | null }[];
  risks: NetworkRisk[];
  warnings: NetworkRisk[];
  blocks: NetworkRisk[];
  requires_acknowledgement: boolean;
  management_device: string | null;
  can_execute: boolean;
}

export interface NetworkChangeResult {
  status: string;
  uuid: string | null;
  verified: boolean;
  mismatches: string[];
  backup_id: string;
}

export type NetworkTemplate = Record<string, { value: string; options: { value: string; label: string; selected: boolean }[] | null }>;

export interface NetDnsHost {
  uuid: string;
  hostname: string;
  domain: string;
  record_type: string;
  server: string;
  enabled: boolean;
  description: string;
}

export interface NetDhcpHost {
  uuid: string;
  host: string;
  domain: string;
  ip: string;
  hwaddr: string;
  lease_time: string;
  description: string;
}

export interface NetKeaSubnet {
  uuid: string;
  subnet: string;
  pools: string;
  routers: string;
  dns_servers: string;
  description: string;
}

export interface NetKeaReservation {
  uuid: string;
  subnet: string;
  ip_address: string;
  hw_address: string;
  hostname: string;
  description: string;
}

export interface HostNetwork {
  host_id: string;
  host_name: string;
  interfaces: NetInterface[];
  vlans: NetVlan[];
  vips: NetVip[];
  gateways: NetGateway[];
  routes: NetRoute[];
  dns_hosts: NetDnsHost[];
  dhcp_hosts: NetDhcpHost[];
  kea_subnets: NetKeaSubnet[];
  kea_reservations: NetKeaReservation[];
  management_device: string | null;
  can_configure_interfaces: boolean;
  unavailable: string[];
}

export interface LiveRule {
  uuid: string;
  description: string;
  action: string;
  action_label: string;
  direction: string;
  direction_label: string;
  interface: string | null;
  interface_label: string | null;
  protocol: string | null;
  protocol_label: string | null;
  source_net: string;
  source_port: string | null;
  destination_net: string;
  destination_port: string | null;
  enabled: boolean;
  is_automatic: boolean;
}

// Raw, untyped OPNsense search rows for the three NAT types' live-fetch
// endpoints — deliberately loose (Record, not a strict interface) since the
// three types have different field shapes (see nat_drift_service.py's
// module docstring for the exact, core-source-verified field names per
// type) and this is read-only display data, not a write payload.
export type NatLiveRow = Record<string, unknown>;
export interface NatLiveSplit {
  rules: NatLiveRow[];
  automatic_rules: NatLiveRow[];
}

export interface FirmwareInfo {
  product_id: string;
  product_version: string;
}

export interface InterfaceOption {
  name: string;
  label: string;
}

export interface AliasOption {
  name: string;
  type: string;
  description: string;
  content: string;
  enabled: boolean;
}

export interface BackupDiff {
  a: string;
  b: string;
  identical: boolean;
  added: number;
  removed: number;
  truncated: boolean;
  lines: string[];
}

export interface BackupSettings {
  scheduled_enabled: boolean;
  interval_hours: number;
  keep_count: number;
  offsite_enabled: boolean;
  last_run_at: string | null;
  last_offsite_ok_at: string | null;
  last_offsite_error: string | null;
  s3_endpoint: string | null;
  s3_region: string;
  s3_bucket: string | null;
  s3_prefix: string;
  s3_access_key: string | null;
  s3_path_style: boolean;
  s3_secret_set: boolean;
  passphrase_set: boolean;
}

export type BackupSettingsUpdate = Partial<Pick<BackupSettings, "scheduled_enabled" | "interval_hours" | "keep_count" | "offsite_enabled" | "s3_endpoint" | "s3_region" | "s3_bucket" | "s3_prefix" | "s3_access_key" | "s3_path_style">> & {
  s3_secret_key?: string;
  clear_s3_secret?: boolean;
  passphrase?: string;
};

export interface HostBackup {
  id: string;
  created_at: string;
  size: number;
}

export interface RestoreManifest {
  hostname: string;
  domain: string;
  webgui_protocol: string;
  webgui_port: string;
  interfaces: { name: string; if: string }[];
  hasync_enabled: boolean;
  synchronize_to_ip: string | null;
  secret_fields_redacted: number;
}

export type RestoreStatus = "preview" | "handed_off" | "verified" | "cancelled";

export interface RestoreOperation {
  id: string;
  host_id: string;
  host_name: string;
  actor: string;
  backup_id: string;
  status: RestoreStatus;
  created_at: string;
  expires_at: string;
  manifest: RestoreManifest;
  warnings: string[];
  handed_off_at: string | null;
  confirmed_at: string | null;
  cancelled_at: string | null;
  message: string | null;
}

export type CertStatus = "valid" | "expiring_soon" | "expired" | "unknown";

export interface HostCertificate {
  uuid: string;
  refid: string;
  descr: string;
  common_name: string | null;
  issuer: string;
  valid_from: string | null;
  valid_to: string | null;
  days_until_expiry: number | null;
  status: CertStatus;
  in_use: boolean;
}

export interface HostCa {
  uuid: string;
  refid: string;
  descr: string;
  common_name: string | null;
  valid_from: string | null;
  valid_to: string | null;
  days_until_expiry: number | null;
  status: CertStatus;
}

export type DeploymentStatus = "success" | "error" | "pending" | "reverted" | "revert_failed";

export interface DeploymentResult {
  host_id: string;
  host_name: string;
  status: DeploymentStatus;
  message: string | null;
  revert_deadline: string | null;
}

export type RuleDirection = "in" | "out";
export type RuleAction = "pass" | "block" | "reject";

export interface Rule {
  id: string;
  description: string;
  interface: string;
  direction: RuleDirection;
  ipprotocol: string;
  protocol: string;
  action: RuleAction;
  source_net: string;
  source_port: string | null;
  destination_net: string;
  destination_port: string | null;
  enabled: boolean;
  log: boolean;
  created_at: string;
  updated_at: string;
  target_host_ids: string[];
  target_group_ids: string[];
  workflow_state: "attention" | "pending" | "cleanup" | "unassigned" | "disabled" | "draft" | "synced";
  workflow_counts: { targets: number; active_targets: number; changes: number; pending: number; errors: number; retained: number };
  effective_host_ids: string[];
  deployments: DeploymentResult[];
}

export interface RuleCreate {
  description: string;
  interface: string;
  direction: RuleDirection;
  ipprotocol: string;
  protocol: string;
  action: RuleAction;
  source_net: string;
  source_port: string | null;
  destination_net: string;
  destination_port: string | null;
  enabled: boolean;
  log?: boolean;
  target_host_ids: string[];
  target_group_ids: string[];
}

export interface UnmanagedRule {
  uuid: string;
  description: string;
  interface: string;
  interface_label: string;
  direction: string;
  action: string;
  protocol: string;
  source_net: string;
  source_port: string;
  destination_net: string;
  destination_port: string;
  enabled: boolean;
  log: boolean;
  adoptable: boolean;
  reasons: string[];
  /** Existing central rules equal to this one (resolved for the host); first = preferred. */
  matches: LinkTarget[];
  /** Same description, different content: shown as a hint with the differing fields, never linked. */
  similar: LinkTarget[];
  linkable: boolean;
}

export interface LinkDiff {
  /** Model field or "variables" (a {{VARIABLE}} of the central object has no value on this host). */
  field: string;
  central: string;
  live: string;
}

/** An existing central object a live object could be linked to (exact match) or resembles (diff). */
export interface LinkTarget {
  id: string;
  name: string;
  targets: string[];
  diff: LinkDiff[];
}

export type AdoptKind = "aliases" | "port-forward" | "one-to-one" | "outbound-nat";

/** An alias / NAT rule found live on a host that OPNexus does not manage yet. */
export interface UnmanagedObject {
  uuid: string;
  title: string;
  enabled: boolean;
  adoptable: boolean;
  /** Not adoptable alone yet, but in the same "adopt all" run (an alias whose referenced aliases come first). */
  bulk_adoptable: boolean;
  reasons: string[];
  reason_details: Record<string, string>;
  /** Settings OPNexus does not manage that stay untouched on the firewall. */
  kept: string[];
  info: Record<string, string>;
  matches: LinkTarget[];
  similar: LinkTarget[];
  linkable: boolean;
  /** What "adopt all with linking" does with it: link to its twin, adopt as new, or leave it. */
  bulk_action: "link" | "new" | "skip" | null;
}

export interface AdoptAllResult {
  adopted: { uuid: string; title: string; id: string; linked: boolean }[];
  skipped: { uuid: string; title: string; reasons: string[]; reason_details: Record<string, string> }[];
}

export type PolicyAliasType = "host" | "network" | "port" | "networkgroup" | "mac" | "asn";

export interface PolicyAlias {
  id: string;
  name: string;
  type: PolicyAliasType;
  content: string;
  description: string;
  enabled: boolean;
  created_at: string;
  updated_at: string;
  target_host_ids: string[];
  target_group_ids: string[];
  policy_hierarchy_group_id: string | null;
  workflow_state: "attention" | "pending" | "cleanup" | "unassigned" | "disabled" | "draft" | "synced";
  workflow_counts: { targets: number; active_targets: number; changes: number; pending: number; errors: number; retained: number };
  effective_host_ids: string[];
  deployments: DeploymentResult[];
}

export interface PolicyAliasCreate {
  name: string;
  type: PolicyAliasType;
  content: string;
  description: string;
  enabled: boolean;
  target_host_ids: string[];
  target_group_ids: string[];
  policy_hierarchy_group_id: string | null;
}

export interface PolicyAliasRevision {
  id: string;
  created_at: string;
  note: string;
  snapshot: PolicyAliasCreate;
}

// Zentrale Log-Suche/Dashboards: OPNexus now persists ingested firewall
// log lines itself (see backend/app/services/log_ingest_service.py) and
// searches/aggregates server-side instead of live-fetching+merging in the
// browser — see frontend/app/logs/page.tsx.
export interface LogSearchEntry {
  id: string;
  host_id: string;
  timestamp: string;
  action: string | null;
  interface: string | null;
  direction: string | null;
  protocol: string | null;
  source_net: string | null;
  source_port: string | null;
  destination_net: string | null;
  destination_port: string | null;
  label: string | null;
}

export interface LogSearchResult {
  total: number;
  offset: number;
  limit: number;
  items: LogSearchEntry[];
}

export interface LogSearchParams {
  host_id?: string;
  cluster_name?: string;
  q?: string;
  action?: string;
  interface?: string;
  protocol?: string;
  since?: string;
  until?: string;
  offset?: number;
  limit?: number;
}

export interface LogStatCount {
  value: string;
  count: number;
}

export interface LogStats {
  by_action: Record<string, number>;
  by_interface: Record<string, number>;
  top_blocked_sources: LogStatCount[];
  top_destination_ports: LogStatCount[];
}

export interface LogFilterOptions {
  actions: string[];
  interfaces: string[];
  protocols: string[];
}

export type UserRole = "admin" | "operator" | "read_only";

export interface AuthState {
  authenticated: boolean;
  username?: string;
  role?: UserRole;
}

export type TokenScope = "read" | "deploy" | "admin";

export interface ApiToken {
  id: string;
  name: string;
  scope: TokenScope;
  prefix: string;
  owner: string;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  last_used_ip: string | null;
}

export type Severity = "critical" | "high" | "medium" | "low" | "info";

export interface ComplianceFinding {
  id: string;
  severity: Severity;
  passed: boolean | null;
  params: { count?: number; examples?: string[]; reason?: string; protocol?: string; age_days?: number | null };
}

export interface RuleHit {
  rule_uuid: string;
  description: string;
  enabled: boolean;
  state: "used" | "unused" | "collecting" | "disabled";
  packets: number;
  bytes: number;
  last_hit_at: string | null;
  first_seen_at: string;
}

export interface ComplianceHost {
  host_id: string;
  host_name: string;
  cluster_name: string | null;
  enabled: boolean;
  checked_at: string | null;
  score: number | null;
  grade: "good" | "fair" | "poor" | null;
  error: "unreachable" | "invalid_config" | null;
  counts: Record<Severity, number> | null;
  findings?: ComplianceFinding[];
}

export interface GuiAccessSettings {
  enabled: boolean;
  idle_timeout_minutes: number;
  absolute_timeout_minutes: number;
  slot_count: number;
  available: boolean;
  unavailable_reasons: string[];
  slots: number[];
  updated_by: string | null;
  updated_at: string | null;
}

export type GuiAccessUpdate = Pick<GuiAccessSettings, "enabled" | "idle_timeout_minutes" | "absolute_timeout_minutes" | "slot_count">;

export interface GuiTicket {
  url: string;
  slot: number;
  expires_in: number;
}

export interface AuditExportSettings {
  syslog_enabled: boolean;
  syslog_host: string | null;
  syslog_port: number;
  syslog_protocol: "udp" | "tcp" | "tls";
  webhook_enabled: boolean;
  webhook_url: string | null;
  webhook_signing_secret_set: boolean;
  last_success_at: string | null;
  last_error: string | null;
  last_error_at: string | null;
}

export type AuditExportUpdate = Partial<Omit<AuditExportSettings, "webhook_signing_secret_set" | "last_success_at" | "last_error" | "last_error_at">> & {
  webhook_signing_secret?: string;
  clear_webhook_signing_secret?: boolean;
};

export interface OidcSettings {
  enabled: boolean;
  display_name: string;
  issuer: string | null;
  client_id: string | null;
  client_secret_set: boolean;
  scopes: string;
  groups_claim: string;
  admin_group: string | null;
  operator_group: string | null;
  read_only_group: string | null;
}

export type OidcSettingsUpdate = Partial<Omit<OidcSettings, "client_secret_set">> & { client_secret?: string; clear_client_secret?: boolean };

export interface OverlaySettings {
  enabled: boolean;
  endpoint: string | null;
  subnet: string;
  listen_port: number;
  hub_public_key: string | null;
  hub_address: string;
  hub_state: "running" | "stale" | "absent";
}

export interface OverlayPeer {
  id: string;
  name: string;
  ip: string;
  public_key: string | null;
  host_id: string | null;
  host_name: string | null;
  connected: boolean;
  last_handshake_at: string | null;
  rx_bytes: number;
  tx_bytes: number;
  remote_endpoint: string | null;
}

export interface OverlayInstructions {
  hub_public_key: string;
  endpoint: string | null;
  tunnel_address: string;
  allowed_ips: string;
  psk: string;
  keepalive: number;
  hub_ip: string;
  host_url: string;
}

export interface HostVariable {
  name: string;
  value: string | null;
  used_by: { kind: string; id: string; label: string }[];
  missing: boolean;
}

export interface User {
  id: string;
  username: string;
  role: UserRole;
  created_at: string;
  last_login_at: string | null;
  totp_enabled?: boolean;
  auth_source?: "local" | "oidc";
}

export interface LoginResult extends AuthState {
  previous_login_at?: string | null;
  previous_login_ip?: string | null;
  failed_attempts_since_last_login?: number;
  session_expires_at?: string;
}

// IPsec-VPN-Management (Site-to-Site, PSK) — see
// backend/app/services/ipsec_deploy_service.py's module docstring for the
// full field/endpoint verification and the deliberately simpler status
// model (only "success"/"error", no pending/confirm/auto-revert window —
// an IPsec reconfigure can sever the very management path OPNexus would
// need to auto-revert it, so this app never pretends to have a safety net
// here). pre_shared_key is write-only: never returned by the API, only
// pre_shared_key_set (whether one has been configured).
export type IkeVersion = "ike" | "ikev1" | "ikev2";

export interface IpsecTunnelBase {
  description: string;
  local_address: string;
  remote_address: string;
  ike_version: IkeVersion;
  proposal: string;
  local_identity: string;
  remote_identity: string;
  local_networks: string;
  remote_networks: string;
  esp_proposal: string;
  enabled: boolean;
  target_host_ids: string[];
  target_group_ids: string[];
}

export interface IpsecTunnelCreate extends IpsecTunnelBase {
  pre_shared_key: string;
}

export type IpsecDeploymentStatus = "success" | "error";

export interface IpsecDeploymentResult {
  host_id: string;
  host_name: string;
  status: IpsecDeploymentStatus;
  message: string | null;
}

export interface IpsecTunnel extends IpsecTunnelBase {
  id: string;
  pre_shared_key_set: boolean;
  created_at: string;
  updated_at: string;
  effective_host_ids: string[];
  workflow_state: "attention" | "cleanup" | "unassigned" | "disabled" | "draft" | "synced";
  workflow_counts: { targets: number; active_targets: number; changes: number; errors: number; retained: number };
  deployments: IpsecDeploymentResult[];
}

export interface IpsecTunnelRevision {
  id: string;
  created_at: string;
  note: string;
  snapshot: IpsecTunnelBase & { pre_shared_key_set: boolean };
}

export interface IpsecPreview {
  token: string;
  can_deploy: boolean;
  basis: string;
  warnings: string[];
  hosts: {
    host_id: string; host_name: string; action: string; status: string | null; message: string | null;
    can_remove: boolean; changes: { field: string; before: unknown; after: unknown }[];
  }[];
}

// OpenVPN instance management — see
// backend/app/services/openvpn_deploy_service.py's module docstring for
// the full field/endpoint verification, why tls_key hardening is optional
// rather than a PSK-equivalent sole auth mechanism, and the deliberately
// simpler status model (only "success"/"error", no pending/confirm/
// auto-revert — same rationale as IPsec: an OpenVPN reconfigure can sever
// the very management path OPNexus would need to auto-revert it).
// cert_refs/ca_refs are per-host maps ({host_id: uuid}) since a
// certificate's UUID is host-local — see the same module docstring.
// tls_key_material is write-only: never returned by the API, only
// tls_key_material_set (whether one has been configured).
export type OpenVpnRole = "server" | "client";
export type OpenVpnProto = "udp" | "tcp";
export type OpenVpnTlsKeyMode = "auth" | "crypt";

export interface OpenVpnInstanceBase {
  description: string;
  role: OpenVpnRole;
  proto: OpenVpnProto;
  port: string;
  local: string;
  remote: string;
  server_network: string;
  cert_refs: Record<string, string>;
  ca_refs: Record<string, string>;
  tls_key_enabled: boolean;
  tls_key_mode: OpenVpnTlsKeyMode;
  enabled: boolean;
  target_host_ids: string[];
  target_group_ids: string[];
}

export interface OpenVpnInstanceCreate extends OpenVpnInstanceBase {
  tls_key_material?: string | null;
}

export type OpenVpnDeploymentStatus = "success" | "error";

export interface OpenVpnDeploymentResult {
  host_id: string;
  host_name: string;
  status: OpenVpnDeploymentStatus;
  message: string | null;
}

export interface OpenVpnInstance extends OpenVpnInstanceBase {
  id: string;
  tls_key_material_set: boolean;
  created_at: string;
  updated_at: string;
  effective_host_ids: string[];
  workflow_state: "attention" | "cleanup" | "unassigned" | "disabled" | "draft" | "synced";
  workflow_counts: { targets: number; active_targets: number; changes: number; errors: number; retained: number };
  deployments: OpenVpnDeploymentResult[];
}

export interface OpenVpnInstanceRevision {
  id: string;
  created_at: string;
  note: string;
  snapshot: OpenVpnInstanceBase & { tls_key_material_set: boolean };
}

export interface OpenVpnPreview {
  token: string;
  can_deploy: boolean;
  basis: string;
  warnings: string[];
  hosts: {
    host_id: string; host_name: string; action: string; status: string | null; message: string | null;
    can_remove: boolean; changes: { field: string; before: unknown; after: unknown }[];
  }[];
}

// WireGuard tunnel management — see
// backend/app/services/wireguard_deploy_service.py's module docstring for
// the full field/endpoint verification, the entity-shape decision (one
// Server + exactly one Peer per tunnel, mirroring how the real object
// model's denormalized peer<->server linkage is handled at deploy time),
// and the deliberately simpler status model (only "success"/"error", no
// pending/confirm/auto-revert — same rationale as IPsec/OpenVPN: a
// WireGuard reconfigure can sever the very management path OPNexus would
// need to auto-revert it).
// server_private_key/peer_psk_material are write-only: never returned by
// the API, only server_private_key_set/peer_psk_set (whether material has
// been configured).
export interface WireguardTunnelBase {
  server_name: string;
  server_public_key: string;
  listen_port: string;
  server_tunnel_address: string;
  dns: string;
  mtu: string;
  peer_name: string;
  peer_public_key: string;
  peer_psk_enabled: boolean;
  peer_allowed_ips: string;
  peer_endpoint_address: string;
  peer_endpoint_port: string;
  peer_keepalive: string;
  enabled: boolean;
  target_host_ids: string[];
  target_group_ids: string[];
}

export interface WireguardTunnelCreate extends WireguardTunnelBase {
  server_private_key: string;
  peer_psk_material?: string | null;
}

export type WireguardDeploymentStatus = "success" | "error";

export interface WireguardDeploymentResult {
  host_id: string;
  host_name: string;
  status: WireguardDeploymentStatus;
  message: string | null;
}

export interface WireguardTunnel extends WireguardTunnelBase {
  id: string;
  server_private_key_set: boolean;
  peer_psk_set: boolean;
  created_at: string;
  updated_at: string;
  effective_host_ids: string[];
  workflow_state: "attention" | "cleanup" | "unassigned" | "disabled" | "draft" | "synced";
  workflow_counts: { targets: number; active_targets: number; changes: number; errors: number; retained: number };
  deployments: WireguardDeploymentResult[];
}

export interface WireguardTunnelRevision {
  id: string;
  created_at: string;
  note: string;
  snapshot: WireguardTunnelBase & { server_private_key_set: boolean; peer_psk_set: boolean };
}

export interface WireguardPreview {
  token: string;
  can_deploy: boolean;
  basis: string;
  warnings: string[];
  hosts: {
    host_id: string; host_name: string; action: string; status: string | null; message: string | null;
    can_remove: boolean; changes: { field: string; before: unknown; after: unknown }[];
  }[];
}

// Trust-Store (ManagedCertificate) + ACME (AcmeAccount,
// AcmeCertificateRequest) certificate management — see
// backend/app/services/managed_certificate_service.py / acme_service.py's
// module docstrings for the full field/endpoint verification. Both are
// deliberately PER-HOST (a plain host_id, no target_host_ids/
// target_group_ids the way IPsec/OpenVPN/WireGuard have) — caref/account/
// validationMethod UUIDs are host-local, not portable across hosts (see
// the plan / README). Trust-Store pushes need NO warning gate in the UI
// (no restart/lockout-shaped risk exists — confirmed against
// opnsense/core#9399). ACME's register/sign DO get a two-step warning
// gate (AcmeActionConfirm.tsx) for a DIFFERENT reason than the VPN
// features' no-rollback warning: they reach a REAL external CA and count
// against its rate limits, not a connectivity-loss risk.
export type DeployStatus = "draft" | "success" | "error";
export type CertAction = "import" | "internal" | "external" | "sign_csr" | "import_csr" | "reissue" | "manual";
// Curated subset of Trust/Cert.xml's key_type OptionValues.
export type CertKeyType = "2048" | "3072" | "4096" | "prime256v1" | "secp384r1" | "secp521r1";
export type CertDigest = "sha1" | "sha224" | "sha256" | "sha384" | "sha512";
export type CertType = "usr_cert" | "server_cert" | "combined_server_client" | "v3_ca";

export interface ManagedCertificateBase {
  host_id: string;
  action: CertAction;
  description: string;
  commonname: string;
  country: string;
  state: string;
  city: string;
  organization: string;
  email: string;
  key_type: CertKeyType;
  digest: CertDigest;
  cert_type: CertType;
  lifetime: number;
  caref: string | null;
  csr_payload: string | null;
  crt_payload: string | null;
}

export interface ManagedCertificateCreate extends ManagedCertificateBase {
  // Only meaningful for action="import" — write-only, never returned by
  // the API (only private_key_set is).
  private_key?: string | null;
}

export interface ManagedCertificate extends ManagedCertificateBase {
  id: string;
  private_key_set: boolean;
  opnsense_uuid: string | null;
  status: DeployStatus;
  message: string | null;
  deployed_at: string | null;
  created_at: string;
  updated_at: string;
}

// ca OptionValues verified against AcmeClient.xml.
export type AcmeCA =
  | "letsencrypt" | "letsencrypt_test" | "zerossl" | "buypass" | "buypass_test" | "google" | "google_test" | "sslcom" | "custom";

export interface AcmeAccountBase {
  host_id: string;
  name: string;
  description: string;
  email: string;
  ca: AcmeCA;
  custom_ca: string;
  eab_kid: string;
  enabled: boolean;
}

export interface AcmeAccountCreate extends AcmeAccountBase {
  // Write-only External-Account-Binding HMAC — never returned, only
  // eab_hmac_set is.
  eab_hmac?: string | null;
}

export interface AcmeAccount extends AcmeAccountBase {
  id: string;
  eab_hmac_set: boolean;
  opnsense_uuid: string | null;
  status: DeployStatus;
  message: string | null;
  deployed_at: string | null;
  // Outcome of the last `register` trigger (a REAL external-CA call) —
  // orthogonal to `status` (the local config-write outcome).
  register_status: string | null;
  register_message: string | null;
  register_at: string | null;
  created_at: string;
  updated_at: string;
}

// keyLength OptionValues are PREFIXED on the wire (key_4096, not 4096) —
// verified against AcmeClient.xml, see AcmeCertificateRequest's backend
// docstring for why this is a real field-shape trap.
export type AcmeKeyLength = "key_2048" | "key_3072" | "key_4096" | "key_ec256" | "key_ec384";

export interface AcmeCertificateRequestBase {
  host_id: string;
  name: string;
  // Comma-separated (OPNsense's own CSVListField convention), not a
  // sub-array.
  alt_names: string;
  account_id: string;
  // Plain, OPNexus-unvalidated UUID the operator types in themselves —
  // DNS-provider validation config is entirely out of v1 scope, see
  // README.
  validation_ref: string;
  key_length: AcmeKeyLength;
  ocsp: boolean;
  auto_renewal: boolean;
  // DAYS SINCE LAST ISSUANCE, NOT days-before-expiry — see the form
  // label in AcmeCertificateForm.tsx and the README.
  renew_interval: number;
  enabled: boolean;
}

export type AcmeCertificateRequestCreate = AcmeCertificateRequestBase;

export interface AcmeCertificateRequest extends AcmeCertificateRequestBase {
  id: string;
  opnsense_uuid: string | null;
  status: DeployStatus;
  message: string | null;
  deployed_at: string | null;
  // Outcome of the last `sign` trigger. "triggered" means the
  // ASYNCHRONOUS issuance was accepted, NOT that it succeeded — the real
  // result only shows up in remote_status_code/remote_status_last_update/
  // remote_last_update after a poll.
  sign_status: string | null;
  sign_message: string | null;
  sign_triggered_at: string | null;
  remote_status_code: number | null;
  remote_status_last_update: string | null;
  remote_last_update: string | null;
  created_at: string;
  updated_at: string;
}

export interface SearchHit {
  category: string;
  id: string;
  title: string;
  subtitle: string | null;
  href: string;
}

export interface VersionCheck {
  enabled: boolean;
  current: string;
  latest: string | null;
  update_available: boolean;
  release_url: string | null;
  checked_at: string | null;
  error: string | null;
}

export interface UpdaterJob {
  id: string;
  action: "update" | "rollback";
  target: string | null;
  state: "running" | "succeeded" | "failed" | "rolled_back";
  started_at: string;
  finished_at: string | null;
  exit_code: number | null;
  from_version: string | null;
}

export type UpdateCheckSettings = { enabled: boolean; feed_url: string; managed_by_server: boolean };
export interface UpdaterStatus {
  /** false = Updater nicht aktiviert/erreichbar (`reason` sagt warum). */
  available: boolean;
  reason: "not_enabled" | "unreachable" | "unauthorized" | null;
  mode: "release" | "local" | null;
  current: string;
  latest: string | null;
  update_available: boolean;
  release_url: string | null;
  feed_enabled: boolean;
  feed_error: string | null;
  checked_at: string | null;
  can_update: boolean;
  major_change: boolean;
  job: UpdaterJob | null;
  phases: string[];
  log: string[];
  services: Record<string, string>;
  rollback_available: boolean;
  rollback_loses_data: boolean;
  rollback_version: string | null;
  active_rollouts?: number;
}

export const api = {
  search: (q: string, options?: RequestInit) => request<SearchHit[]>(`/search?${new URLSearchParams({ q })}`, options),
  settings: () => request<AppSettings>("/settings"),
  saveSettings: (partial:{theme?:Theme;language?:Language;setup_done?:boolean}) => request<AppSettings>("/settings",{method:"PUT",body:JSON.stringify(partial)}),
  notificationSettings: () => request<NotificationSettings>("/notifications/settings"),
  saveNotificationSettings: (data: NotificationSettingsUpdate) => request<NotificationSettings>("/notifications/settings", { method: "PUT", body: JSON.stringify(data) }),
  logSettings: () => request<LogSettings>("/logs/settings"),
  saveLogSettings: (data: LogSettingsUpdate) => request<LogSettings>("/logs/settings", { method: "PUT", body: JSON.stringify(data) }),
  getGuiAccess: () => request<GuiAccessSettings>("/settings/gui-access"),
  updateGuiAccess: (data: GuiAccessUpdate) => request<GuiAccessSettings>("/settings/gui-access", { method: "PUT", body: JSON.stringify(data) }),
  createGuiTicket: (hostId: string) => request<GuiTicket>(`/hosts/${hostId}/gui-ticket`, { method: "POST" }),
  getAuditExport: () => request<AuditExportSettings>("/settings/audit-export"),
  updateAuditExport: (data: Partial<AuditExportUpdate>) => request<AuditExportSettings>("/settings/audit-export", { method: "PUT", body: JSON.stringify(data) }),
  testAuditExport: () => request<{ success: boolean }>("/settings/audit-export/test", { method: "POST" }),
  notificationChannels: () => request<NotificationChannel[]>("/notifications/channels"),
  createNotificationChannel: (data: NotificationChannelCreate) => request<NotificationChannel>("/notifications/channels", { method: "POST", body: JSON.stringify(data) }),
  updateNotificationChannel: (id: string, data: NotificationChannelUpdate) => request<NotificationChannel>(`/notifications/channels/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteNotificationChannel: (id: string) => request<void>(`/notifications/channels/${id}`, { method: "DELETE" }),
  testNotificationChannel: (id: string) => request<{ success: boolean }>(`/notifications/channels/${id}/test`, { method: "POST" }),
  hostGateways: (id: string) => request<GatewayHealth[]>(`/hosts/${id}/gateways`),
  hostApiRights: (id: string) => request<ApiRightsState>(`/hosts/${id}/gateways/api-rights`),
  setGatewayAlert: (id: string, name: string, enabled: boolean) =>
    request<{ name: string; alert_enabled: boolean }>(`/hosts/${id}/gateways/${encodeURIComponent(name)}/alert`, { method: "PUT", body: JSON.stringify({ enabled }) }),
  testNotification: (channel: "webhook" | "email") => request<{ success: boolean }>("/notifications/test", { method: "POST", body: JSON.stringify({ channel }) }),
  notificationProblems: (includeResolved = false) => request<NotificationProblem[]>(`/notifications/problems?include_resolved=${includeResolved}`),
  carpEvents: (clusterName?: string) => request<CarpEvent[]>(`/cluster/carp-events${clusterName ? `?cluster_name=${encodeURIComponent(clusterName)}` : ""}`),
  changePassword: (current_password:string,new_password:string) => request<{ok:boolean}>("/auth/change-password",{method:"POST",body:JSON.stringify({current_password,new_password})}),
  hostMaintenance: (id:string) => request<MaintenanceRead>(`/hosts/${id}/maintenance`),
  previewMaintenance: (id:string, action:"reboot"|"update"|"major_update"|"firmware_rollback") => request<MaintenancePreview>(`/hosts/${id}/maintenance/preview`,{method:"POST",body:JSON.stringify({action})}),
  executeMaintenance: (id:string, preview_id:string, confirm_name:string, acknowledge_downtime:boolean) => request<HostOperation>(`/hosts/${id}/maintenance/execute`,{method:"POST",body:JSON.stringify({preview_id,confirm_name,acknowledge_downtime})}),
  acknowledgeMaintenance: (id:string, operationId:string, confirm_name:string) => request<HostOperation>(`/hosts/${id}/maintenance/${operationId}/acknowledge`,{method:"POST",body:JSON.stringify({confirm_name})}),
  cancelMaintenance: (id:string, operationId:string, confirm_name:string, reason:string) => request<HostOperation>(`/hosts/${id}/maintenance/${operationId}/cancel`,{method:"POST",body:JSON.stringify({confirm_name,reason})}),
  checkHostUpdates: (id:string) => request<unknown>(`/hosts/${id}/maintenance/check-updates`,{method:"POST"}),
  hostUpdateProgress: (id:string) => request<{status:string;log?:string}>(`/hosts/${id}/maintenance/progress`),
  previewCarpSwitch: (id:string) => request<CarpSwitchPreview>(`/hosts/${id}/maintenance/carp-preview`,{method:"POST"}),
  executeCarpSwitch: (id:string, preview_id:string, confirm_name:string, acknowledge_downtime:boolean, auto_revert_minutes?: number | null) => request<HostOperation>(`/hosts/${id}/maintenance/carp-execute`,{method:"POST",body:JSON.stringify({preview_id,confirm_name,acknowledge_downtime,auto_revert_minutes})}),
  listResourceSamples: (id:string, sinceHours = 24) => request<HostResourceSample[]>(`/hosts/${id}/resource-samples?since_hours=${sinceHours}`),
  listServices: (id:string) => request<{rows:OpnsenseService[]}>(`/hosts/${id}/services`),
  startService: (id:string, name:string) => request<{result:string}>(`/hosts/${id}/services/${encodeURIComponent(name)}/start`,{method:"POST"}),
  stopService: (id:string, name:string) => request<{result:string}>(`/hosts/${id}/services/${encodeURIComponent(name)}/stop`,{method:"POST"}),
  restartService: (id:string, name:string) => request<{result:string}>(`/hosts/${id}/services/${encodeURIComponent(name)}/restart`,{method:"POST"}),
  listScheduledMaintenance: (id:string) => request<ScheduledMaintenanceItem[]>(`/hosts/${id}/maintenance/schedule`),
  createScheduledMaintenance: (id:string, action:"reboot"|"update"|"major_update", run_at:string, carp_handover=false, auto_verify=false) => request<ScheduledMaintenanceItem>(`/hosts/${id}/maintenance/schedule`,{method:"POST",body:JSON.stringify({action,run_at,acknowledge_unattended:true,carp_handover,auto_verify})}),
  cancelScheduledMaintenance: (id:string, scheduleId:string) => request<ScheduledMaintenanceItem>(`/hosts/${id}/maintenance/schedule/${scheduleId}`,{method:"DELETE"}),
  // Maintenance windows and firmware rollouts (1.25)
  listMaintenanceWindows: () => request<MaintenanceWindow[]>("/maintenance-windows"),
  createMaintenanceWindow: (data: MaintenanceWindowWrite) => request<MaintenanceWindow>("/maintenance-windows", { method: "POST", body: JSON.stringify(data) }),
  updateMaintenanceWindow: (id: string, data: MaintenanceWindowWrite) => request<MaintenanceWindow>(`/maintenance-windows/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteMaintenanceWindow: (id: string) => request<void>(`/maintenance-windows/${id}`, { method: "DELETE" }),
  addWindowException: (id: string, data: { occurrence_date: string; kind: "skip" | "move"; moved_start?: string; reason?: string }) => request<MaintenanceWindow>(`/maintenance-windows/${id}/exceptions`, { method: "POST", body: JSON.stringify(data) }),
  deleteWindowException: (id: string, exceptionId: string) => request<MaintenanceWindow>(`/maintenance-windows/${id}/exceptions/${exceptionId}`, { method: "DELETE" }),
  listRolloutPlans: () => request<RolloutPlan[]>("/rollouts/plans"),
  getRolloutPlan: (id: string) => request<RolloutPlan>(`/rollouts/plans/${id}`),
  createRolloutPlan: (data: RolloutPlanWrite) => request<RolloutPlan>("/rollouts/plans", { method: "POST", body: JSON.stringify(data) }),
  updateRolloutPlan: (id: string, data: RolloutPlanWrite) => request<RolloutPlan>(`/rollouts/plans/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteRolloutPlan: (id: string) => request<void>(`/rollouts/plans/${id}`, { method: "DELETE" }),
  previewRolloutPlan: (id: string) => request<RolloutPreview>(`/rollouts/plans/${id}/preview`, { method: "POST" }),
  activateRolloutPlan: (id: string, preview_token: string) => request<RolloutPlan>(`/rollouts/plans/${id}/activate`, { method: "POST", body: JSON.stringify({ preview_token }) }),
  deactivateRolloutPlan: (id: string) => request<RolloutPlan>(`/rollouts/plans/${id}/deactivate`, { method: "POST" }),
  startRolloutPlan: (id: string, preview_token: string, confirm_name: string) => request<RolloutRunDetail>(`/rollouts/plans/${id}/start`, { method: "POST", body: JSON.stringify({ preview_token, confirm_name, acknowledge_unattended: true }) }),
  listRolloutRuns: () => request<RolloutRunSummary[]>("/rollouts/runs"),
  rolloutStatus: () => request<RolloutStatus>("/rollouts/status"),
  getRolloutRun: (id: string) => request<RolloutRunDetail>(`/rollouts/runs/${id}`),
  pauseRolloutRun: (id: string, reason: string) => request<RolloutRunDetail>(`/rollouts/runs/${id}/pause`, { method: "POST", body: JSON.stringify({ reason }) }),
  resumeRolloutRun: (id: string) => request<RolloutRunDetail>(`/rollouts/runs/${id}/resume`, { method: "POST" }),
  approveRolloutWave: (id: string) => request<RolloutRunDetail>(`/rollouts/runs/${id}/approve`, { method: "POST" }),
  skipRolloutHost: (id: string, host_id: string, reason: string) => request<RolloutRunDetail>(`/rollouts/runs/${id}/skip-host`, { method: "POST", body: JSON.stringify({ host_id, reason }) }),
  abortRolloutRun: (id: string, reason: string, confirm_name: string) => request<RolloutRunDetail>(`/rollouts/runs/${id}/abort`, { method: "POST", body: JSON.stringify({ reason, confirm_name }) }),
  audit: (filters: Record<string,string | number> = {}) => request<AuditPage>(`/audit?${new URLSearchParams(Object.entries(filters).filter(([,v])=>v!=="").map(([k,v])=>[k,String(v)]))}`),
  dashboard: () => request<DashboardSummary>("/dashboard"),
  versionCheck: () => request<VersionCheck>("/system/version-check"),
  updateCheck: () => request<UpdateCheckSettings>("/system/update-check"),
  updateUpdateCheck: (body: { enabled?: boolean; feed_url?: string }) => request<UpdateCheckSettings>("/system/update-check", { method: "PUT", body: JSON.stringify(body) }),
  updaterStatus: () => request<UpdaterStatus>("/system/updater"),
  updaterUpdate: (version: string) => request<{ job: UpdaterJob }>("/system/updater/update", { method: "POST", body: JSON.stringify({ version }) }),
  updaterRollback: () => request<{ job: UpdaterJob }>("/system/updater/rollback", { method: "POST" }),
  getOutboundMode: (id: string) => request<OutboundMode>(`/outbound-nat/hosts/${id}/mode`),
  listPortForwards: () => request<PortForward[]>(`/port-forward`),
  createPortForward: (data: PortForwardCreate) => request<PortForward>(`/port-forward`, { method: "POST", body: JSON.stringify(data) }),
  updatePortForward: (id: string, data: PortForwardCreate, expectedUpdatedAt: string) => request<PortForward>(`/port-forward/${id}`, { method: "PUT", body: JSON.stringify({...data, expected_updated_at: expectedUpdatedAt}) }),
  previewPortForward: (id: string) => request<NatPreview>(`/port-forward/${id}/preview`),
  portForwardRevisions: (id: string) => request<PortForwardRevision[]>(`/port-forward/${id}/revisions`),
  deployPortForward: (id: string, previewToken: string) => request<PortForward>(`/port-forward/${id}/deploy`, { method: "POST", body: JSON.stringify({preview_token: previewToken}) }),
  confirmPortForward: (id: string) => request<PortForward>(`/port-forward/${id}/confirm`, { method: "POST" }),
  deletePortForward: (id: string) => request<void>(`/port-forward/${id}`, { method: "DELETE" }),
  removePortForwardPreviousTarget: (id: string, hostId: string, token: string) => request<void>(`/port-forward/${id}/deployments/${hostId}/remove`, { method: "POST", body: JSON.stringify({preview_token: token}) }),
  retryPortForwardRollback: (id: string, hostId: string, token: string) => request<void>(`/port-forward/${id}/deployments/${hostId}/retry-rollback`, { method: "POST", body: JSON.stringify({preview_token: token}) }),
  getLivePortForward: (id: string) => request<NatLive>(`/port-forward/hosts/${id}/live`),
  listOutboundNats: () => request<OutboundNat[]>(`/outbound-nat`),
  createOutboundNat: (data: OutboundNatCreate) => request<OutboundNat>(`/outbound-nat`, { method: "POST", body: JSON.stringify(data) }),
  updateOutboundNat: (id: string, data: OutboundNatCreate, expectedUpdatedAt: string) => request<OutboundNat>(`/outbound-nat/${id}`, { method: "PUT", body: JSON.stringify({...data, expected_updated_at: expectedUpdatedAt}) }),
  previewOutboundNat: (id: string) => request<NatPreview>(`/outbound-nat/${id}/preview`),
  outboundNatRevisions: (id: string) => request<OutboundNatRevision[]>(`/outbound-nat/${id}/revisions`),
  deployOutboundNat: (id: string, previewToken: string) => request<OutboundNat>(`/outbound-nat/${id}/deploy`, { method: "POST", body: JSON.stringify({preview_token: previewToken}) }),
  confirmOutboundNat: (id: string) => request<OutboundNat>(`/outbound-nat/${id}/confirm`, { method: "POST" }),
  deleteOutboundNat: (id: string) => request<void>(`/outbound-nat/${id}`, { method: "DELETE" }),
  removeOutboundNatPreviousTarget: (id: string, hostId: string, token: string) => request<void>(`/outbound-nat/${id}/deployments/${hostId}/remove`, { method: "POST", body: JSON.stringify({preview_token: token}) }),
  retryOutboundNatRollback: (id: string, hostId: string, token: string) => request<void>(`/outbound-nat/${id}/deployments/${hostId}/retry-rollback`, { method: "POST", body: JSON.stringify({preview_token: token}) }),
  getLiveOutboundNat: (id: string) => request<NatLive>(`/outbound-nat/hosts/${id}/live`),
  listOneToOnes: () => request<OneToOne[]>(`/one-to-one`),
  createOneToOne: (data: OneToOneCreate) => request<OneToOne>(`/one-to-one`, { method: "POST", body: JSON.stringify(data) }),
  updateOneToOne: (id: string, data: OneToOneCreate, expectedUpdatedAt: string) => request<OneToOne>(`/one-to-one/${id}`, { method: "PUT", body: JSON.stringify({...data, expected_updated_at: expectedUpdatedAt}) }),
  previewOneToOne: (id: string) => request<NatPreview>(`/one-to-one/${id}/preview`),
  oneToOneRevisions: (id: string) => request<OneToOneRevision[]>(`/one-to-one/${id}/revisions`),
  deployOneToOne: (id: string, previewToken: string) => request<OneToOne>(`/one-to-one/${id}/deploy`, { method: "POST", body: JSON.stringify({preview_token: previewToken}) }),
  confirmOneToOne: (id: string) => request<OneToOne>(`/one-to-one/${id}/confirm`, { method: "POST" }),
  deleteOneToOne: (id: string) => request<void>(`/one-to-one/${id}`, { method: "DELETE" }),
  removeOneToOnePreviousTarget: (id: string, hostId: string, token: string) => request<void>(`/one-to-one/${id}/deployments/${hostId}/remove`, { method: "POST", body: JSON.stringify({preview_token: token}) }),
  retryOneToOneRollback: (id: string, hostId: string, token: string) => request<void>(`/one-to-one/${id}/deployments/${hostId}/retry-rollback`, { method: "POST", body: JSON.stringify({preview_token: token}) }),
  getLiveOneToOne: (id: string) => request<NatLive>(`/one-to-one/hosts/${id}/live`),

  listHosts: () => request<Host[]>("/hosts"),
  getHost: (id: string) => request<Host>(`/hosts/${id}`),
  createHost: (data: HostCreate) => request<Host>("/hosts", { method: "POST", body: JSON.stringify(data) }),
  updateHost: (id: string, data: HostUpdate) =>
    request<Host>(`/hosts/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  // The server wants the host's own name too (T4): deleting drops its stored credentials at once.
  deleteHost: (id: string, confirmName: string) => request<void>(`/hosts/${id}?confirm_name=${encodeURIComponent(confirmName)}`, { method: "DELETE" }),
  getLiveRules: (id: string) => request<LiveRule[]>(`/hosts/${id}/live-rules`),
  getHostNetwork: (id: string) => request<HostNetwork>(`/hosts/${id}/network`),
  runPing: (id: string, data: { target: string; ipproto: string; count: number; packetsize?: number | null; source?: string }) =>
    request<PingResult>(`/hosts/${id}/diagnostics/ping`, { method: "POST", body: JSON.stringify(data) }),
  runTraceroute: (id: string, data: { target: string; ipproto: string; protocol: string; source?: string }) =>
    request<TracerouteResult>(`/hosts/${id}/diagnostics/traceroute`, { method: "POST", body: JSON.stringify(data) }),
  runPortprobe: (id: string, data: { target: string; port: number; ipproto: string; source?: string }) =>
    request<PortprobeResult>(`/hosts/${id}/diagnostics/portprobe`, { method: "POST", body: JSON.stringify(data) }),
  runDnsLookup: (id: string, data: { name: string; server?: string }) =>
    request<DnsLookupResult>(`/hosts/${id}/diagnostics/dns`, { method: "POST", body: JSON.stringify(data) }),
  listNetworkObjects: (id: string, kind: NetworkKind) => request<NetworkObjects>(`/hosts/${id}/network/objects/${kind}`),
  getNetworkDrift: () => request<{ clusters: ClusterNetworkDrift[] }>("/network/drift"),
  listSnapshots: (id: string) => request<{ supported: boolean; rows: HostSnapshot[] }>(`/hosts/${id}/snapshots`),
  createSnapshot: (id: string, name: string, note: string) =>
    request<HostSnapshot>(`/hosts/${id}/snapshots`, { method: "POST", body: JSON.stringify({ name, note }) }),
  deleteSnapshot: (id: string, uuid: string) => request<void>(`/hosts/${id}/snapshots/${uuid}`, { method: "DELETE" }),
  getNetworkItem: (id: string, kind: NetworkKind, uuid: string) => request<Record<string, string>>(`/hosts/${id}/network/item/${kind}/${uuid}`),
  getNetworkTemplate: (id: string, kind: NetworkKind) => request<NetworkTemplate>(`/hosts/${id}/network/template/${kind}`),
  previewNetworkChange: (id: string, data: NetworkChangeRequest) =>
    request<NetworkChangePreview>(`/hosts/${id}/network/changes/preview`, { method: "POST", body: JSON.stringify(data) }),
  executeNetworkChange: (id: string, data: NetworkChangeRequest, previewToken: string, acknowledge: boolean) =>
    request<NetworkChangeResult>(`/hosts/${id}/network/changes`, {
      method: "POST",
      body: JSON.stringify({ ...data, preview_token: previewToken, acknowledge }),
    }),
  getHostPortForwards: (id: string) => request<NatLiveSplit>(`/port-forward/hosts/${id}/live`),
  getHostOneToOne: (id: string) => request<NatLiveSplit>(`/one-to-one/hosts/${id}/live`),
  getHostOutboundNat: (id: string) => request<NatLiveSplit>(`/outbound-nat/hosts/${id}/live`),
  getHostCertificates: (id: string) => request<HostCertificate[]>(`/hosts/${id}/certificates`),
  getHostCas: (id: string) => request<HostCa[]>(`/hosts/${id}/cas`),
  listHostBackups: (id: string) => request<HostBackup[]>(`/hosts/${id}/backups`),
  triggerHostBackup: (id: string) => request<HostBackup>(`/hosts/${id}/backups`, { method: "POST" }),
  getBackupDiff: (id: string, a?: string, b?: string) => {
    const query = new URLSearchParams();
    if (a) query.set("a", a);
    if (b) query.set("b", b);
    return request<BackupDiff>(`/hosts/${id}/backup-diff${query.size ? `?${query}` : ""}`);
  },
  getBackupSettings: () => request<BackupSettings>("/settings/backups"),
  updateBackupSettings: (data: BackupSettingsUpdate) => request<BackupSettings>("/settings/backups", { method: "PUT", body: JSON.stringify(data) }),
  testBackupOffsite: () => request<{ success: boolean }>("/settings/backups/test-offsite", { method: "POST" }),
  runBackupsNow: () => request<{ hosts: number; taken: number; failed: number; pruned: number; uploaded: number }>("/backups/run", { method: "POST" }),
  downloadHostBackup: (id: string, backupId: string) => requestBlob(`/hosts/${id}/backups/${backupId}`),
  // Configuration as code (1.32)
  exportConfig: () => requestBlob("/config/export"),
  planConfig: (yaml: string, prune: boolean) => request<ConfigPlan>("/config/import/plan", { method: "POST", body: JSON.stringify({ yaml, prune }) }),
  applyConfig: (yaml: string, prune: boolean, plan_token: string) =>
    request<{ summary: ConfigPlan["summary"]; sha256: string; kinds: string[] }>("/config/import/apply", { method: "POST", body: JSON.stringify({ yaml, prune, plan_token }) }),

  previewRestore: (id: string, backupId: string) =>
    request<RestoreOperation>(`/hosts/${id}/restore/preview`, { method: "POST", body: JSON.stringify({ backup_id: backupId }) }),
  getRestoreOperation: (id: string, operationId: string) => request<RestoreOperation>(`/hosts/${id}/restore/${operationId}`),
  listRestoreOperations: (id: string) => request<RestoreOperation[]>(`/hosts/${id}/restore`),
  handoffRestore: (id: string, operationId: string, confirmName: string) =>
    request<RestoreOperation>(`/hosts/${id}/restore/${operationId}/handoff`, { method: "POST", body: JSON.stringify({ confirm_name: confirmName }) }),
  confirmRestore: (id: string, operationId: string, confirmName: string, message?: string) =>
    request<RestoreOperation>(`/hosts/${id}/restore/${operationId}/confirm`, { method: "POST", body: JSON.stringify({ confirm_name: confirmName, message }) }),
  cancelRestore: (id: string, operationId: string) =>
    request<RestoreOperation>(`/hosts/${id}/restore/${operationId}/cancel`, { method: "POST" }),
  getFirmwareInfo: (id: string) => request<FirmwareInfo>(`/hosts/${id}/firmware-info`),
  getHostInterfaces: (id: string) => request<InterfaceOption[]>(`/hosts/${id}/interfaces`),
  getHostProtocols: (id: string) => request<InterfaceOption[]>(`/hosts/${id}/protocols`),
  getHostAliases: (id: string) => request<AliasOption[]>(`/hosts/${id}/aliases`),

  listRules: () => request<Rule[]>("/rules"),
  createRule: (data: RuleCreate) => request<Rule>("/rules", { method: "POST", body: JSON.stringify(data) }),
  listUnmanagedRules: (hostId: string) => request<UnmanagedRule[]>(`/rules/unmanaged?host_id=${hostId}`),
  listUnmanagedObjects: (kind: AdoptKind, hostId: string) => request<UnmanagedObject[]>(`/${kind}/unmanaged?host_id=${hostId}`),
  adoptObject: (kind: AdoptKind, hostId: string, uuid: string, linkTo?: string) => request<{ id: string }>(`/${kind}/adopt`, { method: "POST", body: JSON.stringify({ host_id: hostId, uuid, link_to: linkTo ?? null }) }),
  adoptAllObjects: (kind: AdoptKind, hostId: string, linkExisting = true) => request<AdoptAllResult>(`/${kind}/adopt-all`, { method: "POST", body: JSON.stringify({ host_id: hostId, link_existing: linkExisting }) }),
  adoptRule: (hostId: string, ruleUuid: string, linkTo?: string) => request<Rule>("/rules/adopt", { method: "POST", body: JSON.stringify({ host_id: hostId, rule_uuid: ruleUuid, link_to: linkTo ?? null }) }),
  updateRule: (id: string, data: RuleCreate, expectedUpdatedAt: string) => request<Rule>(`/rules/${id}`, { method: "PUT", body: JSON.stringify({ ...data, expected_updated_at: expectedUpdatedAt }) }),
  previewRule: (id: string) => request<RulePreview>(`/rules/${id}/preview`),
  ruleRevisions: (id: string) => request<RuleRevision[]>(`/rules/${id}/revisions`),
  deployRule: (id: string, previewToken: string) => request<Rule>(`/rules/${id}/deploy`, { method: "POST", body: JSON.stringify({ preview_token: previewToken }) }),
  listAliases: () => request<PolicyAlias[]>("/aliases"),
  createAlias: (data: PolicyAliasCreate) => request<PolicyAlias>("/aliases", { method: "POST", body: JSON.stringify(data) }),
  updateAlias: (id: string, data: PolicyAliasCreate, expectedUpdatedAt: string) => request<PolicyAlias>(`/aliases/${id}`, { method: "PUT", body: JSON.stringify({ ...data, expected_updated_at: expectedUpdatedAt }) }),
  previewAlias: (id: string) => request<RulePreview>(`/aliases/${id}/preview`),
  aliasRevisions: (id: string) => request<PolicyAliasRevision[]>(`/aliases/${id}/revisions`),
  deployAlias: (id: string, previewToken?: string) => request<PolicyAlias>(`/aliases/${id}/deploy`, { method: "POST", body: previewToken ? JSON.stringify({ preview_token: previewToken }) : undefined }),
  confirmAlias: (id: string) => request<PolicyAlias>(`/aliases/${id}/confirm`, { method: "POST" }),
  deleteAlias: (id: string) => request<void>(`/aliases/${id}`, { method: "DELETE" }),
  removeAliasPreviousTarget: (id: string, hostId: string, token: string) => request<void>(`/aliases/${id}/deployments/${hostId}/remove`, { method: "POST", body: JSON.stringify({ preview_token: token }) }),
  retryAliasRollback: (id: string, hostId: string, token: string) => request<void>(`/aliases/${id}/deployments/${hostId}/retry-rollback`, { method: "POST", body: JSON.stringify({ preview_token: token }) }),

  listGroups: () => request<DeviceGroup[]>("/groups"),
  createGroup: (data: GroupWrite) => request<DeviceGroup>("/groups", { method: "POST", body: JSON.stringify(data) }),
  previewGroup: (id: string, data: GroupWrite) => request<GroupImpact>(`/groups/${id}/preview`, { method: "POST", body: JSON.stringify(data) }),
  updateGroup: (id: string, data: GroupWrite, token: string) => request<DeviceGroup>(`/groups/${id}`, { method: "PUT", body: JSON.stringify({ ...data, preview_token: token }) }),
  removePreviousTarget: (id: string, hostId: string, token: string) => request<void>(`/rules/${id}/deployments/${hostId}/remove`, { method: "POST", body: JSON.stringify({ preview_token: token }) }),
  retryRollback: (id: string, hostId: string, token: string) => request<void>(`/rules/${id}/deployments/${hostId}/retry-rollback`, { method: "POST", body: JSON.stringify({ preview_token: token }) }),
  deleteGroup: (id: string) => request<void>(`/groups/${id}`, { method: "DELETE" }),
  confirmRule: (id: string) => request<Rule>(`/rules/${id}/confirm`, { method: "POST" }),
  deleteRule: (id: string) => request<void>(`/rules/${id}`, { method: "DELETE" }),

  searchLogs: (params: LogSearchParams = {}) =>
    request<LogSearchResult>(
      `/logs?${new URLSearchParams(
        Object.entries(params).filter(([, v]) => v !== undefined && v !== "").map(([k, v]) => [k, String(v)])
      ).toString()}`
    ),
  getLogStats: (params: Pick<LogSearchParams, "host_id" | "cluster_name" | "since" | "until"> = {}) =>
    request<LogStats>(
      `/logs/stats?${new URLSearchParams(
        Object.entries(params).filter(([, v]) => v !== undefined && v !== "").map(([k, v]) => [k, String(v)])
      ).toString()}`
    ),
  getLogFilterOptions: () => request<LogFilterOptions>("/logs/filter-options"),

  me: () => request<AuthState>("/auth/me"),
  login: (username: string, password: string, totp_code?: string) =>
    request<LoginResult>("/auth/login", { method: "POST", body: JSON.stringify({ username, password, ...(totp_code ? { totp_code } : {}) }) }),
  twoFactorStatus: () => request<{ enabled: boolean; recovery_codes_left: number }>("/auth/2fa"),
  twoFactorSetup: (password: string) =>
    request<{ secret: string; otpauth_uri: string }>("/auth/2fa/setup", { method: "POST", body: JSON.stringify({ password }) }),
  twoFactorEnable: (code: string) =>
    request<{ enabled: boolean; recovery_codes: string[] }>("/auth/2fa/enable", { method: "POST", body: JSON.stringify({ code }) }),
  twoFactorDisable: (password: string, code: string) =>
    request<{ enabled: boolean }>("/auth/2fa/disable", { method: "POST", body: JSON.stringify({ password, code }) }),
  resetUserTwoFactor: (id: string) => request<User>(`/users/${id}/reset-2fa`, { method: "POST" }),
  tlsProbe: (base_url: string) => request<TlsProbe>("/hosts/tls-probe", { method: "POST", body: JSON.stringify({ base_url }) }),
  logout: () => request<AuthState>("/auth/logout", { method: "POST" }),

  listUsers: () => request<User[]>("/users"),
  createUser: (data: { username: string; password: string; role: UserRole }) =>
    request<User>("/users", { method: "POST", body: JSON.stringify(data) }),
  updateUserRole: (id: string, role: UserRole) =>
    request<User>(`/users/${id}/role`, { method: "PATCH", body: JSON.stringify({ role }) }),
  resetUserPassword: (id: string, new_password: string) =>
    request<User>(`/users/${id}/reset-password`, { method: "POST", body: JSON.stringify({ new_password }) }),
  deleteUser: (id: string) => request<void>(`/users/${id}`, { method: "DELETE" }),

  listCompliance: () => request<ComplianceHost[]>("/compliance"),
  getHostCompliance: (id: string) => request<ComplianceHost>(`/hosts/${id}/compliance`),
  analyzeRule: (query: AnalysisQuery) => {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) if (value !== undefined && value !== "" && value !== false) params.set(key, String(value));
    return request<AnalysisResult[]>(`/rule-analysis/match?${params.toString()}`);
  },
  getRuleHits: (id: string) => request<RuleHit[]>(`/hosts/${id}/rule-hits`),
  getRuleCleanup: (id: string, unusedDays: number, refresh = false) =>
    request<RuleCleanupResult>(`/hosts/${id}/rule-analysis/cleanup?unused_days=${unusedDays}${refresh ? "&refresh=true" : ""}`),
  listCleanupOperations: (id: string) => request<CleanupOperation[]>(`/hosts/${id}/rule-cleanup`),
  previewCleanup: (id: string, body: CleanupPreviewRequest) =>
    request<CleanupPreview>(`/hosts/${id}/rule-cleanup/preview`, { method: "POST", body: JSON.stringify(body) }),
  cancelCleanup: (id: string, op: string) => request<CleanupOperation>(`/hosts/${id}/rule-cleanup/${op}/cancel`, { method: "POST" }),
  deployCleanup: (id: string, op: string, confirmName: string) =>
    request<CleanupOperation>(`/hosts/${id}/rule-cleanup/${op}/deploy`, { method: "POST", body: JSON.stringify({ confirm_name: confirmName }) }),
  confirmCleanup: (id: string, op: string) => request<CleanupOperation>(`/hosts/${id}/rule-cleanup/${op}/confirm`, { method: "POST" }),
  revertCleanup: (id: string, op: string) => request<CleanupOperation>(`/hosts/${id}/rule-cleanup/${op}/revert`, { method: "POST" }),
  retryCleanup: (id: string, op: string) => request<CleanupOperation>(`/hosts/${id}/rule-cleanup/${op}/retry`, { method: "POST" }),
  restoreCleanupItem: (id: string, item: string, confirmName: string) =>
    request<CleanupOperation>(`/hosts/${id}/rule-cleanup/items/${item}/restore`, { method: "POST", body: JSON.stringify({ confirm_name: confirmName }) }),
  getRuleHistory: (id: string, ruleUuid: string) =>
    request<RuleHistory>(`/hosts/${id}/live-rules/${encodeURIComponent(ruleUuid)}/history`),
  checkHostCompliance: (id: string) => request<ComplianceHost>(`/hosts/${id}/compliance/check`, { method: "POST" }),
  checkFleetCompliance: () => request<ComplianceHost[]>("/compliance/check", { method: "POST" }),

  oidcConfig: () => request<{ enabled: boolean; name: string | null }>("/auth/oidc/config"),
  getOidcSettings: () => request<OidcSettings>("/settings/oidc"),
  updateOidcSettings: (data: OidcSettingsUpdate) => request<OidcSettings>("/settings/oidc", { method: "PUT", body: JSON.stringify(data) }),
  testOidc: () => request<{ success: boolean; authorization_endpoint: string }>("/settings/oidc/test", { method: "POST" }),
  getGovernance: () => request<GovernanceSettings>("/settings/governance"),
  updateGovernance: (operators_may_deploy: boolean) => request<GovernanceSettings>("/settings/governance", { method: "PUT", body: JSON.stringify({ operators_may_deploy }) }),
  updateCleanupSettings: (data: CleanupSettingsUpdate) => request<GovernanceSettings>("/settings/governance", { method: "PUT", body: JSON.stringify(data) }),

  getOverlay: () => request<{ settings: OverlaySettings; peers: OverlayPeer[] }>("/overlay"),
  updateOverlay: (data: Partial<Pick<OverlaySettings, "enabled" | "endpoint" | "subnet" | "listen_port">>) =>
    request<OverlaySettings>("/overlay/settings", { method: "PUT", body: JSON.stringify(data) }),
  createOverlayPeer: (name: string) =>
    request<{ id: string; name: string; ip: string; instructions: OverlayInstructions }>("/overlay/peers", { method: "POST", body: JSON.stringify({ name }) }),
  overlayInstructions: (id: string) => request<OverlayInstructions>(`/overlay/peers/${id}/instructions`),
  updateOverlayPeer: (id: string, data: { public_key?: string; host_id?: string; clear_host?: boolean }) =>
    request<{ ok: boolean }>(`/overlay/peers/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  deleteOverlayPeer: (id: string) => request<void>(`/overlay/peers/${id}`, { method: "DELETE" }),

  getHostVariables: (id: string) => request<{ variables: HostVariable[] }>(`/hosts/${id}/variables`),
  setHostVariables: (id: string, variables: Record<string, string>) =>
    request<{ variables: HostVariable[] }>(`/hosts/${id}/variables`, { method: "PUT", body: JSON.stringify({ variables }) }),

  listApiTokens: () => request<ApiToken[]>("/tokens"),
  createApiToken: (data: { name: string; scope: TokenScope; expires_in_days: number | null; current_password: string }) =>
    request<ApiToken & { token: string }>("/tokens", { method: "POST", body: JSON.stringify(data) }),
  revokeApiToken: (id: string) => request<void>(`/tokens/${id}`, { method: "DELETE" }),

  listPolicyHierarchyGroups: () => request<PolicyHierarchyGroup[]>("/policy-hierarchy/groups"),
  createPolicyHierarchyGroup: (data: PolicyHierarchyGroupWrite) => request<PolicyHierarchyGroup>("/policy-hierarchy/groups", { method: "POST", body: JSON.stringify(data) }),
  updatePolicyHierarchyGroup: (id: string, data: PolicyHierarchyGroupWrite) => request<PolicyHierarchyGroup>(`/policy-hierarchy/groups/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  deletePolicyHierarchyGroup: (id: string) => request<void>(`/policy-hierarchy/groups/${id}`, { method: "DELETE" }),
  reassertHierarchyOrdering: (groupId: string) => request<{ status: string }>(`/policy-hierarchy/groups/${groupId}/reassert-ordering`, { method: "POST" }),

  listPolicyHierarchyRules: () => request<PolicyHierarchyRule[]>("/policy-hierarchy/rules"),
  createPolicyHierarchyRule: (data: PolicyHierarchyRuleCreate) => request<PolicyHierarchyRule>("/policy-hierarchy/rules", { method: "POST", body: JSON.stringify(data) }),
  updatePolicyHierarchyRule: (id: string, data: PolicyHierarchyRuleCreate, expectedUpdatedAt: string) => request<PolicyHierarchyRule>(`/policy-hierarchy/rules/${id}`, { method: "PUT", body: JSON.stringify({ ...data, expected_updated_at: expectedUpdatedAt }) }),
  movePolicyHierarchyRule: (id: string, direction: "up" | "down") => request<PolicyHierarchyRule>(`/policy-hierarchy/rules/${id}/move`, { method: "POST", body: JSON.stringify({ direction }) }),
  reorderPolicyHierarchyRules: (groupId: string, rulebase: PolicyHierarchyRulebase, rules: PolicyHierarchyRule[]) =>
    request<PolicyHierarchyRule[]>(`/policy-hierarchy/groups/${groupId}/rules/${rulebase}/order`, {
      method: "PUT",
      body: JSON.stringify({ items: rules.map((rule) => ({ id: rule.id, expected_updated_at: rule.updated_at })) }),
    }),
  previewPolicyHierarchyRule: (id: string) => request<RulePreview>(`/policy-hierarchy/rules/${id}/preview`),
  policyHierarchyRuleRevisions: (id: string) => request<PolicyHierarchyRuleRevision[]>(`/policy-hierarchy/rules/${id}/revisions`),
  deployPolicyHierarchyRule: (id: string, previewToken: string) => request<PolicyHierarchyRule>(`/policy-hierarchy/rules/${id}/deploy`, { method: "POST", body: JSON.stringify({ preview_token: previewToken }) }),
  confirmPolicyHierarchyRule: (id: string) => request<PolicyHierarchyRule>(`/policy-hierarchy/rules/${id}/confirm`, { method: "POST" }),
  deletePolicyHierarchyRule: (id: string) => request<void>(`/policy-hierarchy/rules/${id}`, { method: "DELETE" }),
  removePolicyHierarchyPreviousTarget: (id: string, hostId: string, token: string) => request<void>(`/policy-hierarchy/rules/${id}/deployments/${hostId}/remove`, { method: "POST", body: JSON.stringify({ preview_token: token }) }),
  retryPolicyHierarchyRollback: (id: string, hostId: string, token: string) => request<void>(`/policy-hierarchy/rules/${id}/deployments/${hostId}/retry-rollback`, { method: "POST", body: JSON.stringify({ preview_token: token }) }),

  listIpsecTunnels: () => request<IpsecTunnel[]>("/ipsec/tunnels"),
  createIpsecTunnel: (data: IpsecTunnelCreate) => request<IpsecTunnel>("/ipsec/tunnels", { method: "POST", body: JSON.stringify(data) }),
  updateIpsecTunnel: (id: string, data: Omit<IpsecTunnelCreate, "pre_shared_key"> & { pre_shared_key?: string }, expectedUpdatedAt: string) =>
    request<IpsecTunnel>(`/ipsec/tunnels/${id}`, { method: "PUT", body: JSON.stringify({ ...data, expected_updated_at: expectedUpdatedAt }) }),
  previewIpsecTunnel: (id: string) => request<IpsecPreview>(`/ipsec/tunnels/${id}/preview`),
  ipsecTunnelRevisions: (id: string) => request<IpsecTunnelRevision[]>(`/ipsec/tunnels/${id}/revisions`),
  deployIpsecTunnel: (id: string, previewToken: string) => request<IpsecTunnel>(`/ipsec/tunnels/${id}/deploy`, { method: "POST", body: JSON.stringify({ preview_token: previewToken }) }),
  deleteIpsecTunnel: (id: string) => request<void>(`/ipsec/tunnels/${id}`, { method: "DELETE" }),
  removeIpsecTunnelPreviousTarget: (id: string, hostId: string, token: string) => request<void>(`/ipsec/tunnels/${id}/deployments/${hostId}/remove`, { method: "POST", body: JSON.stringify({ preview_token: token }) }),

  listOpenVpnInstances: () => request<OpenVpnInstance[]>("/openvpn/instances"),
  createOpenVpnInstance: (data: OpenVpnInstanceCreate) => request<OpenVpnInstance>("/openvpn/instances", { method: "POST", body: JSON.stringify(data) }),
  updateOpenVpnInstance: (id: string, data: Omit<OpenVpnInstanceCreate, "tls_key_material"> & { tls_key_material?: string | null }, expectedUpdatedAt: string) =>
    request<OpenVpnInstance>(`/openvpn/instances/${id}`, { method: "PUT", body: JSON.stringify({ ...data, expected_updated_at: expectedUpdatedAt }) }),
  previewOpenVpnInstance: (id: string) => request<OpenVpnPreview>(`/openvpn/instances/${id}/preview`),
  openVpnInstanceRevisions: (id: string) => request<OpenVpnInstanceRevision[]>(`/openvpn/instances/${id}/revisions`),
  deployOpenVpnInstance: (id: string, previewToken: string) => request<OpenVpnInstance>(`/openvpn/instances/${id}/deploy`, { method: "POST", body: JSON.stringify({ preview_token: previewToken }) }),
  deleteOpenVpnInstance: (id: string) => request<void>(`/openvpn/instances/${id}`, { method: "DELETE" }),
  removeOpenVpnInstancePreviousTarget: (id: string, hostId: string, token: string) => request<void>(`/openvpn/instances/${id}/deployments/${hostId}/remove`, { method: "POST", body: JSON.stringify({ preview_token: token }) }),

  listWireguardTunnels: () => request<WireguardTunnel[]>("/wireguard/tunnels"),
  createWireguardTunnel: (data: WireguardTunnelCreate) => request<WireguardTunnel>("/wireguard/tunnels", { method: "POST", body: JSON.stringify(data) }),
  updateWireguardTunnel: (id: string, data: Omit<WireguardTunnelCreate, "server_private_key"> & { server_private_key?: string | null }, expectedUpdatedAt: string) =>
    request<WireguardTunnel>(`/wireguard/tunnels/${id}`, { method: "PUT", body: JSON.stringify({ ...data, expected_updated_at: expectedUpdatedAt }) }),
  previewWireguardTunnel: (id: string) => request<WireguardPreview>(`/wireguard/tunnels/${id}/preview`),
  wireguardTunnelRevisions: (id: string) => request<WireguardTunnelRevision[]>(`/wireguard/tunnels/${id}/revisions`),
  deployWireguardTunnel: (id: string, previewToken: string) => request<WireguardTunnel>(`/wireguard/tunnels/${id}/deploy`, { method: "POST", body: JSON.stringify({ preview_token: previewToken }) }),
  deleteWireguardTunnel: (id: string) => request<void>(`/wireguard/tunnels/${id}`, { method: "DELETE" }),
  removeWireguardTunnelPreviousTarget: (id: string, hostId: string, token: string) => request<void>(`/wireguard/tunnels/${id}/deployments/${hostId}/remove`, { method: "POST", body: JSON.stringify({ preview_token: token }) }),

  // Trust-Store certificates — no preview/deploy-warning flow (see the
  // type block above): "push" is the single local add/set call.
  listManagedCertificates: () => request<ManagedCertificate[]>("/certificates/managed"),
  createManagedCertificate: (data: ManagedCertificateCreate) => request<ManagedCertificate>("/certificates/managed", { method: "POST", body: JSON.stringify(data) }),
  editManagedCertificate: (id: string, data: ManagedCertificateCreate, expectedUpdatedAt: string) =>
    request<ManagedCertificate>(`/certificates/managed/${id}`, { method: "PUT", body: JSON.stringify({ ...data, expected_updated_at: expectedUpdatedAt }) }),
  pushManagedCertificate: (id: string) => request<ManagedCertificate>(`/certificates/managed/${id}/push`, { method: "POST" }),
  deleteManagedCertificate: (id: string) => request<void>(`/certificates/managed/${id}`, { method: "DELETE" }),

  // ACME accounts — "push" is a local config write; "register" is the
  // REAL external-CA call, gated by AcmeActionConfirm.tsx in the UI.
  listAcmeAccounts: () => request<AcmeAccount[]>("/acme/accounts"),
  createAcmeAccount: (data: AcmeAccountCreate) => request<AcmeAccount>("/acme/accounts", { method: "POST", body: JSON.stringify(data) }),
  editAcmeAccount: (id: string, data: AcmeAccountCreate, expectedUpdatedAt: string) =>
    request<AcmeAccount>(`/acme/accounts/${id}`, { method: "PUT", body: JSON.stringify({ ...data, expected_updated_at: expectedUpdatedAt }) }),
  pushAcmeAccount: (id: string) => request<AcmeAccount>(`/acme/accounts/${id}/push`, { method: "POST" }),
  registerAcmeAccount: (id: string) => request<AcmeAccount>(`/acme/accounts/${id}/register`, { method: "POST" }),
  deleteAcmeAccount: (id: string) => request<void>(`/acme/accounts/${id}`, { method: "DELETE" }),

  // ACME certificate requests — "push" is a local config write; "sign" is
  // the REAL, asynchronous external-CA call, also gated by
  // AcmeActionConfirm.tsx. "poll" re-reads statusCode/lastUpdate from
  // OPNsense after a sign trigger (or any time) — the only way to learn
  // whether an async sign actually succeeded.
  listAcmeCertificateRequests: () => request<AcmeCertificateRequest[]>("/acme/certificates"),
  createAcmeCertificateRequest: (data: AcmeCertificateRequestCreate) => request<AcmeCertificateRequest>("/acme/certificates", { method: "POST", body: JSON.stringify(data) }),
  editAcmeCertificateRequest: (id: string, data: AcmeCertificateRequestCreate, expectedUpdatedAt: string) =>
    request<AcmeCertificateRequest>(`/acme/certificates/${id}`, { method: "PUT", body: JSON.stringify({ ...data, expected_updated_at: expectedUpdatedAt }) }),
  pushAcmeCertificateRequest: (id: string) => request<AcmeCertificateRequest>(`/acme/certificates/${id}/push`, { method: "POST" }),
  signAcmeCertificateRequest: (id: string) => request<AcmeCertificateRequest>(`/acme/certificates/${id}/sign`, { method: "POST" }),
  pollAcmeCertificateRequest: (id: string) => request<AcmeCertificateRequest>(`/acme/certificates/${id}/poll`, { method: "POST" }),
  deleteAcmeCertificateRequest: (id: string) => request<void>(`/acme/certificates/${id}`, { method: "DELETE" }),
};


export interface GroupWrite { name: string; description: string; host_ids: string[]; }
export interface DeviceGroup extends GroupWrite { id: string; }
export interface RuleRevision { id: string; created_at: string; note: string; snapshot: RuleCreate; }
export interface RulePreview {
  token: string; can_deploy: boolean; basis: string; warnings: string[];
  hosts: { host_id: string; host_name: string; action: string; status: string | null; message: string | null; can_remove: boolean; can_retry_rollback: boolean;
    changes: { field: string; before: string | null; after: string }[] }[];
}

export interface GroupImpact {
  nat: {id: string; kind: string; description: string; added_host_ids: string[]; removed_host_ids: string[]; retained_host_ids: string[]}[];
  token: string;
  added_members: { id: string; name: string }[];
  removed_members: { id: string; name: string }[];
  rules: { rule_id: string; description: string; added_host_ids: string[]; removed_host_ids: string[]; retained_host_ids: string[] }[];
  aliases: { alias_id: string; description: string; added_host_ids: string[]; removed_host_ids: string[]; retained_host_ids: string[] }[];
}

export interface PortForwardCreate {
  description: string;
  interface: string;
  ipprotocol: string;
  protocol: string;
  source_net: string;
  source_port: string | null;
  destination_net: string;
  destination_port: string | null;
  target: string;
  local_port: string | null;
  natreflection: string;
  enabled: boolean;
  target_host_ids: string[];
  target_group_ids: string[];
  policy_hierarchy_group_id: string | null;
}
export interface PortForward extends PortForwardCreate {
  id: string; created_at: string; updated_at: string;
  workflow_state: Rule["workflow_state"]; workflow_counts: Rule["workflow_counts"];
  effective_host_ids: string[]; deployments: DeploymentResult[];
}
export interface PortForwardRevision { id: string; created_at: string; note: string; snapshot: PortForwardCreate; }
export interface NatPreview extends Omit<RulePreview, "hosts"> {
  hosts: (Omit<RulePreview["hosts"][number], "changes"> & {changes: {field: string; before: unknown; after: unknown}[]})[];
}
export interface NatLive { rules: Record<string, unknown>[]; automatic_rules: Record<string, unknown>[]; }

export interface OneToOneCreate {
  description: string;
  interface: string;
  source_net: string;
  destination_net: string;
  external: string;
  type: "binat" | "nat";
  log: boolean;
  natreflection: string;
  source_not: boolean;
  destination_not: boolean;
  enabled: boolean;
  target_host_ids: string[];
  target_group_ids: string[];
  policy_hierarchy_group_id: string | null;
}
export interface OneToOne extends OneToOneCreate {
  id: string; created_at: string; updated_at: string;
  workflow_state: Rule["workflow_state"]; workflow_counts: Rule["workflow_counts"];
  effective_host_ids: string[]; deployments: DeploymentResult[];
}
export interface OneToOneRevision { id: string; created_at: string; note: string; snapshot: OneToOneCreate; }

export interface OutboundNatCreate {
  description: string;
  interface: string;
  ipprotocol: string;
  protocol: string;
  source_net: string;
  source_port: string | null;
  destination_net: string;
  destination_port: string | null;
  target: string;
  target_port: string | null;
  source_not: boolean;
  destination_not: boolean;
  enabled: boolean;
  target_host_ids: string[];
  target_group_ids: string[];
  policy_hierarchy_group_id: string | null;
}
export interface OutboundNat extends OutboundNatCreate {
  id: string; created_at: string; updated_at: string;
  workflow_state: Rule["workflow_state"]; workflow_counts: Rule["workflow_counts"];
  effective_host_ids: string[]; deployments: DeploymentResult[];
}
export interface OutboundNatRevision { id: string; created_at: string; note: string; snapshot: OutboundNatCreate; }

export interface OutboundMode { host_id: string; host_name: string; mode: string; can_deploy: boolean; }

export interface AuditEvent { id: string; created_at: string; correlation_id: string; actor: string; kind: string; action: string; status: string; entity_id: string | null; entity_name: string | null; host_id: string | null; host_name: string | null; message: string | null; details: Record<string,unknown>; }
export interface AuditPage { total: number; offset: number; limit: number; items: AuditEvent[]; }

export type GatewayState = "online" | "degraded" | "offline" | "forced_down" | "unknown";
export interface GatewayHealth { name: string; address: string | null; status: string; state: GatewayState; loss: number | null; delay: number | null; stddev: number | null; monitor: string | null; alert_enabled: boolean; }
/** Side readings the newest fresh sample could not take because the API key lacks the right; null = unknown. */
export interface ApiRightsState { missing: string[] | null; }
export interface DashboardHost { id: string; name: string; status: HostStatus; carp_status: string | null; cluster_name: string | null; gateways: GatewayHealth[] | null; api_rights_missing: string[] | null; }
export interface PendingAction { kind: string; status: string; entity_name: string; host_name: string; message: string | null; created_at: string; link: string; }
export interface ActivityItem { id: string; created_at: string; actor: string; kind: string; action: string; status: string; entity_name: string | null; host_name: string | null; message: string | null; }
export interface DashboardSummary { hosts: DashboardHost[]; pending_actions: PendingAction[]; recent_activity: ActivityItem[]; }

export interface FirmwarePackageChange {
  name: string;
  reason: string;
  old: string;
  new: string;
  repository?: string;
}
export interface MaintenanceState {
  host_name: string;
  base_url: string;
  cluster_name?: string | null;
  version: string;
  product_latest?: string | null;
  carp_status?: string | null;
  carp_maintenance_mode?: boolean;
  running: string;
  firmware_status: string;
  firmware_message: string;
  last_check: string | null;
  reboot_required: boolean;
  packages?: Record<string, FirmwarePackageChange>;
  upgrade_sets?: Record<string, FirmwarePackageChange>;
  backup: {created_at:string;id:string;size:number} | null;
  backup_fresh?: boolean;
  peers?: {id:string;name:string;enabled:boolean;status:string|null;carp_status:string|null;carp_maintenance_mode:boolean}[];
  actions: {reboot:string[];update:string[];major_update:string[];firmware_rollback?:string[]};
  // Only in the preview of the action firmware_rollback (1.32).
  rollback?: {
    supported: boolean;
    target: {uuid:string;name:string;version:string;stamp:string;active:string;created:string} | null;
    current: {uuid:string;name:string} | null;
    changes_since: {kind:string;status:string;deployed_at:string}[];
  };
}
export interface HostOperation { id:string; host_id:string; action:"reboot"|"update"|"major_update"|"firmware_rollback"|"carp_enter"|"carp_leave"; status:string; created_at:string; expires_at:string; actor:string; message:string|null; }
export interface OpnsenseService { id:string; name:string; description:string; locked:boolean|number; running:boolean|number; }
export interface HostResourceSample { id:string; sampled_at:string; mem_used_pct:number|null; load_avg_1m:number|null; disk_used_pct:number|null; raw:Record<string,unknown>; }
export interface ScheduledMaintenanceItem { id:string; host_id:string; action:"reboot"|"update"|"major_update"; run_at:string; status:"pending"|"running"|"succeeded"|"failed"|"cancelled"; actor:string; created_at:string; result:{error?:string;response?:unknown;handover?:boolean;auto_verified?:boolean;version_before?:string|null;version_after?:string|null;reboot_required?:boolean}; host_operation_id:string|null; carp_handover:boolean; phase:"handover_wait"|"job_wait"|"verify"|"leave"|"leave_wait"|null; auto_verify?:boolean; origin?:"manual"|"rollout"; rollout_run_id?:string|null; }

// ── Maintenance windows and firmware rollouts (1.25) ──
export interface Occurrence { local_date: string; start: string; end: string; cutoff: string; moved: boolean; skipped: boolean; }
export interface MaintenanceWindowWrite {
  name: string; timezone: string; recurrence: "weekly" | "monthly"; weekdays: number[];
  month_mode: "nth_weekday" | "day" | null; month_nth: number | null; month_weekday: number | null; month_day: number | null;
  start_time: string; duration_minutes: number; start_cutoff_minutes: number; enabled: boolean;
}
export interface MaintenanceWindow extends MaintenanceWindowWrite {
  id: string; created_by: string; created_at: string; open_now: boolean; next: Occurrence[];
  exceptions: { id: string; occurrence_date: string; kind: "skip" | "move"; moved_start: string | null; reason: string; actor: string; created_at: string }[];
  used_by: { id: string; name: string; auto_start: boolean; active: boolean }[];
}
export type RolloutCheck = "online" | "version" | "carp" | "gateways" | "services" | "locks" | "compliance" | "probes";
export interface RolloutProbe { type: "ping" | "tcp"; target: string; port: number | null; }
export interface RolloutIssue { code: string; params: Record<string, string | number | boolean | null>; }
export interface RolloutWaveWrite { label: string; group_ids: string[]; soak_minutes: number; carp_handover: boolean; require_approval: boolean; }
export interface RolloutPlanWrite {
  name: string; description: string; action: "update" | "major_update"; window_id: string | null; auto_start: boolean;
  gate_checks: Partial<Record<RolloutCheck, boolean>>; gate_probes?: RolloutProbe[]; compliance_mode: "warn" | "fail"; waves: RolloutWaveWrite[];
}
export interface RolloutPlan extends Omit<RolloutPlanWrite, "waves"> {
  id: string;
  waves: (RolloutWaveWrite & { id: string; position: number; groups: string[]; host_count: number })[];
  active: boolean; activated_by: string | null; activated_at: string | null; fingerprint_ok: boolean;
  auto_start_error: { occurrence: string; at: string; code: string; params?: Record<string, string> } | null;
  window: { id: string; name: string; next: Occurrence[] } | null; errors: RolloutIssue[]; warnings: RolloutIssue[];
  host_count: number; last_run: { id: string; status: RolloutRunStatus; created_at: string } | null;
  created_by: string; created_at: string; updated_at: string;
}
export interface RolloutPreview {
  waves: { index: number; label: string; soak_minutes: number; carp_handover: boolean; require_approval: boolean; groups: string[];
           hosts: { id: string; name: string; cluster_name: string | null; enabled: boolean }[] }[];
  errors: RolloutIssue[]; warnings: RolloutIssue[]; window: RolloutPlan["window"]; preview_token: string; can_start: boolean; plan: RolloutPlan;
}
export type RolloutRunStatus = "waiting_window" | "preflight" | "wave_running" | "soaking" | "gate" | "awaiting_approval" | "resuming" | "paused" | "succeeded" | "cancelled";
export type RolloutHostStatus = "queued" | "running" | "verifying" | "succeeded" | "skipped" | "failed" | "blocked" | "cancelled";
export interface RolloutCheckResult { result: "ok" | "warn" | "fail" | "skip"; code: string; params: Record<string, string | number | null>; }
export interface RolloutGate { hosts: Record<string, Partial<Record<RolloutCheck, RolloutCheckResult>>>; verdict: "ok" | "warn" | "fail"; }
export interface RolloutRunSummary {
  id: string; plan_id: string | null; plan_name: string; action: "update" | "major_update"; status: RolloutRunStatus; paused_from: string | null;
  current_wave: number; wave_count: number; current_wave_label: string | null; target_version: string | null;
  pause_reason: string | null; pause_detail: Record<string, unknown> | null; started_by: string; trigger: "manual" | "window";
  window_occurrence: string | null; created_at: string; finished_at: string | null; step_at: string | null;
  host_counts: Partial<Record<RolloutHostStatus, number>>; active: boolean;
}
export interface RolloutRunDetail extends RolloutRunSummary {
  soak_until: string | null; progress: { gate_recheck_at?: string | null; cluster_recheck_at?: string | null; blocked_since?: string | null; resume_gate?: RolloutGate; cluster_gate?: RolloutGate; resume_recheck_at?: string | null };
  waves: { index: number; label: string; status: string; gate_result: RolloutGate | null; gate_attempts: number; soak_until: string | null;
           approved_by: string | null; started_at: string | null; finished_at: string | null;
           spec: { label: string; soak_minutes: number; carp_handover: boolean; require_approval: boolean; groups: string[] } | null }[];
  hosts: { id: string; host_id: string | null; host_name: string; cluster_name: string | null; wave_index: number; order: number; status: RolloutHostStatus;
           version_before: string | null; version_after: string | null; detail: RolloutIssue | null; started_at: string | null; finished_at: string | null;
           schedule: { id: string; status: string; phase: string | null; carp_handover: boolean; host_operation_id: string | null } | null }[];
}
export interface RolloutStatus { active: RolloutRunSummary | null; host_ids: string[]; current_host_ids: string[]; }
export interface MaintenanceRead {state:MaintenanceState|null;error:string|null;operations:HostOperation[];}
export interface MaintenancePreview {preview_id:string;expires_at:string;action:"reboot"|"update"|"major_update"|"firmware_rollback";state:MaintenanceState;}
export interface CarpSwitchState {
  host_id:string;
  host_name:string;
  base_url:string;
  cluster_name:string;
  action:"carp_enter"|"carp_leave";
  carp_status:string|null;
  carp_maintenance_mode:boolean;
  carp_demotion:string|number|null;
  peers:{id:string;name:string;status:string;carp_status:string|null;carp_maintenance_mode:boolean}[];
  blocked:string[];
  actions:{carp:string[]};
  auto_revert_window: { auto_revert_at: string } | null;
}
export interface CarpSwitchPreview {preview_id:string;expires_at:string;action:"carp_enter"|"carp_leave";state:CarpSwitchState;}

export type Theme="light"|"dark"|"system";
export interface AppSettings {username:string;theme:Theme;language:Language;setup_done:boolean;deploy_confirm_window_seconds:number;session_lifetime_seconds:number;}

export type SmtpSecurity = "none" | "starttls" | "ssl";

export interface NotificationSettings {
  webhook_enabled: boolean;
  webhook_url: string | null;
  webhook_signing_secret_set: boolean;
  email_enabled: boolean;
  smtp_host: string | null;
  smtp_port: number;
  smtp_security: SmtpSecurity;
  smtp_username: string | null;
  smtp_password_set: boolean;
  smtp_from_address: string | null;
  smtp_to_addresses: string[];
  host_offline_enabled: boolean;
  cert_expiring_enabled: boolean;
  rule_drift_enabled: boolean;
  revert_failed_enabled: boolean;
  maintenance_unknown_enabled: boolean;
  carp_health_enabled: boolean;
  nat_drift_enabled: boolean;
  network_drift_enabled: boolean;
  carp_auto_revert_failed_enabled: boolean;
  scheduled_maintenance_failed_enabled: boolean;
  backup_failed_enabled: boolean;
  compliance_alerts_enabled: boolean;
  gateway_down_enabled: boolean;
  resource_threshold_enabled: boolean;
  service_down_enabled: boolean;
  rollout_problems_enabled: boolean;
  api_rights_enabled: boolean;
  gateway_loss_threshold_pct: number;
  gateway_delay_threshold_ms: number;
  disk_threshold_pct: number;
  memory_threshold_pct: number;
  swap_threshold_pct: number;
  load_per_cpu_threshold: number;
  alert_confirm_samples: number;
  resource_confirm_samples: number;
  service_watchlist: string[];
  poll_interval_seconds: number;
  updated_at: string;
}

export type ChannelType = "ntfy" | "gotify" | "telegram" | "teams" | "matrix";
export interface NotificationChannel {
  id: string;
  name: string;
  channel_type: ChannelType;
  enabled: boolean;
  config: Record<string, string | number>;
  secret_set: boolean;
  created_at: string;
  updated_at: string;
}
export interface NotificationChannelCreate { name: string; channel_type: ChannelType; enabled: boolean; config: Record<string, string | number>; secret?: string; }
export interface NotificationChannelUpdate { name?: string; enabled?: boolean; config?: Record<string, string | number>; secret?: string; clear_secret?: boolean; }

export interface NotificationSettingsUpdate {
  webhook_enabled?: boolean;
  webhook_url?: string;
  webhook_signing_secret?: string;
  clear_webhook_signing_secret?: boolean;
  email_enabled?: boolean;
  smtp_host?: string;
  smtp_port?: number;
  smtp_security?: SmtpSecurity;
  smtp_username?: string;
  smtp_password?: string;
  clear_smtp_password?: boolean;
  smtp_from_address?: string;
  smtp_to_addresses?: string[];
  host_offline_enabled?: boolean;
  cert_expiring_enabled?: boolean;
  rule_drift_enabled?: boolean;
  revert_failed_enabled?: boolean;
  maintenance_unknown_enabled?: boolean;
  carp_health_enabled?: boolean;
  nat_drift_enabled?: boolean;
  network_drift_enabled?: boolean;
  carp_auto_revert_failed_enabled?: boolean;
  scheduled_maintenance_failed_enabled?: boolean;
  backup_failed_enabled?: boolean;
  compliance_alerts_enabled?: boolean;
  gateway_down_enabled?: boolean;
  resource_threshold_enabled?: boolean;
  service_down_enabled?: boolean;
  rollout_problems_enabled?: boolean;
  api_rights_enabled?: boolean;
  gateway_loss_threshold_pct?: number;
  gateway_delay_threshold_ms?: number;
  disk_threshold_pct?: number;
  memory_threshold_pct?: number;
  swap_threshold_pct?: number;
  load_per_cpu_threshold?: number;
  alert_confirm_samples?: number;
  resource_confirm_samples?: number;
  service_watchlist?: string[];
  poll_interval_seconds?: number;
}

export interface LogSettings {
  enabled: boolean;
  poll_interval_seconds: number;
  retention_days: number;
  max_size_gb: number;
  used_bytes: number;
  entries: number;
  oldest_at: string | null;
}

export interface LogSettingsUpdate {
  enabled?: boolean;
  poll_interval_seconds?: number;
  retention_days?: number;
  max_size_gb?: number;
}

export type ProblemType =
  | "host_offline" | "cert_expiry" | "rule_drift" | "revert_failed" | "maintenance_unknown"
  | "carp_split_brain" | "nat_drift" | "carp_auto_revert_failed" | "network_drift"
  | "scheduled_maintenance_failed"
  | "backup_stale"
  | "backup_offsite_failed"
  | "compliance_finding"
  | "gateway_down" | "resource_threshold" | "service_down" | "api_rights_missing"
  | "rollout_paused" | "rollout_start_failed";

export interface CarpEvent {
  id: string;
  host_id: string;
  host_name: string;
  cluster_name: string | null;
  from_status: string | null;
  to_status: string | null;
  detected_at: string;
}

export interface NotificationProblem {
  id: string;
  problem_type: ProblemType;
  status: "open" | "resolved";
  context: Record<string, unknown>;
  first_detected_at: string;
  last_seen_at: string;
  resolved_at: string | null;
  last_notified_at: string | null;
  resolved_notified_at: string | null;
}

// Hierarchische Policies (Pre-/Post-Rulebases mit Vererbung) — separate
// tree from DeviceGroup/FirewallRule on purpose (see backend
// models/policy_hierarchy_group.py): a host belongs to at most one
// PolicyHierarchyGroup, and rules are authored on a group, inherited
// downward to every descendant group's hosts, and deployed in a specific
// cross-group order (root-most first for pre-rules, leaf-most first for
// post-rules) since every pushed rule is "quick" (strict first-match).
export interface PolicyHierarchyGroupWrite {
  name: string;
  description: string;
  parent_id: string | null;
}
export interface PolicyHierarchyGroup extends PolicyHierarchyGroupWrite {
  id: string;
  created_at: string;
  host_ids: string[];
}

export type PolicyHierarchyRulebase = "pre" | "post";

export interface PolicyHierarchyRuleCreate {
  hierarchy_group_id: string;
  rulebase: PolicyHierarchyRulebase;
  description: string;
  interface: string;
  direction: RuleDirection;
  ipprotocol: string;
  protocol: string;
  action: RuleAction;
  source_net: string;
  source_port: string | null;
  destination_net: string;
  destination_port: string | null;
  enabled: boolean;
}
export interface PolicyHierarchyRule extends PolicyHierarchyRuleCreate {
  id: string;
  sequence: number;
  created_at: string;
  updated_at: string;
  effective_host_ids: string[];
  workflow_state: Rule["workflow_state"];
  workflow_counts: Rule["workflow_counts"];
  deployments: DeploymentResult[];
}
export interface PolicyHierarchyRuleRevision { id: string; created_at: string; note: string; snapshot: PolicyHierarchyRuleCreate; }

export type AnalysisAction = "pass" | "block" | "reject";
export type AnalysisConfidence = "sure" | "probable" | "unknown";
export type AnalysisTri = "true" | "false" | "unknown";
export interface AnalysisRule {
  uuid: string | null; label: string | null; description: string | null; section: string; sort_order: string | null;
  pf_numbers: number[]; action: AnalysisAction | null; quick: boolean | null; interfaces: string[]; interface_not: boolean;
  direction: string; automatic: boolean; legacy: boolean; loaded: boolean | null; editable: boolean;
  source: string; destination: string; source_port: string | null; destination_port: string | null;
  gateway: string | null; schedule: string | null;
}
export interface AnalysisStep {
  rule: AnalysisRule; result: AnalysisTri;
  effect: "decisive" | "overridden" | "possible" | "no_match" | "not_loaded" | "disabled";
  fields: Record<string, AnalysisTri>; reasons: string[];
}
export interface AnalysisLeg {
  direction: string; interface: string; interface_source: string; action: AnalysisAction | null;
  confidence: AnalysisConfidence; rule_confidence: AnalysisConfidence; decisive_rule: AnalysisRule | null;
  notes: string[]; unknowns: string[];
}
export interface AnalysisResult {
  host_id: string; host_name: string; cluster_name: string | null;
  error: { code: string; params?: Record<string, string> } | null;
  query?: { src: string; dst: string; proto: string; sport: number | null; dport: number | null; icmptype: string | null; direction: string; assumptions: string[] };
  action?: AnalysisAction | null; confidence?: AnalysisConfidence; rule_confidence?: AnalysisConfidence;
  decisive_rule?: AnalysisRule | null; notes?: string[]; unknowns?: string[];
  legs?: AnalysisLeg[]; trace?: { leg: number; direction: string; interface: string; steps: AnalysisStep[] }[];
}
export interface AnalysisQuery {
  src: string; dst: string; proto?: string; dport?: string; sport?: string; icmptype?: string;
  iface?: string; egress?: string; host_ids?: string; group_id?: string; refresh?: boolean;
}

export type CleanupKind = "unused" | "shadowed" | "duplicate" | "disabled_long";
export type CleanupBlock = "automatic" | "legacy" | "managed";
export interface CleanupCategory {
  kind: CleanupKind; confidence: AnalysisConfidence; days?: number; since?: string;
  by?: { uuid: string | null; description: string; section: string; automatic: boolean };
}
export interface CleanupCandidate extends AnalysisRule {
  enabled: boolean; categories: CleanupCategory[]; hit_state: "used" | "unused" | "collecting" | "disabled" | null;
  last_hit_at: string | null; watched_since: string | null; removable: boolean; not_removable_reason: CleanupBlock | null;
  managed_by: { kind: "rule" | "policy_hierarchy"; id: string; name: string } | null;
}
export interface RuleCleanupResult {
  host_id: string; host_name: string; cluster_name: string | null; unused_days: number; generated_at: string;
  rules_total: number; candidates: CleanupCandidate[]; counts: Record<CleanupKind, number>; notes: string[];
  usage: { tracked: number; collecting: number; watching_since: string | null };
  history: { watching_since: string | null };
}
export interface GovernanceSettings {
  operators_may_deploy: boolean; cleanup_max_rules: number; cleanup_delete_min_days: number;
  // The list itself only comes for admins; everybody else just learns whether it is set.
  cleanup_management_sources: string[]; cleanup_management_sources_configured: boolean;
  // Extra management ports (besides API port, 443, 80, 22); the list only for admins, too.
  cleanup_management_ports: number[]; cleanup_management_ports_configured: boolean;
  // B12: the address this browser's request came from, as a suggestion (admins only).
  your_address?: string | null;
}
export interface CleanupSettingsUpdate {
  cleanup_max_rules?: number; cleanup_delete_min_days?: number; cleanup_management_sources?: string[];
  cleanup_management_ports?: number[];
}
export type CleanupActionKind = "disable" | "enable" | "delete";
export type CleanupOperationKind = CleanupActionKind | "restore";
export type CleanupOperationStatus =
  | "preview" | "running" | "awaiting_confirm" | "confirmed" | "partial" | "apply_failed" | "reverting" | "reverted"
  | "revert_failed" | "done" | "failed" | "cancelled" | "expired";
export type CleanupItemState =
  | "queued" | "disabling" | "disabled" | "confirmed" | "reverting" | "reverted" | "deleting" | "deleted" | "restoring"
  | "restored" | "failed" | "unknown" | "revert_failed" | "skipped";
export interface CleanupItem {
  id: string; host_id: string; host_name: string | null; rule_uuid: string; seq: number; carp_role: string | null;
  state: CleanupItemState; description: string | null; categories: CleanupCategory[] | null; confidence: AnalysisConfidence | null;
  cover_uuid: string | null; restored_uuid: string | null; changed_at: string | null; message: string | null;
  has_snapshot: boolean; restorable: boolean;
}
export interface CleanupPlanItem {
  uuid: string; given: string; seq: number; description: string; action: string | null;
  categories: { kind: CleanupKind; confidence: AnalysisConfidence; notes?: string[]; days?: number }[];
  level: AnalysisConfidence | null; needs_ack: boolean; notes: string[]; cover_uuid: string | null;
  protection: { result: "false" | "true" | "unknown"; queries: number; reasons: string[] } | null;
  external: string | null;
  basis?: { type: "confirmed" | "disabled_long"; confirmed_at?: string; since?: string };
}
// opnexus_source (B10): the address this firewall sees OPNexus come from; protected as well. The address only for admins.
export interface CleanupPlanNode { host_id: string; host_name: string; role: string | null; items: CleanupPlanItem[];
  opnexus_source?: { address?: string | null; status: "added" | "configured" | "unknown" | null }; }
// sources/ports only for admins (the backend leaves them out for everybody else, sources_configured instead).
export interface CleanupPlan { kind: CleanupActionKind; uuids: string[]; unused_days: number; sources?: string[]; ports?: number[]; sources_configured?: boolean; acknowledged: string[]; nodes: CleanupPlanNode[]; }
export interface CleanupOperation {
  id: string; host_id: string; host_name: string | null; cluster_name: string | null; kind: CleanupOperationKind;
  status: CleanupOperationStatus; actor: string; created_at: string | null; expires_at: string | null;
  revert_deadline: string | null; confirmed_at: string | null; finished_at: string | null; message: string | null;
  plan: CleanupPlan | null; items: CleanupItem[]; can_confirm: boolean; can_revert: boolean; can_retry: boolean; warnings: string[];
}
export interface CleanupPreview {
  preview_id: string; expires_at: string | null; kind: CleanupActionKind; plan: CleanupPlan; max_rules: number;
  window_seconds: number; operation: CleanupOperation;
}
export interface CleanupPreviewRequest {
  items: { uuid: string; kind: CleanupActionKind; ack: boolean }[]; unused_days: number; ack_text_version: string;
}
export interface RuleHistoryChange { field: string; before: unknown; after: unknown; }
export interface RuleHistoryEvent {
  source: "observed" | "audit" | "revision"; at: string; kind: string; origin: "opnexus" | "external";
  actor: string | null; changes: RuleHistoryChange[]; status?: string; message?: string | null; note?: string;
  audit_id: string | null; managed_rule_id?: string;
}
export interface RuleHistory { host_id: string; rule_uuid: string; watching_since: string | null; events: RuleHistoryEvent[]; }

// Configuration as code (1.32)
export interface ConfigFileError { code: string; path: string; line: number | null; params: Record<string, string | number | null>; }
export interface ConfigChange { field: string; before: unknown; after: unknown; }
export type ConfigAction = "create" | "update" | "delete" | "unchanged" | "blocked";
export interface ConfigPlanItem {
  kind: string;
  key: string;
  action: ConfigAction;
  planned_action: Exclude<ConfigAction, "blocked">;
  changes: ConfigChange[];
  deployed_hosts: number;
  reasons: string[];
  implicit: boolean;
}
export interface ConfigWarning { code: string; path: string; params: Record<string, string | number | null>; }
export interface ConfigPlan {
  token: string;
  sha256: string;
  prune: boolean;
  kinds: string[];
  summary: { create: number; update: number; delete: number; unchanged: number; blocked: number };
  items: ConfigPlanItem[];
  warnings: ConfigWarning[];
  applicable: boolean;
}
