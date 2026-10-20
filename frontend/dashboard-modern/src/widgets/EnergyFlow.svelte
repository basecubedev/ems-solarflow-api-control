<script module>
  export const manifest = {
    id: "energy-flow",
    data: ["live", "energy.today"],
    refresh: "sse",
    minWidth: 320,
    states: ["waiting", "stale", "meter-offline", "no-battery", "device-offline", "ac-charging"],
  };
</script>

<script>
  import Card from "../ui/Card.svelte";
  import Segmented from "../ui/Segmented.svelte";
  import Icon from "../ui/Icon.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { live } from "../state/live.svelte.js";
  import { prefs, setPref } from "../state/prefs.svelte.js";
  import { devices as allDevices, isStale } from "../state/derived.svelte.js";
  import { createFlowActivity, createFlowScale, createHouseLines, busFlows, ribbonWidth, batteryDirection, gridDirection, gridHomeWatts } from "../lib/flow.js";
  import { box, elbow, vertical, down, roundedPath, runs } from "../lib/wires.js";
  import { flowDots, DOT_PERIOD_PX } from "../lib/flowAnim.js";
  import { channelKwh } from "../lib/energy.js";
  import { finite } from "../lib/format.js";

  let { span = "span-8", fixedMode = null } = $props();

  const activity = createFlowActivity();
  const scale = createFlowScale();
  const houseLines = createHouseLines();

  let stage = $state();
  let size = $state({ width: 0, height: 0 });
  let geometry = $state.raw({});
  let bus = $state.raw({ above: [], below: [] });

  const snapshot = $derived(live.snapshot);
  const today = $derived(snapshot?.energy_stats?.today);
  const stale = $derived(isStale());
  const deviceList = $derived(allDevices().filter((device) => device.enabled || device.online));
  const deviceKey = $derived(deviceList.map((device) => `${device.name}:${device.socReported || device.batteryW !== null}`).join("|"));
  const narrow = $derived(size.width > 0 && size.width < 600);
  const mode = $derived((fixedMode || prefs.flow) === "devices" && deviceList.length > 0 ? "devices" : "system");
  const houses = $derived(
    new Map(deviceList.map((device) => [
      device.name,
      houseLines.direction(device.name, device.online ? device.outputW : 0, device.online ? device.chargeW : 0, live.sequence),
    ])),
  );

  const system = $derived.by(() => {
    if (!snapshot) return null;
    const batteryW = deviceList.length
      ? deviceList.reduce((sum, device) => sum + (device.batteryW || 0), 0)
      : finite(snapshot.battery_power_w) || 0;
    const gridValid = snapshot.grid_power_valid !== false;
    const gridW = gridValid ? finite(snapshot.grid_power_w) || 0 : 0;
    const chargeW = Math.abs(finite(snapshot.inverter_charge_w) || 0);
    return {
      pvW: finite(snapshot.pv_total_w),
      batteryW,
      hasBattery: deviceList.length === 0 || deviceList.some((device) => device.socReported || device.batteryW !== null),
      soc: finite(snapshot.average_soc),
      outputW: finite(snapshot.inverter_output_w),
      chargeW,
      homeW: finite(snapshot.home_load_w),
      gridW,
      gridValid,
    };
  });

  const wireSpecs = $derived.by(() => {
    if (!system) return [];
    const specs = [];
    if (mode === "system") {
      const v = system;
      scale.update(Math.max(Math.abs(v.pvW || 0), Math.abs(v.batteryW), Math.abs(v.outputW || 0), Math.abs(v.gridW), v.chargeW));
      specs.push({ id: "pv", token: "--pv", watts: v.pvW, reverse: false });
      if (v.hasBattery) specs.push({ id: "bat", token: "--battery", watts: v.batteryW, reverse: v.batteryW > 0 });
      specs.push({ id: "out", token: "--output", watts: v.outputW, reverse: false });
      specs.push({ id: "grid", token: "--grid", watts: gridHomeWatts(v.gridW, v.chargeW), reverse: v.gridW < 0 });
      if (v.chargeW > 0) specs.push({ id: "charge", token: "--grid", watts: v.chargeW, reverse: false });
    } else {
      const flows = deviceList.map((device) => {
        const house = houses.get(device.name);
        if (!device.online || !house?.active) return 0;
        return house.charging ? -house.watts : house.watts;
      });
      const joined = busFlows(flows, bus);
      const total = joined.total;
      scale.update(Math.max(
        Math.abs(system.gridW),
        Math.abs(total),
        ...deviceList.map((d) => Math.max(Math.abs(d.pvW || 0), Math.abs(d.outputW || 0), Math.abs(d.batteryW || 0), Math.abs(d.chargeW || 0))),
      ));
      const toward = (id, watts) => ({ id, token: watts < 0 ? "--grid" : "--output", watts: Math.abs(watts), reverse: watts < 0 });
      Object.entries(joined.segments).forEach(([id, watts]) => specs.push(toward(id, watts)));
      specs.push(toward("trunk", total));
      deviceList.forEach((device, index) => {
        const on = device.online;
        specs.push({ id: `pv${index}`, token: "--pv", watts: on ? device.pvW : 0, reverse: false });
        if (device.socReported || device.batteryW !== null) {
          specs.push({ id: `bat${index}`, token: "--battery", watts: on ? device.batteryW : 0, reverse: (device.batteryW || 0) > 0 });
        }
        specs.push(toward(`out${index}`, flows[index]));
      });
      specs.push({ id: "grid", token: "--grid", watts: system.gridW, reverse: system.gridW < 0 });
    }
    return specs.map((spec) => {
      const active = activity.isActive(`${mode}:${spec.id}`, spec.watts);
      return { ...spec, active, width: ribbonWidth(spec.watts, active, scale.reference) };
    });
  });

  function measure() {
    if (!stage) return;
    const origin = stage.getBoundingClientRect();
    const nodes = {};
    stage.querySelectorAll("[data-node]").forEach((el) => {
      nodes[el.dataset.node] = box(el.getBoundingClientRect(), origin);
    });
    const points = {};
    if (mode === "system") {
      const { pv, bat, hub, home, grid } = nodes;
      if (!hub || !home || !grid) return;
      if (narrow) {
        if (pv) points.pv = down(pv.cx, pv.b, hub.l, hub.cy - 10);
        if (bat) points.bat = down(bat.cx, bat.t, hub.l, hub.cy + 10);
        points.out = down(home.cx, home.b, hub.r, hub.cy).reverse();
        points.grid = vertical(grid.cx, grid.t, home.b);
        points.charge = down(grid.cx - 22, grid.t, hub.r, hub.cy + 14);
      } else {
        const inset = (hub.r - hub.l) * 0.07;
        if (pv) points.pv = elbow(pv.r, pv.cy, hub.l + inset, hub.cy - 22);
        if (bat) points.bat = elbow(bat.r, bat.cy, hub.l + inset, hub.cy + 22);
        points.out = elbow(hub.r - inset, hub.cy - 8, home.l, home.cy);
        points.grid = vertical(grid.cx, grid.t, home.b);
        points.charge = elbow(grid.l, grid.cy, hub.r - inset, hub.cy + 14);
      }
    } else {
      const { home, grid } = nodes;
      if (!home || !grid) return;
      const hubs = deviceList.map((_, index) => nodes[`hub${index}`]).filter(Boolean);
      if (!hubs.length) return;
      const busX = Math.round(Math.max(...hubs.map((hub) => hub.r)) + (home.l - Math.max(...hubs.map((hub) => hub.r))) / 2);
      const junction = home.cy;
      const rows = [];
      deviceList.forEach((_, index) => {
        const pv = nodes[`pv${index}`];
        const bat = nodes[`bat${index}`];
        const hub = nodes[`hub${index}`];
        if (!hub) return;
        const inset = (hub.r - hub.l) * 0.07;
        if (pv) points[`pv${index}`] = elbow(pv.r, pv.cy, hub.l + inset, hub.cy - (bat ? 12 : 0));
        if (bat) points[`bat${index}`] = elbow(bat.r, bat.cy, hub.l + inset, hub.cy + (pv ? 12 : 0));
        points[`out${index}`] = [[hub.r - inset, hub.cy], [busX, hub.cy]];
        rows.push({ index, y: hub.cy });
      });
      const above = rows.filter((row) => row.y < junction - 0.5).sort((a, b) => a.y - b.y);
      const below = rows.filter((row) => row.y > junction + 0.5).sort((a, b) => b.y - a.y);
      above.forEach((row, k) => {
        points[`bus-above${k}`] = [[busX, row.y], [busX, above[k + 1]?.y ?? junction]];
      });
      below.forEach((row, k) => {
        points[`bus-below${k}`] = [[busX, row.y], [busX, below[k + 1]?.y ?? junction]];
      });
      points.trunk = [[busX, junction], [home.l, junction]];
      points.grid = vertical(grid.cx, grid.t, home.b);
      bus = { above: above.map((row) => row.index), below: below.map((row) => row.index) };
    }
    geometry = points;
  }

  $effect(() => {
    if (!stage) return;
    const observer = new ResizeObserver(() => {
      size = { width: stage.clientWidth, height: stage.clientHeight };
      measure();
    });
    observer.observe(stage);
    return () => observer.disconnect();
  });

  $effect(() => {
    void mode;
    void deviceKey;
    void narrow;
    void prefs.density;
    void prefs.lang;
    void snapshot !== null;
    requestAnimationFrame(measure);
  });

  const drawn = $derived(
    wireSpecs
      .filter((spec) => geometry[spec.id])
      .map((spec) => ({ ...spec, points: geometry[spec.id], d: roundedPath(geometry[spec.id]), runs: runs(geometry[spec.id]) })),
  );

  const f = $derived(fmt());
  const batteryWord = (w) => t(`flow.${batteryDirection(w)}`);
  const gridWord = (w, valid) => t(`flow.${gridDirection(w, valid)}`);
  const ariaLabel = $derived(
    system
      ? t("flow.aria", {
          pv: f.power(system.pvW),
          battery: f.power(system.batteryW, { signed: true }),
          home: f.power(system.homeW),
          grid: system.gridValid ? f.power(system.gridW, { signed: true }) : t("flow.offline"),
        })
      : t("flow.waiting"),
  );
  const pvToday = $derived(channelKwh(today, "pv_yield").kwh);
  const homeToday = $derived(channelKwh(today, "home_consumption").kwh);
  const importToday = $derived(channelKwh(today, "grid_import").kwh);
