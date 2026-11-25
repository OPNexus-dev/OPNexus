// Node half of scripts/check_server_texts.py: which of the German sample texts does the web interface translate?
// Usage: node scripts/check_server_texts.cjs <samples.json>   (needs frontend/node_modules for TypeScript)
const fs = require("fs"), os = require("os"), path = require("path");
const root = path.resolve(__dirname, "..");
let ts;
try { ts = require(process.env.TS_MODULE || path.join(root, "frontend/node_modules/typescript")); }
catch { console.error("TypeScript not found: run `npm ci` in frontend/ first."); process.exit(2); }
const src = path.join(root, "frontend/lib/i18n");
const out = fs.mkdtempSync(path.join(os.tmpdir(), "opnexus-i18n-"));
for (const f of ["index.ts", "serverMessages.ts"]) {
  const js = ts.transpileModule(fs.readFileSync(path.join(src, f), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true, resolveJsonModule: true } }).outputText;
  fs.writeFileSync(path.join(out, f.replace(".ts", ".js")), js);
}
for (const f of ["de.json", "en.json"]) fs.copyFileSync(path.join(src, f), path.join(out, f));
const { translateServerText } = require(path.join(out, "index.js"));
const GERMAN = /[äöüÄÖÜß]|\b(nicht|wurde|werden|wird|konnte|bitte|keine|kein|noch|prüfen|bereits|sind|und|oder|dem|den|mit|von|nach|wenn|weil|erst)\b/;
const samples = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const bad = samples.filter((s) => { const en = translateServerText("en", s.sample); return en === s.sample || GERMAN.test(en); });
fs.rmSync(out, { recursive: true, force: true });
console.log(JSON.stringify(bad));
