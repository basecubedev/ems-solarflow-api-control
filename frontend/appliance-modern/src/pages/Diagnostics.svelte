<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import Bar from "../../../shared/ui/Bar.svelte";
  import KeyValues from "../widgets/KeyValues.svelte";
  import PlanButton from "../widgets/PlanButton.svelte";
  import LogPanel from "../widgets/LogPanel.svelte";
  import { t, fmt, say, langOf } from "../i18n/i18n.svelte.js";
  import { host, ensure, held } from "../state/host.svelte.js";
  import { detail } from "../state/prefs.svelte.js";
  import { gigabytes } from "../lib/host.js";
  import { logSources } from "../lib/logs.js";
  import { UNIT_STATES, word } from "../lib/words.js";

  /** Bounded, redacted host information, and the support archive that carries it off the box. */
  const system = $derived(host.status?.system || {});
  const memory = $derived(system.memory || {});
  const root = $derived(system.storage?.root || {});
  const data = $derived(system.storage?.ems_data || {});
  const sources = $derived(logSources(held("settings"), detail.expert));

  $effect(() => {
    if (host.settings === undefined) ensure("settings");
  });

  const size = (megabytes) => {
    const value = gigabytes(megabytes);
    return value ? `${fmt().number(value.value, value.digits)} GB` : null;
  };
  const used = (value) => (value === null || value === undefined ? null : t("diag.used", { percent: fmt().number(value, 1) }));
</script>

<Card icon="thermometer" tone="c-pv" title={t("diag.temperature")} span="span-4">
  <div data-testid="diag-temperature">
    <p class="big num">{system.temperature?.celsius ? `${fmt().number(system.temperature.celsius, 1)} °C` : "—"}</p>
    {#if system.power?.available}<Status tone={system.power.under_voltage ? "c-fault" : "c-ok"} text={system.power.under_voltage ? t("diag.underVoltage") : t("diag.powerOk")} />{/if}
  </div>
</Card>

<Card icon="memory-stick" tone="c-inv" title={t("diag.memory")} span="span-4">
  <div data-testid="diag-memory">
    <p class="big num">{used(memory.used_percent) || "—"}</p>
    {#if memory.used_percent !== undefined}<Bar value={memory.used_percent} label={t("diag.memory")} />{/if}
    <KeyValues rows={[{ label: t("diag.total"), value: size(memory.total_mb) }, { label: t("diag.available"), value: size(memory.available_mb) }]} />
  </div>
</Card>

<Card icon="hard-drive" tone="c-inv" title={t("diag.root")} span="span-4">
  <div data-testid="diag-storage">
    <p class="big num">{used(root.used_percent) || "—"}</p>
    {#if root.used_percent !== undefined}<Bar value={root.used_percent} label={t("diag.root")} />{/if}
    <KeyValues rows={[{ label: t("diag.free"), value: size(root.free_mb) }]} />
  </div>
</Card>

<Card icon="database" tone="c-soc" title={t("diag.emsData")} span="span-4">
  <div data-testid="diag-ems-data">
    <p class="big num">{used(data.used_percent) || "—"}</p>
    {#if data.used_percent !== undefined}<Bar value={data.used_percent} label={t("diag.emsData")} />{/if}
    <KeyValues rows={[{ label: t("diag.path"), value: data.path, mono: true }]} />
  </div>
</Card>

<Card icon="server" tone="c-inv" title={t("diag.services")} span="span-4">
  <ul class="units" data-testid="diag-services">
    {#each system.services || [] as unit, index (index)}
      <li><span class="mono">{unit.unit}</span><Status tone={unit.active === "active" ? "c-ok" : unit.active === "failed" ? "c-fault" : "c-idle"} text={say(word("systemd", UNIT_STATES, unit.active)) || "—"} lang={langOf(word("systemd", UNIT_STATES, unit.active))} /></li>
    {/each}
  </ul>
</Card>

{#if detail.expert}
  <Card icon="cpu" tone="c-inv" title={t("diag.kernel")} span="span-4">
    <div data-testid="diag-kernel">
      <p class="big mono">{system.operating_system?.kernel || "—"}</p>
      <KeyValues rows={[{ label: t("admin.architecture"), value: system.hardware?.architecture }, { label: t("diag.osVersion"), value: system.operating_system?.version }]} />
    </div>
  </Card>
{/if}

<Card icon="file-archive" tone="c-soc" title={t("diag.archive")} subtitle={t("diag.archiveSub")} span={detail.expert ? "span-4" : "span-4"}>
  <div class="stack-sm" data-testid="diag-support">
    <p class="note"><Icon name="shield-check" />{t("diag.archiveRedacted")}</p>
    <PlanButton plan={{ key: "support-archive", endpoint: "/api/support/archive", title: { key: "plan.title.archive" }, confirm: "plan.create" }} label={t("diag.createArchive")} icon="file-archive" small={false} testid="support-archive" />
    <p class="muted small">{t("diag.archiveDownload")}</p>
  </div>
</Card>

{#if sources.length}
  <LogPanel title={t("diag.logs")} {sources} />
{:else if host.settings?.error}
  <p class="span-12 form-error" role="alert">{t("settings.failed", { code: host.settings.error })}</p>
{:else}
  <div class="span-12 muted small"><Icon name="loader" size="sm" /> {t("diag.sourcesLoading")}</div>
{/if}

<style>
  .big { font-size: 22px; font-weight: 650; margin: 0 0 var(--s3); overflow-wrap: anywhere; }
  .big.mono { font-size: 15px; }
  .units { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--s2); }
  .units li { display: flex; justify-content: space-between; align-items: center; gap: var(--s2); font-size: 13px; }
  .units .mono { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
  .stack-sm { display: flex; flex-direction: column; gap: var(--s3); align-items: flex-start; }
  .stack-sm .note { margin: 0; }
  .small { font-size: 12.5px; margin: 0; }
  [data-testid^="diag-"] :global(.bar) { margin-bottom: var(--s3); }
</style>
