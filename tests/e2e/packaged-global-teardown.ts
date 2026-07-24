import { execFileSync } from "node:child_process";

export default function teardownPackagedAdmin() {
  const port = process.env.EMS_ADMIN_PACKAGED_E2E_PORT ?? "8124";
  try {
    execFileSync("docker", ["rm", "-f", `ems-admin-system-build-browser-gate-${port}`], {
      stdio: "ignore",
    });
  } catch {
    // The web-server process may already have removed the disposable container.
  }
}
