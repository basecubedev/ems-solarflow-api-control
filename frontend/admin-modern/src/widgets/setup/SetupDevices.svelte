<script>
  import Card from "../../../../shared/ui/Card.svelte";
  import Icon from "../../../../shared/ui/Icon.svelte";
  import Status from "../../../../shared/ui/Status.svelte";
  import Bar from "../../../../shared/ui/Bar.svelte";
  import ConfirmDialog from "../../../../shared/ui/ConfirmDialog.svelte";
  import SourcesPanel from "../SourcesPanel.svelte";
  import { t, tn } from "../../i18n/i18n.svelte.js";
  import { errorView } from "../../lib/errors.js";
  import { reasonKey } from "../../lib/devices.js";
  import { isExternalProposal, isGridMeterProposal, proposalRole, proposalTopic } from "../../lib/discovery.js";
  import { isReady, proposalCanBeChosen, selection } from "../../lib/setupDraft.js";
  import { SOURCES, setupDiscovery, setupRoute, allNetworks, cancel, loadPreparation, loadSourceStatus, progressPercent, refresh, refreshMdns, rescanNetworks, rescanSource, resetResults, runDiscovery, scanManually, setMdns, sourceEnabled } from "../../state/setupDiscovery.svelte.js";
  import { setupDraft, enableSource, replacesGridMeter, reprioritize, toggleProposal } from "../../state/setupDraft.svelte.js";
  import { sources, routeSources } from "../../state/sources.svelte.js";

  let manual = $state("");
  let manualError = $state("");
  let configuring = $state("");
  let open = $state.raw([]);
  let resetting = $state(false);
  let replacing = $state.raw(null);
  let details = $state.raw({});

  const busy = $derived(setupDiscovery.running || setupDiscovery.scanning);
  const enabled = $derived(setupDiscovery.preparation.discovery_priority.filter((source) => setupDiscovery.preparation.sources?.[source]?.enabled !== false));
  const networks = $derived(allNetworks());
  const lan = $derived(networks.filter((network) => !network.is_docker_like));
  const docker = $derived(networks.filter((network) => network.is_docker_like));
  const unified = $derived(Array.isArray(setupDiscovery.unified?.devices) ? setupDiscovery.unified.devices : []);
  const unifiedDetails = $derived(setupDiscovery.unified?.details || {});
  const found = $derived(setupDiscovery.devices);
  const inverterCount = $derived(found.filter((device) => device.role_suggestion === "inverter").length);
  const meterCount = $derived(found.filter((device) => device.role_suggestion === "grid_meter").length);
  const brokerDevices = $derived(setupDiscovery.brokers.reduce((sum, broker) => sum + (Array.isArray(broker.devices) ? broker.devices.length : 0), 0));
  const cloudDevices = $derived(Array.isArray(unifiedDetails.zendure_mqtt?.candidates) ? unifiedDetails.zendure_mqtt.candidates : []);
  const mdnsState = $derived(mdnsStateOf(setupDiscovery.mdns));
  const percent = $derived(progressPercent());
  const running = $derived(setupDiscovery.scans.find((entry) => entry.status === "running") || null);

  $effect(() => {
    routeSources(setupRoute);
    enter();
    return () => routeSources(null);
  });

  /** The first visit runs discovery on its own; a later one only reads what it found. */
  async function enter() {
    await loadPreparation();
    if (setupDiscovery.started) {
      refresh();
      return;
    }
    setupDiscovery.started = true;
    runDiscovery();
  }

  function mdnsStateOf(status) {
    if (!status) return "checking";
    const state = String(status.state || (status.available ? "disabled" : "unavailable_dependency"));
    if (state.startsWith("running_")) return "enabled";
    if (state.startsWith("unavailable_")) return "unavailable";
    return state === "request_failed" ? "noAnswer" : state === "disabled" ? "disabled" : "checking";
  }

  function mdnsMessage(status) {
    const text = String(status?.message || "");
    const cause = typeof status?.last_error === "string" ? status.last_error.trim() : "";
    return cause && ["unavailable", "noAnswer"].includes(mdnsState) ? `${text} ${cause}`.trim() : text;
  }

  function sourceCount(source) {
    return Number(unifiedDetails[source]?.device_count || 0);
  }

  function sourceLabel(source) {
    return SOURCES.includes(source) ? t(`setup.source.${source}`) : String(source);
  }

  function toggle(list, key) {
    return list.includes(key) ? list.filter((item) => item !== key) : [...list, key];
  }

  function openDetail(source) {
    details = { ...details, [source]: true };
    queueMicrotask(() => document.getElementById(`setup-detail-${source}`)?.scrollIntoView({ behavior: "smooth", block: "start" }));
  }

  async function runManual(event) {
    event.preventDefault();
    manualError = await scanManually(manual);
  }

  function chooseProposal(proposal) {
    if (!selection(setupDraft.draft, proposal.id) && replacesGridMeter(proposal)) {
      replacing = proposal;
      return;
    }
    toggleProposal(proposal.id);
  }

  function title(device) {
    return device.role === "grid_meter" ? t("setup.devices.gridMeter") : device.role === "inverter" ? t("setup.devices.inverter") : t("setup.devices.device");
  }

  function identity(device) {
    if (device.read_only === true) return device.device_id ? `ID ${device.device_id}` : t("setup.devices.idMissing");
    return device.serial_number ? `SN ${device.serial_number}` : t("setup.devices.snMissing");
  }

  const ATTEMPTS = ["tcp_open", "mqtt_connected", "mqtt_listened_no_topics", "topics_seen", "auth_failed", "tls_failed", "connection_failed", "timeout"];
