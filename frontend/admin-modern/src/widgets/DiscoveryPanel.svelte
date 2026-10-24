<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import ConfirmDialog from "../../../shared/ui/ConfirmDialog.svelte";
  import { t, tn } from "../i18n/i18n.svelte.js";
  import { settings, setHardwareValues } from "../state/settings.svelte.js";
  import { discovery, search, scanManually, clearResults, ignore, progressPercent } from "../state/discovery.svelte.js";
  import {
    addFound,
    brokerProfile,
    buildReview,
    configuredFor,
    externalDeviceFromProposal,
    isControlDowngrade,
    isExternalProposal,
    issuedPhysicalIdentity,
    itemAction,
    itemKey,
    meterFromProposal,
    mqttDeviceFromProposal,
    proposalIdentity,
    proposalRole,
    proposalTopic,
    switchTransport,
    useExternalConnection,
  } from "../lib/discovery.js";
  import { connectionSource, reasonKey } from "../lib/devices.js";

  let { config } = $props();

  const STATUS = { found: "found", new: "new", missing: "missing", conflict: "moved", transport: "alternative", added: "added", identity_conflict: "identityConflict", unavailable: "unavailable" };

  let manual = $state("");
  let manualError = $state("");
  let replacing = $state.raw(null);

  const catalog = $derived(config.catalog);
  const draft = $derived(settings.draft);
  const saved = $derived(settings.saved?.draft);
  const review = $derived(discovery.searched ? buildReview(Object.values(discovery.found), discovery.proposals, draft, saved) : []);
  const counts = $derived(review.reduce((all, item) => ({ ...all, [item.state]: (all[item.state] || 0) + 1 }), {}));
  const busy = $derived(discovery.running || discovery.scanning);
  const percent = $derived(progressPercent(discovery.progress, discovery.scans));
  const current = $derived(discovery.scans.find((scan) => scan.status === "running" && scan.total_hosts > 0) || null);

  function reasonOf(proposal) {
    return t(reasonKey(proposal?.control_block_reason || proposal?.output_control_reason));
  }

  function sourceLabel(source) {
    if (source === "zendure_mqtt" || source === "zendure_cloud_mqtt") return t("settings.source.zendure_cloud_mqtt");
    if (source === "local_mqtt") return t("settings.source.local_mqtt");
    if (source === "local_api") return t("settings.kind.local_api");
    return "";
  }

  function title(item) {
    if (item.proposal) return item.proposal.display_name || t("discovery.mqttDevice");
    if (item.role === "grid_meter") return t("discovery.title.gridMeter");
    if (item.role === "read_only_device") return t("discovery.title.readOnly");
    return t("discovery.title.inverter");
  }

  function label(item) {
    if (item.proposal) return [item.proposal.catalog_label || item.proposal.hardware_generation_label || item.proposal.hardware_generation || "Zendure MQTT", item.proposal.alternative_layout ? t("devices.mqtt.alternativeLayout") : ""].filter(Boolean).join(" · ");
    if (item.role === "grid_meter") return t("devices.grid.title");
    if (item.configured) return item.configured.name || t("discovery.configured");
    return item.discovered?.display_name || item.discovered?.model || t("devices.model.local");
  }

  function meta(item) {
    if (item.proposal) {
      const p = item.proposal;
      return [p.serial_number || p.device_id || "", sourceLabel(p.connection_source)].filter(Boolean).join(" · ");
    }
    const found = item.discovered || {};
    const configured = item.configured || {};
    const serial = found.serial_number || configured.sn || "";
    return [found.ip || configured.ip || "", serial ? `SN ${serial}` : t("devices.serialMissing"), found.api_family || configured.api_family || "", found.device_type || configured.type || ""].filter(Boolean).join(" · ");
  }

  function relationship(item) {
    if (item.state !== "transport") return "";
    const configured = configuredFor(item, draft);
    return configured ? t("discovery.alreadyAs", { name: configured.name || t("settings.unnamed"), via: sourceLabel(connectionSource(configured)) || "—" }) : "";
  }

  function apply(item, action) {
    const devices = draft?.devices || [];
    let result = null;
    if (item.proposal) {
      const proposal = item.proposal;
      if (action.kind === "meter") {
        const current = draft?.grid_meter;
        if (current && (current.present || current.type)) {
          replacing = item;
          return;
        }
        adoptMeter(proposal);
        return;
      }
      if (action.kind === "addProposal") result = { devices: [...devices, mqttDeviceFromProposal(proposal, devices, catalog)] };
      else if (action.kind === "addExternal") result = { devices: [...devices, externalDeviceFromProposal(proposal, devices)] };
      else if (action.kind === "externalConnection") result = useExternalConnection(devices, proposal);
      else if (action.kind === "switch") result = switchTransport(devices, proposalIdentity(proposal), String(proposal.connection_source || "") === "zendure_cloud_mqtt" ? "zendure_mqtt" : "local_mqtt", { proposal }, catalog);
    } else if (action.kind === "add") {
      result = addFound(draft, item, catalog);
    } else if (action.kind === "updateAddress") {
      const next = [...devices];
      if (!next[item.index]) return;
      next[item.index] = { ...next[item.index], ip: item.discovered?.ip || next[item.index].ip };
      result = { devices: next };
    } else if (action.kind === "switch") {
      result = switchTransport(devices, issuedPhysicalIdentity(item.configured), item.targetSource || "local_api", { discovered: item.discovered }, catalog);
    }
    if (!result) return;
    const changes = {};
    if (result.devices) changes.devices = result.devices;
    if (result.grid_meter) changes.grid_meter = result.grid_meter;
    setHardwareValues(changes);
  }

  function adoptMeter(proposal) {
    const mapping = meterFromProposal(proposal);
    if (!mapping) return;
    const broker = brokerProfile(proposal, mapping.mqtt);
    setHardwareValues({ grid_meter: broker.ref ? { ...mapping, broker } : mapping });
  }

  async function runManual(event) {
    event.preventDefault();
    manualError = await scanManually(manual);
  }

  function failures() {
    const causes = [...discovery.causes];
    return causes.length > 3 ? `${causes.slice(0, 3).join("; ")}; …` : causes.join("; ");
  }
