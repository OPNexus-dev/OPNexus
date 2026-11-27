import { act, renderHook } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { useChanged, useDirty } from "@/lib/useDirty";

// A form like the settings sections: `apply(server)` fills the fields and stores the server object (the version) in one update.
function useForm() {
  const [cfg, setCfg] = useState<{ n: number } | null>(null);
  const [host, setHost] = useState("");
  const [secret, setSecret] = useState("");
  const apply = (server: { n: number; host: string }) => { setCfg({ n: server.n }); setHost(server.host); setSecret(""); };
  const dirty = useDirty({ host, secret }, cfg);
  return { dirty, host, setHost, secret, setSecret, apply };
}

describe("useDirty", () => {
  it("is clean while the server state is still loading, whatever is typed", () => {
    const { result } = renderHook(() => useForm());
    expect(result.current.dirty).toBe(false);
    act(() => result.current.setHost("typed early"));
    expect(result.current.dirty).toBe(false);
  });

  it("is clean right after the server state is applied", () => {
    const { result } = renderHook(() => useForm());
    act(() => result.current.apply({ n: 1, host: "syslog.lan" }));
    expect(result.current.host).toBe("syslog.lan");
    expect(result.current.dirty).toBe(false);
  });

  it("becomes dirty when a field changes and clean again when it is changed back", () => {
    const { result } = renderHook(() => useForm());
    act(() => result.current.apply({ n: 1, host: "a" }));
    act(() => result.current.setHost("b"));
    expect(result.current.dirty).toBe(true);
    act(() => result.current.setHost("a"));
    expect(result.current.dirty).toBe(false);
  });

  it("counts a typed secret as a change (secrets are write-only and always start empty)", () => {
    const { result } = renderHook(() => useForm());
    act(() => result.current.apply({ n: 1, host: "a" }));
    act(() => result.current.setSecret("s3cret"));
    expect(result.current.dirty).toBe(true);
  });

  it("is clean again after a save (the answer of the server is applied), but stays dirty when the save fails (nothing is applied)", () => {
    const { result } = renderHook(() => useForm());
    act(() => result.current.apply({ n: 1, host: "a" }));
    act(() => { result.current.setHost("b"); result.current.setSecret("x"); });
    expect(result.current.dirty).toBe(true);
    // failed save: no apply() call
    expect(result.current.dirty).toBe(true);
    // successful save: the server answers with the saved values; the secret field is cleared by apply()
    act(() => result.current.apply({ n: 2, host: "b" }));
    expect(result.current.dirty).toBe(false);
  });

  it("a reload of the same values from the server is clean too (new version, same content)", () => {
    const { result } = renderHook(() => useForm());
    act(() => result.current.apply({ n: 1, host: "a" }));
    act(() => result.current.setHost("edited"));
    act(() => result.current.apply({ n: 3, host: "a" }));
    expect(result.current.dirty).toBe(false);
    expect(result.current.host).toBe("a");
  });
});

describe("useChanged", () => {
  function useDialog(initial: { role: string }) {
    const [role, setRole] = useState(initial.role);
    const [password, setPassword] = useState("");
    return { changed: useChanged({ role, password }), setRole, setPassword };
  }

  it("is clean when the dialog opens", () => {
    const { result } = renderHook(() => useDialog({ role: "admin" }));
    expect(result.current.changed).toBe(false);
  });

  it("is changed after an edit and clean again when it is undone", () => {
    const { result } = renderHook(() => useDialog({ role: "admin" }));
    act(() => result.current.setRole("operator"));
    expect(result.current.changed).toBe(true);
    act(() => result.current.setRole("admin"));
    expect(result.current.changed).toBe(false);
  });

  it("counts typing into a field that starts empty (a new password)", () => {
    const { result } = renderHook(() => useDialog({ role: "admin" }));
    act(() => result.current.setPassword("x"));
    expect(result.current.changed).toBe(true);
  });
});
