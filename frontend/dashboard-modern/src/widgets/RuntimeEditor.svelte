<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script module>
  export const manifest = { id: "runtime-editor", data: ["runtime", "auth"], refresh: "on-save", minWidth: 320, states: ["auth-not-configured", "signed-out", "runtime-unavailable", "editing"] };
</script>

<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Empty from "../../../shared/ui/Empty.svelte";
  import RuntimeForm from "./RuntimeForm.svelte";
  import { t, fmt, translatedOr } from "../i18n/i18n.svelte.js";
  import { useShell } from "../../../shared/shell/context.js";
  import { auth } from "../state/auth.svelte.js";
  import { live, loadRuntime } from "../state/live.svelte.js";
  import { SYSTEM_ENDPOINT, WINTER_ENDPOINT, AC_CHARGE_ENDPOINT, BATTERY_CARE_ENDPOINT, deviceEndpoint, acRole, settingWord, deviceSettings, switchDefault, withDefault } from "../lib/runtime.js";

  const runtime = $derived(live.runtime || {});
  const limits = $derived(runtime._limits || {});
  const defaults = $derived(limits.catalogue_defaults || {});
  const catalogue = $derived(limits.device_settings || []);
  const unsupported = $derived(new Set(limits.ac_role_unsupported || []));
  const devices = $derived(Object.entries(runtime.devices || {}));
  const shell = useShell();
  const settingsHref = shell?.pageSet?.pages?.some((page) => page.id === "settings") ? shell.nav.href("settings") : null;

  /** A number field whose maximum is someone's limit: it names the limit and, where this app has it, the page that sets it. */
  function bounded(field, bound, max) {
    const tab = bound === "installation" ? "safety" : "devices";
    return {
      ...field,
      max,
      bound,
      help: t(`runtime.bound.${bound}`, { max: `${fmt().number(max)}\u00a0W` }),
      link: settingsHref ? { href: `${settingsHref}/${tab}`, label: t(`runtime.bound.open.${tab}`) } : null,
    };
  }

  /** The range the EMS's writer accepts for a field, so this form offers no other. */
  function accepted(scope, name) {
    const fields = limits.fields || {};
    const spec = (scope === "system" || scope === "devices" ? fields[scope] : fields.sections?.[scope])?.[name] || {};
    return { min: spec.min, max: spec.max };
  }

  /** The values the EMS's writer accepts for a device field, in its order. */
  function choices(name) {
    return limits.fields?.devices?.[name]?.options || [];
  }

  $effect(() => {
    if (auth.authenticated) loadRuntime();
  });

  function gridTargetMax(phase) {
    return Number(limits.system?.[`grid_target_${phase}_w`] ?? 200);
  }

  function settingLabel(key) {
    return translatedOr(`setting.${key}`, catalogue.find((setting) => setting.key === key)?.label || key);
  }

  function valueLabel(value) {
    return translatedOr(`settingValue.${value.value}`, value.label || value.value);
  }

  const batteryCareFields = $derived([
    { name: "enabled", type: "toggle", fallback: true, label: t("runtime.batteryCare") },
    { name: "summer_full_points", type: "number", label: t("runtime.batteryCareSummer"), ...accepted("battery_care", "summer_full_points"), unit: "%" },
    { name: "winter_full_points", type: "number", label: t("runtime.batteryCareWinter"), ...accepted("battery_care", "winter_full_points"), unit: "%" },
    { name: "pv_only_points", type: "number", label: t("runtime.batteryCarePvOnly"), ...accepted("battery_care", "pv_only_points"), unit: "%" },
    { name: "min_discharge_w", type: "number", label: t("runtime.batteryCareMinDischarge"), ...accepted("battery_care", "min_discharge_w"), unit: "W" },
    { name: "pv_margin_percent", type: "number", label: t("runtime.batteryCarePvMargin"), ...accepted("battery_care", "pv_margin_percent"), unit: "%" },
    { name: "pv_margin_min_w", type: "number", label: t("runtime.batteryCarePvMarginMin"), ...accepted("battery_care", "pv_margin_min_w"), unit: "W" },
  ].map((field) => withDefault(field, defaults.battery_care?.[field.name])));

  const systemFields = $derived([
    { name: "enabled", type: "toggle", fallback: switchDefault(limits, null, "enabled"), label: t("runtime.emsEnabled") },
    bounded({ name: "max_total_power", type: "number", label: t("runtime.maxTotal"), min: 0, unit: "W" }, "installation", Number(limits.system?.max_total_power || 5000)),
    { name: "min_output_limit", type: "number", label: t("runtime.minOutput"), min: 0, max: Number(limits.system?.min_output_limit || 5000), unit: "W" },
    { name: "grid_target_discharge_w", type: "gridTarget", phase: "discharge", direction: "import", label: t("gridTarget.whileDischarging"), min: -gridTargetMax("discharge"), max: gridTargetMax("discharge"), unit: "W" },
    { name: "grid_target_charge_w", type: "gridTarget", phase: "charge", direction: "export", label: t("gridTarget.whileCharging"), help: t("gridTarget.help"), min: -gridTargetMax("charge"), max: gridTargetMax("charge"), unit: "W" },
    { name: "loop_interval", type: "number", label: t("runtime.loop"), ...accepted("system", "loop_interval"), unit: "s" },
  ].map((field) => withDefault(field, defaults.system?.[field.name])));

  function deviceFields(name, device) {
    const maxPower = Number(limits.devices?.[name] || limits.fallback_device_max_power || 5000);
    const fields = [
      { name: "enabled", type: "toggle", fallback: true, label: t("runtime.deviceEnabled") },
      { name: "ac_charge_enabled", type: "toggle", fallback: switchDefault(limits, name, "ac_charge_enabled"), label: t("runtime.acCharging") },
      { name: "ac_discharge_enabled", type: "toggle", strict: true, label: t("runtime.acDischarging"), help: t("runtime.acDischargingHelp") },
      bounded({ name: "max_power", type: "number", label: t("runtime.maxPower"), min: 0, unit: "W" }, "device", maxPower),
      { name: "pv_priority_factor", type: "number", label: t("runtime.pvPriority"), ...accepted("devices", "pv_priority_factor"), step: 0.01, unit: "×" },
      {
        name: "offgrid_socket_mode",
        type: "select",
        label: t("runtime.offgrid"),
        options: choices("offgrid_socket_mode").map((value) => ({ value, label: t(`runtime.offgrid.${value}`) })),
      },
    ];
    if (unsupported.has(name)) {
      fields.push({ name: "_ac_role", type: "readonly", label: t("runtime.acRole"), value: t("runtime.acRoleMqtt") });
    } else {
      fields.push({
        name: "runtime_role",
        type: "select",
        label: t("runtime.acRole"),
        options: choices("runtime_role").map((value) => ({ value, label: t(`runtime.role.${value.replace("ac_", "")}`) })),
      });
      fields.push({ name: "ac_charge_power_w", type: "number", label: t("runtime.acPower"), min: 0, max: Number(limits.ac_charge_power_max || 5000), unit: "W", clearable: true, placeholder: t("control.deviceMax"), help: t("runtime.acPowerHelp") });
      const reason = String(device.runtime_role_reason || "").trim();
      if (reason) fields.push({ name: "_role_reason", type: "readonly", label: t("runtime.roleSetBy"), value: reason });
    }
    deviceSettings(device, catalogue, limits.device_settings_offered?.[name]).forEach((setting) => {
      const current = settingWord(device[setting.key]);
      const known = current === "unmanaged" || setting.values.some((value) => value.value === current);
      const values = setting.offered ? setting.values : setting.values.filter((value) => value.value === current);
      fields.push({
        name: setting.key,
        type: "select",
        label: settingLabel(setting.key),
        help: translatedOr(`setting.${setting.key}.help`, setting.help),
        options: [
          { value: "unmanaged", label: t("runtime.deviceDecides") },
          ...values.map((value) => ({ value: value.value, label: valueLabel(value) })),
          ...(known ? [] : [{ value: current, label: t("runtime.notAValue", { value: current }) }]),
        ],
      });
    });
    return fields.map((field) => withDefault(field, defaults.devices?.[field.name]));
  }

  function deviceInitial(device) {
    return {
      ...device,
      runtime_role: acRole(device),
      ...Object.fromEntries(catalogue.map((setting) => [setting.key, settingWord(device[setting.key])])),
    };
  }
