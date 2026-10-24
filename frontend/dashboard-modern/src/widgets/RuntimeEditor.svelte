<script module>
  export const manifest = { id: "runtime-editor", data: ["runtime", "auth"], refresh: "on-save", minWidth: 320, states: ["auth-not-configured", "signed-out", "editing"] };
</script>

<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Empty from "../../../shared/ui/Empty.svelte";
  import RuntimeForm from "./RuntimeForm.svelte";
  import { t, translatedOr } from "../i18n/i18n.svelte.js";
  import { auth } from "../state/auth.svelte.js";
  import { live, loadRuntime } from "../state/live.svelte.js";
  import { SYSTEM_ENDPOINT, WINTER_ENDPOINT, AC_CHARGE_ENDPOINT, deviceEndpoint, acRole, settingWord, deviceSettings } from "../lib/runtime.js";

  const runtime = $derived(live.runtime || {});
  const limits = $derived(runtime._limits || {});
  const catalogue = $derived(limits.device_settings || []);
  const unsupported = $derived(new Set(limits.ac_role_unsupported || []));
  const devices = $derived(Object.entries(runtime.devices || {}));

  $effect(() => {
    if (auth.authenticated) loadRuntime();
  });

  function settingLabel(key) {
    return translatedOr(`setting.${key}`, catalogue.find((setting) => setting.key === key)?.label || key);
  }

  function valueLabel(value) {
    return translatedOr(`settingValue.${value.value}`, value.label || value.value);
  }

  const systemFields = $derived([
    { name: "enabled", type: "toggle", label: t("runtime.emsEnabled") },
    { name: "max_total_power", type: "number", label: t("runtime.maxTotal"), min: 0, max: Number(limits.system?.max_total_power || 5000), unit: "W" },
    { name: "min_output_limit", type: "number", label: t("runtime.minOutput"), min: 0, max: Number(limits.system?.min_output_limit || 5000), unit: "W" },
    { name: "loop_interval", type: "number", label: t("runtime.loop"), min: 1, max: 3600, unit: "s" },
  ]);

  function deviceFields(name, device) {
    const maxPower = Number(limits.devices?.[name] || limits.fallback_device_max_power || 5000);
    const fields = [
      { name: "enabled", type: "toggle", label: t("runtime.deviceEnabled") },
      { name: "ac_charge_enabled", type: "toggle", label: t("runtime.acCharging") },
      { name: "max_power", type: "number", label: t("runtime.maxPower"), min: 0, max: maxPower, unit: "W" },
      { name: "pv_priority_factor", type: "number", label: t("runtime.pvPriority"), min: 0.01, max: 100, step: 0.01, unit: "×" },
      {
        name: "offgrid_socket_mode",
        type: "select",
        label: t("runtime.offgrid"),
        options: ["off", "eco", "standard"].map((value) => ({ value, label: t(`runtime.offgrid.${value}`) })),
      },
    ];
    if (unsupported.has(name)) {
      fields.push({ name: "_ac_role", type: "readonly", label: t("runtime.acRole"), value: t("runtime.acRoleMqtt") });
    } else {
      fields.push({
        name: "runtime_role",
        type: "select",
        label: t("runtime.acRole"),
        options: [
          { value: "ac_output", label: t("runtime.role.output") },
          { value: "ac_input", label: t("runtime.role.input") },
        ],
      });
      fields.push({ name: "ac_charge_power_w", type: "number", label: t("runtime.acPower"), min: 0, max: Number(limits.ac_charge_power_max || 5000), unit: "W" });
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
    return fields;
  }

  function deviceInitial(device) {
    return {
      ...device,
      runtime_role: acRole(device),
      ...Object.fromEntries(catalogue.map((setting) => [setting.key, settingWord(device[setting.key])])),
    };
  }
</script>

<Card title={t("runtime.title")} subtitle={t("runtime.sub")} span="span-12">
  {#if !auth.loaded}
    <div class="skeleton"></div>
  {:else if !auth.configured}
    <Empty text={t("runtime.notConfigured")} icon="lock" />
  {:else if !auth.authenticated}
    <Empty text={t("runtime.signIn")} icon="lock">
      {#snippet action()}<button class="btn small" type="button" onclick={() => (auth.loginOpen = true)}>{t("auth.signIn")}</button>{/snippet}
    </Empty>
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
    </div>
  {/if}
</Card>

<style>
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: var(--s4); align-items: start; }
  .skeleton { height: 120px; }
</style>
