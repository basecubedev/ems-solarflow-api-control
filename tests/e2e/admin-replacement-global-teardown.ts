// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
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
