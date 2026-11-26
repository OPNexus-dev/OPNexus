"use client";

import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { useLanguage } from "@/components/LanguageProvider";
import { createContext, ReactNode, useCallback, useContext, useRef, useState } from "react";

type ConfirmOptions = { danger?: boolean; confirmLabel?: string };
type ConfirmFn = (message: string, options?: ConfirmOptions) => Promise<boolean>;

const ConfirmContext = createContext<ConfirmFn | null>(null);

/**
 * One confirmation dialog for the whole app, instead of the browser's native confirm(): it follows the theme and the
 * language, can be reached by keyboard (Escape cancels) and says what the action will do. Usage:
 *   const confirmAction = useConfirm();   ...   if (!(await confirmAction(a translated question, { danger: true }))) return;
 */
export function ConfirmProvider({ children }: { children: ReactNode }) {
  const { t } = useLanguage();
  const [open, setOpen] = useState<{ message: string; options: ConfirmOptions } | null>(null);
  const resolver = useRef<((value: boolean) => void) | null>(null);

  const confirmAction = useCallback<ConfirmFn>((message, options = {}) => new Promise<boolean>((resolve) => {
    resolver.current?.(false);
    resolver.current = resolve;
    setOpen({ message, options });
  }), []);

  function answer(value: boolean) {
    resolver.current?.(value);
    resolver.current = null;
    setOpen(null);
  }

  return (
    <ConfirmContext.Provider value={confirmAction}>
      {children}
      {open && (
        <Modal title={t("common.confirmTitle")} onClose={() => answer(false)}>
          <p className="mb-5 whitespace-pre-line break-words text-sm text-slate-700">{open.message}</p>
          <div className="flex flex-wrap justify-end gap-2">
            <Button variant="ghost" onClick={() => answer(false)}>{t("common.cancel")}</Button>
            <Button variant={open.options.danger ? "danger" : "primary"} onClick={() => answer(true)}>{open.options.confirmLabel ?? t("common.confirm")}</Button>
          </div>
        </Modal>
      )}
    </ConfirmContext.Provider>
  );
}

export function useConfirm(): ConfirmFn {
  const confirmAction = useContext(ConfirmContext);
  if (!confirmAction) throw new Error("useConfirm needs a ConfirmProvider");
  return confirmAction;
}