</script>

{#snippet facts(rows)}
  <div class="facts-list">
    {#each rows as row (row[0])}
      {#if row[1] !== null && row[1] !== undefined && row[1] !== ""}
        <div class="fact-line"><span class="fact-name">{row[0]}</span><span class="fact-text num" lang={row[2] ? "en" : null}>{row[1]}</span></div>
      {/if}
    {/each}
  </div>
{/snippet}

{#snippet failure(error)}
  {@const view = errorView(error, t)}
  <span lang={view.lang}>{view.text}</span>
{/snippet}

<Card title={t("setup.devices.title")} subtitle={t("setup.devices.sub")} span="span-8">
  {#snippet actions()}
    <button class="btn small" class:secondary={setupDiscovery.scanning} type="button" onclick={() => (setupDiscovery.running ? cancel() : runDiscovery())} disabled={setupDiscovery.scanning} data-testid="setup-discovery-run">
      <Icon name={setupDiscovery.running ? "x" : "search"} size="sm" />{setupDiscovery.running ? t("setup.devices.cancel") : setupDiscovery.hasRun ? t("setup.devices.runAgain") : t("setup.devices.run")}
    </button>
    <button class="btn secondary small" type="button" onclick={() => (setupDiscovery.scanning ? cancel() : rescanNetworks())} disabled={setupDiscovery.running} data-testid="setup-networks-scan">
      <Icon name={setupDiscovery.scanning ? "x" : "network"} size="sm" />{setupDiscovery.scanning ? t("setup.devices.cancelScan") : t("setup.devices.scanNetworks")}
    </button>
  {/snippet}
  <div class="stack">
    <p class="muted small">{t("setup.devices.lead")}</p>
    <div class="row-wrap head-line">
      <h3 class="sub">{t("setup.devices.priority")}</h3>
      <span class="badge num" data-testid="setup-source-count">{tn("setup.devices.sourcesEnabled", enabled.length)}</span>
    </div>
    <ol class="sources" data-testid="setup-sources">
      {#each setupDiscovery.preparation.discovery_priority as source, index (source)}
        {@const on = sourceEnabled(source)}
        <li class="source inner" class:off={!on} data-source={source}>
          <div class="row">
            <span class="rank num">{index + 1}</span>
            <label class="check grow"><input type="checkbox" checked={on} onchange={(event) => enableSource(source, event.currentTarget.checked)} /><span>{t(`setup.source.${source}`)}</span></label>
            <button class="icon-btn" type="button" disabled={index === 0} onclick={() => reprioritize(source, -1)} aria-label={t("setup.devices.up", { source: t(`setup.source.${source}`) })}><Icon name="arrow-up" size="sm" /></button>
            <button class="icon-btn" type="button" disabled={index === SOURCES.length - 1} onclick={() => reprioritize(source, 1)} aria-label={t("setup.devices.down", { source: t(`setup.source.${source}`) })}><Icon name="arrow-down" size="sm" /></button>
            <button class="btn secondary small" type="button" aria-expanded={configuring === source ? "true" : "false"} onclick={() => (configuring = configuring === source ? "" : source)} data-testid={`setup-configure-${source}`}>{configuring === source ? t("setup.devices.close") : t("setup.devices.configure")}</button>
          </div>
          {#if configuring === source}
            <div class="configure">
              <p class="muted small">
                {on ? t("setup.devices.enabled") : t("setup.devices.disabled")} ·
                {#if source === "zendure_mqtt"}{sources.cloud?.token_saved ? t("setup.devices.keySaved") : t("setup.devices.noKey")} · {/if}{tn("setup.devices.found", sourceCount(source))}
              </p>
              {#if source === "local_api"}
                <div class="mdns inner">
                  <div class="row-wrap">
                    <strong>{t("setup.devices.mdns")}</strong>
                    <Status tone={mdnsState === "enabled" ? "c-ok" : mdnsState === "unavailable" || mdnsState === "noAnswer" ? "c-warn" : "c-idle"} text={t(`setup.mdns.${mdnsState}`)} />
                    <span class="muted small num">{tn("setup.devices.found", Number(setupDiscovery.mdns?.verified_count) || 0)}</span>
                  </div>
                  {#if mdnsMessage(setupDiscovery.mdns)}<p class="muted small" lang="en">{mdnsMessage(setupDiscovery.mdns)}</p>{/if}
                  <div class="row-wrap">
                    <button class="btn secondary small" type="button" onclick={refreshMdns} disabled={mdnsState === "unavailable" || setupDiscovery.mdns?.controls_unavailable === true}>{t("setup.devices.mdnsRefresh")}</button>
                    <button class="btn secondary small" type="button" onclick={() => setMdns(!setupDiscovery.mdns?.enabled)} disabled={mdnsState === "unavailable" || setupDiscovery.mdns?.controls_unavailable === true}>{setupDiscovery.mdns?.enabled ? t("setup.devices.mdnsOff") : t("setup.devices.mdnsOn")}</button>
                  </div>
                </div>
              {/if}
              <div class="row-wrap">
                <button class="btn small" type="button" onclick={() => rescanSource(source)} disabled={busy || Boolean(sources.busy)}>{t("setup.devices.rescan", { source: t(`setup.source.${source}`) })}</button>
                <button class="btn secondary small" type="button" onclick={() => openDetail(source)}>{t("setup.devices.openResults")}</button>
                {#if source === "local_api"}<button class="btn secondary small" type="button" onclick={() => (resetting = true)} disabled={busy}>{t("setup.devices.reset")}</button>{/if}
              </div>
            </div>
          {/if}
        </li>
      {/each}
    </ol>
  </div>
</Card>

<Card title={t("setup.devices.progress")} span="span-4">
  <div class="stack" data-testid="setup-discovery-progress">
    {#if setupDiscovery.progress.total}
      <Bar value={percent} label={t("setup.devices.progress")} />
      <span class="muted small num">{t("discovery.progress", { done: setupDiscovery.progress.done + setupDiscovery.progress.failed, total: setupDiscovery.progress.total, active: setupDiscovery.progress.active, found: found.length, failed: setupDiscovery.progress.failed })}{#if running}{` · ${running.cidr} ${running.checked_hosts}/${running.total_hosts}`}{/if}</span>
    {:else}
      <p class="muted small">{t("setup.devices.idle")}</p>
    {/if}
    {#if setupDiscovery.gateError}<p class="note warn"><Icon name="triangle-alert" />{@render failure(setupDiscovery.gateError)}</p>{/if}
    <div class="facts two">
      <div class="fact"><div class="fact-label">{t("setup.devices.inverters")}</div><div class="fact-value num">{inverterCount}</div></div>
      <div class="fact"><div class="fact-label">{t("setup.devices.gridMeters")}</div><div class="fact-value num">{meterCount}</div></div>
      <div class="fact"><div class="fact-label">{t("setup.devices.networks")}</div><div class="fact-value num">{lan.length}</div></div>
      <div class="fact"><div class="fact-label">{t("setup.devices.routers")}</div><div class="fact-value num">{setupDiscovery.gateways.length}</div></div>
      <div class="fact"><div class="fact-label">mDNS</div><div class="fact-value">{t(`setup.mdns.${mdnsState}`)}</div></div>
      <div class="fact"><div class="fact-label">{t("setup.devices.brokers")}</div><div class="fact-value num">{setupDiscovery.brokers.length}</div></div>
    </div>
  </div>
</Card>

<Card title={t("setup.devices.unified")} subtitle={tn("setup.devices.deviceCount", unified.length)} span="span-12">
  <div class="stack" data-testid="setup-unified">
    {#if unified.length}
      <ul class="cards">
        {#each unified as device, index (`${device.id || device.serial_number || device.display_name || "device"}-${index}`)}
          {@const key = String(device.id || device.serial_number || device.display_name || index)}
          <li class="card-item inner" data-role={device.role || "unknown"}>
            <button class="summary" type="button" aria-expanded={open.includes(key) ? "true" : "false"} onclick={() => (open = toggle(open, key))}>
              <span class="words">
                <span class="name">{title(device)}</span>
                <span class="muted small" lang="en">{device.display_name || device.model_hint || device.device_type || t("setup.devices.device")}</span>
                <span class="muted small num">{[device.ip, identity(device), device.api_family, device.device_type].filter(Boolean).join(" · ")}</span>
              </span>
              <Status tone={device.read_only === true ? "c-idle" : device.confidence === "low" ? "c-warn" : "c-ok"} text={device.read_only === true ? t("setup.devices.readOnly") : device.confidence === "low" ? t("setup.devices.lowConfidence") : t("setup.devices.detectedOne")} />
              <Icon name={open.includes(key) ? "chevron-up" : "chevron-down"} size="sm" />
            </button>
            {#if open.includes(key)}
              {@render facts([["IP", device.ip, true], [t("setup.devices.identity"), identity(device), true], [t("setup.devices.apiFamily"), device.api_family, true], [t("setup.devices.type"), device.device_type, true]])}
              <p class="badges">
                {#each Array.isArray(device.sources) ? device.sources : [] as source (source)}<span class="badge" class:chosen={source === device.selected_source}>{sourceLabel(source)}</span>{/each}
                {#if device.selected_source}<span class="muted small">{t("setup.devices.selectedBy", { source: sourceLabel(device.selected_source) })}</span>{/if}
              </p>
            {/if}
          </li>
        {/each}
      </ul>
    {:else}
      <p class="muted">{setupDiscovery.hasRun ? t("setup.devices.noneDetected") : t("setup.devices.runFirst")}</p>
    {/if}
    <div class="row-wrap chips" data-testid="setup-source-chips">
      {#each SOURCES as source (source)}
        <button class="btn secondary small" type="button" onclick={() => openDetail(source)}><strong>{t(`setup.source.${source}`)}</strong>&nbsp;<span class="muted num">{sourceEnabled(source) ? tn("setup.devices.found", sourceCount(source)) : t("setup.devices.disabled")}</span></button>
      {/each}
    </div>
  </div>
</Card>

<Card title={t("setup.devices.networksTitle")} span="span-6">
  <div class="stack" data-testid="setup-networks">
    {#each setupDiscovery.networkWarnings as warning, index (index)}<p class="note warn"><Icon name="triangle-alert" /><span lang="en">{warning}</span></p>{/each}
    {#if lan.length}
      <ul class="chips-list">
        {#each lan as network (network.cidr)}
          {@const gateway = network.source === "gateway"}
          <li class="chip inner" class:recommended={gateway || network.scan_recommended} title={network.reason || ""}>
            <span class="num">{network.cidr}</span>
            <span class="badge">{gateway ? t("setup.devices.gateway") : network.scan_recommended ? t("setup.devices.recommended") : t("setup.devices.advanced")}</span>
            <span class="muted small num">{gateway ? t("setup.devices.viaGateway", { gateway: network.gateway_candidate || "" }) : [network.interface, network.address].filter(Boolean).join(" · ")}{#if !gateway && network.original_cidr && network.original_cidr !== network.cidr} · {t("setup.devices.was", { cidr: network.original_cidr })}{/if}</span>
          </li>
        {/each}
      </ul>
    {:else}
      <p class="muted small">{setupDiscovery.detecting ? t("setup.devices.detecting") : docker.length ? t("setup.devices.onlyContainer") : t("setup.devices.noNetworks")}</p>
    {/if}
    {#if docker.length}
      <details>
        <summary>{t("setup.devices.containers")}</summary>
        <ul class="chips-list">
          {#each docker as network (network.cidr)}
            <li class="chip inner"><span class="num">{network.cidr}</span><span class="muted small num">{[network.interface, network.address].filter(Boolean).join(" · ")}</span><button class="btn secondary small" type="button" disabled={busy} onclick={async () => (manualError = await scanManually(network.cidr))}>{t("discovery.scan")}</button></li>
          {/each}
        </ul>
      </details>
    {/if}
    <p class="muted small">{t(`setup.gateway.${setupDiscovery.gatewayState || "idle"}`)}</p>
  </div>
</Card>

<Card title={t("setup.devices.manualTitle")} span="span-6">
  <form class="stack" onsubmit={runManual}>
    <p class="muted small">{t("setup.devices.manualLead")}</p>
    <div class="row-wrap">
      <input class="input num grow" type="text" inputmode="decimal" placeholder="192.168.178.0/24" aria-label={t("discovery.manualLabel")} bind:value={manual} data-testid="setup-manual-scan" />
      <button class="btn secondary small" type="submit" disabled={busy}>{t("setup.devices.scanManually")}</button>
    </div>
    {#if manualError}<p class="form-error">{t(`discovery.target.${manualError}`)}</p>{/if}
  </form>
</Card>

<Card title={t("setup.devices.details")} span="span-12">
  <div class="stack">
    <details id="setup-detail-local_api" open={details.local_api} ontoggle={(event) => (details = { ...details, local_api: event.currentTarget.open })}>
      <summary>{t("setup.source.local_api")} <span class="muted num">({tn("setup.devices.deviceCount", found.length)})</span></summary>
      {#if found.length}
        <ul class="cards">
          {#each found as device (device.observation_id || `${device.ip}-${device.port}`)}
            <li class="card-item inner" data-role={device.role_suggestion || "unknown"}>
              <div class="row-wrap"><strong lang="en">{device.display_name || device.device_type || t("setup.devices.device")}</strong><span class="badge">{device.role_suggestion || "unknown"}</span>{#if device.stale}<span class="badge warn">{t("setup.devices.stale")}</span>{/if}</div>
              {@render facts([["IP", device.ip, true], [t("setup.devices.serial"), device.serial_number || t("setup.devices.missing"), Boolean(device.serial_number)], [t("setup.devices.apiFamily"), device.api_family, true], [t("setup.devices.type"), device.device_type, true]])}
              <p class="muted small">{isReady(device) ? t("setup.devices.configReady") : t("setup.devices.needsInfo")} · {t("setup.devices.match", { percent: Math.round((Number(device.confidence) || 0) * 100) })}</p>
            </li>
          {/each}
        </ul>
      {:else}
        <p class="muted small">{busy ? t("setup.devices.waitScan") : t("setup.devices.noneSupported")}</p>
      {/if}
      {#if setupDiscovery.ignored.length}
        <details>
          <summary>{tn("setup.devices.ignored", setupDiscovery.ignored.length)}</summary>
          <ul class="cards">
            {#each setupDiscovery.ignored as device, index (index)}
              <li class="card-item inner">
                <div class="row-wrap"><strong lang="en">{device.service_name || device.display_name || "mDNS"}</strong>{#if device.stale}<span class="badge warn">{t("setup.devices.stale")}</span>{/if}</div>
                <p class="muted small num" lang="en">{[device.source_detail, device.ip ? `${device.ip}${device.port ? `:${device.port}` : ""}` : "", [device.vendor, device.model_hint].filter(Boolean).join(" · "), device.last_seen ? `seen: ${device.last_seen}` : "", device.last_verify_attempt ? `verified: ${device.last_verify_attempt}` : ""].filter(Boolean).join(" · ")}</p>
                <p class="small" lang="en">{device.reason || t("setup.devices.notSupported")}</p>
              </li>
            {/each}
          </ul>
        </details>
      {/if}
    </details>

    <details id="setup-detail-local_mqtt" open={details.local_mqtt} ontoggle={(event) => (details = { ...details, local_mqtt: event.currentTarget.open })}>
      <summary>{t("setup.source.local_mqtt")} <span class="muted num">({tn("setup.devices.brokerCount", setupDiscovery.brokers.length)} · {tn("setup.devices.deviceCount", brokerDevices)})</span></summary>
      {#if setupDiscovery.brokerMessage}
        <p class="muted small">{t(`setup.brokers.${setupDiscovery.brokerMessage.kind}`, setupDiscovery.brokerMessage.values || {})}{#if setupDiscovery.brokerMessage.error} {@render failure(setupDiscovery.brokerMessage.error)}{/if}</p>
      {/if}
      {#if setupDiscovery.brokers.length}
        <ul class="cards">
          {#each setupDiscovery.brokers as broker (`${broker.host}:${broker.port}`)}
            {@const devices = Array.isArray(broker.devices) ? broker.devices : []}
            <li class="card-item inner">
              <div class="row-wrap"><strong class="num">{t("setup.devices.broker", { endpoint: `${broker.host || ""}:${broker.port || ""}` })}</strong><span class="badge" lang="en">{String(broker.status || (broker.reachable ? "reachable" : "candidate")).replace(/_/g, " ")}</span></div>
              {@render facts([[t("setup.devices.source"), broker.source === "mdns" ? "mDNS" : broker.source === "network_probe" ? t("setup.devices.networkProbe") : broker.source || "—", true], [t("setup.devices.transport"), broker.transport === "tls" ? "TLS" : t("setup.devices.plain"), false], [t("setup.devices.hostname"), broker.hostname || "—", true], [t("setup.devices.lastSeen"), broker.last_seen || "—", true]])}
              {#if Array.isArray(broker.attempts) && broker.attempts.length}
                <ul class="attempts">
                  {#each broker.attempts as attempt, index (index)}
                    <li class="small"><span lang="en">{attempt.label || attempt.credential_ref || t("setup.devices.anonymous")}</span>: {ATTEMPTS.includes(attempt.status) ? t(`setup.attempt.${attempt.status}`) : String(attempt.status || "").replace(/_/g, " ")}{#if Number(attempt.device_count) > 0}, {tn("setup.devices.deviceCount", Number(attempt.device_count))}{/if}</li>
                  {/each}
                </ul>
              {/if}
              {#each devices as device, index (index)}
                <div class="inner nested">
                  <strong lang="en">{device.display_name || t("discovery.mqttDevice")}</strong>
                  {@render facts([[t("setup.devices.deviceSn"), device.serial_number || device.device_id || "—", true], [t("setup.devices.topicFamily"), device.topic_family || "—", true], [t("setup.devices.model"), device.model_hint, true], [t("setup.devices.metrics"), (device.metrics_seen || []).slice(0, 8).join(", "), true], [t("setup.devices.topics"), (device.topics_seen || []).slice(0, 4).join(", "), true]])}
                </div>
              {:else}
                <p class="muted small">{t("setup.devices.noTopics")}</p>
              {/each}
            </li>
          {/each}
        </ul>
      {:else}
        <p class="muted small">{t("setup.devices.noBrokers")}</p>
      {/if}
    </details>

    <details id="setup-detail-zendure_mqtt" open={details.zendure_mqtt} ontoggle={(event) => (details = { ...details, zendure_mqtt: event.currentTarget.open })}>
      <summary>{t("setup.source.zendure_mqtt")} <span class="muted num">({tn("setup.devices.deviceCount", cloudDevices.length)})</span></summary>
      {#if cloudDevices.length}
        <ul class="cards">
          {#each cloudDevices as device, index (index)}
            <li class="card-item inner">
              <strong lang="en">{device.display_name || t("discovery.mqttDevice")}</strong>
              {@render facts([[t("setup.devices.deviceName"), device.device_name, true], [t("setup.devices.deviceSn"), device.serial_number || device.device_id || "—", true], [t("setup.devices.model"), device.model_hint, true], [t("setup.devices.status"), String(device.discovery_status || "device_list_only").replace(/_/g, " "), true], [t("setup.devices.topicFamily"), device.topic_family || "—", true], [t("setup.devices.metrics"), (device.metrics_seen || []).slice(0, 8).join(", "), true], ["TLS", device.tls_mode || "—", true]])}
            </li>
          {/each}
        </ul>
      {:else}
        <p class="muted small">{t("setup.devices.noCloud")}</p>
      {/if}
    </details>

    <details id="setup-detail-proposals" open={details.proposals} ontoggle={(event) => (details = { ...details, proposals: event.currentTarget.open })}>
      <summary>{t("setup.devices.proposals")} <span class="muted num">({tn("setup.devices.proposalCount", setupDiscovery.proposals.length)})</span></summary>
      <p class="muted small">{t("setup.devices.proposalsLead")}</p>
      {#if setupDiscovery.proposalsError}<p class="note warn"><Icon name="triangle-alert" />{@render failure(setupDiscovery.proposalsError)}</p>{/if}
      {#if setupDiscovery.proposals.length}
        <ul class="cards" data-testid="setup-proposals">
          {#each setupDiscovery.proposals as proposal (proposal.id)}
            {@const grid = isGridMeterProposal(proposal)}
            {@const chosen = Boolean(selection(setupDraft.draft, proposal.id))}
            {@const controllable = !grid && Boolean(proposal.output_control_supported)}
            <li class="card-item inner" data-role={proposalRole(proposal)}>
              <div class="row-wrap"><strong lang="en">{proposal.display_name || t("discovery.mqttDevice")}</strong><span class="badge" lang="en">{t("setup.devices.confidence", { value: String(proposal.confidence || "unknown") })}</span></div>
              <p class="badges">
                {#if grid}<span class="badge">{t("setup.devices.gridMeter")}</span><span class="badge">{t("setup.devices.readOnly")}</span>
                {:else if controllable}<span class="badge">{t("setup.devices.controlAvailable")}</span><span class="badge">{t(`setup.source.${proposal.connection_source === "zendure_cloud_mqtt" ? "zendure_mqtt" : "local_mqtt"}`)}</span>
                {:else}<span class="badge">{isExternalProposal(proposal) ? t("setup.devices.readOnly") : t("setup.devices.telemetryOnly")}</span>{/if}
              </p>
              {@render facts([[t("setup.devices.deviceSn"), proposal.serial_number || proposal.device_id || proposal.id, true], [t("setup.devices.generation"), proposal.catalog_label || proposal.hardware_generation_label || proposal.hardware_generation || "Zendure MQTT", true], [t("setup.devices.topic"), grid ? proposalTopic(proposal) || "—" : "", true], [t("setup.devices.reason"), !grid && !controllable ? t(reasonKey(proposal.control_block_reason || proposal.output_control_reason)) : "", false], [t("setup.devices.capabilities"), (proposal.capabilities || []).join(", "), true], [t("setup.devices.metrics"), (proposal.metrics || []).slice(0, 8).join(", "), true]])}
              {#each proposal.warnings || [] as warning, index (index)}<p class="note warn"><Icon name="triangle-alert" /><span lang="en">{String(warning)}</span></p>{/each}
              {#if proposalCanBeChosen(proposal)}
                <div class="row-wrap">
                  <button class="btn small" class:secondary={chosen} type="button" onclick={() => chooseProposal(proposal)} data-testid="setup-proposal-toggle">{grid ? (chosen ? t("setup.devices.gridChosen") : t("setup.devices.useAsGrid")) : chosen ? t("setup.devices.added") : t("setup.devices.addToConfig")}</button>
                  {#if chosen}<span class="muted small">{grid ? t("setup.devices.gridReadOnly") : controllable ? t("setup.devices.controlEnabled") : t("setup.devices.telemetryOnlyNote")}</span>{/if}
                </div>
              {/if}
            </li>
          {/each}
        </ul>
      {:else}
        <p class="muted small">{t("setup.devices.noProposals")}</p>
      {/if}
    </details>
  </div>
</Card>

<Card title={t("setup.devices.sourcesTitle")} subtitle={t("setup.devices.sourcesSub")} span="span-12">
  <SourcesPanel onchanged={() => { loadSourceStatus(); refresh(); }} />
</Card>

<ConfirmDialog
  open={resetting}
  title={t("setup.devices.resetTitle")}
  lines={[t("setup.devices.resetLine")]}
  confirmLabel={t("setup.devices.reset")}
  onconfirm={() => { resetting = false; resetResults(); }}
  oncancel={() => (resetting = false)}
/>
<ConfirmDialog
  open={Boolean(replacing)}
  title={t("discovery.replaceMeterTitle")}
  lines={[t("discovery.replaceMeter")]}
  confirmLabel={t("setup.devices.useAsGrid")}
  onconfirm={() => { const proposal = replacing; replacing = null; if (proposal) toggleProposal(proposal.id, { replace: true }); }}
  oncancel={() => (replacing = null)}
/>

<style>
  .stack { display: flex; flex-direction: column; gap: var(--s3); min-width: 0; }
  .stack > p, .stack p { margin: 0; }
  .small { font-size: 12.5px; overflow-wrap: anywhere; }
  .sub { font-size: 13px; font-weight: 620; margin: 0; }
  .head-line { align-items: center; gap: var(--s2); }
  .sources, .cards, .chips-list, .attempts { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--s2); }
  .source { display: flex; flex-direction: column; gap: var(--s2); }
  .source.off .check span { color: var(--m-muted); }
  .row { display: flex; align-items: center; gap: var(--s2); flex-wrap: wrap; }
  .rank { display: inline-grid; place-items: center; min-width: 24px; height: 24px; border-radius: 8px; background: var(--m-surface-3); font-size: 12px; font-weight: 600; }
  .grow { flex: 1 1 160px; min-width: 0; }
  .check { display: flex; align-items: center; gap: var(--s2); }
  .configure { display: flex; flex-direction: column; gap: var(--s2); padding-left: 32px; }
  .mdns { display: flex; flex-direction: column; gap: var(--s2); }
  .row-wrap { gap: var(--s2); align-items: center; }
  .card-item { display: flex; flex-direction: column; gap: var(--s2); min-width: 0; }
  .nested { display: flex; flex-direction: column; gap: 4px; }
  .summary { all: unset; cursor: pointer; display: flex; align-items: center; gap: var(--s3); min-width: 0; }
  .summary:focus-visible { outline: 2px solid var(--m-accent); outline-offset: 4px; border-radius: 6px; }
  .words { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
  .name { font-weight: 600; }
  .badges { display: flex; flex-wrap: wrap; gap: var(--s2); align-items: center; margin: 0; }
  .badge.chosen { border-color: var(--m-accent); }
  .badge.warn { color: var(--m-warn); }
  .chip { display: flex; flex-wrap: wrap; align-items: center; gap: var(--s2); }
  .chip.recommended { border-color: var(--m-accent); }
  .chips { gap: var(--s2); }
  details > summary { cursor: pointer; font-weight: 560; }
  details[open] > summary { margin-bottom: var(--s2); }
  .facts-list { display: flex; flex-direction: column; gap: 2px; }
  .fact-line { display: flex; gap: var(--s2); font-size: 12.5px; flex-wrap: wrap; }
  .fact-name { color: var(--m-muted); min-width: 120px; }
  .fact-text { overflow-wrap: anywhere; min-width: 0; }
</style>
