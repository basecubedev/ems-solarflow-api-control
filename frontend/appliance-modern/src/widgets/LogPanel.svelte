<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t, tn, say, langOf } from "../i18n/i18n.svelte.js";
  import { host, loadLog } from "../state/host.svelte.js";
  import { forms } from "../state/forms.svelte.js";
  import { errorWords } from "../lib/errors.js";
  import { KNOWN_LOG_SOURCES, LOG_LINES, logSummary } from "../lib/logs.js";

  /** One log at a time, read on request: a log is a per-click read, never polled. sources: the choices, or one fixed source. */
  let { title, sources = [], source: fixed = "", span = "span-12" } = $props();

  const source = $derived(fixed || (sources.includes(forms.logSource) ? forms.logSource : sources[0] || ""));
  const summary = $derived(host.log && host.log.source === source ? logSummary(host.log) : null);
  const failed = $derived(host.logFailed && host.logFailed.source === source && !host.logLoading ? errorWords(host.logFailed.error, "logs.failed") : null);
  const loading = $derived(host.logLoading === source);
  const name = (item) => (KNOWN_LOG_SOURCES.includes(item) ? t(`log.${item}`) : item.replace(/_/g, " "));
</script>

<Card {title} subtitle={t("logs.sub", { lines: LOG_LINES })} {span}>
  {#snippet actions()}
    {#if !fixed && sources.length}
      <label class="source">
        <span class="sr-only">{t("logs.source")}</span>
        <select class="input" value={source} onchange={(event) => (forms.logSource = event.currentTarget.value)} data-testid="log-source">
          {#each sources as item, index (index)}<option value={item}>{name(item)}</option>{/each}
        </select>
      </label>
    {/if}
    <button class="btn secondary small" type="button" onclick={() => loadLog(source)} disabled={!source || loading} aria-busy={loading ? "true" : undefined} data-testid="log-load">
      <Icon name={loading ? "loader" : summary ? "refresh-cw" : "file-text"} />{summary ? t("logs.reload") : t("logs.load")}
    </button>
  {/snippet}
  <div data-testid="log-panel">
    {#if failed}
      <p class="form-error" role="alert" lang={langOf(failed)}>{say(failed)}</p>
    {:else if summary?.unreadable}
      <p class="note warn"><Icon name="triangle-alert" />{t("logs.unreadable", { reason: summary.unreadable })}</p>
    {:else if summary}
      <p class="note"><Icon name="info" />{tn("logs.lines", summary.lines)}{summary.truncated ? ` · ${t("logs.truncated")}` : ""}</p>
      <pre class="log" data-testid="log-output">{summary.text || t("logs.empty")}</pre>
    {:else}
      <p class="muted empty-line">{t("logs.none")}</p>
    {/if}
  </div>
</Card>

<style>
  .source .input { height: 28px; width: auto; max-width: 220px; }
  .empty-line { margin: 0; font-size: 13px; }
</style>
