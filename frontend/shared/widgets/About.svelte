<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import Card from "../ui/Card.svelte";
  import Fact from "../ui/Fact.svelte";
  import Icon from "../ui/Icon.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { useShell } from "../shell/context.js";
  import {
    BUILD_FILE,
    BUNDLED_FILE,
    COPYRIGHT,
    DEVICE_REPORT,
    PROJECT_LICENSE,
    REPOSITORY,
    SHARED_NOTICES,
    SUPPORTED_SETUPS,
    buildFacts,
    noticeEntries,
    noticeKey,
    sourceLinks,
    sourceRef,
  } from "../lib/about.js";

  /**
   * The project's copyright and license, the source of the running version and
   * the notices of everything this page is built from. The app adds through the
   * shell's ``about``: its own notice files, the tag prefix of its releases and,
   * when it knows it better than the build file, its version.
   */
  const shell = useShell();
  const about = shell.about || {};
  const product = t("app.name");

  let build = $state({ version: "", revision: "" });
  let notices = $state({ loading: true, entries: [], failed: [] });

  async function readJson(path) {
    const response = await fetch(path, { credentials: "same-origin", cache: "no-store" });
    if (!response.ok) throw new Error(String(response.status));
    return response.json();
  }

  async function readText(path) {
    const response = await fetch(path, { credentials: "same-origin" });
    if (!response.ok) throw new Error(String(response.status));
    return response.text();
  }

  $effect(() => {
    readJson(BUILD_FILE).then(
      (value) => (build = buildFacts(value)),
      () => (build = buildFacts(null)),
    );
  });

  $effect(() => {
    const files = [...SHARED_NOTICES, ...(about.notices || [])];
    Promise.allSettled([readJson(BUNDLED_FILE), ...files.map((entry) => readText(entry.file))]).then(([bundled, ...texts]) => {
      const failed = [];
      if (bundled.status !== "fulfilled") failed.push(BUNDLED_FILE);
      const read = files.map((entry, index) => {
        if (texts[index].status !== "fulfilled") failed.push(entry.file);
        return { ...entry, text: texts[index].status === "fulfilled" ? texts[index].value : "" };
      });
      notices = {
        loading: false,
        entries: noticeEntries(bundled.status === "fulfilled" ? bundled.value : [], read),
        failed: [...new Set(failed)],
      };
    });
  });

  const version = $derived(about.version || build.version);
  const ref = $derived(sourceRef({ revision: build.revision, version, tagPrefix: about.tagPrefix || "v" }));
  const links = $derived(sourceLinks(ref));
</script>

<Card icon="info" tone="c-pv" title={product} subtitle={t("about.title")} span="span-6">
  <div class="facts two">
    <Fact label={t("about.version")} value={version || t("about.development")} missing={!version} />
    <Fact label={t("about.revision")} value={build.revision ? build.revision.slice(0, 12) : "—"} title={build.revision} missing={!build.revision} />
  </div>
  <div class="row-wrap">
    <a class="btn secondary small" href={links.tree} target="_blank" rel="noopener" data-testid="about-source">
      <Icon name="arrow-up-right" /><span>{t("about.source")}</span>
    </a>
    {#each about.links || [] as link (link.href)}
      <a class="btn secondary small" href={link.href} target="_blank" rel="noopener" data-testid={link.testid}>
        <Icon name="arrow-up-right" /><span>{t(link.label)}</span>
      </a>
    {/each}
  </div>
</Card>

<Card icon="scale" tone="c-soc" title={t("about.license.title")} subtitle={`${COPYRIGHT} · ${PROJECT_LICENSE}`} span="span-6">
  <p class="muted small" data-testid="about-license">{t("about.license.notice", { product })}</p>
  <div class="row-wrap">
    <a class="ghost-link" href={links.license} target="_blank" rel="noopener" data-testid="about-license-text">
      <Icon name="arrow-up-right" /><span>{t("about.license.text")}</span>
    </a>
    <a class="ghost-link" href={links.inventory} target="_blank" rel="noopener" data-testid="about-inventory">
      <Icon name="arrow-up-right" /><span>{t("about.license.inventory")}</span>
    </a>
  </div>
</Card>

<Card icon="users" tone="c-idle" title={t("about.community.title")} span="span-12">
  <p class="muted small" data-testid="about-community">{t("about.community.text", { product })}</p>
  <p class="muted small">{t("about.community.help")}</p>
  <div class="row-wrap">
    <a class="ghost-link" href={`${REPOSITORY}/discussions`} target="_blank" rel="noopener" data-testid="about-discussions">
      <Icon name="arrow-up-right" /><span>{t("about.discussions")}</span>
    </a>
    <a class="ghost-link" href={`${REPOSITORY}/issues`} target="_blank" rel="noopener" data-testid="about-issues">
      <Icon name="arrow-up-right" /><span>{t("about.issues")}</span>
    </a>
  </div>
</Card>

<Card icon="circle-check" tone="c-grid" title={t("about.devices.title")} span="span-12">
  <p class="muted small" data-testid="about-devices">{t("about.devices.text", { product })}</p>
  <div class="row-wrap">
    <a class="ghost-link" href={DEVICE_REPORT} target="_blank" rel="noopener" data-testid="about-device-report">
      <Icon name="arrow-up-right" /><span>{t("about.devices.report")}</span>
    </a>
    <a class="ghost-link" href={SUPPORTED_SETUPS} target="_blank" rel="noopener" data-testid="about-supported-setups">
      <Icon name="arrow-up-right" /><span>{t("about.devices.supported")}</span>
    </a>
  </div>
</Card>

<Card icon="info" tone="c-out" title={t("about.components.title")} subtitle={t("about.components.sub")} span="span-12">
  {#if notices.loading}
    <p class="muted small">{t("about.components.loading")}</p>
  {:else}
    {#each notices.failed as file (file)}
      <p class="form-error" role="alert">{t("about.components.failed", { file })}</p>
    {/each}
    <ul class="notices" data-testid="about-notices">
      {#each notices.entries as entry (noticeKey(entry))}
        <li>
          <details>
            <summary data-testid="about-notice">
              <Icon name="chevron-right" />
              <span class="notice-name">{entry.name}</span>
              {#if entry.version}<span class="muted num">{entry.version}</span>{/if}
              <span class="pill">{entry.license}</span>
            </summary>
            {#if entry.text}<pre class="notice-text">{entry.text}</pre>{/if}
          </details>
        </li>
      {/each}
    </ul>
  {/if}
</Card>

<style>
  .notices { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--s2); }
  summary { display: flex; flex-wrap: wrap; align-items: center; gap: var(--s2); cursor: pointer; list-style: none; }
  summary::-webkit-details-marker { display: none; }
  summary :global(svg.i) { width: 14px; height: 14px; color: var(--m-muted); transition: transform 0.15s; }
  details[open] > summary :global(svg.i) { transform: rotate(90deg); }
  .notice-name { font-weight: 560; }
  .notice-text { margin: var(--s2) 0 0; padding: var(--s3); max-height: 320px; overflow: auto; white-space: pre-wrap; font-size: 12px; font-family: var(--m-mono); background: var(--m-surface-2); border-radius: var(--m-radius-control); }
</style>
