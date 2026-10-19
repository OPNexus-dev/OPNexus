"use client";

import { InfoTip, Tooltip } from "@/components/ui/Tooltip";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { FieldGroup, Input, Select } from "@/components/ui/Field";
import { Modal } from "@/components/ui/Modal";
import { Skeleton } from "@/components/ui/Skeleton";
import { useToast } from "@/components/ui/Toast";
import { api, User, UserRole } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { useNotAdmin } from "@/lib/useReadOnly";
import { Pencil, Plus, ShieldCheck, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";

function RoleBadge({ role }: { role: UserRole }) {
  const { t } = useLanguage();
  const label = role === "admin" ? t("users.roleAdmin") : role === "operator" ? t("users.roleOperator") : t("users.roleReadOnly");
  return <Badge tone={role === "admin" ? "info" : role === "operator" ? "accent" : "neutral"}>{label}</Badge>;
}

export default function UsersPage() {
  const { t, errorMessage, locale } = useLanguage();
  const readOnly = useNotAdmin();
  const toast = useToast();
  const [users, setUsers] = useState<User[] | null>(null);
  const [ownUsername, setOwnUsername] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [editing, setEditing] = useState<User | null>(null);

  async function load() {
    try {
      const [userList, me] = await Promise.all([api.listUsers(), api.me()]);
      setUsers(userList);
      setOwnUsername(me.username ?? null);
      setError(null);
    } catch (e) {
      setError(errorMessage(e, "users.loadFailed"));
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function handleReset2fa(user: User) {
    if (!confirm(t("twofa.resetConfirm", { name: user.username }))) return;
    try {
      await api.resetUserTwoFactor(user.id);
      await load();
      toast.show(t("twofa.resetDone", { name: user.username }), "success");
    } catch (e) {
      setError(errorMessage(e, "common.saveFailed"));
    }
  }

  async function handleDelete(user: User) {
    if (!confirm(t("users.deleteConfirm", { name: user.username }))) return;
    try {
      await api.deleteUser(user.id);
      await load();
      toast.show(t("users.deleted", { name: user.username }), "success");
    } catch (e) {
      setError(errorMessage(e, "common.deleteFailed"));
    }
  }

  return (
    <fieldset disabled={readOnly} className="contents">
    <div>
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-slate-900 flex items-center gap-2">{t("users.title")}<InfoTip text={t("users.intro")} size={18} /></h1>
        <Button onClick={() => setShowCreate(true)}>
          <Plus size={16} />
          {t("users.new")}
        </Button>
      </div>

      {error && <p className="mb-4 text-sm text-red-600">{error}</p>}

      <Card className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-5 py-3 font-medium">{t("users.colUsername")}</th>
              <th className="px-5 py-3 font-medium">{t("users.colRole")}</th>
              <th className="px-5 py-3 font-medium">{t("users.colCreated")}</th>
              <th className="px-5 py-3 font-medium">{t("users.colLastLogin")}</th>
              <th className="px-5 py-3 font-medium"><span className="sr-only">{t("common.actions")}</span></th>
            </tr>
          </thead>
          <tbody>
            {users === null &&
              [0, 1].map((i) => (
                <tr key={i} className="border-b border-slate-100 last:border-0">
                  <td className="px-5 py-3"><Skeleton className="h-4 w-24" /></td>
                  <td className="px-5 py-3"><Skeleton className="h-4 w-14 rounded-full" /></td>
                  <td className="px-5 py-3"><Skeleton className="h-4 w-24" /></td>
                  <td className="px-5 py-3"><Skeleton className="h-4 w-24" /></td>
                  <td className="px-5 py-3" />
                </tr>
              ))}
            {users?.map((user) => {
              const isSelf = user.username === ownUsername;
              return (
                <tr key={user.id} className="border-b border-slate-100 last:border-0">
                  <td className="px-5 py-3 font-medium text-slate-900">
                    {user.username}
                    {isSelf && <span className="ml-2 text-xs font-normal text-slate-400">{t("users.you")}</span>}
                  </td>
                  <td className="px-5 py-3">
                    <span className="inline-flex flex-wrap items-center gap-1.5">
                      <RoleBadge role={user.role} />
                      {user.auth_source === "oidc" && <Tooltip content={t("users.ssoTip")} focusable><Badge tone="neutral">SSO</Badge></Tooltip>}
                      {user.totp_enabled && <Tooltip content={t("twofa.userOnTip")} focusable><Badge tone="success"><ShieldCheck size={12} />2FA</Badge></Tooltip>}
                    </span>
                  </td>
                  <td className="px-5 py-3 text-slate-500">{new Date(user.created_at).toLocaleDateString(locale)}</td>
                  <td className="px-5 py-3 text-slate-500">
                    {user.last_login_at ? new Date(user.last_login_at).toLocaleString(locale) : <span className="text-slate-300">—</span>}
                  </td>
                  <td className="px-5 py-3 text-right">
                    <div className="flex justify-end gap-2">
                      <Button variant="outline" disabled={user.auth_source === "oidc"} title={user.auth_source === "oidc" ? t("users.ssoTip") : undefined} onClick={() => setEditing(user)}>
                        <Pencil size={14} />
                        {t("rules.edit")}
                      </Button>
                      {user.totp_enabled && !isSelf && (
                        <Tooltip content={t("twofa.resetTip")}><Button variant="outline" onClick={() => handleReset2fa(user)}>{t("twofa.reset")}</Button></Tooltip>
                      )}
                      <Button
                        variant="dangerSubtle"
                        disabled={isSelf}
                        title={isSelf ? t("users.cannotDeleteSelf") : undefined}
                        onClick={() => handleDelete(user)}
                      >
                        <Trash2 size={14} />
                        {t("rules.delete")}
                      </Button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Card>

      {showCreate && (
        <CreateUserModal
          onClose={() => setShowCreate(false)}
          onCreated={async () => {
            setShowCreate(false);
            await load();
            toast.show(t("users.created"), "success");
          }}
        />
      )}

      {editing && (
        <EditUserModal
          user={editing}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null);
            await load();
            toast.show(t("users.updated", { name: editing.username }), "success");
          }}
        />
      )}
    </div>
    </fieldset>
  );
}

function CreateUserModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const { t, errorMessage } = useLanguage();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<UserRole>("read_only");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await api.createUser({ username, password, role });
      onCreated();
    } catch (err) {
      setError(errorMessage(err, "users.createFailed"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal title={t("users.new")} onClose={onClose}>
      <form onSubmit={handleSubmit} className="space-y-4">
        <FieldGroup label={t("users.colUsername")}>
          <Input required maxLength={150} value={username} onChange={(e) => setUsername(e.target.value)} />
        </FieldGroup>
        <FieldGroup label={t("users.password")}>
          <Input required type="password" minLength={10} maxLength={1024} value={password} onChange={(e) => setPassword(e.target.value)} />
        </FieldGroup>
        <FieldGroup label={t("users.colRole")}>
          <Select value={role} onChange={(e) => setRole(e.target.value as UserRole)}>
            <option value="read_only">{t("users.roleReadOnly")}</option>
            <option value="operator">{t("users.roleOperator")}</option>
            <option value="admin">{t("users.roleAdmin")}</option>
          </Select>
        </FieldGroup>
        {error && <p className="text-sm text-red-600">{error}</p>}
        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button type="submit" disabled={saving} loading={saving}>
            {saving ? t("common.saving") : t("users.createUser")}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function EditUserModal({ user, onClose, onSaved }: { user: User; onClose: () => void; onSaved: () => void }) {
  const { t, errorMessage } = useLanguage();
  const [role, setRole] = useState<UserRole>(user.role);
  const [newPassword, setNewPassword] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      if (role !== user.role) {
        await api.updateUserRole(user.id, role);
      }
      if (newPassword) {
        await api.resetUserPassword(user.id, newPassword);
      }
      onSaved();
    } catch (err) {
      setError(errorMessage(err, "common.saveFailed"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal title={t("users.editTitle", { name: user.username })} onClose={onClose}>
      <form onSubmit={handleSubmit} className="space-y-4">
        <FieldGroup label={t("users.colRole")}>
          <Select value={role} onChange={(e) => setRole(e.target.value as UserRole)}>
            <option value="read_only">{t("users.roleReadOnly")}</option>
            <option value="operator">{t("users.roleOperator")}</option>
            <option value="admin">{t("users.roleAdmin")}</option>
          </Select>
        </FieldGroup>
        <FieldGroup label={t("users.newPassword")}>
          <Input
            type="password"
            minLength={10}
            maxLength={1024}
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            placeholder={t("users.keepPassword")}
          />
        </FieldGroup>
        {error && <p className="text-sm text-red-600">{error}</p>}
        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button type="submit" variant="secondary" disabled={saving} loading={saving}>
            {saving ? t("common.saving") : t("common.save")}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
