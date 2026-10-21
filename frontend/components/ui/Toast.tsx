"use client";
import clsx from "clsx";
import { CheckCircle2, Info, X, XCircle } from "lucide-react";
import { createContext, useCallback, useContext, useRef, useState } from "react";
import { useLanguage } from "@/components/LanguageProvider";

export type ToastTone = "success" | "danger" | "info";

interface ToastItem {
  id: number;
  tone: ToastTone;
  message: string;
}

interface ToastContextValue {
  show: (message: string, tone?: ToastTone) => void;
}

const ToastContext = createContext<ToastContextValue>({ show: () => {} });

const toneIcon: Record<ToastTone, typeof CheckCircle2> = {
  success: CheckCircle2,
  danger: XCircle,
  info: Info,
};

const toneClasses: Record<ToastTone, string> = {
  success:
    "bg-[rgb(var(--tone-success-bg))] text-[rgb(var(--tone-success-text))] border-[rgb(var(--tone-success-border))]",
  danger:
    "bg-[rgb(var(--tone-danger-bg))] text-[rgb(var(--tone-danger-text))] border-[rgb(var(--tone-danger-border))]",
  info: "bg-[rgb(var(--tone-info-bg))] text-[rgb(var(--tone-info-text))] border-[rgb(var(--tone-info-border))]",
};

/**
 * App-wide toast host + `useToast()` hook, mounted once in app/layout.tsx.
 * Fills the "no feedback system for actions" gap — pages currently show
 * success/error only as inline text next to the triggering button, which is
 * easy to miss and disappears the moment the surrounding state re-renders.
 * Purely additive: no existing page calls useToast() yet, this only wires up
 * the host so pages can adopt it incrementally.
 */
export function ToastProvider({ children }: { children: React.ReactNode }) {
  const { t } = useLanguage();
  const [items, setItems] = useState<ToastItem[]>([]);
  const nextId = useRef(0);

  const dismiss = useCallback((id: number) => {
    setItems((current) => current.filter((item) => item.id !== id));
  }, []);

  const show = useCallback(
    (message: string, tone: ToastTone = "info") => {
      const id = nextId.current++;
      setItems((current) => [...current, { id, tone, message }]);
      setTimeout(() => dismiss(id), 5000);
    },
    [dismiss]
  );

  return (
    <ToastContext.Provider value={{ show }}>
      {children}
      <div className="pointer-events-none fixed inset-x-0 bottom-4 z-[100] flex flex-col items-center gap-2 px-4 sm:inset-x-auto sm:right-4 sm:items-end">
        {items.map((item) => {
          const Icon = toneIcon[item.tone];
          return (
            <div
              key={item.id}
              role="status"
              className={clsx(
                "pointer-events-auto flex w-full max-w-sm items-start gap-2 rounded-xl border px-4 py-3 text-sm shadow-lg",
                toneClasses[item.tone]
              )}
            >
              <Icon size={18} className="mt-0.5 shrink-0" />
              <p className="flex-1">{item.message}</p>
              <button
                onClick={() => dismiss(item.id)}
                aria-label={t("common.close")} title={t("common.close")}
                className="shrink-0 opacity-70 hover:opacity-100"
              >
                <X size={16} />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  return useContext(ToastContext);
}