</script>

<div class="discovery" data-testid="discovery">
  <div class="row-wrap controls">
    <button class="btn" type="button" onclick={() => search(draft)} disabled={busy} data-testid="discovery-start"><Icon name={discovery.running ? "loader" : "search"} size="sm" />{discovery.running ? t("discovery.searching") : t("discovery.start")}</button>
    <form class="manual" onsubmit={runManual}>
      <input class="input num" type="text" inputmode="decimal" placeholder="192.168.1.0/24" aria-label={t("discovery.manualLabel")} bind:value={manual} data-testid="discovery-manual" />
      <button class="btn secondary small" type="submit" disabled={busy}>{t("discovery.scan")}</button>
    </form>
    {#if discovery.searched}<button class="btn secondary small" type="button" onclick={clearResults} disabled={busy} data-testid="discovery-clear"><Icon name="eraser" size="sm" />{t("discovery.clear")}</button>{/if}
  </div>
  {#if manualError}<p class="form-error">{t(`discovery.target.${manualError}`)}</p>{/if}
  <p class="muted small">{t("discovery.lead")}</p>

  {#if discovery.progress.total}
    <div class="progress" role="status">
      <div class="track"><div class="fill" style:width={`${percent}%`}></div></div>
      <span class="muted small num">{t("discovery.progress", { done: discovery.progress.done + discovery.progress.failed, total: discovery.progress.total, active: discovery.progress.active, found: Object.keys(discovery.found).length, failed: discovery.progress.failed })}{#if current}{` · ${current.cidr} ${current.checked_hosts}/${current.total_hosts}`}{/if}</span>
    </div>
  {/if}
  {#if discovery.outcome}
    <p class="note" class:warn={["warnings", "manualWarnings", "failed"].includes(discovery.outcome)} data-testid="discovery-outcome">
      <Icon name={["warnings", "manualWarnings", "failed"].includes(discovery.outcome) ? "triangle-alert" : "info"} />
      <span>{t(`discovery.outcome.${discovery.outcome}`)}{#if ["warnings", "manualWarnings"].includes(discovery.outcome) && discovery.causes.length} <span lang="en">({failures()})</span>{/if}{#if discovery.cloudSkipped && discovery.outcome === "done"} {t("discovery.cloudSkipped")}{/if}</span>
    </p>
  {/if}

  {#if discovery.searched && !discovery.running && review.length}
    <p class="summary num" data-testid="discovery-summary">{t("discovery.summary", { found: counts.found || 0, new: counts.new || 0, missing: counts.missing || 0, moved: counts.conflict || 0 })}</p>
  {/if}

  {#if review.length}
    <ul class="items" data-testid="discovery-items">
      {#each review as item, index (`${itemKey(item)}-${index}`)}
        {@const key = itemKey(item)}
        {@const action = itemAction(item, draft, discovery.ignored.includes(key))}
        {@const downgrade = isControlDowngrade(item, draft)}
        {@const note = relationship(item)}
        <li class="item inner" data-state={item.state} data-role={item.proposal ? proposalRole(item.proposal) : item.role}>
          <div class="head">
            <div class="words">
              <span class="name">{title(item)}</span>
              <span class="muted small">{label(item)}</span>
              <span class="muted small num">{meta(item)}</span>
            </div>
            <Status tone={["found", "added"].includes(item.state) ? "c-ok" : ["missing", "identity_conflict", "unavailable"].includes(item.state) ? "c-warn" : "c-acc"} text={discovery.ignored.includes(key) ? t("discovery.status.ignored") : t(`discovery.status.${STATUS[item.state] || "new"}`)} />
          </div>
          {#if item.proposal}
            {#if proposalRole(item.proposal) === "grid_meter"}
              <p class="muted small">{item.state === "unavailable" ? t("discovery.note.meterUnavailable") : t("discovery.note.meter", { topic: proposalTopic(item.proposal) })}</p>
            {:else if item.proposal.output_control_supported}
              <p class="muted small">{t("discovery.note.controllable")}</p>
            {:else if isExternalProposal(item.proposal)}
              <p class="muted small">{t("discovery.note.external")}</p>
            {:else}
              <p class="muted small">{t("discovery.note.readOnly", { reason: reasonOf(item.proposal) })}</p>
            {/if}
            {#if item.state === "identity_conflict"}<p class="note warn"><Icon name="triangle-alert" />{t("discovery.note.identityConflict")}</p>{/if}
          {:else if item.role === "read_only_device"}
            <p class="muted small">{t("discovery.note.e3dc")}</p>
          {/if}
          {#if note}<p class="muted small">{note}</p>{/if}
          {#if downgrade}<p class="note warn"><Icon name="triangle-alert" />{t("discovery.note.downgrade", { reason: reasonOf(item.proposal) })}</p>{/if}
          <div class="row-wrap actions">
            <button class="btn small" class:secondary={action.disabled} type="button" disabled={action.disabled} onclick={() => apply(item, action)} data-testid="discovery-action">{t(`discovery.action.${action.label}`)}</button>
            {#if !item.proposal && ["new", "conflict"].includes(item.state) && !action.disabled}
              <button class="btn secondary small" type="button" onclick={() => ignore(key)} data-testid="discovery-ignore">{t("discovery.ignore")}</button>
            {/if}
          </div>
        </li>
      {/each}
    </ul>
  {:else if discovery.searched && !busy}
    <p class="muted">{t("discovery.none")}</p>
  {/if}
</div>

<ConfirmDialog
  open={Boolean(replacing)}
  title={t("discovery.replaceMeterTitle")}
  lines={[t("discovery.replaceMeter")]}
  confirmLabel={t("discovery.action.useAsGridMeter")}
  onconfirm={() => { const item = replacing; replacing = null; if (item) adoptMeter(item.proposal); }}
  oncancel={() => (replacing = null)}
/>

<style>
  .discovery { display: flex; flex-direction: column; gap: var(--s3); }
  .controls { gap: var(--s2); align-items: center; }
  .manual { display: flex; gap: var(--s2); flex: 1 1 260px; min-width: 0; }
  .manual .input { flex: 1; }
  .small { font-size: 12.5px; overflow-wrap: anywhere; }
  .progress { display: flex; flex-direction: column; gap: 4px; }
  .track { height: 6px; border-radius: 999px; background: var(--m-surface-3); overflow: hidden; }
  .fill { height: 100%; background: var(--m-accent); transition: width 0.3s; }
  .summary { margin: 0; font-weight: 560; }
  .items { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--s2); }
  .item { display: flex; flex-direction: column; gap: var(--s2); }
  .head { display: flex; align-items: flex-start; gap: var(--s3); }
  .words { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
  .name { font-weight: 600; }
  .item p { margin: 0; }
  .actions { gap: var(--s2); }
</style>
