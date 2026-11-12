<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import { onDestroy } from "svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { adminSend } from "../state/auth.svelte.js";
  import { errorText } from "../lib/errors.js";
  import { PAIR_PATH, PAIRING_RETRY_MS, PAIRING_WINDOW_MS, SAID_REASONS, pairingOutcome, pairingRequest } from "../lib/homewizard.js";

  /** Pairs the HomeWizard P1 Meter at ip:port under this installation's pairingName; paired says a token is stored or drafted, onpaired takes the new serial, token and name. */
  let { ip, port, paired = false, serial = "", pairingName = "", onpaired } = $props();

  let phase = $state("idle");
  let failure = $state(null);
  let attempt = 0;
  onDestroy(() => {
    attempt += 1;
  });

  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function ask() {
    failure = null;
    if (paired || phase === "done") phase = "confirm";
    else pair();
  }

  async function pair() {
    const body = pairingRequest(ip, port, pairingName);
    failure = null;
    if (!body) {
      phase = "failed";
      failure = { text: t("devices.homewizard.needIp") };
      return;
    }
    const mine = ++attempt;
    phase = "waiting";
    const deadline = Date.now() + PAIRING_WINDOW_MS;
    while (mine === attempt) {
      let outcome;
      try {
        outcome = pairingOutcome(await adminSend(PAIR_PATH, { body }));
      } catch (error) {
        outcome = { phase: "failed", reason: "", message: errorText(error, t) };
      }
      if (mine !== attempt) return;
      if (outcome.phase === "paired") {
        phase = "done";
        onpaired({ serial: outcome.serial, token: outcome.token, pairingName: outcome.pairingName });
        return;
      }
      if (outcome.phase === "failed") {
        phase = "failed";
        failure = SAID_REASONS.includes(outcome.reason) ? { text: t(`devices.homewizard.reason.${outcome.reason}`) } : { text: t("devices.homewizard.failed"), detail: outcome.message };
        return;
      }
      if (Date.now() + PAIRING_RETRY_MS > deadline) {
        phase = "failed";
        failure = { text: t("devices.homewizard.timeout") };
        return;
      }
      await wait(PAIRING_RETRY_MS);
    }
  }

  function cancel() {
    attempt += 1;
    phase = "idle";
  }
</script>

<div class="setting" data-testid="homewizard-pairing">
  <span class="head">{t("devices.homewizard.title")}</span>
  {#if phase === "waiting"}
    <p class="note" role="status"><Icon name="loader" />{t("devices.homewizard.press")}</p>
    <button class="btn secondary small" type="button" onclick={cancel}>{t("devices.homewizard.cancel")}</button>
  {:else if phase === "confirm"}
    <p class="note warn" role="alert"><Icon name="triangle-alert" />{t("devices.homewizard.repairWarning")}</p>
    <button class="btn small" type="button" onclick={pair} data-testid="homewizard-pair-confirm">{t("devices.homewizard.pairAgain")}</button>
    <button class="btn secondary small" type="button" onclick={cancel}>{t("devices.homewizard.cancel")}</button>
  {:else}
    <p class="desc" role="status" data-testid="homewizard-state">{#if phase === "done"}{t("devices.homewizard.newPairing", { serial })}{:else if paired && serial}{t("devices.homewizard.paired", { serial })}{:else if paired}{t("devices.homewizard.pairedNoSerial")}{:else}{t("devices.homewizard.unpaired")}{/if}</p>
    {#if failure}<p class="note warn" role="alert"><Icon name="triangle-alert" />{failure.text}{#if failure.detail}{" "}<span lang="en">{failure.detail}</span>{/if}</p>{/if}
    <button class="btn small" class:secondary={paired || phase === "done"} type="button" onclick={ask} data-testid="homewizard-pair">{paired || phase === "done" ? t("devices.homewizard.pairAgain") : t("devices.homewizard.pair")}</button>
  {/if}
</div>
