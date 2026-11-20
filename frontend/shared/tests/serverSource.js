// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The Python source of the server packages, for tests that pair a list of
 * codes here with the ones the server writes.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

export const ROOT = fileURLToPath(new URL("../../..", import.meta.url));

function pythonFiles(dir, out) {
  for (const name of readdirSync(dir)) {
    if (name === "__pycache__" || name.startsWith(".")) continue;
    const path = join(dir, name);
    if (statSync(path).isDirectory()) pythonFiles(path, out);
    else if (name.endsWith(".py")) out.push(readFileSync(path, "utf8"));
  }
  return out;
}

/** Every .py file of the named top-level packages, read once and joined. */
export function serverSource(...packages) {
  return packages.flatMap((name) => pythonFiles(join(ROOT, name), [])).join("\n");
}

/** The codes of ``codes`` that appear nowhere in ``source`` as a quoted string. */
export function unwritten(source, codes) {
  return [...codes].filter((code) => !source.includes(`"${code}"`) && !source.includes(`'${code}'`));
}
