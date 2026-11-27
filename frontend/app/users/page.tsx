"use client";

import { useChanged } from "@/lib/useDirty";
import { DataTable } from "@/components/ui/DataTable";
import { PageHeader } from "@/components/ui/PageHeader";
import { useConfirm } from "@/components/ui/Confirm";
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
import { Pencil, Plus, ShieldCheck, ShieldOff, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";

function RoleBadge({ role }: { role: UserRole }) {
  const { t } = useLanguage();
  const label = role === "admin" ? t("users.roleAdmin") : role === "operator" ? t("users.roleOperator") : t("users.roleReadOnly");
  return <Badge tone={role === "admin" ? "info" : role === "operator" ? "accent" : "neutral"}>{label}</Badge>;
}

export default function UsersPage() {
  const { t, errorMessage, locale } = useLanguage();
  const confirmAction = useConfirm();
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
    if (!(await confirmAction(t("twofa.resetConfirm", { name: user.username }), { danger: true }))) return;
    try {
      await api.resetUserTwoFactor(user.id);
      await load();
      toast.show(t("twofa.resetDone", { name: user.username }), "success");
    } catch (e) {
      setError(errorMessage(e, "common.saveFailed"));
    }
  }

  async function handleDelete(user: User) {
    if (!(await confirmAction(t("users.deleteConfirm", { name: user.username }), { danger: true }))) return;
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
      <PageHeader
        title={t("users.title")}
        info={t("users.intro")}
        actions={
          <Button onClick={() => setShowCreate(true)}>
            <Plus size={16} />
            {t("users.new")}
          </Button>
        }
      />

      {error && <p className="mb-4 text-sm text-red-600">{error}</p>}

      {users === null && <Card className="space-y-3 p-5" aria-busy="true">{[0, 1].map((i) => <Skeleton key={i} className="h-5 w-full" />)}</Card>}
      {users && (
        <DataTable
          label={t("users.title")}
          rows={users}
          rowKey={(user) => user.id}
          columns={[
            {
              key: "username", header: t("users.colUsername"), priority: "primary", isTitle: true,
              cell: (user) => (<>{user.username}{user.username === ownUsername && <span className="ml-2 text-xs font-normal text-slate-400">{t("users.you")}</span>}</>),
            },
            {
              key: "role", header: t("users.colRole"), priority: "primary",
              cell: (user) => (
                <span className="inline-flex flex-wrap items-center gap-1.5">
                  <RoleBadge role={user.role} />
                  {user.auth_source === "oidc" && <Tooltip content={t("users.ssoTip")} focusable><Badge tone="neutral">SSO</Badge></Tooltip>}
                  {user.totp_enabled && <Tooltip content={t("twofa.userOnTip")} focusable><Badge tone="success"><ShieldCheck size={12} />2FA</Badge></Tooltip>}
                </span>
              ),
            },
            { key: "created", header: t("users.colCreated"), priority: "medium", className: "text-slate-500", cell: (user) => new Date(user.created_at).toLocaleDateString(locale) },
            { key: "lastLogin", header: t("users.colLastLogin"), priority: "high", className: "text-slate-500", cell: (user) => (user.last_login_at ? new Date(user.last_login_at).toLocaleString(locale) : <span className="text-slate-300">—</span>) },
          ]}
          actions={(user) => {
            const isSelf = user.username === ownUsername;
            return [
              { key: "edit", label: t("rules.edit"), icon: Pencil, disabled: user.auth_source === "oidc", onSelect: () => setEditing(user) },
              ...(user.totp_enabled && !isSelf ? [{ key: "reset2fa", label: t("twofa.reset"), icon: ShieldOff, onSelect: () => handleReset2fa(user) }] : []),
              { key: "delete", label: t("rules.delete"), icon: Trash2, tone: "danger" as const, disabled: isSelf, onSelect: () => handleDelete(user) },
            ];
          }}
        />
      )}

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
  const changed = useChanged({ role, newPassword });

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
          <Button type="submit" variant="secondary" disabled={saving || !changed} loading={saving}>
            {saving ? t("common.saving") : t("common.save")}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
