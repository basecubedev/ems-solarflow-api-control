<script>
  import Card from "../ui/Card.svelte";
  import Segmented from "../ui/Segmented.svelte";
  import Icon from "../ui/Icon.svelte";
  import OperatorGate from "../ui/OperatorGate.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { query } from "../lib/api.js";
  import { auth, authorizedSend, operatorGet } from "../state/auth.svelte.js";
  import { errorText } from "../lib/errors.js";

  const MAX_ROWS = 1000;
  const LEVELS = ["", "DEBUG", "INFO", "WARNING", "ERROR"];
  const SERVICE_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"];

  let lines = $state.raw([]);
  let cursor = 0;
  let level = $state("");
  let follow = $state(true);
  let serviceLevel = $state("");
  let status = $state("");
  let output = $state();
  const f = $derived(fmt());

  async function poll() {
    if (!auth.authenticated) return;
    try {
      const payload = await operatorGet(`/api/logs${query({ after: cursor, level })}`);
      cursor = payload?.cursor || cursor;
      const incoming = Array.isArray(payload?.lines) ? payload.lines : [];
      if (incoming.length) {
        const combined = lines.concat(incoming);
        lines = combined.length > MAX_ROWS ? combined.slice(combined.length - MAX_ROWS) : combined;
      }
      if (payload?.service_level) serviceLevel = payload.service_level;
      status = payload?.dropped ? t("logs.dropped") : "";
    } catch (error) {
      status = errorText(error, t);
    }
  }

  $effect(() => {
    void level;
    void auth.authenticated;
    cursor = 0;
    lines = [];
    poll();
    const timer = setInterval(() => {
      if (!document.hidden) poll();
    }, 2000);
    return () => clearInterval(timer);
  });

  $effect(() => {
    void lines;
    if (follow && output) requestAnimationFrame(() => (output.scrollTop = output.scrollHeight));
  });

  async function setServiceLevel(event) {
    const next = event.currentTarget.value;
    status = t("logs.settingLevel", { level: next });
    try {
      const payload = await authorizedSend("/api/logs/level", { body: { level: next } });
      serviceLevel = payload?.service_level || next;
      status = t("logs.levelSet", { level: serviceLevel });
    } catch (error) {
      status = errorText(error, t);
    }
  }

  const tone = (value) => ({ ERROR: "error", CRITICAL: "error", WARNING: "warning", DEBUG: "debug" })[value] || "info";
</script>

{#snippet logActions()}
    <Segmented options={LEVELS.map((value) => ({ value, label: value ? value : t("common.all") }))} value={level} label={t("logs.filter")} onchange={(value) => (level = value)} />
    <label class="check"><input type="checkbox" bind:checked={follow} />{t("logs.follow")}</label>
    <label class="inline">
      <span>{t("logs.serviceLevel")}</span>
      <select class="input" value={serviceLevel} onchange={setServiceLevel}>
        {#each SERVICE_LEVELS as value (value)}<option {value}>{value}</option>{/each}
      </select>
    </label>
    <button class="btn secondary small" type="button" onclick={() => (lines = [])} title={t("logs.clearHint")}><Icon name="eraser" size="sm" />{t("logs.clear")}</button>
{/snippet}

<OperatorGate>
  <Card title={t("nav.logs")} subtitle={t("page.logs.sub")} span="span-12" actions={logActions}>
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <div class="output" bind:this={output} role="log" aria-live="off" tabindex="0" data-testid="logs-output">
      {#each lines as line (line.seq ?? `${line.ts}-${line.message}`)}
        <div class="row {tone(line.level)}">
          <span class="time num">{f.time(Number(line.ts) * 1000, { seconds: true })}</span>
          <span class="level">{line.level}</span>
          <span class="message">{line.message}</span>
        </div>
      {:else}
        <div class="row muted">{t("logs.empty")}</div>
      {/each}
    </div>
    {#if status}<p class="note" role="status">{status}</p>{/if}
  </Card>
</OperatorGate>

<style>
  .output { background: var(--m-well); border-radius: var(--m-radius-inner); padding: var(--s3); height: min(62vh, 640px); overflow: auto; font-family: var(--m-mono); font-size: 12px; line-height: 1.55; border: 1px solid var(--m-line); }
  .row { display: grid; grid-template-columns: 74px 70px minmax(0, 1fr); gap: var(--s2); }
  .time { color: var(--m-muted-2); }
  .level { font-weight: 600; color: var(--m-muted); }
  .message { white-space: pre-wrap; overflow-wrap: anywhere; }
  .row.error .level, .row.error .message { color: var(--m-fault); }
  .row.warning .level { color: var(--m-warn); }
  .row.debug .message { color: var(--m-muted); }
  .inline { display: inline-flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--m-muted); }
  .inline .input { width: auto; height: 32px; }
  .check { font-size: 12.5px; color: var(--m-muted); }
  @media (max-width: 760px) { .row { grid-template-columns: 64px minmax(0, 1fr); } .message { grid-column: 1 / -1; } }
</style>
