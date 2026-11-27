// Fake window.matchMedia for a given viewport width (and pointer type) so components that call useBreakpoint() can be tested.
type Listener = () => void;
const listeners = new Set<Listener>();
let width = 1280;
let coarse = false;

function matches(query: string): boolean {
  const min = /\(min-width:\s*(\d+)px\)/.exec(query);
  if (min) return width >= Number(min[1]);
  const max = /\(max-width:\s*(\d+)px\)/.exec(query);
  if (max) return width <= Number(max[1]);
  if (/pointer:\s*coarse/.test(query)) return coarse;
  return false;
}

export function setViewport(w: number, options: { coarse?: boolean } = {}) {
  width = w;
  coarse = options.coarse ?? false;
  listeners.forEach((l) => l());
}

Object.defineProperty(window, "matchMedia", {
  writable: true,
  configurable: true,
  value: (query: string) => ({
    get matches() { return matches(query); },
    media: query, onchange: null,
    addEventListener: (_: string, l: Listener) => listeners.add(l),
    removeEventListener: (_: string, l: Listener) => listeners.delete(l),
    addListener: (l: Listener) => listeners.add(l),
    removeListener: (l: Listener) => listeners.delete(l),
    dispatchEvent: () => false,
  }),
});
