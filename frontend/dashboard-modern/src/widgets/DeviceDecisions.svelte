<script module>
  export const manifest = { id: "device-decisions", data: ["live.control_explain.devices"], refresh: "sse", minWidth: 320, states: ["offline", "blocked", "skip", "send"] };
</script>

<script>
  import Card from "../ui/Card.svelte";
  import Fact from "../ui/Fact.svelte";
  import Status from "../ui/Status.svelte";
  import Icon from "../ui/Icon.svelte";
  import { t, fmt, codesText } from "../i18n/i18n.svelte.js";
  import { live } from "../state/live.svelte.js";
  import { deviceEntries, writeDecision, decisionItem, codeItems, has, finalTarget } from "../lib/control.js";

  const TONES = { send: "c-ok", skip: "c-idle", blocked: "c-fault", warn: "c-warn" };
  const f = $derived(fmt());
  const explain = $derived(live.snapshot?.control_explain || null);
  const devices = $derived(deviceEntries(explain));

  function sentence(name, device) {
    const item = decisionItem(device.decision_reason);
    if (!item) return "";
    if (item.key) return t(item.key, { name });
    return codesText(item.codes);
  }

  const factor = (value) => (has(value) && Number.isFinite(Number(value)) ? `${f.number(value, 2)}×` : "—");
</script>

<Card title={t("control.devices")} subtitle={t("control.devicesSub")} span="span-12">
  {#if !devices.length}
    <p class="muted">{t("control.noExplain")}</p>
  {:else}
    <div class="list">
      {#each devices as [name, device] (name)}
        {@const decision = writeDecision(device, explain)}
        <details class="decision inner">
          <summary>
            <Icon name="chevron-right" />
            <span class="name">{name}</span>
            <span class="target num">{f.power(decision.target)}</span>
            <Status tone={TONES[decision.tone]} text={t(`control.write.${decision.tone}`)} />
            <span class="why muted ellipsis">{sentence(name, device)}</span>
          </summary>
          <div class="body">
            <div class="facts">
              <Fact label={t("device.solar")} icon="sun" tone="c-pv" value={f.power(device.pv_input_w)} />
              <Fact label={t("flow.soc")} icon="battery" tone="c-bat" value={f.percent(device.soc)} />
              <Fact label={t("device.output")} icon="zap" tone="c-out" value={f.power(device.output_w)} />
              <Fact label={t("control.weight")} value={factor(device.effective_weight)} />
              <Fact label={t("control.pvPriority")} value={factor(device.pv_priority_factor)} />
              <Fact label={t("control.allocated")} value={f.power(device.allocated_target_w)} />
              <Fact label={t("control.raw")} value={f.power(device.raw_target_w)} />
              <Fact label={t("control.pvOnly")} value={f.power(device.pv_only_limit_w)} />
              <Fact label={t("control.outputLimit")} value={f.power(device.output_limit_w)} />
              <Fact label={t("control.delta")} value={f.power(device.adjustment_delta_w, { signed: true })} />
              <Fact label={t("control.final")} value={f.power(finalTarget(device))} />
              <Fact label={t("control.deviceMax")} value={f.power(device.max_output_w)} />
            </div>
            <dl class="codes">
              {#if has(device.capability_reason)}<div><dt>{t("control.capability")}</dt><dd>{codesText(codeItems(device.capability_reason))}</dd></div>{/if}
              {#if has(device.limiting_reason)}<div><dt>{t("control.limiting")}</dt><dd>{codesText(codeItems(device.limiting_reason))}</dd></div>{/if}
              {#if has(decision.reason)}<div><dt>{t("control.writeReason")}</dt><dd>{codesText(codeItems(decision.reason))}</dd></div>{/if}
              {#if has(device.min_soc) && has(device.max_soc)}<div><dt>{t("control.socRange")}</dt><dd class="num">{f.percent(device.min_soc)}–{f.percent(device.max_soc)}</dd></div>{/if}
            </dl>
          </div>
        </details>
      {/each}
    </div>
  {/if}
</Card>

<style>
  .list { display: flex; flex-direction: column; gap: var(--s2); }
  .decision > summary { display: grid; grid-template-columns: auto minmax(60px, auto) auto auto minmax(0, 1fr); align-items: center; gap: var(--s3); cursor: pointer; list-style: none; }
  .decision > summary::-webkit-details-marker { display: none; }
  .decision > summary :global(svg.i) { transition: transform 0.15s; }
  .decision[open] > summary > :global(svg.i:first-child) { transform: rotate(90deg); }
  .name { font-weight: 620; }
  .target { font-weight: 650; }
  .why { font-size: 12.5px; }
  .body { margin-top: var(--s3); display: flex; flex-direction: column; gap: var(--s3); }
  .codes { margin: 0; display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: var(--s2); font-size: 12.5px; }
  .codes dt { color: var(--m-muted); }
  .codes dd { margin: 0; }
  .muted { margin: 0; }
  @media (max-width: 760px) { .decision > summary { grid-template-columns: auto 1fr auto; } .why { grid-column: 1 / -1; white-space: normal; } }
</style>
