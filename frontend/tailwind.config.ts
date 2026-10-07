import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ["var(--font-inter)", "system-ui", "sans-serif"],
      },
      colors: {
        // OPNexus's own accent, anchored on the wordmark's "network blue"
        // (#0073A8, see components/Logo.tsx) instead of stock Tailwind
        // `sky` — same role (primary actions, focus rings, active tab),
        // just tied to the actual brand instead of a generic default.
        brand: {
          50: "#EAF6FB",
          100: "#CFEAF5",
          200: "#9FD5EC",
          300: "#66B9DD",
          400: "#2E93C4",
          500: "#0073A8",
          600: "#005E89",
          700: "#004A6E",
          800: "#003A57",
          900: "#062D42",
          950: "#031D2B",
        },
        // The wordmark's other brand color ("OPNsense orange", #D94F00),
        // replacing stock Tailwind `amber` wherever it was used for
        // warnings/highlights — same warm, attention-grabbing role, just
        // on-brand instead of default.
        flame: {
          50: "#FDF1EA",
          100: "#FBDFCB",
          200: "#F6BC97",
          300: "#EF9A61",
          400: "#E8752F",
          500: "#D94F00",
          600: "#B34200",
          700: "#8C3400",
          800: "#692700",
          900: "#481B00",
          950: "#2C1000",
        },
        // Backed by CSS variables (see globals.css) so dark mode flips the
        // whole scale in one place. This composes correctly with opacity
        // modifiers (`bg-slate-50/60`) and arbitrary variants, unlike the
        // old per-class-name dark-mode override table it replaces.
        slate: {
          50: "rgb(var(--slate-50) / <alpha-value>)",
          100: "rgb(var(--slate-100) / <alpha-value>)",
          200: "rgb(var(--slate-200) / <alpha-value>)",
          300: "rgb(var(--slate-300) / <alpha-value>)",
          400: "rgb(var(--slate-400) / <alpha-value>)",
          500: "rgb(var(--slate-500) / <alpha-value>)",
          600: "rgb(var(--slate-600) / <alpha-value>)",
          700: "rgb(var(--slate-700) / <alpha-value>)",
          800: "rgb(var(--slate-800) / <alpha-value>)",
          900: "rgb(var(--slate-900) / <alpha-value>)",
          950: "rgb(var(--slate-950) / <alpha-value>)",
        },
      },
    },
  },
  plugins: [],
};

export default config;
