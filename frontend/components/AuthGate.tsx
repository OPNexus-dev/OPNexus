"use client";

import { CommandPalette } from "@/components/CommandPalette";
import { LoginInfoBanner } from "@/components/LoginInfoBanner";
import { UpdateBanner } from "@/components/UpdateBanner";
import { Nav } from "@/components/Nav";
import { api } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { APP_CODENAME, APP_VERSION } from "@/lib/version";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { ReactNode, useEffect, useState } from "react";
import { matchesNav, navGroups, navLabelKey } from "@/components/Nav";

/**
 * Single-role auth guard — there's exactly one protected "area" (everything
 * except /login) and one user, so this checks GET /api/auth/me once on
 * mount and redirects if it isn't authenticated. Also owns the Nav/main
 * chrome, since /login deliberately renders standalone without it.
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const { t } = useLanguage();
  const pathname = usePathname();
  const router = useRouter();
  const [checked, setChecked] = useState(false);
  const [authenticated, setAuthenticated] = useState(false);

  useEffect(() => {
    if (pathname === "/login") return;
    api
      .me()
      .then((state) => {
        setAuthenticated(state.authenticated);
        if (!state.authenticated) router.replace("/login");
      })
      .catch(() => router.replace("/login"))
      .finally(() => setChecked(true));
  }, [pathname, router]);

  // The app is a client-rendered SPA with one static metadata title; screen reader users rely on
  // the title (and its change) to know a navigation happened and where they are. A rendered
  // <title> is hoisted into <head> by React 19 and wins over the static metadata one.
  const item = navGroups.flatMap((g) => g.items).find((i) => matchesNav(pathname, i.href));
  const pageName = item ? t(navLabelKey(item.href)) : pathname === "/login" ? t("login.signIn") : pathname?.startsWith("/about") ? t("settings.about") : null;
  const title = <title>{pageName ? pageName.includes("OPNexus") ? pageName : `${pageName} · OPNexus` : "OPNexus"}</title>;

  if (pathname === "/login") return <>{title}{children}</>;
  if (!checked || !authenticated) return null;

  return (
    <>
      {title}
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-[60] focus:rounded-lg focus:bg-white focus:px-4 focus:py-2 focus:text-sm focus:font-medium focus:text-slate-900 focus:shadow-lg"
      >
        {t("common.skipToContent")}
      </a>
      <Nav />
      <CommandPalette />
      <LoginInfoBanner />
      <UpdateBanner />
      <main id="main-content" tabIndex={-1} className="mx-auto max-w-6xl px-4 py-6 outline-none sm:px-6 sm:py-8">{children}</main>
      <footer className="mx-auto max-w-6xl px-4 pb-6 text-center text-xs text-slate-400 sm:px-6">
        <Link href="/about" className="hover:text-slate-500">
          {t("common.versionLabel", { version: APP_VERSION, codename: APP_CODENAME })}
        </Link>
      </footer>
    </>
  );
}
