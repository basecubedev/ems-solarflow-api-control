<script module>
  export const manifest = { id: "device-card", data: ["live.devices[name]"], refresh: "sse", minWidth: 320, states: ["online", "offline", "disabled", "telemetry-only", "no-battery", "e3dc-tile"] };
</script>

<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import Bar from "../../../shared/ui/Bar.svelte";
  import Fact from "../../../shared/ui/Fact.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import { t, fmt, tv, translatedOr } from "../i18n/i18n.svelte.js";
  import { assistView, firmwareFacts } from "../lib/devices.js";
  import { batteryDirection } from "../lib/flow.js";

  let { device } = $props();

  const ASSIST = {
    active: "c-ok", waiting: "c-warn", unavailable: "c-idle", window: "c-warn", restore_pending: "c-warn",
    overdue: "c-warn", completed: "c-ok", ok: "c-idle", unknown: "c-idle",
  };

  const f = $derived(fmt());
  const d = $derived(device);
  const state = $derived(!d.enabled ? "disabled" : d.online ? "online" : "offline");
  const pvInputs = $derived(d.pvInputsW.filter((value) => value !== null));
  const settings = $derived(
    Object.entries(d.settings || {})
      .filter(([, entry]) => entry && entry.reported)
      .map(([key, entry]) => ({
        key,
        label: translatedOr(`setting.${key}`, entry.label || key),
        reported: entry.reported_value ? translatedOr(`settingValue.${entry.reported_value}`, entry.reported) : entry.reported,
        desired: entry.desired && entry.desired_value ? translatedOr(`settingValue.${entry.desired_value}`, entry.desired) : entry.desired,
      })),
  );
  const assist = $derived(d.fullChargeAssist && d.fullChargeAssist.enabled && d.fullChargeAssist.has_battery ? d.fullChargeAssist : null);
  const assisted = $derived(assist ? assistView(assist) : null);
  const charging = $derived((d.targetW ?? 0) < 0);
  const show = (value) => (d.online ? value : "—");
</script>

