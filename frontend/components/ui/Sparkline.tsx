"use client";

import { useId, useRef, useState } from "react";
import { useLanguage } from "@/components/LanguageProvider";

export interface SparklinePoint {
  t: string;
  v: number;
}

interface SparklineProps {
  label: string;
  points: SparklinePoint[];
  format?: (v: number) => string;
  height?: number;
}

// Single hue, magnitude-over-time, one series per chart — reuses the
// app's existing --tone-info token (sky), already validated for both
// themes via the Badge component. No y-axis ticks: the current value is
// direct-labeled at the line's end instead (see dataviz skill's
// marks-and-anatomy.md, "keep them unless every value is labeled").
// X positions are evenly spaced by sample index, not real elapsed time —
// acceptable for a compact 24h trend view where poll jitter is at most a
// few seconds against a 60s interval; a real time-scaled axis is a
// possible follow-up, not a correctness issue at this sample rate.
export function Sparkline({ label, points, format = (v) => String(v), height = 64 }: SparklineProps) {
  const { t, locale } = useLanguage();
  const clipId = useId();
  const svgRef = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<number | null>(null);
  const width = 100; // viewBox units; scales with the SVG's rendered width

  if (points.length === 0) {
    return (
      <div>
        <div className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-500">{label}</div>
        <div className="flex h-16 items-center justify-center text-xs text-slate-400">{t("sparkline.noData")}</div>
      </div>
    );
  }

  const values = points.map((p) => p.v);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const pad = (max - min) * 0.1 || 1;
  const domainMin = min - pad;
  const domainMax = max + pad;
  const x = (i: number) => (points.length === 1 ? width / 2 : (i / (points.length - 1)) * width);
  const y = (v: number) => height - ((v - domainMin) / (domainMax - domainMin)) * height;

  const linePath = points.map((p, i) => `${i === 0 ? "M" : "L"} ${x(i).toFixed(2)} ${y(p.v).toFixed(2)}`).join(" ");
  const areaPath = `${linePath} L ${x(points.length - 1).toFixed(2)} ${height} L ${x(0)} ${height} Z`;

  function handleMove(e: React.MouseEvent<SVGSVGElement>) {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return;
    const ratio = (e.clientX - rect.left) / rect.width;
    const idx = Math.round(ratio * (points.length - 1));
    setHover(Math.max(0, Math.min(points.length - 1, idx)));
  }

  const current = points[points.length - 1];
  const hovered = hover !== null ? points[hover] : null;

  return (
    <div>
      <div className="mb-1 flex items-baseline justify-between">
        <span className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</span>
        <span className="text-sm font-semibold text-slate-900">{format(current.v)}</span>
      </div>
      <div className="relative">
        <svg
          ref={svgRef}
          viewBox={`0 0 ${width} ${height}`}
          preserveAspectRatio="none"
          className="h-16 w-full cursor-crosshair"
          onMouseMove={handleMove}
          onMouseLeave={() => setHover(null)}
        >
          <defs>
            <clipPath id={clipId}>
              <rect x={0} y={0} width={width} height={height} />
            </clipPath>
          </defs>
          <line x1={0} y1={height - 0.5} x2={width} y2={height - 0.5} stroke="rgb(var(--tone-neutral-border))" strokeWidth={1} />
          <g clipPath={`url(#${clipId})`}>
            <path d={areaPath} fill="rgb(var(--tone-info-text) / 0.12)" stroke="none" />
            <path d={linePath} fill="none" stroke="rgb(var(--tone-info-text))" strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
          </g>
          {hover !== null && (
            <line x1={x(hover)} y1={0} x2={x(hover)} y2={height} stroke="rgb(var(--tone-neutral-border))" strokeWidth={1} />
          )}
          {hover !== null && (
            <circle cx={x(hover)} cy={y(points[hover].v)} r={3} fill="rgb(var(--tone-info-text))" stroke="rgb(var(--tone-info-bg))" strokeWidth={1.5} />
          )}
        </svg>
        {hovered && (
          <div
            className="pointer-events-none absolute -top-1 z-10 -translate-y-full whitespace-nowrap rounded-md border border-slate-200 bg-white px-2 py-1 text-xs shadow-sm"
            style={{ left: `${(hover! / Math.max(points.length - 1, 1)) * 100}%`, transform: "translate(-50%, -100%)" }}
          >
            <div className="font-medium text-slate-900">{format(hovered.v)}</div>
            <div className="text-slate-400">{new Date(hovered.t).toLocaleTimeString(locale)}</div>
          </div>
        )}
      </div>
    </div>
  );
}
