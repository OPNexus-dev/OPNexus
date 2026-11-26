import type { Metadata } from "next";
import { Inter } from "next/font/google";
import { AuthGate } from "@/components/AuthGate";
import { ThemeProvider } from "@/components/ThemeProvider";
import { LanguageProvider } from "@/components/LanguageProvider";
import { ToastProvider } from "@/components/ui/Toast";
import { ConfirmProvider } from "@/components/ui/Confirm";
import "./globals.css";

// Declared as tailwind.config.ts's fontFamily.sans but was never actually
// loaded anywhere (no next/font, no <link>, no @font-face) — every page was
// silently falling back to the OS system font. This wires the CSS variable
// tailwind.config.ts now points font-sans at.
const inter = Inter({ subsets: ["latin"], variable: "--font-inter", display: "swap" });

export const metadata: Metadata = {
  title: "OPNexus",
  description: "Zentrale Verwaltung mehrerer OPNsense-Firewalls",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning className={inter.variable}>
      <head><script dangerouslySetInnerHTML={{__html: `(function(){try{var t=localStorage.getItem('opnexus-theme')||'system';var d=t==='dark'||(t==='system'&&matchMedia('(prefers-color-scheme: dark)').matches);document.documentElement.dataset.theme=d?'dark':'light';document.documentElement.style.colorScheme=d?'dark':'light';}catch(e){}try{var l=localStorage.getItem('opnexus-language');if(l!=='de'&&l!=='en'){var n=(navigator.languages&&navigator.languages[0])||navigator.language||'';l=n.toLowerCase().indexOf('de')===0?'de':'en';}document.documentElement.lang=l;}catch(e){}})();`}} /></head>
      <body className="min-h-screen bg-slate-50 font-sans text-slate-900 antialiased">
        <ThemeProvider><LanguageProvider><ToastProvider><ConfirmProvider><AuthGate>{children}</AuthGate></ConfirmProvider></ToastProvider></LanguageProvider></ThemeProvider>
      </body>
    </html>
  );
}
