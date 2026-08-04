import { execFileSync } from "node:child_process";

export default function teardownRemotePackagedAdmin() {
  const port = process.env.EMS_ADMIN_REMOTE_E2E_PORT ?? "8125";
  try {
    execFileSync("docker", ["rm", "-f", `ems-admin-remote-system-build-${port}`], {
      stdio: "ignore",
    });
  } catch {
    // The web-server process normally removes the disposable container first.
  }
}
