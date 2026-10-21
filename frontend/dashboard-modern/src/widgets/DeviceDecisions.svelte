<script module>
  export const manifest = { id: "device-decisions", data: ["live.control_explain.devices"], refresh: "sse", minWidth: 320, states: ["offline", "blocked", "skip", "send"] };
</script>

<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t, fmt, codesText } from "../i18n/i18n.svelte.js";
  import { live } from "../state/live.svelte.js";
  import {
    deviceEntries, writeDecision, decisionItem, codeItems, has, finalTarget,
    rawTarget, deviceWeight, baseWeight, totalWeight, deviceShare, adjustedTarget, adjustmentDelta, outputDelta,
  } from "../lib/control.js";

  const TONES = { send: "c-ok", skip: "c-idle", blocked: "c-fault", warn: "c-warn" };
  const f = $derived(fmt());
  const explain = $derived(live.snapshot?.control_explain || null);
  const devices = $derived(deviceEntries(explain));
  const weights = $derived(totalWeight(devices));
  const decimal = (value) => (has(value) && Number.isFinite(Number(value)) ? f.number(value, 2) : "—");
  const keep = (text) => text.replace(/ /g, "\u00a0");
  const share = (value) => (value === null || value === undefined ? "—" : f.percent(value, { fraction: true }));

  function stages(name, device, decision) {
    const sharePart = deviceShare(device, explain, weights);
    const requested = explain?.requested_total_w;
    const raw = rawTarget(device);
    const formula = sharePart !== null && has(requested) && has(raw)
      ? `${keep(f.power(requested))} × ${share(sharePart)} = ${keep(f.power(raw))}`
      : null;
    return [
      {
        key: "inputs",
        facts: [[t("device.solar"), f.power(device.pv_input_w)], [t("flow.soc"), f.percent(device.soc)], [t("device.output"), f.power(device.output_w)]],
        result: [t("control.inputState"), device.online === false ? t("device.offline") : t("control.ready")],
        tone: device.online === false ? "blocked" : "",
      },
      {
        key: "weighting",
        facts: [[t("control.baseWeight"), decimal(baseWeight(device))], [t("control.pvPriority"), factor(device.pv_priority_factor)], [t("control.chargeBalance"), factor(device.charge_balance_multiplier)]],
        result: [t("control.effectiveWeight"), decimal(deviceWeight(device))],
      },
      {
        key: "raw",
        facts: [[t("control.weight"), decimal(deviceWeight(device))], [t("control.share"), share(sharePart)], [t("control.requested"), f.power(requested)], ...(formula ? [[t("control.formula"), formula]] : [])],
        result: [t("control.raw"), f.power(raw)],
      },
      {
        key: "adjust",
        facts: [
          [t("control.pvOnly"), f.power(device.pv_only_limit_w)],
          [t("control.outputLimit"), f.power(device.output_limit_w)],
          [t("control.delta"), f.power(adjustmentDelta(device), { signed: true })],
          ...(has(device.limiting_reason) ? [[t("control.limiting"), codesText(codeItems(device.limiting_reason))]] : []),
          ...(has(device.capability_reason) ? [[t("control.capability"), codesText(codeItems(device.capability_reason))]] : []),
        ],
        result: [t("control.adjusted"), f.power(adjustedTarget(device))],
        tone: device.can_export === false ? "blocked" : "",
      },
      {
        key: "final",
        facts: [
          [t("control.adjusted"), f.power(adjustedTarget(device))],
          [t("control.finalTarget"), f.power(finalTarget(device))],
          [t("control.outputDelta"), f.power(outputDelta(device), { signed: true })],
          [t("control.writeLabel"), t(`control.write.${decision.tone}`)],
          ...(has(decision.reason) ? [[t("control.writeReason"), codesText(codeItems(decision.reason))]] : []),
        ],
        result: [t("control.finalWrite"), `${t(`control.write.${decision.tone}`)} / ${has(decision.target) ? f.power(decision.target) : "—"}`],
        tone: decision.tone === "blocked" ? "blocked" : "",
      },
    ];
  }

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
            <div class="context-row">
              <span>{device.online === false ? t("device.offline") : t("device.online")}</span>
              {#if has(device.min_soc) && has(device.max_soc)}<span>{t("control.socRange")} <b class="num">{f.percent(device.min_soc)}–{f.percent(device.max_soc)}</b></span>{/if}
              <span>{t("control.deviceMax")} <b class="num">{f.power(device.max_output_w)}</b></span>
              {#if has(device.capacity_weight)}<span>{t("control.capacity")} <b class="num">{decimal(device.capacity_weight)}</b></span>{/if}
            </div>
            <ol class="stages">
              {#each stages(name, device, decision) as stage, index (stage.key)}
                <li class="stage" class:blocked={stage.tone === "blocked"}>
                  <div class="stage-head"><span class="step num">{String(index + 1).padStart(2, "0")}</span>{t(`control.deviceStage.${stage.key}`)}</div>
                  <dl>
                    {#each stage.facts as [label, value] (label)}
                      <div><dt>{label}</dt><dd class="num">{value}</dd></div>
                    {/each}
                  </dl>
                  <div class="result"><span>{stage.result[0]}</span><b class="num">{stage.result[1]}</b></div>
                </li>
              {/each}
            </ol>
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
  .context-row { display: flex; flex-wrap: wrap; gap: 4px var(--s4); font-size: 12.5px; color: var(--m-muted); }
  .context-row b { color: var(--m-text); font-weight: 600; }
  .stages { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: var(--s2); }
  .stage { display: flex; flex-direction: column; gap: var(--s2); padding: var(--s3); border-radius: var(--m-radius-inner); border: 1px solid var(--m-line); }
  .stage.blocked { border-color: color-mix(in srgb, var(--m-fault) 45%, transparent); }
  .stage-head { display: flex; align-items: center; gap: 8px; font-size: 12.5px; font-weight: 620; }
  .step { font-size: 11px; color: var(--m-muted-2); font-weight: 600; }
  .stage dl { margin: 0; display: grid; gap: 3px; font-size: 12px; }
  .stage dl div { display: flex; justify-content: space-between; gap: var(--s2); }
  .stage dt { color: var(--m-muted); }
  .stage dd { margin: 0; font-weight: 600; text-align: right; overflow-wrap: break-word; min-width: 0; }
  .result { margin-top: auto; display: flex; flex-direction: column; gap: 2px; padding: 8px 10px; border-radius: var(--m-radius-control); background: var(--m-surface-3); font-size: 11.5px; color: var(--m-muted); }
  .result b { font-size: 14px; color: var(--m-text); }
  .stage.blocked .result b { color: var(--m-fault); }
  .muted { margin: 0; }
  @media (max-width: 760px) { .decision > summary { grid-template-columns: auto 1fr auto; } .why { grid-column: 1 / -1; white-space: normal; } }
</style>
