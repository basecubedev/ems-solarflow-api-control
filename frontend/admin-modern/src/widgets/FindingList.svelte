<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { href } from "../app/router.svelte.js";

  /** findings: views from lib/health.js, in the server's order. */
  let { findings, onrefresh } = $props();

  function listText(items) {
    return items.length ? items.map((name) => t(`health.path.${name}`)).join(", ") : "";
  }

  function statusText(status) {
    return status.key ? t(status.key) : status.text;
  }

  /** The finding's sentence is the server's own, in English, for an unknown code and for a free-text warning. */
  function serverMessage(finding) {
    return !finding.known || finding.code === "installation_warning";
  }

  function title(finding) {
    if (!finding.known) return finding.title;
    if (finding.code === "installation_incomplete") return t(`install.state.${finding.vars.state}`);
    return t(`finding.${finding.code}.title`);
  }

  function message(finding) {
    if (!finding.known) return finding.message;
    const vars = finding.vars;
    if (finding.code === "installation_incomplete") return t(`install.message.${vars.state}`);
    if (finding.code === "ems_stopped") return t("finding.ems_stopped.message", { status: statusText(vars.status) });
    if (finding.code === "ems_safe_mode") return t("finding.ems_safe_mode.message", { fields: vars.fields.join(", ") });
    if (finding.code === "installation_warning") return finding.message;
    return t(`finding.${finding.code}.message`);
  }

  function next(finding) {
    if (!finding.known) return finding.next;
    if (finding.code === "installation_incomplete" && finding.vars.missing.length) {
      return t("finding.installation_incomplete.missing", { paths: listText(finding.vars.missing) });
    }
    return t(`finding.${finding.code}.next`);
  }
</script>

<ul class="findings" data-testid="findings">
  {#each findings as finding, index (`${finding.code}-${index}`)}
    <li class="finding inner {finding.tone}" data-code={finding.code} data-severity={finding.severity}>
      <span class="mark"><Icon name={finding.icon} /></span>
      <div class="body">
        <div class="finding-title" lang={finding.known ? undefined : "en"}>{title(finding)}</div>
        {#if message(finding)}<p lang={serverMessage(finding) ? "en" : undefined}>{message(finding)}</p>{/if}
        {#if finding.code === "docker_unavailable" && finding.vars.detail}<p class="detail" lang="en">{finding.vars.detail}</p>{/if}
        {#if next(finding)}<p class="next" lang={finding.known ? undefined : "en"}><Icon name="arrow-right" size="sm" />{next(finding)}</p>{/if}
        {#if finding.actions.length}
          <div class="actions">
            {#each finding.actions as action (action.label)}
              {#if action.kind === "page"}
                <a class="btn secondary small" href={href(action.target)}>{t(action.label)}</a>
              {:else if onrefresh}
                <button class="btn secondary small" type="button" onclick={onrefresh}><Icon name="refresh-cw" size="sm" />{t(action.label)}</button>
              {/if}
            {/each}
          </div>
        {/if}
      </div>
    </li>
  {/each}
</ul>

<style>
  .findings { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--s2); }
  .finding { display: flex; gap: var(--s3); align-items: flex-start; border-left: 3px solid var(--c); }
  .mark { color: var(--c); margin-top: 1px; }
  .body { min-width: 0; flex: 1; }
  .finding-title { font-weight: 620; }
  p { margin: 4px 0 0; color: var(--m-muted); font-size: 13px; overflow-wrap: anywhere; }
  .detail { font-family: var(--m-mono); font-size: 12px; }
  .next { display: flex; gap: 6px; align-items: flex-start; color: var(--m-text); }
  .next :global(svg.i) { margin-top: 2px; color: var(--m-muted); }
  .actions { display: flex; gap: var(--s2); flex-wrap: wrap; margin-top: var(--s2); }
</style>
