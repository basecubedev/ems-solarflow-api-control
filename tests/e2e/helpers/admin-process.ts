import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createServer } from "node:net";
import { mkdtempSync, mkdirSync, existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { request, type APIRequestContext } from "@playwright/test";

// A real Admin server process the test owns, so a restart is an actual process
// restart rather than a page reload. The data directory is created once and
// reused across restarts, which is what makes persisted-state claims meaningful.

const REPO_ROOT = join(__dirname, "..", "..", "..");
const READY_TIMEOUT_MS = 30_000;
const POLL_MS = 100;

async function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const probe = createServer();
    probe.on("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const address = probe.address();
      const port = typeof address === "object" && address ? address.port : 0;
      probe.close(() => resolve(port));
    });
  });
}

function python(): string {
  const venv = join(REPO_ROOT, ".venv", "bin", "python");
  return existsSync(venv) ? venv : "python3";
}

export class AdminProcess {
  readonly adminDataDir: string;
  readonly installDir: string;
  private readonly root: string;
  private child: ChildProcessWithoutNullStreams | null = null;
  private port = 0;
  // Incremented on every start so a test can prove it talked to a new process.
  generation = 0;
  readonly log: string[] = [];

  constructor() {
    this.root = mkdtempSync(join(tmpdir(), "ems-admin-restart-"));
    this.adminDataDir = join(this.root, "admin-data");
    this.installDir = join(this.root, "install");
    mkdirSync(this.adminDataDir, { recursive: true });
    mkdirSync(this.installDir, { recursive: true });
  }

  get baseURL(): string {
    return `http://127.0.0.1:${this.port}`;
  }

  get pid(): number | undefined {
    return this.child?.pid;
  }

  async start(): Promise<void> {
    if (this.child) throw new Error("the Admin process is already running");
    this.port = this.port || (await freePort());
    this.generation += 1;
    this.child = spawn(
      python(),
      ["-m", "admin", "--host", "127.0.0.1", "--port", String(this.port)],
      {
        cwd: REPO_ROOT,
        env: {
          ...process.env,
          EMS_ADMIN_TEST_MODE: "1",
          EMS_ADMIN_DATA_DIR: this.adminDataDir,
          EMS_INSTALL_DIR: this.installDir,
          PUID: process.env.PUID ?? String(process.getuid?.() ?? 1000),
          PGID: process.env.PGID ?? String(process.getgid?.() ?? 1000),
        },
      },
    );
    this.child.stdout.on("data", (chunk) => this.log.push(String(chunk)));
    this.child.stderr.on("data", (chunk) => this.log.push(String(chunk)));
    await this.waitUntilReady();
  }

  private async waitUntilReady(): Promise<void> {
    const deadline = Date.now() + READY_TIMEOUT_MS;
    const ctx = await request.newContext({ baseURL: this.baseURL });
    try {
      while (Date.now() < deadline) {
        if (this.child?.exitCode !== null && this.child?.exitCode !== undefined) {
          throw new Error(
            `Admin exited during startup (${this.child.exitCode}): ${this.log.join("")}`,
          );
        }
        const answered = await ctx
          .get("/api/admin/auth/status")
          .then((res) => res.ok())
          .catch(() => false);
        if (answered) return;
        await new Promise((resolve) => setTimeout(resolve, POLL_MS));
      }
      throw new Error(`Admin did not become ready: ${this.log.join("")}`);
    } finally {
      await ctx.dispose();
    }
  }

  async stop(): Promise<void> {
    const child = this.child;
    if (!child) return;
    const exited = new Promise<void>((resolve) => child.once("exit", () => resolve()));
    child.kill("SIGTERM");
    const deadline = Date.now() + READY_TIMEOUT_MS;
    while (child.exitCode === null && child.signalCode === null) {
      if (Date.now() > deadline) {
        child.kill("SIGKILL");
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    }
    await exited;
    this.child = null;
    // The socket must actually be free before the next process claims the port.
    await this.waitUntilPortClosed();
  }

  private async waitUntilPortClosed(): Promise<void> {
    const deadline = Date.now() + READY_TIMEOUT_MS;
    const ctx = await request.newContext({ baseURL: this.baseURL });
    try {
      while (Date.now() < deadline) {
        const stillAnswering = await ctx
          .get("/api/admin/auth/status", { timeout: 1000 })
          .then(() => true)
          .catch(() => false);
        if (!stillAnswering) return;
        await new Promise((resolve) => setTimeout(resolve, POLL_MS));
      }
      throw new Error("the Admin port never closed after SIGTERM");
    } finally {
      await ctx.dispose();
    }
  }

  async restart(): Promise<void> {
    await this.stop();
    await this.start();
  }

  /** Stop the process and drop the data directory it preserved across restarts. */
  async dispose(): Promise<void> {
    await this.stop();
    rmSync(this.root, { recursive: true, force: true });
  }
}

export const ADMIN_PASSWORD = "e2e-restart-password";

/** Authenticate against a freshly started process (create on first run). */
export async function authenticate(ctx: APIRequestContext): Promise<string> {
  let res = await ctx.post("/api/admin/auth/setup", {
    data: { password: ADMIN_PASSWORD, confirm_password: ADMIN_PASSWORD },
  });
  if (res.status() === 409) {
    res = await ctx.post("/api/admin/auth/login", { data: { password: ADMIN_PASSWORD } });
  }
  const body = await res.json();
  if (!res.ok()) throw new Error(`authentication failed: ${JSON.stringify(body)}`);
  return body.csrf_token as string;
}