</script>

<Card icon="sliders-horizontal" tone="c-pv" title={t("runtime.title")} subtitle={t("runtime.sub")} span="span-12">
  {#if !auth.loaded}
    <div class="skeleton"></div>
  {:else if !auth.configured}
    <Empty text={t("runtime.notConfigured")} icon="lock" />
  {:else if !auth.authenticated}
    <Empty text={t("runtime.signIn")} icon="lock">
      {#snippet action()}<button class="btn small" type="button" onclick={() => (auth.loginOpen = true)}>{t("auth.signIn")}</button>{/snippet}
    </Empty>
  {:else if !live.runtime && live.runtimeUnavailable}
    <Empty text={t("runtime.unavailable")} icon="loader" />
  {:else if !live.runtime}
    <div class="skeleton"></div>
  {:else}
    <div class="grid" data-testid="runtime-editor">
      <RuntimeForm endpoint={SYSTEM_ENDPOINT} title={t("runtime.system")} subtitle={t("runtime.systemSub")} icon="gauge" tone="c-soc" step={1} fields={systemFields} initial={runtime.system || {}} {settingLabel} />
      {#each devices as [name, device], index (name)}
        <RuntimeForm endpoint={deviceEndpoint(name)} title={name} subtitle={t("runtime.deviceSub")} icon="zap" tone="c-inv" step={index + 2} fields={deviceFields(name, device || {})} initial={deviceInitial(device || {})} {settingLabel} />
      {/each}
      <RuntimeForm endpoint={WINTER_ENDPOINT} title={t("runtime.winter")} subtitle={t("runtime.winterSub")} icon="snowflake" tone="c-out" step={devices.length + 2} fields={[{ name: "enabled", type: "toggle", label: t("runtime.winter") }]} initial={runtime.winter || {}} {settingLabel} />
      <RuntimeForm endpoint={AC_CHARGE_ENDPOINT} title={t("runtime.acChargeTitle")} subtitle={t("runtime.acChargeSub")} icon="plug-zap" tone="c-grid" step={devices.length + 3} fields={[{ name: "enabled", type: "toggle", label: t("runtime.acCharging") }]} initial={runtime.ac_charge_control || {}} {settingLabel} />
      <RuntimeForm endpoint={BATTERY_CARE_ENDPOINT} title={t("runtime.batteryCare")} subtitle={t("runtime.batteryCareSub")} icon="battery-low" tone="c-soc" step={devices.length + 4} fields={batteryCareFields} initial={runtime.battery_care || {}} {settingLabel} />
    </div>
  {/if}
</Card>

<style>
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: var(--s4); align-items: start; }
  .skeleton { height: 120px; }
</style>
