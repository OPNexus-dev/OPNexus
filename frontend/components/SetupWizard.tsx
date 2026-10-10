"use client";

import { useLanguage } from "@/components/LanguageProvider";
import { TlsPinControl, TlsPinValue } from "@/components/TlsPinControl";
import { TwoFactorSection } from "@/components/TwoFactorSection";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { FieldGroup, Input } from "@/components/ui/Field";
import { StatusDot } from "@/components/ui/StatusDot";
import { useToast } from "@/components/ui/Toast";
import { InfoTip } from "@/components/ui/Tooltip";
import { api, AppSettings, Host } from "@/lib/api";
import clsx from "clsx";
import { ArrowLeft, ArrowRight, Bell, Check, DownloadCloud, ExternalLink, Shield, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

const STEPS = ["welcome", "account", "firewall", "done"] as const;
type Step = (typeof STEPS)[number];

const emptyHostForm = { name: "", base_url: "", api_key: "", api_secret: "", cluster_name: "" };
const noPin: TlsPinValue = { pem: null, insecure: false, clear: false };

function Stepper({ step, onGo }: { step: number; onGo: (index: number) => void }) {
  const { t } = useLanguage();
  return (
    <nav aria-label={t("setup.stepsLabel")}>
      <ol className="flex flex-wrap gap-2">
        {STEPS.map((key, index) => {
          const current = index === step;
          const done = index < step;
          return (
            <li key={key}>
              <button
                type="button"
                aria-current={current ? "step" : undefined}
                onClick={() => onGo(index)}
                className={clsx(
                  "inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-sm font-medium transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-500",
                  current ? "border-brand-500 bg-brand-500 text-white" : done ? "border-slate-300 text-slate-700 hover:bg-slate-100" : "border-slate-200 text-slate-500 hover:bg-slate-100"
                )}
              >
                <span
                  aria-hidden="true"
                  className={clsx("flex h-5 w-5 items-center justify-center rounded-full text-xs", current ? "bg-white/25" : done ? "bg-emerald-100 text-emerald-700" : "bg-slate-100")}
                >
                  {done ? <Check size={12} /> : index + 1}
                </span>
                {t(`setup.steps.${key}`)}
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

function WelcomeStep({ settings, onSettings }: { settings: AppSettings | null; onSettings: (s: AppSettings) => void }) {
  const { t, errorMessage, language, setLanguage } = useLanguage();
  const [error, setError] = useState("");

  async function pick(value: "de" | "en") {
    const previous = language;
    setLanguage(value);
    setError("");
    try {
      onSettings(await api.saveSettings({ language: value }));
    } catch (e) {
      setLanguage(previous);
      setError(errorMessage(e, "common.saveFailed"));
    }
  }

  return (
    <div className="space-y-5">
      <p className="text-sm text-slate-600">{t("setup.welcome.intro")}</p>
      <ul className="space-y-2 text-sm text-slate-700">
        {(["account", "firewall", "done"] as const).map((key) => (
          <li key={key} className="flex items-start gap-2">
            <Check size={16} className="mt-0.5 shrink-0 text-emerald-600" aria-hidden="true" />
            <span>{t(`setup.welcome.points.${key}`)}</span>
          </li>
        ))}
      </ul>
      <p className="flex items-start gap-2 rounded-xl bg-slate-50 p-3 text-sm text-slate-600">
        <ShieldCheck size={16} className="mt-0.5 shrink-0 text-emerald-600" aria-hidden="true" />
        {t("setup.welcome.readOnlyNote")}
      </p>
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium text-slate-700">{t("settings.language.label")}</legend>
        <div role="radiogroup" aria-label={t("settings.language.label")} className="flex flex-wrap gap-3">
          {(["de", "en"] as const).map((value) => (
            <label
              key={value}
              className={clsx("flex cursor-pointer items-center gap-2 rounded-lg border px-4 py-2.5 text-sm", language === value ? "border-sky-500 bg-sky-50 text-sky-700" : "border-slate-200")}
            >
              <input type="radio" name="setup-language" checked={language === value} disabled={!settings} onChange={() => void pick(value)} />
              {t(`settings.language.${value}`)}
            </label>
          ))}
        </div>
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      </fieldset>
    </div>
  );
}

function AccountStep() {
  const { t, errorMessage } = useLanguage();
  const toast = useToast();
  const [current, setCurrent] = useState("");
  const [password, setPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [busy, setBusy] = useState(false);
  const [changed, setChanged] = useState(false);
  const [error, setError] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (password !== repeat) {
      setError(t("settings.passwordMismatch"));
      return;
    }
    setBusy(true);
    try {
      await api.changePassword(current, password);
      setCurrent("");
      setPassword("");
      setRepeat("");
      setChanged(true);
      toast.show(t("settings.passwordChanged"), "success");
    } catch (err) {
      setError(errorMessage(err, "settings.passwordFailed"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <section aria-labelledby="setup-password" className="space-y-3">
        <h3 id="setup-password" className="flex items-center gap-2 text-base font-semibold text-slate-900">
          {t("setup.account.passwordTitle")}
          {changed && <Badge tone="success"><Check size={12} />{t("setup.account.passwordDone")}</Badge>}
        </h3>
        <p className="text-sm text-slate-600">{t("setup.account.passwordIntro")}</p>
        <form onSubmit={submit} className="max-w-md space-y-3">
          <FieldGroup label={t("settings.currentPassword")} hint={t("setup.account.currentHint")}>
            <Input type="password" autoComplete="current-password" required value={current} onChange={(e) => setCurrent(e.target.value)} />
          </FieldGroup>
          <FieldGroup label={t("settings.newPassword")} hint={t("setup.account.newHint")}>
            <Input type="password" autoComplete="new-password" minLength={10} maxLength={1024} required value={password} onChange={(e) => setPassword(e.target.value)} />
          </FieldGroup>
          <FieldGroup label={t("settings.repeatPassword")}>
            <Input type="password" autoComplete="new-password" minLength={10} required value={repeat} onChange={(e) => setRepeat(e.target.value)} />
          </FieldGroup>
          {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
          <Button type="submit" variant="secondary" loading={busy}>{t("settings.changePassword")}</Button>
        </form>
      </section>
      <section aria-labelledby="setup-2fa" className="space-y-3">
        <h3 id="setup-2fa" className="text-base font-semibold text-slate-900">{t("setup.account.twofaTitle")}</h3>
        <p className="text-sm text-slate-600">{t("setup.account.twofaIntro")}</p>
        <TwoFactorSection />
      </section>
    </div>
  );
}

function FirewallStep({ hosts, onHostsChanged }: { hosts: Host[] | null; onHostsChanged: () => Promise<void> }) {
  const { t, ts, errorMessage } = useLanguage();
  const toast = useToast();
  const [form, setForm] = useState(emptyHostForm);
  const [pin, setPin] = useState<TlsPinValue>(noPin);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [checking, setChecking] = useState<string | null>(null);

  const firstHost = !hosts || hosts.length === 0;

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError("");
    try {
      const created = await api.createHost({
        name: form.name.trim(),
        base_url: form.base_url.trim(),
        api_key: form.api_key.trim(),
        api_secret: form.api_secret.trim(),
        verify_tls: !pin.insecure,
        tls_ca_pem: pin.pem,
        enabled: true,
        cluster_name: form.cluster_name.trim() || null,
        policy_hierarchy_group_id: null,
      });
      toast.show(t("hosts.created", { name: created.name }), "success");
      setForm(emptyHostForm);
      setPin(noPin);
      await onHostsChanged();
    } catch (err) {
      setError(errorMessage(err, "hosts.createFailed"));
    } finally {
      setSaving(false);
    }
  }

  async function recheck(id: string) {
    setChecking(id);
    try {
      await onHostsChanged();
    } finally {
      setChecking(null);
    }
  }

  return (
    <div className="space-y-6">
      <p className="text-sm text-slate-600">{t("setup.firewall.intro")}</p>

      {hosts && hosts.length > 0 && (
        <section aria-labelledby="setup-hosts" className="space-y-2">
          <h3 id="setup-hosts" className="text-base font-semibold text-slate-900">{t("setup.firewall.connected")}</h3>
          <ul className="divide-y divide-slate-100 rounded-xl border border-slate-200">
            {hosts.map((host) => (
              <li key={host.id} className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-3 text-sm">
                <Link href={`/hosts/${host.id}`} className="font-medium text-sky-700 hover:underline">{host.name}</Link>
                <span className="text-slate-500">{host.base_url}</span>
                <span className="ml-auto flex items-center gap-3">
                  <StatusDot status={host.status} />
                  {host.status === "offline" && (
                    <Button type="button" variant="outline" loading={checking === host.id} onClick={() => void recheck(host.id)}>
                      {t("setup.firewall.recheck")}
                    </Button>
                  )}
                </span>
                {host.status === "offline" && (
                  <div className="basis-full space-y-0.5 text-xs">
                    {host.status_detail && <p className="text-slate-500">{ts(host.status_detail).slice(0, 160)}</p>}
                    <p className="text-flame-700">{t("setup.firewall.offlineHelp")}</p>
                  </div>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section aria-labelledby="setup-key" className="space-y-3 rounded-xl border border-slate-200 p-4">
        <h3 id="setup-key" className="text-base font-semibold text-slate-900">{t("setup.firewall.keyTitle")}</h3>
        <ol className="list-decimal space-y-1.5 pl-5 text-sm text-slate-700">
          {(["menu", "user", "key", "rights"] as const).map((key) => (
            <li key={key}>{t(`setup.firewall.keySteps.${key}`)}</li>
          ))}
        </ol>
        <p className="flex items-start gap-1.5 text-xs text-slate-500">
          <DownloadCloud size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
          {t("setup.firewall.keyFileNote")}
        </p>
      </section>

      <form onSubmit={create} className="space-y-4" aria-labelledby="setup-add">
        <h3 id="setup-add" className="text-base font-semibold text-slate-900">
          {firstHost ? t("setup.firewall.addFirst") : t("setup.firewall.addAnother")}
        </h3>
        <div className="grid gap-4 sm:grid-cols-2">
          <FieldGroup label="Name" hint={t("setup.firewall.nameHint")}>
            <Input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="fw-primary" />
          </FieldGroup>
          <FieldGroup label={t("hosts.baseUrl")} hint={t("setup.firewall.urlHint")}>
            <Input required value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} placeholder="https://192.168.1.1" />
          </FieldGroup>
          <FieldGroup label="API-Key">
            <Input required autoComplete="off" value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })} />
          </FieldGroup>
          <FieldGroup label="API-Secret">
            <Input required type="password" autoComplete="off" value={form.api_secret} onChange={(e) => setForm({ ...form, api_secret: e.target.value })} />
          </FieldGroup>
          <FieldGroup label={t("hosts.clusterOptional")} hint={t("setup.firewall.clusterHint")}>
            <Input value={form.cluster_name} onChange={(e) => setForm({ ...form, cluster_name: e.target.value })} placeholder={t("hosts.clusterPlaceholder")} />
          </FieldGroup>
        </div>
        <TlsPinControl baseUrl={form.base_url} value={pin} onChange={setPin} />
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" loading={saving}>{saving ? t("hosts.saving") : t("setup.firewall.connect")}</Button>
          <span className="text-xs text-slate-500">{t("setup.firewall.connectNote")}</span>
        </div>
      </form>
    </div>
  );
}

function DoneStep({ hosts, settings }: { hosts: Host[] | null; settings: AppSettings | null }) {
  const { t } = useLanguage();
  const [twofa, setTwofa] = useState<boolean | null>(null);

  useEffect(() => {
    api.twoFactorStatus().then((s) => setTwofa(s.enabled)).catch(() => setTwofa(null));
  }, []);

  const online = hosts?.filter((h) => h.status === "online").length ?? 0;
  const total = hosts?.length ?? 0;
  const seconds = settings?.deploy_confirm_window_seconds ?? 0;

  const next: { href: string; icon: typeof Shield; key: string; params?: Record<string, string | number> }[] = [
    { href: "/rules", icon: Shield, key: "rules", params: { seconds } },
    { href: "/settings#notifications", icon: Bell, key: "notify" },
    { href: "/settings#opnexus-update", icon: DownloadCloud, key: "update" },
  ];

  return (
    <div className="space-y-6">
      <ul className="space-y-2 text-sm">
        <li className="flex items-center gap-2">
          <Badge tone={total > 0 && online === total ? "success" : total > 0 ? "warning" : "neutral"}>
            {total > 0 ? t("setup.done.hostsOnline", { online, total }) : t("setup.done.noHosts")}
          </Badge>
        </li>
        <li className="flex items-center gap-2">
          <Badge tone={twofa ? "success" : "neutral"}>{twofa ? t("setup.done.twofaOn") : t("setup.done.twofaOff")}</Badge>
        </li>
      </ul>

      <section aria-labelledby="setup-next" className="space-y-3">
        <h3 id="setup-next" className="text-base font-semibold text-slate-900">{t("setup.done.nextTitle")}</h3>
        <ul className="grid gap-3 sm:grid-cols-3">
          {next.map(({ href, icon: Icon, key, params }) => (
            <li key={key}>
              <Link href={href} className="flex h-full flex-col gap-1 rounded-xl border border-slate-200 p-4 text-sm transition-colors hover:border-brand-500 hover:bg-slate-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-500">
                <span className="flex items-center gap-2 font-medium text-slate-900"><Icon size={16} aria-hidden="true" />{t(`setup.done.next.${key}.title`)}</span>
                <span className="text-slate-600">{t(`setup.done.next.${key}.text`, params)}</span>
              </Link>
            </li>
          ))}
        </ul>
        <p className="text-sm text-slate-600">
          {t("setup.done.backupBefore")} <code className="rounded bg-slate-100 px-1.5 py-0.5 text-xs">./opnexus backup</code>
          <InfoTip text={t("setup.done.backupHelp")} size={13} className="ml-1 align-middle" />
        </p>
        <p className="text-sm text-slate-500">
          <a href="https://opnexus.dev/docs/cli/" target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-sky-700 hover:underline">
            {t("setup.done.docs")} <ExternalLink size={13} aria-hidden="true" />
          </a>
        </p>
      </section>
    </div>
  );
}

export function SetupWizard() {
  const { t } = useLanguage();
  const router = useRouter();
  const [index, setIndex] = useState(0);
  const [role, setRole] = useState<string | null>(null);
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [hosts, setHosts] = useState<Host[] | null>(null);
  const [finishing, setFinishing] = useState(false);
  const [error, setError] = useState("");
  const headingRef = useRef<HTMLHeadingElement>(null);
  const mounted = useRef(false);

  async function loadHosts() {
    try {
      setHosts(await api.listHosts());
    } catch {
      setHosts([]);
    }
  }

  useEffect(() => {
    api.me().then((s) => setRole(s.role ?? "admin")).catch(() => setRole("admin"));
    api.settings().then(setSettings).catch(() => {});
    void loadHosts();
  }, []);

  // Move focus to the step heading on navigation so screen-reader users hear
  // where they are; skipped on the very first render.
  useEffect(() => {
    if (!mounted.current) {
      mounted.current = true;
      return;
    }
    headingRef.current?.focus();
  }, [index]);

  async function leave(markDone: boolean) {
    setFinishing(true);
    setError("");
    try {
      if (markDone && !settings?.setup_done) await api.saveSettings({ setup_done: true });
      router.push("/dashboard");
    } catch {
      setError(t("common.saveFailed"));
      setFinishing(false);
    }
  }

  const step: Step = STEPS[index];
  const last = index === STEPS.length - 1;

  if (role === "read_only") {
    return (
      <div className="mx-auto max-w-3xl space-y-3">
        <h1 className="text-2xl font-semibold text-slate-900">{t("setup.title")}</h1>
        <p className="text-sm text-slate-600">{t("setup.readOnly")}</p>
        <Button href="/dashboard" variant="outline">{t("setup.toDashboard")}</Button>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900">{t("setup.title")}</h1>
          <p className="mt-1 text-sm text-slate-500">{t("setup.stepOf", { current: index + 1, total: STEPS.length })}</p>
        </div>
        <Button type="button" variant="ghost" loading={finishing} onClick={() => void leave(true)}>
          {t("setup.skip")}
        </Button>
      </div>

      <Stepper step={index} onGo={setIndex} />

      <Card className="space-y-5 p-5 sm:p-6">
        <div>
          <h2 ref={headingRef} tabIndex={-1} className="text-xl font-semibold text-slate-900 outline-none">
            {t(`setup.${step}.title`)}
          </h2>
        </div>
        {step === "welcome" && <WelcomeStep settings={settings} onSettings={setSettings} />}
        {step === "account" && <AccountStep />}
        {step === "firewall" && <FirewallStep hosts={hosts} onHostsChanged={loadHosts} />}
        {step === "done" && <DoneStep hosts={hosts} settings={settings} />}
      </Card>

      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}

      <div className="flex items-center justify-between gap-3">
        <Button type="button" variant="outline" disabled={index === 0} onClick={() => setIndex(index - 1)}>
          <ArrowLeft size={16} aria-hidden="true" />
          {t("setup.back")}
        </Button>
        {last ? (
          <Button type="button" loading={finishing} onClick={() => void leave(true)}>
            {t("setup.finish")}
          </Button>
        ) : (
          <Button type="button" onClick={() => setIndex(index + 1)}>
            {t("setup.next")}
            <ArrowRight size={16} aria-hidden="true" />
          </Button>
        )}
      </div>
    </div>
  );
}
