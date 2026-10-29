<script>
  import Icon from "./Icon.svelte";

  /**
   * subtitleLang: set when the subtitle is the server's own word, in another language than the page.
   * icon and tone: the card's mark, an icon tile in a tone class beside the title, as a tile wears it.
   */
  let { title = "", subtitle = "", subtitleLang = undefined, span = "", className = "", icon = "", tone = "c-idle", actions, children, labelledby = "" } = $props();
  const uid = $props.id();
  const headingId = `card-${uid}`;
</script>

<section class="card {span} {className}" aria-labelledby={title ? labelledby || headingId : undefined}>
  {#if title || actions}
    <div class="card-head">
      {#if title && icon}<span class="icon-tile sm card-mark {tone}" aria-hidden="true"><Icon name={icon} /></span>{/if}
      {#if title}
        <div class="card-heading">
          <h2 class="card-title" id={labelledby || headingId}>{title}</h2>
          {#if subtitle}<div class="card-sub" lang={subtitleLang}>{subtitle}</div>{/if}
        </div>
      {/if}
      {#if actions}<div class="card-actions">{@render actions()}</div>{/if}
    </div>
  {/if}
  {@render children?.()}
</section>
