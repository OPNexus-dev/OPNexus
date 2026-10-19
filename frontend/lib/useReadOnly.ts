"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

// True once we know the session's role is read_only. Starts false (not
// "loading") deliberately: pages wrap their write controls in
// <fieldset disabled={readOnly}>, and briefly enabling controls that get
// disabled a moment later is far less confusing than a page that flashes
// everything disabled-then-enabled for an admin on every load.
export function useReadOnly(): boolean {
  const [readOnly, setReadOnly] = useState(false);

  useEffect(() => {
    let active = true;
    api
      .me()
      .then((state) => {
        if (active) setReadOnly(state.role === "read_only");
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, []);

  return readOnly;
}

// True once we know the session's role is NOT admin (read_only or operator). For the
// few things only an admin may do: users, settings, hosts, OPNexus updates, raw backups.
// Same "false until known" rule as useReadOnly.
export function useNotAdmin(): boolean {
  const [notAdmin, setNotAdmin] = useState(false);

  useEffect(() => {
    let active = true;
    api
      .me()
      .then((state) => {
        if (active) setNotAdmin(state.role !== undefined && state.role !== "admin");
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, []);

  return notAdmin;
}
