<script module>
  export const manifest = { id: "control-pipeline", data: ["live.control_explain"], refresh: "sse", minWidth: 320, states: ["no-explain", "blocked", "skip"] };
</script>

<script>
  import Card from "../../../shared/ui/Card.svelte";
  import { t, fmt, codesText } from "../i18n/i18n.svelte.js";
  import { live } from "../state/live.svelte.js";
  import { deviceEntries, finalTarget, finalTotal, commandableTotal, globalWrite, writeDecision, activeLimits, codeItems } from "../lib/control.js";

  const f = $derived(fmt());
  const snapshot = $derived(live.snapshot);
  const explain = $derived(snapshot?.control_explain || null);
  const devices = $derived(deviceEntries(explain));
  const write = $derived(explain ? globalWrite(devices, explain) : "none");
  const split = (pick) => devices.map(([name, d]) => `${name} ${f.power(pick(d))}`).join("\n") || "—";
  const counts = $derived.by(() => {
    const result = {};
    devices.forEach(([, d]) => {
      const tone = writeDecision(d, explain).tone;
      result[tone] = (result[tone] || 0) + 1;
    });
    return Object.entries(result).map(([tone, n]) => `${t(`control.write.${tone}`)} ${n}`).join(" · ");
  });

  const stages = $derived(
    explain
      ? [
          { key: "measure", result: f.power(explain.filtered_load_w), facts: [[t("flow.solar"), f.power(snapshot?.pv_total_w)], [t("flow.inverter"), f.power(snapshot?.inverter_output_w)]] },
          { key: "target", result: f.power(explain.effective_target_total_w), facts: [[t("control.requested"), f.power(explain.requested_total_w)], [t("control.strategy"), codesText(codeItems(explain.mode)) || "—"]] },
          { key: "distribution", result: f.power(explain.allocated_target_total_w), facts: [[t("control.allocated"), split((d) => d.allocated_target_w)]] },
          { key: "limits", result: f.power(commandableTotal(explain)), facts: [[t("control.limits"), activeLimits(explain).map((l) => codesText(codeItems(l.name)) || l.name).join(", ") || "—"]] },
          { key: "commands", result: t(`control.write.${write}`), tone: write, facts: [[t("control.commanded"), f.power(explain.commanded_total_w)], [t("control.writes"), counts || "—"]] },
          { key: "result", result: f.power(finalTotal(explain)), facts: [[t("control.final"), split(finalTarget)]] },
        ]
      : [],
  );
</script>

<Card title={t("control.pipeline")} subtitle={t("control.pipelineSub")} span="span-12">
  {#if !explain}
    <p class="muted">{t("control.noExplain")}</p>
  {:else}
    <ol class="pipeline">
      {#each stages as stage, index (stage.key)}
        <li class="stage" class:blocked={stage.tone === "blocked"}>
          <div class="step num">{String(index + 1).padStart(2, "0")}</div>
          <div class="title">{t(`control.stage.${stage.key}`)}</div>
          <div class="result num">{stage.result}</div>
          <dl>
            {#each stage.facts as [label, value] (label)}
              <div><dt>{label}</dt><dd class="num">{value}</dd></div>
            {/each}
          </dl>
        </li>
      {/each}
    </ol>
  {/if}
</Card>

<style>
  .pipeline { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: var(--s3); counter-reset: none; }
  .stage { background: var(--m-surface-2); border-radius: var(--m-radius-inner); padding: var(--s3); display: flex; flex-direction: column; gap: 4px; position: relative; min-width: 0; }
  .stage.blocked { box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--m-fault) 50%, transparent); }
  .step { font-size: 11px; color: var(--m-muted-2); font-weight: 600; }
  .title { font-size: 12.5px; color: var(--m-muted); font-weight: 560; }
  .result { font-size: 18px; font-weight: 650; }
  dl { margin: 4px 0 0; display: grid; gap: 4px; font-size: 12px; }
  dl div { display: flex; flex-direction: column; }
  dt { color: var(--m-muted-2); }
  dd { margin: 0; overflow-wrap: anywhere; white-space: pre-line; }
  .muted { margin: 0; }
  @media (max-width: 1280px) { .pipeline { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
  @media (max-width: 760px) { .pipeline { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
