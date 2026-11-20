// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import { ApiError } from "../../../shared/lib/api.js";

/**
 * The answers this console gives in place of the EMS's, by their code
 * (admin/ems_forwarding.py): the EMS is not set up, its dashboard is switched
 * off, or it does not answer. Anything else is read as not answering. Pure.
 */
export const EMS_REFUSALS = Object.freeze(["ems_not_configured", "ems_dashboard_disabled", "ems_unreachable"]);
const READ_AS = Object.freeze({
  ems_config_unreadable: "ems_not_configured",
  ems_timeout: "ems_unreachable",
  ems_certificate_unreadable: "ems_unreachable",
  ems_certificate_mismatch: "ems_unreachable",
});

/** The state a failed read of the EMS says it is in. */
export function emsRefusal(error) {
  const code = error instanceof ApiError ? error.code : null;
  if (EMS_REFUSALS.includes(code)) return code;
  return READ_AS[code] || "ems_unreachable";
}
