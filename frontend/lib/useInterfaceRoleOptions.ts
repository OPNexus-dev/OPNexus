"use client";

import { useEffect, useState } from "react";
import { api, InterfaceOption, InterfaceRole } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";

/** The interface roles in use, as extra choices for the interface field: the value is the placeholder {{IF_NAME}}. */
export function useInterfaceRoleOptions(): InterfaceOption[] {
  const { t } = useLanguage();
  const [roles, setRoles] = useState<InterfaceRole[]>([]);
  useEffect(() => {
    api.getInterfaceRoles().then((r) => setRoles(r.roles)).catch(() => setRoles([]));
  }, []);
  return roles.map((role) => ({
    name: `{{${role.name}}}`,
    label: t("roles.option", {
      name: role.name.replace(/^IF_/, ""),
      hosts: Object.entries(role.hosts).map(([host, id]) => `${host}: ${id}`).join(", "),
    }),
  }));
}
