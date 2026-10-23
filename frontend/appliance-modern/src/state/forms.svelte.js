// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * What the owner typed or picked in a form, kept while they move between
 * pages. Presentation state only: what is installed or changed is still
 * decided by the plan the backend returns. A passphrase is never kept here --
 * it would outlive the form it was typed into.
 */
class Forms {
  adminVersion = $state("latest_stable");
  adminTag = $state("");
  adminReinstall = $state(false);
  bootstrapVersion = $state("latest_stable");
  bootstrapTag = $state("");
  managerVersion = $state("");
  wifiSsid = $state("");
  wifiHidden = $state(false);
  hostname = $state("");
  timezone = $state(null);
  keyAccount = $state("");
  keyValue = $state("");
  logSource = $state("");
}

export const forms = new Forms();

const DEFAULTS = Object.freeze({
  adminVersion: "latest_stable",
  adminTag: "",
  adminReinstall: false,
  bootstrapVersion: "latest_stable",
  bootstrapTag: "",
  managerVersion: "",
  wifiSsid: "",
  wifiHidden: false,
  hostname: "",
  timezone: null,
  keyAccount: "",
  keyValue: "",
});

/** Fields go back to their defaults: after the plan they fed was confirmed, or when what they chose from moved. */
export function forget(...names) {
  for (const name of names) {
    if (name in DEFAULTS) forms[name] = DEFAULTS[name];
  }
}

export const ADMIN_CHOICES = Object.freeze(["adminVersion", "adminTag", "adminReinstall", "bootstrapVersion", "bootstrapTag"]);
export const MANAGER_CHOICES = Object.freeze(["managerVersion"]);
