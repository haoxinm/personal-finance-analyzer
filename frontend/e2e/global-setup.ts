import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import * as readline from "node:readline";
import { fileURLToPath } from "node:url";

/**
 * Boots the real backend for D9's Playwright happy path (§2, Phase 3 Gate 3): a fresh, throwaway
 * `PFA_HOME`, LLM mode "none" (D2 — the happy path never calls an LLM), migrated and
 * serving whatever is currently built into `src/personal_finance_analyzer/web/` (`npm run build` first —
 * CI does). Reads the server's own printed per-launch URL and token (A31) from its stdout rather
 * than hard-coding either, and tears the process down again in the function this returns.
 */
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..", "..");
const READY_LINE = /Personal Finance Analyzer is running at (http:\/\/127\.0\.0\.1:\d+\/)\?token=(\S+)/;

function run(command: string, args: string[], env: NodeJS.ProcessEnv): Promise<void> {
  return new Promise((resolve, reject) => {
    const proc = spawn(command, args, { cwd: REPO_ROOT, env, stdio: "inherit" });
    proc.on("error", reject);
    proc.on("exit", (code) => {
      if (code === 0) resolve();
      else reject(new Error(`${command} ${args.join(" ")} exited with code ${code}`));
    });
  });
}

function startServer(
  env: NodeJS.ProcessEnv,
): Promise<{ proc: ChildProcessWithoutNullStreams; baseUrl: string; token: string }> {
  return new Promise((resolve, reject) => {
    const proc = spawn("uv", ["run", "pfa", "serve", "--no-open-browser"], {
      cwd: REPO_ROOT,
      env,
    });
    const rl = readline.createInterface({ input: proc.stdout });
    const timeout = setTimeout(() => {
      rl.close();
      reject(new Error("timed out waiting for the server's ready line on stdout"));
    }, 30_000);

    rl.on("line", (line) => {
      process.stdout.write(`[server] ${line}\n`);
      const match = READY_LINE.exec(line);
      if (match) {
        clearTimeout(timeout);
        rl.close();
        const [, baseUrl, token] = match;
        resolve({ proc, baseUrl, token });
      }
    });
    proc.stderr.on("data", (chunk: Buffer) => process.stderr.write(`[server] ${chunk.toString()}`));
    proc.on("error", reject);
    proc.on("exit", (code) => {
      clearTimeout(timeout);
      if (code !== null && code !== 0) reject(new Error(`server exited early with code ${code}`));
    });
  });
}

export default async function globalSetup(): Promise<() => void> {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "pfa-e2e-"));
  const port = process.env.PFA_SERVER__PORT ?? "8811";
  const env: NodeJS.ProcessEnv = {
    ...process.env,
    PFA_HOME: home,
    PFA_SERVER__PORT: port,
    PFA_LLM__MODE: "none",
  };

  await run("uv", ["run", "pfa", "migrate"], env);
  const { proc, baseUrl, token } = await startServer(env);

  // Handed to every test file via `process.env` — set here, in the single runner process,
  // before Playwright forks its worker processes, so each worker inherits it.
  process.env.E2E_BASE_URL = baseUrl;
  process.env.E2E_TOKEN = token;

  return () => {
    proc.kill();
    fs.rmSync(home, { recursive: true, force: true });
  };
}
