<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Empty from "../../../shared/ui/Empty.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import { t, tn, fmt } from "../i18n/i18n.svelte.js";
  import { system, ensure, reload } from "../state/system.svelte.js";
  import { noteView, pairView, releaseList, RELEASES_SHOWN } from "../lib/updates.js";
  import { emsFact } from "../lib/health.js";
  import { errorView } from "../lib/errors.js";
  import { classicPage } from "../state/prefs.svelte.js";
  import StaleNote from "../widgets/StaleNote.svelte";

  ensure("overview", "alignment", "releases");

  const STEPS = ["backup", "config", "admin", "deploy", "check"];

  const f = $derived(fmt());
  const overview = $derived(system.overview.data);
  const ems = $derived(emsFact(overview));
  const pair = $derived(system.alignment.data?.running_pair || null);
  const pairState = $derived(pairView(pair));
  const releases = $derived(releaseList(system.releases.data));
  const recommended = $derived(releases.find((release) => release.recommended) || null);
  let showAll = $state(false);
  const shown = $derived(showAll ? releases : releases.slice(0, RELEASES_SHOWN));
</script>

<Card title={t("updates.current")} subtitle={t("updates.currentSub")} span="span-6">
  <div class="facts two">
    <div class="fact"><div class="fact-label">{t("version.admin")}</div><div class="fact-value num" class:na={!overview?.components?.admin?.tag && !pair?.admin?.release}>{overview?.components?.admin?.tag || pair?.admin?.release || t("version.unknown")}</div></div>
    <div class="fact"><div class="fact-label">{t("version.ems")}</div><div class="fact-value num" class:na={!ems.version}>{ems.version || t("version.unknown")}</div></div>
  </div>
  {#if pairState}
    <p class="pair"><Status tone={pairState.tone} text={t(`updates.pair.${pairState.state}`)} /><span class="muted">{t("updates.pairHint")}</span></p>
  {/if}
</Card>

<Card title={t("updates.guidedTitle")} subtitle={t("updates.guidedSub")} span="span-6">
  <ol class="how">
    {#each STEPS as step, index (step)}<li><span class="n num">{index + 1}</span>{t(`updates.step.${step}`)}</li>{/each}
  </ol>
  <div class="row-wrap actions">
    <a class="btn" href={classicPage("maintenance-upgrade")} data-testid="start-upgrade"><Icon name="arrow-up-right" size="sm" />{t("updates.start")}</a>
  </div>
  <p class="note"><Icon name="info" />{t("updates.classicNote")}</p>
</Card>

<Card title={t("updates.releases")} subtitle={recommended ? t("updates.recommendedIs", { tag: recommended.tag }) : t("updates.releasesSub")} span="span-12">
  {#snippet actions()}
    <button class="icon-btn" type="button" onclick={() => reload("releases")} disabled={system.releases.loading} aria-label={t("common.refresh")} title={t("common.refresh")}><Icon name="refresh-cw" /></button>
  {/snippet}
  <StaleNote resource={system.releases} />
  {#if system.releases.loading && !system.releases.data}
    <Empty text={t("updates.loadingReleases")} icon="loader" />
  {:else if system.releases.error && !system.releases.data}
    <Empty error {...errorView(system.releases.error, t)} />
  {:else if !releases.length}
    <Empty text={t("updates.noReleases")} />
  {:else}
    <ul class="releases" data-testid="releases">
      {#each shown as release (release.tag)}
        <li class="release inner" class:off={!release.selectable} data-tag={release.tag}>
          <div class="words">
            <div class="tag">
              <span class="num">{release.tag}</span>
              <span class="badge channel">{t(`updates.channel.${release.channel}`)}</span>
              {#if release.recommended}<span class="badge">{t("updates.recommended")}</span>{/if}
              {#if release.running}<span class="badge">{t("updates.running")}</span>{/if}
            </div>
            <div class="muted small">
              {#if release.publishedAt}<span class="num">{f.dateTime(release.publishedAt)}</span>{/if}{#if release.publishedAt && release.note}<span aria-hidden="true"> · </span>{/if}{#if release.note?.key}{t(release.note.key)}{:else if release.note}<span lang="en">{release.note.text}</span>{/if}
            </div>
          </div>
          {#if release.state}<Status tone={release.tone} text={t(`updates.state.${release.state}`)} />{/if}
        </li>
      {/each}
    </ul>
    {#if releases.length > RELEASES_SHOWN}
      <button class="btn secondary small more-btn" type="button" onclick={() => (showAll = !showAll)}>{showAll ? t("updates.showFewer") : tn("updates.showAll", releases.length)}</button>
    {/if}
    {#each (system.releases.data?.warnings || []).map(noteView).filter(Boolean) as warning, index (index)}<p class="note warn"><Icon name="triangle-alert" />{#if warning.key}{t(warning.key)}{:else}<span lang="en">{warning.text}</span>{/if}</p>{/each}
  {/if}
</Card>

<style>
  .pair { display: flex; align-items: center; gap: var(--s2); flex-wrap: wrap; margin: var(--s3) 0 0; font-size: 12.5px; }
  .how { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; font-size: 13.5px; }
  .how li { display: flex; gap: 10px; align-items: flex-start; }
  .n { flex: none; width: 22px; height: 22px; border-radius: 50%; display: grid; place-items: center; font-size: 11.5px; font-weight: 650; background: var(--m-surface-3); color: var(--m-muted); }
  .actions { margin-top: var(--s4); }
  .releases { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--s2); }
  .release { display: flex; align-items: center; gap: var(--s3); flex-wrap: wrap; }
  .release.off { opacity: 0.6; }
  .release .words { flex: 1 1 280px; min-width: 0; }
  .tag { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-weight: 620; }
  .badge.channel { background: var(--m-surface-3); color: var(--m-muted); }
  .small { font-size: 12.5px; margin-top: 2px; overflow-wrap: anywhere; }
  .more-btn { margin-top: var(--s3); }
</style>
