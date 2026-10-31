// SPDX-License-Identifier: AGPL-3.0-or-later
// Playwright may stop the harness with SIGKILL, before it can remove the broker
// it started; the container it named in the state file is removed here.
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, rmSync } from "node:fs";

export default function globalTeardown() {
  const path = process.env.EMS_MQTT_E2E_STATE;
  if (!path || !existsSync(path)) return;
  const { broker_container: name } = JSON.parse(readFileSync(path, "utf8"));
  if (name) execFileSync("docker", ["rm", "-f", name], { stdio: "ignore" });
  rmSync(path, { force: true });
}
