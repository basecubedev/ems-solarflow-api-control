<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import Icon from "./Icon.svelte";

  /**
   * subtitleLang: set when the subtitle is the server's own word, in another language than the page.
   * icon and tone: the card's mark, an icon tile in a tone class beside the title, as a tile wears it.
   * fold: the title becomes the button that folds the card; folded, only the head shows, with summary under it,
   * and the body stays mounted, hidden, so what it loads and what was typed into it survive.
   */
  let { title = "", subtitle = "", subtitleLang = undefined, span = "", className = "", icon = "", tone = "c-idle", actions, children, labelledby = "", fold = false, open = $bindable(false), summary = "", testid = undefined } = $props();
  const uid = $props.id();
  const headingId = `card-${uid}`;
  const bodyId = `card-body-${uid}`;
  const summaryId = `card-summary-${uid}`;
</script>

<section class="card {span} {className}" class:folded={fold && !open} aria-labelledby={title ? labelledby || headingId : undefined} data-testid={testid}>
  {#if title || actions}
    <div class="card-head">
      {#if title && icon}<span class="icon-tile sm card-mark {tone}" aria-hidden="true"><Icon name={icon} /></span>{/if}
      {#if title}
        <div class="card-heading">
          {#if fold}
            <h2 class="card-title" id={labelledby || headingId}>
              <button class="card-fold" type="button" aria-expanded={open ? "true" : "false"} aria-controls={bodyId} aria-describedby={!open && summary ? summaryId : undefined} onclick={() => (open = !open)}>
                <span class="card-fold-name">{title}</span>
                <Icon name="chevron-right" size="sm" />
              </button>
            </h2>
          {:else}
            <h2 class="card-title" id={labelledby || headingId}>{title}</h2>
          {/if}
          {#if subtitle}<div class="card-sub" lang={subtitleLang}>{subtitle}</div>{/if}
          {#if fold && !open && summary}<div class="card-sub card-summary" id={summaryId}>{summary}</div>{/if}
        </div>
      {/if}
      {#if actions}<div class="card-actions">{@render actions()}</div>{/if}
    </div>
  {/if}
  {#if fold}
    <div class="card-fold-body" id={bodyId} hidden={!open}>{@render children?.()}</div>
  {:else}
    {@render children?.()}
  {/if}
</section>
