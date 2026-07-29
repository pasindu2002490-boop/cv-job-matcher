import "dotenv/config";

import { mkdir, rm, copyFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { build } from "esbuild";

const scriptsDirectory = path.dirname(fileURLToPath(import.meta.url));
const frontendDirectory = path.resolve(scriptsDirectory, "..");
const outputDirectory = path.join(frontendDirectory, "dist");

await rm(outputDirectory, { recursive: true, force: true });
await mkdir(outputDirectory, { recursive: true });

await Promise.all([
  copyFile(path.join(frontendDirectory, "index.html"), path.join(outputDirectory, "index.html")),
  copyFile(
    path.join(frontendDirectory, "src", "styles.css"),
    path.join(outputDirectory, "styles.css"),
  ),
  build({
    entryPoints: [path.join(frontendDirectory, "src", "app.js")],
    outfile: path.join(outputDirectory, "app.js"),
    bundle: true,
    format: "esm",
    minify: true,
    sourcemap: false,
    target: ["es2020"],
    legalComments: "none",
  }),
]);

const runtimeConfig = {
  apiBaseUrl: readString("VITE_API_BASE_URL", "http://localhost:8080"),
  supabaseUrl: readString("VITE_SUPABASE_URL", ""),
  supabasePublishableKey: readString(
    "VITE_SUPABASE_PUBLISHABLE_KEY",
    readString("VITE_SUPABASE_ANON_KEY", ""),
  ),
  allowAnonymous: readBoolean("VITE_ALLOW_ANONYMOUS", false),
  pollIntervalMs: readInteger("VITE_POLL_INTERVAL_MS", 2500),
  maxCvBytes: readInteger("VITE_MAX_CV_BYTES", 10 * 1024 * 1024),
};

const configSource =
  `// Generated during the Netlify build. Contains public browser configuration only.\n` +
  `window.__CV_MATCHER_CONFIG__ = Object.freeze(${JSON.stringify(runtimeConfig, null, 2)});\n`;
await writeFile(path.join(outputDirectory, "config.js"), configSource, "utf8");

console.log(`Frontend built in ${outputDirectory}`);

function readString(name, fallback) {
  const value = process.env[name]?.trim();
  return value || fallback;
}

function readBoolean(name, fallback) {
  const value = process.env[name]?.trim().toLowerCase();
  if (!value) {
    return fallback;
  }
  return ["1", "true", "yes", "on"].includes(value);
}

function readInteger(name, fallback) {
  const value = Number.parseInt(process.env[name] ?? "", 10);
  return Number.isFinite(value) && value > 0 ? value : fallback;
}
