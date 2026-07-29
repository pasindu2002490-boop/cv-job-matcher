import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptsDirectory = path.dirname(fileURLToPath(import.meta.url));
const frontendDirectory = path.resolve(scriptsDirectory, "..");

const build = spawn(process.execPath, [path.join(scriptsDirectory, "build.mjs")], {
  cwd: frontendDirectory,
  stdio: "inherit",
});

build.on("exit", (code) => {
  if (code !== 0) {
    process.exitCode = code ?? 1;
    return;
  }
  const server = spawn(
    process.platform === "win32" ? "npx.cmd" : "npx",
    ["--yes", "serve", "dist", "--listen", "4173"],
    { cwd: frontendDirectory, stdio: "inherit" },
  );
  server.on("exit", (serverCode) => {
    process.exitCode = serverCode ?? 0;
  });
});
