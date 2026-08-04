import { execFileSync } from "node:child_process";

export default function teardownAdminReplacement() {
  try {
    execFileSync("docker", ["rm", "-f", "ems-solarflow-admin"], {
      stdio: "ignore",
    });
  } catch {
    // The web-server trap normally removes the replacement first.
  }
}
