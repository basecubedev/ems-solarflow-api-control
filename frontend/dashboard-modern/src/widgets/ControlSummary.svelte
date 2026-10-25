<script module>
  export const manifest = { id: "control-summary", data: ["live.control_explain", "live.controller"], refresh: "sse", minWidth: 300, states: ["off", "no-explain", "limits"] };
</script>

<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Fact from "../../../shared/ui/Fact.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t, fmt, codesText } from "../i18n/i18n.svelte.js";
  import { live } from "../state/live.svelte.js";
  import { activeLimits, inactiveLimits, codeItems, has, LIMIT_REASONS, limitValueText, noteText } from "../lib/control.js";

  const f = $derived(fmt());
  const snapshot = $derived(live.snapshot);
  const explain = $derived(snapshot?.control_explain || null);
  const enabled = $derived(snapshot?.controller?.enabled !== false);
  const limits = $derived(activeLimits(explain));
  const inactive = $derived(inactiveLimits(explain).map((limit) => codesText(codeItems(limit.name)) || limit.name));
  const notes = $derived((Array.isArray(explain?.notes) ? explain.notes : []).filter(Boolean));
</script>

<Card icon="gauge" tone="c-inv" title={t("control.now")} span="span-12">
  {#if !snapshot}
    <div class="skeleton"></div>
  {:else if !enabled}
    <p class="sentence warn"><Icon name="circle-pause" />{t("control.sentenceOff")}</p>
  {:else if !explain}
    <p class="sentence">{t("control.noExplain")}</p>
  {:else}
    <p class="sentence" data-testid="control-sentence">
      {t("control.sentence", {
        load: f.power(explain.filtered_load_w),
        target: f.power(explain.effective_target_total_w),
        mode: codesText(codeItems(explain.mode)) || "—",
      })}
    </p>
    <div class="facts eight">
      <Fact label={t("control.load")} icon="house" tone="c-out" value={f.power(explain.filtered_load_w)} />
      <Fact label={t("control.requested")} icon="gauge" tone="c-inv" value={f.power(explain.requested_total_w)} />
      <Fact label={t("control.effective")} icon="crosshair" tone="c-inv" value={f.power(explain.effective_target_total_w)} />
      <Fact label={t("control.commanded")} icon="send" tone="c-inv" value={f.power(explain.commanded_total_w)} />
      <Fact label={t("control.maxTotal")} icon="sliders-horizontal" tone="c-soc" value={f.power(explain.max_total_power_w)} />
      <Fact label={t("control.deadband")} icon="move-horizontal" tone="c-soc" value={f.power(explain.deadband_w)} />
      <Fact label={t("control.minOutput")} icon="sliders-horizontal" tone="c-soc" value={f.power(explain.min_output_limit_w)} missing={!has(explain.min_output_limit_w)} />
      <Fact label={t("control.undistributed")} icon="gauge" tone="c-soc" value={f.power(explain.undistributed_target_w)} missing={!has(explain.undistributed_target_w)} />
    </div>
  {/if}
  {#if explain}
    <div class="lists">
      <div>
        <h3 class="sub">{t("control.limits")}</h3>
        {#if limits.length}
          <ul>
            {#each limits as limit, index (index)}
              {@const value = limitValueText(limit, explain, f)}
              {@const reason = LIMIT_REASONS[limit.reason]}
              <li><span class="dot warn"></span><b>{codesText(codeItems(limit.name)) || limit.name}</b>{#if value}<span class="num">{` · ${value}`}</span>{/if}{#if limit.reason}<br /><span class="muted" lang={reason ? undefined : "en"}>{reason ? t(`limit.reason.${reason}`) : limit.reason}</span>{/if}</li>
            {/each}
          </ul>
        {:else}
          <p class="muted">{t("control.noLimits")}</p>
        {/if}
        {#if inactive.length}<p class="muted inactive-gates">{t("control.inactiveGates")}: {inactive.join(", ")}</p>{/if}
      </div>
      {#if notes.length}
        <div>
          <h3 class="sub">{t("control.notes")}</h3>
          <ul>{#each notes as note, index (index)}{@const said = noteText(note, t, f)}<li class="muted" lang={said.lang}>{said.text}</li>{/each}</ul>
        </div>
      {/if}
    </div>
  {/if}
</Card>

<style>
  .sentence { font-size: 16px; margin: 0 0 var(--s4); display: flex; gap: 10px; align-items: center; }
  .sentence.warn { color: var(--m-warn); }
  .eight { grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .inactive-gates { font-size: 12.5px; margin: var(--s2) 0 0; }
  .lists { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: var(--s4); margin-top: var(--s4); }
  .sub { font-size: 13px; font-weight: 600; margin: 0 0 var(--s2); }
  ul { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 8px; font-size: 13px; }
  li .dot { display: inline-block; width: 7px; height: 7px; margin-right: 8px; box-shadow: none; vertical-align: middle; }
  p.muted { margin: 0; font-size: 13px; }
  .skeleton { height: 120px; }
  @media (max-width: 760px) { .eight { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
