import clsx from "clsx";
import { Loader2 } from "lucide-react";
import { AnchorHTMLAttributes, ButtonHTMLAttributes } from "react";

type Variant = "primary" | "secondary" | "danger" | "ghost" | "outline" | "dangerSubtle";

const variantClasses: Record<Variant, string> = {
  primary: "border-transparent bg-brand-500 hover:bg-brand-600 text-white shadow-sm",
  secondary: "border-transparent bg-[rgb(var(--btn-secondary))] hover:bg-[rgb(var(--btn-secondary-hover))] text-white shadow-sm",
  danger: "border-transparent bg-red-600 hover:bg-red-700 text-white shadow-sm",
  ghost: "border-transparent bg-transparent hover:bg-slate-100 text-slate-600",
  // Secondary row actions (edit, delete): visible but quieter than the one primary action.
  outline: "border-slate-300 bg-transparent hover:bg-slate-100 text-slate-700",
  dangerSubtle: "border-transparent bg-transparent hover:bg-red-50 text-red-600",
};

const baseClasses =
  "inline-flex items-center gap-1.5 rounded-lg border px-3.5 py-2 text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed";

// `loading` shows a spinner before the label and disables the button, so a running
// push/save is visibly in progress rather than just a changed word.
type ButtonProps = { variant?: Variant; loading?: boolean } & (
  | ({ href?: undefined } & ButtonHTMLAttributes<HTMLButtonElement>)
  | ({ href: string } & AnchorHTMLAttributes<HTMLAnchorElement>)
);

export function Button({ variant = "primary", loading = false, className, children, ...props }: ButtonProps) {
  const classes = clsx(baseClasses, variantClasses[variant], className);
  const content = (
    <>
      {loading && <Loader2 size={16} aria-hidden="true" className="animate-spin motion-reduce:animate-none" />}
      {children}
    </>
  );
  if (props.href !== undefined) {
    return <a className={classes} {...(props as AnchorHTMLAttributes<HTMLAnchorElement>)}>{content}</a>;
  }
  const buttonProps = props as ButtonHTMLAttributes<HTMLButtonElement>;
  return (
    <button className={classes} {...buttonProps} disabled={buttonProps.disabled || loading} aria-busy={loading || undefined}>
      {content}
    </button>
  );
}