<article class="card device" class:offline={state !== "online"} data-testid="device-card" data-device={d.name} data-state={state}>
  <div class="head">
    <div class="icon-tile c-inv"><Icon name="zap" /></div>
    <div class="title">
      <h2 class="name ellipsis">{d.name}</h2>
      {#if d.readOnly}<div class="sub">{t("device.telemetryOnly")}</div>{/if}
    </div>
    {#if state === "online"}
      <Status tone="c-ok" icon="circle-check" text={t("device.online")} />
    {:else if state === "offline"}
      <Status tone="c-warn" icon="wifi-off" text={t("device.offline")} />
    {:else}
      <Status tone="c-idle" icon="circle-pause" text={t("device.disabled")} />
    {/if}
  </div>

  {#if d.socReported}
    <div class="soc">
      <div>
        <div class="label">{t("device.battery")}</div>
        <div class="soc-value num">{d.online ? f.percent(d.soc) : "—"}</div>
      </div>
      <div class="soc-meta num">
        {#if d.online}{t(`flow.${batteryDirection(d.batteryW)}`)} {f.power(d.batteryW, { signed: true })}{/if}
        {#if d.minSoc !== null && d.maxSoc !== null && d.maxSoc > 0}<br /><span class="muted">{t("device.socRange")} {f.percent(d.minSoc)}–{f.percent(d.maxSoc)}</span>{/if}
      </div>
    </div>
    <Bar value={d.online ? d.soc : null} label={`${d.name} ${t("flow.soc")}`} />
  {/if}

  <div class="facts">
    <Fact label={t("device.solar")} icon="sun" tone="c-pv" value={show(f.power(d.pvW))} missing={!d.online || d.pvW === null} />
    <Fact label={t("device.output")} icon="zap" tone="c-out" value={show(f.power(d.outputW))} missing={!d.online || d.outputW === null} />
    <Fact label={t("device.battery")} icon="battery-charging" tone="c-bat" value={show(f.power(d.batteryW, { signed: true }))} missing={!d.online || d.batteryW === null} />
    {#if d.gridW !== null}<Fact label={t("device.grid")} icon="utility-pole" tone="c-grid" value={show(f.power(d.gridW, { signed: true }))} />{/if}
    {#if !d.readOnly}
      {#if charging}
        <Fact label={t("device.charge")} icon="plug-zap" tone="c-grid" value={f.power(d.targetW, { signed: true })} />
      {:else}
        <Fact label={t("device.target")} icon="gauge" value={f.power(d.targetW)} missing={d.targetW === null} />
      {/if}
    {/if}
    {#if d.limitApplicable}<Fact label={t("device.limit")} icon="sliders-horizontal" value={f.power(d.limitW)} missing={d.limitW === null} />{/if}
    {#if d.packs !== null}<Fact label={t("device.packs")} icon="battery" value={String(d.packs)} />{/if}
    {#if d.inputLimitW !== null}<Fact label={t("device.inputLimit")} icon="plug-zap" value={f.power(d.inputLimitW)} />{/if}
  </div>

  {#if pvInputs.length > 1}
    <details class="more">
      <summary><Icon name="chevron-right" />{t("device.solar")} · {pvInputs.length}</summary>
      <div class="facts body">
        {#each d.pvInputsW as value, index (index)}
          {#if value !== null}<Fact label={`PV ${index + 1}`} icon="solar-panel" tone="c-pv" value={f.power(value)} />{/if}
        {/each}
      </div>
    </details>
  {/if}

  {#if d.firmwareApplicable}
    <details class="more">
      <summary><Icon name="chevron-right" />{t("device.firmware")}</summary>
      <div class="facts two body">
        {#each firmwareFacts(d.raw) as fact (fact.label)}
          <Fact label={t(fact.label)} value={tv(fact.value)} />
        {/each}
      </div>
    </details>
  {/if}

  {#if settings.length}
    <details class="more">
      <summary><Icon name="chevron-right" />{t("device.settings")}</summary>
      <div class="facts two body">
        {#each settings as entry (entry.key)}
          <Fact label={entry.label} value={entry.desired ? `${entry.reported} → ${entry.desired}` : entry.reported} />
        {/each}
      </div>
    </details>
  {/if}

  {#if assist}
    <div class="assist inner">
      <div class="assist-head">
        <span class="label"><Icon name="battery-full" size="sm" />{t("device.fullCharge")}</span>
        <Status tone={ASSIST[assisted.status] || "c-idle"} text={t(`assist.${assisted.status}`)} />
      </div>
      {#if assist.message}<p class="note" lang="en">{assist.message}</p>{/if}
      {#each assisted.description as key (key)}<p class="note">{t(key)}</p>{/each}
      <div class="facts two" data-testid="assist-facts">
        {#each assisted.facts as fact (fact.label)}
          <Fact
            label={t(fact.label)}
            wrap={Boolean(fact.firmware)}
            value={fact.firmware ? t("assist.firmwareValue", { soc: tv(fact.firmware.socGuard), ac: tv(fact.firmware.acPath) }) : fact.key ? t(fact.key) : "days" in fact ? (fact.days === null ? "—" : t("assist.days", { n: fact.days })) : fact.time ? f.dateTime(fact.time) : "—"}
          />
        {/each}
      </div>
    </div>
  {/if}

  {#if state === "offline"}
    <p class="note"><Icon name="info" />{t("device.excluded")}</p>
  {:else if state === "disabled"}
    <p class="note"><Icon name="info" />{t("device.disabledNote")}</p>
  {:else if d.readOnly}
    <p class="note"><Icon name="info" />{t("device.telemetryNote")}</p>
  {/if}
</article>

<style>
  .device { display: flex; flex-direction: column; gap: var(--s4); }
  .device.offline { border-style: dashed; }
  .head { display: flex; gap: var(--s3); align-items: center; }
  .title { flex: 1; min-width: 0; }
  .name { font-weight: 640; font-size: 16px; margin: 0; }
  .sub { color: var(--m-muted); font-size: 12.5px; }
  .soc { display: flex; align-items: flex-end; justify-content: space-between; gap: 8px; margin-bottom: -6px; }
  .label { font-size: 12.5px; color: var(--m-muted); display: inline-flex; gap: 6px; align-items: center; }
  .soc-value { font-size: 30px; font-weight: 680; letter-spacing: -0.03em; }
  .soc-meta { font-size: 12px; color: var(--m-muted); text-align: right; }
  details.more { margin-top: 0; }
  .assist { display: flex; flex-direction: column; gap: var(--s3); }
  .assist-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; }
  .assist .note { margin-top: 0; }
  .note { margin-top: 0; }
</style>
