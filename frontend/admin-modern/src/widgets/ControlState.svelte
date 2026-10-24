<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import { t, tn, fmt } from "../i18n/i18n.svelte.js";
  import { controlView } from "../lib/control.js";

  /**
   * control: the "control" block of the Maintenance config summary, which reads
   * the saved file only; overrides: the same answer's runtime overrides, which
   * the view follows where the dashboard changed a control setting.
   */
  let { control, overrides = null } = $props();
  const view = $derived(controlView(control, overrides));
  const LIVE_NOTES = ["control.note.live", "control.note.devicesLive"];
  const f = $derived(fmt());

  function limits(envelope) {
    const parts = [];
    if (envelope.totalW !== null) parts.push(t("control.limit.total", { power: f.power(envelope.totalW) }));
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
  {#each view.notes as note (note.key)}<p class="note" class:warn={LIVE_NOTES.includes(note.key)} data-testid={LIVE_NOTES.includes(note.key) ? "control-live" : undefined}><Icon name={LIVE_NOTES.includes(note.key) ? "triangle-alert" : "info"} />{t(note.key, note.vars)}</p>{/each}
</div>

<style>
  .verdict { display: flex; align-items: center; gap: 8px; margin: 0 0 var(--s3); font-weight: 600; }
  .verdict :global(svg.i) { color: var(--c); }
  .transports { list-style: none; margin: 0 0 var(--s3); padding: 0; display: flex; flex-direction: column; gap: 6px; }
  .transports li { display: flex; justify-content: space-between; align-items: center; gap: var(--s2); font-size: 13px; flex-wrap: wrap; }
</style>
