// Inlined directly as JSX rather than a public/ static asset — it's a
// simple text wordmark (no raster/paths), and inlining sidesteps the
// standalone Next.js build's runner stage needing an extra `COPY` for
// public/ (it currently only copies .next/standalone + .next/static).
export function Logo({ className }: { className?: string }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="142"
      height="40"
      viewBox="0 0 142 40"
      role="img"
      aria-labelledby="opnexus-logo-title opnexus-logo-desc"
      className={className}
    >
      <title id="opnexus-logo-title">OPNexus</title>
      <desc id="opnexus-logo-desc">OPNexus wordmark in OPNsense orange and network blue</desc>
      <text
        x="1"
        y="27.5"
        fontFamily="Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
        fontSize="24"
        fontWeight="750"
        letterSpacing="-0.8"
      >
        <tspan fill="#D94F00" fontWeight="800">
          OPN
        </tspan>
        <tspan fill="#0073A8">exus</tspan>
      </text>
    </svg>
  );
}
