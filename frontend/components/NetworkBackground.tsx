"use client";

import { useEffect, useRef } from "react";

type Node = { x: number; y: number; vx: number; vy: number };
type Pulse = { a: number; b: number; t: number; speed: number; warm: boolean };

const LINK_DISTANCE = 160;
const BLUE = "0, 115, 168";
const ORANGE = "217, 79, 0";

/**
 * Decorative drifting network graph for the login screen: nodes move slowly,
 * nearby nodes are linked, and small "packets" travel along links (blue =
 * allowed, an occasional orange one = blocked). Purely visual — aria-hidden,
 * pointer-events none, pauses while the tab is hidden, and with
 * prefers-reduced-motion it draws a single static frame instead of animating.
 */
export function NetworkBackground() {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;

    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let width = 0;
    let height = 0;
    let nodes: Node[] = [];
    const pulses: Pulse[] = [];
    let raf = 0;
    let last = 0;
    let sincePulse = 0;
    let lineRgb = "148, 163, 184";

    function readTheme() {
      const raw = getComputedStyle(document.documentElement).getPropertyValue("--slate-400").trim();
      const parts = raw.split(/\s+/);
      if (parts.length === 3) lineRgb = parts.join(", ");
    }

    function resize() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      width = canvas!.clientWidth;
      height = canvas!.clientHeight;
      canvas!.width = Math.floor(width * dpr);
      canvas!.height = Math.floor(height * dpr);
      ctx!.setTransform(dpr, 0, 0, dpr, 0, 0);
      const count = Math.max(18, Math.min(70, Math.floor((width * height) / 17000)));
      nodes = Array.from({ length: count }, () => ({
        x: Math.random() * width,
        y: Math.random() * height,
        vx: (Math.random() - 0.5) * 22,
        vy: (Math.random() - 0.5) * 22,
      }));
      pulses.length = 0;
      if (reduceMotion) draw();
    }

    function linked(i: number, j: number) {
      return Math.hypot(nodes[i].x - nodes[j].x, nodes[i].y - nodes[j].y) < LINK_DISTANCE;
    }

    function spawnPulse() {
      const a = Math.floor(Math.random() * nodes.length);
      const candidates: number[] = [];
      for (let j = 0; j < nodes.length; j += 1) if (j !== a && linked(a, j)) candidates.push(j);
      if (candidates.length === 0 || pulses.length > 14) return;
      pulses.push({ a, b: candidates[Math.floor(Math.random() * candidates.length)], t: 0, speed: 0.5 + Math.random() * 0.5, warm: Math.random() < 0.15 });
    }

    function draw() {
      ctx!.clearRect(0, 0, width, height);
      for (let i = 0; i < nodes.length; i += 1) {
        for (let j = i + 1; j < nodes.length; j += 1) {
          const distance = Math.hypot(nodes[i].x - nodes[j].x, nodes[i].y - nodes[j].y);
          if (distance >= LINK_DISTANCE) continue;
          ctx!.strokeStyle = `rgba(${lineRgb}, ${0.45 * (1 - distance / LINK_DISTANCE)})`;
          ctx!.lineWidth = 1;
          ctx!.beginPath();
          ctx!.moveTo(nodes[i].x, nodes[i].y);
          ctx!.lineTo(nodes[j].x, nodes[j].y);
          ctx!.stroke();
        }
      }
      ctx!.fillStyle = `rgba(${lineRgb}, 0.7)`;
      for (const node of nodes) {
        ctx!.beginPath();
        ctx!.arc(node.x, node.y, 2, 0, Math.PI * 2);
        ctx!.fill();
      }
      for (const pulse of pulses) {
        const from = nodes[pulse.a];
        const to = nodes[pulse.b];
        const x = from.x + (to.x - from.x) * pulse.t;
        const y = from.y + (to.y - from.y) * pulse.t;
        const color = pulse.warm ? ORANGE : BLUE;
        const glow = ctx!.createRadialGradient(x, y, 0, x, y, 12);
        glow.addColorStop(0, `rgba(${color}, 0.55)`);
        glow.addColorStop(1, `rgba(${color}, 0)`);
        ctx!.fillStyle = glow;
        ctx!.beginPath();
        ctx!.arc(x, y, 12, 0, Math.PI * 2);
        ctx!.fill();
        ctx!.fillStyle = `rgba(${color}, 0.95)`;
        ctx!.beginPath();
        ctx!.arc(x, y, 2.6, 0, Math.PI * 2);
        ctx!.fill();
      }
    }

    function frame(now: number) {
      const dt = Math.min(0.05, (now - last) / 1000 || 0);
      last = now;
      for (const node of nodes) {
        node.x += node.vx * dt;
        node.y += node.vy * dt;
        if (node.x < -20 || node.x > width + 20) node.vx *= -1;
        if (node.y < -20 || node.y > height + 20) node.vy *= -1;
      }
      sincePulse += dt;
      if (sincePulse > 0.7) {
        sincePulse = 0;
        spawnPulse();
      }
      for (let i = pulses.length - 1; i >= 0; i -= 1) {
        pulses[i].t += dt * pulses[i].speed;
        if (pulses[i].t >= 1 || !linked(pulses[i].a, pulses[i].b)) pulses.splice(i, 1);
      }
      draw();
      raf = requestAnimationFrame(frame);
    }

    function start() {
      if (reduceMotion || raf) return;
      last = performance.now();
      raf = requestAnimationFrame(frame);
    }
    function stop() {
      cancelAnimationFrame(raf);
      raf = 0;
    }
    function onVisibility() {
      if (document.hidden) stop();
      else start();
    }

    readTheme();
    resize();
    start();
    const themeObserver = new MutationObserver(() => {
      readTheme();
      if (reduceMotion) draw();
    });
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    window.addEventListener("resize", resize);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stop();
      themeObserver.disconnect();
      window.removeEventListener("resize", resize);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);

  return <canvas ref={canvasRef} aria-hidden="true" className="pointer-events-none fixed inset-0 h-full w-full" />;
}
