import type { Metadata } from "next";
import { Inter } from "next/font/google";
import { AuthGate } from "@/components/AuthGate";
import { ThemeProvider } from "@/components/ThemeProvider";
import { LanguageProvider } from "@/components/LanguageProvider";
import { ToastProvider } from "@/components/ui/Toast";
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
    <html lang="de" suppressHydrationWarning className={inter.variable}>
      <head><script dangerouslySetInnerHTML={{__html: `(function(){try{var t=localStorage.getItem('opnexus-theme')||'system';var d=t==='dark'||(t==='system'&&matchMedia('(prefers-color-scheme: dark)').matches);document.documentElement.dataset.theme=d?'dark':'light';document.documentElement.style.colorScheme=d?'dark':'light';var l=localStorage.getItem('opnexus-language');document.documentElement.lang=l==='en'?'en':'de';}catch(e){}})();`}} /></head>
      <body className="min-h-screen bg-slate-50 font-sans text-slate-900 antialiased">
        <ThemeProvider><LanguageProvider><ToastProvider><AuthGate>{children}</AuthGate></ToastProvider></LanguageProvider></ThemeProvider>
      </body>
    </html>
  );
}
