"use client";

import { InfoTip } from "@/components/ui/Tooltip";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { FieldGroup, Input, Select } from "@/components/ui/Field";
import { Modal } from "@/components/ui/Modal";
import { Skeleton } from "@/components/ui/Skeleton";
import { useToast } from "@/components/ui/Toast";
import { api, ApiToken, TokenScope } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { Copy, Plus, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";

const SCOPE_TONE: Record<TokenScope, "neutral" | "info" | "warning"> = { read: "neutral", deploy: "info", admin: "warning" };

export default function TokensPage() {
  const { t, errorMessage, locale } = useLanguage();
  const toast = useToast();
  const [tokens, setTokens] = useState<ApiToken[] | null>(null);
  const [isAdmin, setIsAdmin] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [created, setCreated] = useState<(ApiToken & { token: string }) | null>(null);

  async function load() {
    try {
      const [list, me] = await Promise.all([api.listApiTokens(), api.me()]);
      setTokens(list);
      setIsAdmin(me.role === "admin");
      setError(null);
    } catch (e) {
      setError(errorMessage(e, "tokens.loadFailed"));
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function handleRevoke(token: ApiToken) {
    if (!confirm(t("tokens.revokeConfirm", { name: token.name }))) return;
    try {
      await api.revokeApiToken(token.id);
      await load();
      toast.show(t("tokens.revoked", { name: token.name }), "success");
    } catch (e) {
      setError(errorMessage(e, "common.deleteFailed"));
    }
  }

  const when = (value: string | null) => (value ? new Date(value).toLocaleString(locale) : null);

  return (
    <div>
      <div className="mb-6 flex items-center justify-between">
        <h1 className="flex items-center gap-2 text-2xl font-semibold text-slate-900">
          {t("tokens.title")}
          <InfoTip text={t("tokens.intro")} size={18} />
        </h1>
        <Button onClick={() => setShowCreate(true)}>
          <Plus size={16} />
          {t("tokens.new")}
        </Button>
      </div>

      {error && <p className="mb-4 text-sm text-red-600">{error}</p>}

      <Card className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-5 py-3 font-medium">{t("tokens.colName")}</th>
              <th className="px-5 py-3 font-medium">{t("tokens.colScope")}</th>
              {isAdmin && <th className="px-5 py-3 font-medium">{t("tokens.colOwner")}</th>}
              <th className="px-5 py-3 font-medium">{t("tokens.colExpires")}</th>
              <th className="px-5 py-3 font-medium">{t("tokens.colLastUsed")}</th>
              <th className="px-5 py-3 font-medium"><span className="sr-only">{t("common.actions")}</span></th>
            </tr>
          </thead>
          <tbody>
            {tokens === null &&
              [0, 1].map((i) => (
                <tr key={i} className="border-b border-slate-100 last:border-0">
                  <td className="px-5 py-3"><Skeleton className="h-4 w-28" /></td>
                  <td className="px-5 py-3"><Skeleton className="h-4 w-14 rounded-full" /></td>
                  <td className="px-5 py-3"><Skeleton className="h-4 w-24" /></td>
                  <td className="px-5 py-3"><Skeleton className="h-4 w-24" /></td>
                  <td className="px-5 py-3" />
                </tr>
              ))}
            {tokens?.length === 0 && (
              <tr>
                <td colSpan={6} className="px-5 py-8 text-center text-slate-500">{t("tokens.empty")}</td>
              </tr>
            )}
            {tokens?.map((token) => (
              <tr key={token.id} className="border-b border-slate-100 last:border-0">
                <td className="px-5 py-3">
                  <div className="font-medium text-slate-900">{token.name}</div>
                  <div className="font-mono text-xs text-slate-400">{token.prefix}…</div>
                </td>
                <td className="px-5 py-3"><Badge tone={SCOPE_TONE[token.scope]}>{t(`tokens.scope.${token.scope}`)}</Badge></td>
                {isAdmin && <td className="px-5 py-3 text-slate-600">{token.owner}</td>}
                <td className="px-5 py-3 text-slate-500">{when(token.expires_at) ?? t("tokens.never")}</td>
                <td className="px-5 py-3 text-slate-500">
                  {when(token.last_used_at) ?? <span className="text-slate-300">{t("tokens.neverUsed")}</span>}
                </td>
                <td className="px-5 py-3 text-right">
                  <Button variant="dangerSubtle" onClick={() => handleRevoke(token)}>
                    <Trash2 size={14} />
                    {t("tokens.revoke")}
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      {showCreate && (
        <CreateTokenModal
          isAdmin={isAdmin}
          onClose={() => setShowCreate(false)}
          onCreated={async (token) => {
            setShowCreate(false);
            setCreated(token);
            await load();
          }}
        />
      )}
      {created && <TokenCreatedModal token={created} onClose={() => setCreated(null)} />}
    </div>
  );
}

function CreateTokenModal({
  isAdmin,
  onClose,
  onCreated,
}: {
  isAdmin: boolean;
  onClose: () => void;
  onCreated: (token: ApiToken & { token: string }) => void;
}) {
  const { t, errorMessage } = useLanguage();
  const [name, setName] = useState("");
  const [scope, setScope] = useState<TokenScope>("read");
  const [days, setDays] = useState("90");
  const [password, setPassword] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const token = await api.createApiToken({
        name: name.trim(),
        scope,
        expires_in_days: days === "never" ? null : Number(days),
        current_password: password,
      });
      onCreated(token);
    } catch (err) {
      setError(errorMessage(err, "tokens.createFailed"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal title={t("tokens.new")} onClose={onClose}>
      <form onSubmit={handleSubmit} className="space-y-4">
        <FieldGroup label={t("tokens.colName")} hint={t("tokens.nameHint")}>
          <Input required maxLength={80} value={name} onChange={(e) => setName(e.target.value)} />
        </FieldGroup>
        <FieldGroup label={t("tokens.colScope")} hint={t(`tokens.scopeHint.${scope}`)}>
          <Select value={scope} onChange={(e) => setScope(e.target.value as TokenScope)}>
            <option value="read">{t("tokens.scope.read")}</option>
            {isAdmin && <option value="deploy">{t("tokens.scope.deploy")}</option>}
            {isAdmin && <option value="admin">{t("tokens.scope.admin")}</option>}
          </Select>
        </FieldGroup>
        <FieldGroup label={t("tokens.colExpires")}>
          <Select value={days} onChange={(e) => setDays(e.target.value)}>
            <option value="30">{t("tokens.days", { n: 30 })}</option>
            <option value="90">{t("tokens.days", { n: 90 })}</option>
            <option value="365">{t("tokens.days", { n: 365 })}</option>
            <option value="never">{t("tokens.never")}</option>
          </Select>
        </FieldGroup>
        <FieldGroup label={t("tokens.password")} hint={t("tokens.passwordHint")}>
          <Input required type="password" autoComplete="current-password" maxLength={1024} value={password} onChange={(e) => setPassword(e.target.value)} />
        </FieldGroup>
        {error && <p className="text-sm text-red-600">{error}</p>}
        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button type="submit" disabled={saving} loading={saving}>
            {saving ? t("common.saving") : t("tokens.create")}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function TokenCreatedModal({ token, onClose }: { token: ApiToken & { token: string }; onClose: () => void }) {
  const { t } = useLanguage();
  const toast = useToast();
  const example = `curl -H "Authorization: Bearer ${token.token}" ${typeof window === "undefined" ? "" : window.location.origin}/api/hosts`;

  function copy(text: string) {
    void navigator.clipboard?.writeText(text);
    toast.show(t("tokens.copied"), "success");
  }

  return (
    <Modal title={t("tokens.createdTitle", { name: token.name })} onClose={onClose}>
      <div className="space-y-4">
        <p className="text-sm text-amber-700">{t("tokens.shownOnce")}</p>
        <div className="flex items-start gap-2">
          <code className="flex-1 break-all rounded-md border border-slate-200 bg-slate-50 p-2 font-mono text-xs text-slate-900">{token.token}</code>
          <Button type="button" variant="outline" onClick={() => copy(token.token)}>
            <Copy size={14} />
            {t("tokens.copy")}
          </Button>
        </div>
        <div>
          <p className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-500">{t("tokens.example")}</p>
          <code className="block break-all rounded-md border border-slate-200 bg-slate-50 p-2 font-mono text-xs text-slate-700">{example}</code>
        </div>
        <div className="flex justify-end pt-2">
          <Button onClick={onClose}>{t("tokens.done")}</Button>
        </div>
      </div>
    </Modal>
  );
}
