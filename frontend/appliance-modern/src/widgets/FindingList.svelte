<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t, say, langOf } from "../i18n/i18n.svelte.js";
  import { href } from "../app/router.svelte.js";
  import { PAGE_IDS } from "../app/pages.js";
  import { SEVERITY_ICONS, SEVERITY_TONES, findingTarget, findingWords } from "../lib/findings.js";

  /** The ranked findings, each with the page that fixes it; the server's words when this build has none of its own. */
  let { findings = [], status = null, page = "" } = $props();
</script>

<ul class="findings" data-testid="findings">
  {#each findings as finding, index (`${finding.code}-${index}`)}
    {@const words = findingWords(finding, status)}
    {@const target = findingTarget(finding, page, PAGE_IDS)}
    <li class="finding {SEVERITY_TONES[finding.severity] || 'c-idle'}" data-severity={finding.severity} data-code={finding.code}>
      <span class="icon-tile sm"><Icon name={SEVERITY_ICONS[finding.severity] || "info"} /></span>
      <div class="body">
        <p class="title" lang={langOf(words.title)}>{say(words.title)}</p>
        {#if words.message && say(words.message)}<p class="message" lang={langOf(words.message)}>{say(words.message)}</p>{/if}
        {#if words.next && say(words.next)}<p class="next" lang={langOf(words.next)}>{say(words.next)}</p>{/if}
      </div>
      {#if target}
        <a class="btn secondary small" href={href(target)} data-testid="finding-open-{target}">{target === "admin" ? t("updates.openAdmin") : t("finding.open", { page: t(`nav.${target}`) })}<Icon name="chevron-right" /></a>
      {/if}
    </li>
  {/each}
</ul>

<style>
  .findings { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--s2); }
  .finding {
    display: flex; gap: var(--s3); align-items: flex-start; flex-wrap: wrap; padding: var(--s3); border-radius: var(--m-radius-inner);
    background: color-mix(in srgb, var(--c) 7%, var(--m-surface-2)); border: 1px solid color-mix(in srgb, var(--c) 25%, transparent);
  }
  .body { flex: 1 1 240px; min-width: 0; }
  .title { margin: 0; font-weight: 600; }
  .message { margin: 4px 0 0; color: var(--m-muted); font-size: 13px; }
  .next { margin: 4px 0 0; font-size: 13px; }
  .btn { align-self: center; }
</style>