</script>

{#snippet actions()}
  <Segmented
    options={[{ value: "system", label: t("flow.system") }, { value: "devices", label: t("flow.perDevice") }]}
    value={prefs.flow}
    label={t("flow.title")}
    onchange={(value) => setPref("flow", value)}
  />
{/snippet}

<Card title={t("flow.title")} subtitle={t("flow.sub")} {span} actions={fixedMode ? undefined : actions} className="flow-card">
  {#if !system}
    <div class="skeleton flow-skeleton" aria-busy="true" aria-label={t("flow.waiting")}></div>
  {:else}
    <div
      class="flow-stage {mode}"
      class:narrow
      class:stale
      bind:this={stage}
      role="img"
      aria-label={ariaLabel}
      data-testid="energy-flow"
      data-mode={mode}
    >
      <svg class="wires" aria-hidden="true" width={size.width} height={size.height}>
        {#each drawn as wire (wire.id)}
          <path class="wire-track" class:idle={!wire.active} d={wire.d} stroke={`var(${wire.token})`} stroke-width={wire.width} data-wire={wire.id}></path>
        {/each}
      </svg>
      <div class="dots-layer" aria-hidden="true">
        {#each drawn as wire (wire.id)}
          {#if wire.active}
            {#each wire.runs as run, index (index)}
              <div
                class="run"
                style:left={`${run.x}px`}
                style:top={`${run.y - wire.width / 2}px`}
                style:width={`${run.length}px`}
                style:height={`${wire.width}px`}
                style:transform={`rotate(${run.angle}deg)`}
                style:--c={`var(${wire.token})`}
              >
                <div
                  class="dots"
                  style:width={`${run.length + DOT_PERIOD_PX}px`}
                  use:flowDots={{ active: wire.active, reverse: wire.reverse, animate: prefs.animate && !stale, slow: prefs.serverAnimation === "reduced" }}
                ></div>
              </div>
            {/each}
          {/if}
        {/each}
      </div>

      {#if mode === "system"}
        <div class="node c-pv n-pv" data-node="pv">
          <div class="icon-tile"><Icon name="solar-panel" /></div>
          <div class="node-label">{t("flow.solar")}</div>
          <div class="node-value num">{f.power(system.pvW)}</div>
          <div class="node-foot"><span>{t("common.today")}</span><b class="num">{f.energy(pvToday)}</b></div>
        </div>
        {#if system.hasBattery}
          <div class="node c-bat n-bat" data-node="bat">
            <div class="icon-tile"><Icon name="battery-charging" /></div>
            <div class="node-label">{t("flow.battery")} · {batteryWord(system.batteryW)}</div>
            <div class="node-value num">{f.power(system.batteryW, { signed: true })}</div>
            <div class="node-foot"><span>{t("flow.soc")}</span><b class="num">{f.percent(system.soc)}</b></div>
          </div>
        {/if}
        <div class="hub" data-node="hub">
          <div>
            <Icon name="zap" />
            <div class="hub-value num">{f.power(system.outputW)}</div>
            <div class="hub-label">{t("flow.inverter")}</div>
            {#if system.chargeW >= 8}<div class="hub-charge num">{t("flow.acCharging", { v: f.power(system.chargeW) })}</div>{/if}
          </div>
        </div>
        <div class="node c-out n-home" data-node="home">
          <div class="icon-tile"><Icon name="house" /></div>
          <div class="node-label">{t("flow.home")}</div>
          <div class="node-value num">{f.power(system.homeW)}</div>
          <div class="node-foot"><span>{t("common.today")}</span><b class="num">{f.energy(homeToday)}</b></div>
        </div>
        <div class="node c-grid n-grid" class:offline={!system.gridValid} data-node="grid">
          <div class="icon-tile"><Icon name="utility-pole" /></div>
          <div class="node-label">{t("flow.grid")} · {gridWord(system.gridW, system.gridValid)}</div>
          <div class="node-value num">{system.gridValid ? f.power(system.gridW, { signed: true }) : "—"}</div>
          <div class="node-foot"><span>{t("flow.importedToday")}</span><b class="num">{f.energy(importToday)}</b></div>
        </div>
      {:else}
        {#each deviceList as device, index (device.name)}
          <div class="node c-pv col-l" class:offline={!device.online} data-node={`pv${index}`} style:grid-row={`${index * 2 + 1}`}>
            <div class="icon-tile sm"><Icon name="solar-panel" /></div>
            <div class="node-label ellipsis" title={`${device.name} · ${t("flow.solar")}`}>{device.name} · {t("flow.solar")}</div>
            <div class="node-value num">{device.online ? f.power(device.pvW) : "—"}</div>
          </div>
          {#if device.socReported || device.batteryW !== null}
            <div class="node c-bat col-l" class:offline={!device.online} data-node={`bat${index}`} style:grid-row={`${index * 2 + 2}`}>
              <div class="icon-tile sm"><Icon name="battery-charging" /></div>
              <div class="node-label ellipsis" title={`${device.name} · ${device.online ? batteryWord(device.batteryW) : t("device.offline")}`}>{device.name} · {device.online ? batteryWord(device.batteryW) : t("device.offline")}</div>
              <div class="node-value num">{device.online ? f.power(device.batteryW, { signed: true }) : "—"}</div>
              {#if device.soc !== null}
                <div class="soc-mini" role="meter" aria-label={t("flow.soc")} aria-valuemin="0" aria-valuemax="100" aria-valuenow={Math.round(device.soc)}>
                  <span style:transform={`scaleX(${device.soc / 100})`}></span>
                </div>
              {/if}
            </div>
          {/if}
          <div class="hub small" class:offline={!device.online} data-node={`hub${index}`} style:grid-row={`${index * 2 + 1} / span 2`}>
            <div>
              <Icon name={device.online ? "zap" : "wifi-off"} />
              <div class="hub-value num">{device.online ? (houses.get(device.name)?.charging ? f.power(-houses.get(device.name).watts, { signed: true }) : f.power(device.outputW)) : "—"}</div>
              <div class="hub-label ellipsis" title={device.name}>{device.name}</div>
            </div>
          </div>
        {/each}
        <div class="node c-out col-r" data-node="home" style:grid-row={`1 / span ${deviceList.length}`}>
          <div class="icon-tile sm"><Icon name="house" /></div>
          <div class="node-label">{t("flow.home")}</div>
          <div class="node-value num">{f.power(system.homeW)}</div>
          <div class="node-foot"><span>{t("common.today")}</span><b class="num">{f.energy(homeToday)}</b></div>
        </div>
        <div class="node c-grid col-r" class:offline={!system.gridValid} data-node="grid" style:grid-row={`${deviceList.length + 1} / span ${deviceList.length}`}>
          <div class="icon-tile sm"><Icon name="utility-pole" /></div>
          <div class="node-label ellipsis" title={`${t("flow.grid")} · ${gridWord(system.gridW, system.gridValid)}`}>{t("flow.grid")} · {gridWord(system.gridW, system.gridValid)}</div>
          <div class="node-value num">{system.gridValid ? f.power(system.gridW, { signed: true }) : "—"}</div>
          <div class="node-foot"><span>{t("flow.importedToday")}</span><b class="num">{f.energy(importToday)}</b></div>
        </div>
      {/if}
    </div>
  {/if}
</Card>

<style>
  .flow-skeleton { height: 360px; }
  :global(.flow-card) { display: flex; flex-direction: column; }
  .flow-stage { position: relative; margin: auto 0; }
  .flow-stage.system { height: 360px; }
  .flow-stage.system.narrow { height: 440px; }
  .flow-stage.stale { opacity: 0.55; }
  .wires { position: absolute; inset: 0; overflow: visible; pointer-events: none; }
  .wire-track { fill: none; stroke-linecap: round; stroke-linejoin: round; opacity: 0.26; }
  .wire-track.idle { stroke-dasharray: 3 7; opacity: 0.35; }
  .dots-layer { position: absolute; inset: 0; pointer-events: none; }
  .run { position: absolute; transform-origin: 0 50%; overflow: hidden; border-radius: 999px; }
  .dots {
    height: 100%;
    background-image: radial-gradient(circle at center, var(--c) 0 34%, transparent 40%);
    background-size: 20px 100%;
    background-repeat: repeat-x;
    transform: translateX(-20px);
    will-change: transform;
  }

  .node {
    position: absolute; width: 214px; padding: 14px 16px; border-radius: 16px; z-index: 1;
    background: color-mix(in srgb, var(--c) 7%, var(--m-surface-2));
    border: 1px solid color-mix(in srgb, var(--c) 38%, transparent);
    box-shadow: 0 0 0 4px color-mix(in srgb, var(--c) 4%, transparent), 0 12px 32px color-mix(in srgb, var(--c) 9%, transparent);
    display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 4px 12px; align-items: center;
  }
  .node .icon-tile { grid-row: span 2; }
  .node.offline { opacity: 0.55; border-style: dashed; box-shadow: none; }
  .node-label { font-size: 12.5px; color: var(--m-muted); font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .node-value { font-size: 22px; font-weight: 650; letter-spacing: -0.02em; line-height: 1.15; white-space: nowrap; }
  .node-foot { grid-column: 1 / -1; display: flex; justify-content: space-between; gap: 8px; font-size: 12px; color: var(--m-muted); border-top: 1px solid var(--m-line); margin-top: 8px; padding-top: 8px; }
  .node-foot b { color: var(--m-text); font-weight: 600; white-space: nowrap; }
  .n-pv { left: 0; top: 8px; }
  .n-bat { left: 0; bottom: 8px; }
  .n-home { right: 0; top: 8px; }
  .n-grid { right: 0; bottom: 8px; }
  .hub {
    position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); z-index: 1;
    width: 136px; height: 136px; border-radius: 50%; display: grid; place-items: center; text-align: center;
    background: radial-gradient(circle at 50% 35%, color-mix(in srgb, var(--m-accent) 16%, var(--m-surface-2)), var(--m-surface-2) 70%);
    border: 1px solid color-mix(in srgb, var(--m-accent) 38%, transparent);
    box-shadow: 0 0 0 8px color-mix(in srgb, var(--m-accent) 5%, transparent), 0 0 48px color-mix(in srgb, var(--m-accent) 13%, transparent);
  }
  .hub :global(svg.i) { width: 24px; height: 24px; color: var(--m-accent); }
  .hub-label { font-size: 11.5px; color: var(--m-muted); margin-top: 2px; max-width: 110px; }
  .hub-value { font-size: 18px; font-weight: 650; }
  .hub-charge { font-size: 11px; color: var(--m-muted); margin-top: 2px; }
  .hub-charge::before { content: ""; display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: var(--grid); margin-right: 5px; vertical-align: 1px; }
  .hub.offline { opacity: 0.55; border-style: dashed; }
  .soc-mini { grid-column: 1 / -1; height: 5px; border-radius: 99px; background: var(--m-well); overflow: hidden; margin-top: 4px; }
  .soc-mini > span { display: block; height: 100%; transform-origin: left center; background: var(--battery); }

  .flow-stage.devices {
    display: grid; grid-template-columns: minmax(0, 214px) minmax(40px, 1fr) 112px minmax(40px, 1fr) minmax(0, 214px);
    row-gap: 14px; align-items: center; padding: 4px 0;
  }
  .flow-stage.devices .node, .flow-stage.devices .hub { position: relative; left: auto; right: auto; top: auto; bottom: auto; transform: none; }
  .flow-stage.devices .node { width: 100%; padding: 11px 14px; }
  .flow-stage.devices .node-value { font-size: 18px; }
  .flow-stage.devices .hub { width: 104px; height: 104px; justify-self: center; grid-column: 3; }
  .flow-stage.devices .hub-value { font-size: 15px; }
  .flow-stage.devices .col-l { grid-column: 1; }
  .flow-stage.devices .col-r { grid-column: 5; }

  .flow-stage.system.narrow .node { width: calc(50% - 6px); padding: 10px 12px; grid-template-columns: minmax(0, 1fr); }
  .flow-stage.system.narrow .node .icon-tile { display: none; }
  .flow-stage.system.narrow .node-value { font-size: 19px; }
  .flow-stage.system.narrow .node-foot { flex-direction: column; gap: 0; }
  .flow-stage.system.narrow .hub { width: 104px; height: 104px; }
  .flow-stage.devices.narrow { grid-template-columns: minmax(0, 1fr) 12px 72px 12px minmax(0, 1fr); row-gap: 10px; }
  .flow-stage.devices.narrow .node { padding: 9px 10px; grid-template-columns: minmax(0, 1fr); }
  .flow-stage.devices.narrow .node .icon-tile { display: none; }
  .flow-stage.devices.narrow .node-value { font-size: 16px; }
  .flow-stage.devices.narrow .hub { width: 72px; height: 72px; }
  .flow-stage.devices.narrow .hub :global(svg.i), .flow-stage.devices.narrow .hub-label { display: none; }
  .flow-stage.devices.narrow .hub-value { font-size: 13px; }
  .flow-stage.devices.narrow .node-foot { flex-direction: column; gap: 0; }
</style>
