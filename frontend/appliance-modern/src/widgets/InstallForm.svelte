<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import PlanButton from "./PlanButton.svelte";
  import TrackHelp from "./TrackHelp.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { forms } from "../state/forms.svelte.js";
  import { detail } from "../state/prefs.svelte.js";
  import { groupByTrack } from "../lib/host.js";
  import { INSTALL_CHANNELS, adminInstallPlan } from "../lib/plans.js";

  /**
   * The EMS Admin version to install. A channel names something the agent
   * resolves; a listed version names the exact tag it was listed under. Either
   * way what crosses the wire is a name, never an image reference. A version
   * this host will not install is still listed, disabled, with the agent's
   * reason, so nothing it refuses can be chosen.
   */
  let { releases, bootstrap = false, blocked = false } = $props();

  const versionKey = $derived(bootstrap ? "bootstrapVersion" : "adminVersion");
  const tagKey = $derived(bootstrap ? "bootstrapTag" : "adminTag");
  const channels = $derived(
    INSTALL_CHANNELS.filter((channel) => (channel === "exact" ? detail.expert : !bootstrap || channel === "latest_stable")),
  );
  const groups = $derived(groupByTrack((releases && releases.available) || []));
  const offered = $derived([
    ...channels,
    ...groups.flatMap((group) => group.items.filter((item) => item.installable !== false).map((item) => `tag:${item.tag}`)),
  ]);
  const selected = $derived(offered.includes(forms[versionKey]) ? forms[versionKey] : "latest_stable");
  const target = $derived(
    selected.startsWith("tag:")
      ? { channel: "exact", tag: selected.slice(4) }
      : selected === "exact"
        ? { channel: "exact", tag: String(forms[tagKey] || "").trim() }
        : { channel: selected, tag: "" },
  );
  const plan = $derived(adminInstallPlan(target.channel, target.tag, bootstrap ? false : forms.adminReinstall, bootstrap ? "admin-bootstrap-plan" : "install-plan"));
  const label = (item) => {
    let text = item.direction === "reinstall" ? t("install.installed", { tag: item.tag }) : item.tag;
    if (item.installable === false && item.reason) text += ` — ${item.reason}`;
    return text;
  };
</script>

<div class="form">
  <label class="field">
    <span>{t("install.version")}</span>
    <select class="input" value={selected} onchange={(event) => (forms[versionKey] = event.currentTarget.value)} data-testid="install-channel">
      <optgroup label={t("install.channels")}>
        {#each channels as channel (channel)}<option value={channel}>{t(`install.channel.${channel}`)}</option>{/each}
      </optgroup>
      {#each groups as group (group.track)}
        <optgroup label={t(`track.${group.track}`)}>
          {#each group.items as item, index (index)}<option value="tag:{item.tag}" disabled={item.installable === false} lang={item.installable === false && item.reason && item.direction !== "reinstall" ? "en" : undefined}>{label(item)}</option>{/each}
        </optgroup>
      {/each}
    </select>
  </label>
  {#if selected === "exact"}
    <label class="field">
      <span>{t("install.tag")}</span>
      <input class="input mono" type="text" placeholder="v0.8.0" bind:value={forms[tagKey]} data-testid="install-tag" />
    </label>
  {/if}
  {#if !bootstrap}
    <label class="check"><input type="checkbox" bind:checked={forms.adminReinstall} data-testid="install-reinstall" />{t("install.reinstall")}</label>
  {/if}
  {#if releases?.error}<p class="note warn"><Icon name="triangle-alert" />{t("install.listFailed", { code: releases.error })}</p>{/if}
  <div class="row-wrap">
    <PlanButton
      {plan}
      label={bootstrap ? t("install.installAdmin") : t("install.installSelected")}
      icon="download"
      small={false}
      disabled={blocked || releases === null || (target.channel === "exact" && !target.tag)}
      testid={bootstrap ? "admin-bootstrap-plan" : "install-plan"}
    />
    {#if releases === null}<span class="muted"><Icon name="loader" size="sm" /> {t("install.loading")}</span>{/if}
  </div>
  <TrackHelp />
</div>

<style>
  .form { display: flex; flex-direction: column; gap: var(--s3); max-width: 560px; }
</style>
