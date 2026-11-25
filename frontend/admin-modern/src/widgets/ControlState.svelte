<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import { t, tn, fmt } from "../i18n/i18n.svelte.js";
  import { controlView } from "../lib/control.js";

  /**
   * control: the "control" block of the Maintenance config summary, control on
   * or off as the EMS runs it; operating: the same answer's operating values,
   * the total among them, which the EMS runs below its limit.
   */
  let { control, operating = null } = $props();
  const view = $derived(controlView(control, operating));
  const WARN_NOTES = ["control.note.devicesOff"];
  const f = $derived(fmt());

  function limits(envelope) {
    const parts = [];
    if (envelope.totalW !== null) {
      const below = envelope.totalLimitW !== null && envelope.totalW < envelope.totalLimitW;
      parts.push(below ? t("control.limit.totalBelow", { power: f.power(envelope.totalW), limit: f.power(envelope.totalLimitW) }) : t("control.limit.total", { power: f.power(envelope.totalW) }));
    }
    if (envelope.deviceW !== null) parts.push(t("control.limit.device", { power: f.power(envelope.deviceW) }));
    return parts.join(" · ");
  }

  function window(envelope) {
    if (envelope.socMin === null || envelope.socMax === null) return "";
    const range = `${f.percent(envelope.socMin)} – ${f.percent(envelope.socMax)}`;
    return envelope.socUniform ? range : t("control.window.perDevice", { range });
  }
</script>

<div class="control" data-status={view.status}>
  <p class="verdict {view.tone}"><Icon name={view.status === "may_control" ? "shield-check" : "shield-alert"} />{t(`control.status.${view.status}`)}</p>
  {#if view.transports.length}
    <ul class="transports">
      {#each view.transports as entry (entry.gate)}
        <li>
          <span>{t(`control.transport.${entry.gate}`)}</span>
          <Status tone={entry.tone} text={entry.state === "writing" ? tn("control.writing", entry.devices) : t(`control.gate.${entry.state}`)} />
        </li>
      {/each}
    </ul>
  {/if}
  {#if limits(view.envelope) || window(view.envelope)}
    <div class="facts two">
      {#if limits(view.envelope)}<div class="fact"><div class="fact-label">{t("control.maxOutput")}</div><div class="fact-value num wrap">{limits(view.envelope)}</div></div>{/if}
      {#if window(view.envelope)}<div class="fact"><div class="fact-label">{t("control.chargeWindow")}</div><div class="fact-value num wrap">{window(view.envelope)}</div></div>{/if}
    </div>
  {/if}
  {#each view.notes as note (note.key)}<p class="note" class:warn={WARN_NOTES.includes(note.key)} data-testid={WARN_NOTES.includes(note.key) ? "control-devices-off" : undefined}><Icon name={WARN_NOTES.includes(note.key) ? "triangle-alert" : "info"} />{t(note.key, note.vars)}</p>{/each}
</div>

<style>
  .verdict { display: flex; align-items: center; gap: 8px; margin: 0 0 var(--s3); font-weight: 600; }
  .verdict :global(svg.i) { color: var(--c); }
  .transports { list-style: none; margin: 0 0 var(--s3); padding: 0; display: flex; flex-direction: column; gap: 6px; }
  .transports li { display: flex; justify-content: space-between; align-items: center; gap: var(--s2); font-size: 13px; flex-wrap: wrap; }
</style>
