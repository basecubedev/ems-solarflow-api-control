<script>
  import Segmented from "../ui/Segmented.svelte";
  import Icon from "../ui/Icon.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { prefs, setPref, THEMES, THEME_NAMES, LANGUAGES, NAV_MODES } from "../state/prefs.svelte.js";
  import { useShell } from "./context.js";

  const shell = useShell();
  const themes = THEMES.map((value) => ({ value, label: THEME_NAMES[value] }));
  const languages = LANGUAGES.map((value) => ({ value, label: value.toUpperCase() }));
</script>

<div class="popover" popover id="appearance" data-testid="appearance">
  <div class="pop-title">{t("appearance.title")}</div>
  <div class="pop-row"><span>{t("appearance.theme")}</span>
    <Segmented options={themes} value={prefs.theme} label={t("appearance.theme")} onchange={(v) => setPref("theme", v)} />
  </div>
  <div class="pop-row"><span>{t("appearance.language")}</span>
    <Segmented options={languages} value={prefs.lang} label={t("appearance.language")} onchange={(v) => setPref("lang", v)} />
  </div>
  <div class="pop-row"><span>{t("appearance.density")}</span>
    <Segmented
      options={[{ value: "normal", label: t("appearance.density.normal") }, { value: "compact", label: t("appearance.density.compact") }]}
      value={prefs.density} label={t("appearance.density")} onchange={(v) => setPref("density", v)} />
  </div>
  <div class="pop-row nav-row"><span>{t("appearance.nav")}</span>
    <Segmented
      options={NAV_MODES.map((value) => ({ value, label: t(`appearance.nav.${value}`) }))}
      value={prefs.nav} label={t("appearance.nav")} onchange={(v) => setPref("nav", v)} />
  </div>
  <div class="pop-row"><span>{t("appearance.motion")}</span>
    <Segmented
      options={[{ value: "on", label: t("common.on") }, { value: "off", label: t("common.off") }]}
      value={prefs.motion} label={t("appearance.motion")} onchange={(v) => setPref("motion", v)} />
  </div>
  {#if prefs.serverAnimation === "off"}<p class="note"><Icon name="info" />{t("appearance.motionServerOff")}</p>{/if}
  <button class="ghost-link" type="button" onclick={() => shell.classic(shell.nav.router.page)}>
    <Icon name="arrow-left-right" /><span>{t("nav.classicSwitch")}</span>
  </button>
</div>
