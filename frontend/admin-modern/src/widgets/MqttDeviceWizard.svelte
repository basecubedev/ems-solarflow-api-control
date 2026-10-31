<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { DEVICE_KINDS, POWER_UNITS, SOC_UNITS, deviceFromAnswers, groupValues, idFromName, isChangeOnly, likelyValue, uniqueId, unitFactor } from "../lib/mqtt.js";

  /**
   * group: the live-topic group the device publishes on; brokerRef: where it does.
   * onsave(device) reviews and applies the device; onfree(device) hands it to the
   * free editor; oncancel() closes. The result is an ordinary mapping entry the
   * server validates and calculates like any other.
   */
  let { group, brokerRef, takenIds = [], busy = false, onsave, onfree, oncancel, onpreview } = $props();

  const values = $derived(groupValues(group));
  const keyOf = (value) => (value ? `${value.topic}|${value.path ?? ""}` : "");
  const byKey = (key) => values.find((value) => keyOf(value) === key) || null;

  let kind = $state("");
  let name = $state("");
  let id = $state("");
  let idTouched = $state(false);
  let powerKey = $state("");
  let powerUnit = $state("W");
  let solarKey = $state("");
  let batteryKey = $state("");
  let socKey = $state("");
  let socUnit = $state("percent");
  let advanced = $state(false);
  let factor = $state("");
  let evaluation = $state.raw(null);

  $effect(() => {
    if (!powerKey) powerKey = keyOf(likelyValue(values, "power") || values[0]);
    if (!socKey && kind && kind !== "balcony") socKey = keyOf(likelyValue(values, "soc"));
  });

  $effect(() => {
    if (!idTouched) id = uniqueId(name ? idFromName(name) : idFromName(group?.key || ""), takenIds);
  });

  const power = $derived(byKey(powerKey));
  const changeOnly = $derived(power ? isChangeOnly(power.topic, power.label) : false);
  const device = $derived.by(() => {
    if (!kind || kind === "other" || !power) return null;
    const result = deviceFromAnswers({
      kind,
      name,
      id: id || null,
      brokerRef,
      power: { ...power, unit: powerUnit },
      solar: byKey(solarKey),
      battery: byKey(batteryKey),
      soc: byKey(socKey) ? { ...byKey(socKey), unit: socUnit } : null,
    });
    const custom = Number(factor);
    if (advanced && factor !== "" && Number.isFinite(custom) && custom !== 0) {
      for (const target of Object.values(result.values)) {
        if (target.sources[0].topic === power.topic && target.sources[0].path === power.path) target.sources[0].factor = custom;
      }
    }
    return result;
  });

  const turned = $derived(Boolean(device?.values?.inverterPower?.sources?.[0]?.invert));

  function choose(next) {
    kind = next;
    evaluation = null;
    if (next === "other") onfree?.({ id: id || idFromName(group?.key || ""), label: name, enabled: true, broker_ref: brokerRef, values: { inverterPower: { sources: [] } } });
  }

  async function preview() {
    evaluation = device ? await onpreview?.(device) : null;
  }

  function display(value) {
    return `${value.path || t("mqtt.topics.whole")} = ${value.value} · ${value.topic}`;
  }
</script>

<section class="wizard inner" data-testid="mqtt-wizard">
  <h3 class="sub">{t("mqtt.wizard.question")}</h3>
  <div class="kinds" role="radiogroup" aria-label={t("mqtt.wizard.question")}>
    {#each DEVICE_KINDS as option (option)}
      <label class="kind inner" class:chosen={kind === option}>
        <input type="radio" name="mqtt-kind" value={option} checked={kind === option} onchange={() => choose(option)} data-testid={`mqtt-kind-${option}`} />
        <span class="words"><strong>{t(`mqtt.wizard.kind.${option}`)}</strong><span class="muted small">{t(`mqtt.wizard.kind.${option}.hint`)}</span></span>
      </label>
    {/each}
  </div>

  {#if kind && kind !== "other"}
    <label class="field">
      <span class="small muted">{t("mqtt.wizard.name")}</span>
      <input class="input" bind:value={name} placeholder={t("mqtt.wizard.namePlaceholder")} data-testid="mqtt-wizard-name" />
    </label>

    {#if !values.length}
      <p class="note warn"><Icon name="triangle-alert" />{t("mqtt.wizard.noValues")}</p>
    {:else}
      <div class="row-wrap">
        <label class="field grow">
          <span class="small muted">{kind === "ac_battery" ? t("mqtt.wizard.powerAc") : t("mqtt.wizard.power")}</span>
          <select class="input" bind:value={powerKey} data-testid="mqtt-wizard-power">
            {#each values as value (keyOf(value))}<option value={keyOf(value)}>{display(value)}</option>{/each}
          </select>
        </label>
        <label class="field">
          <span class="small muted">{t("mqtt.wizard.unit")}</span>
          <select class="input" bind:value={powerUnit}>
            {#each POWER_UNITS as unit (unit)}<option value={unit}>{t(`mqtt.unit.${unit}`)}</option>{/each}
          </select>
        </label>
      </div>
      {#if kind === "ac_battery"}<p class="muted small">{t("mqtt.wizard.whyInverter")}</p>{/if}
      {#if turned}<p class="note" data-testid="mqtt-wizard-turned"><Icon name="info" />{t("mqtt.wizard.turned")}</p>{/if}
      {#if changeOnly}<p class="muted small">{t("mqtt.wizard.changeOnly")}</p>{/if}

      {#if kind === "hybrid"}
        <label class="field">
          <span class="small muted">{t("mqtt.wizard.solar")}</span>
          <select class="input" bind:value={solarKey}>
            <option value="">{t("mqtt.wizard.none")}</option>
            {#each values as value (keyOf(value))}<option value={keyOf(value)}>{display(value)}</option>{/each}
          </select>
        </label>
      {/if}
      {#if kind === "hybrid" || kind === "ac_battery"}
        <label class="field">
          <span class="small muted">{t("mqtt.wizard.battery")}</span>
          <select class="input" bind:value={batteryKey}>
            <option value="">{t("mqtt.wizard.none")}</option>
            {#each values as value (keyOf(value))}<option value={keyOf(value)}>{display(value)}</option>{/each}
          </select>
        </label>
        <div class="row-wrap">
          <label class="field grow">
            <span class="small muted">{t("mqtt.wizard.soc")}</span>
            <select class="input" bind:value={socKey}>
              <option value="">{t("mqtt.wizard.none")}</option>
              {#each values as value (keyOf(value))}<option value={keyOf(value)}>{display(value)}</option>{/each}
            </select>
          </label>
          <label class="field">
            <span class="small muted">{t("mqtt.wizard.unit")}</span>
            <select class="input" bind:value={socUnit}>
              {#each SOC_UNITS as unit (unit)}<option value={unit}>{t(`mqtt.unit.${unit}`)}</option>{/each}
            </select>
          </label>
        </div>
      {/if}

      <details bind:open={advanced}>
        <summary>{t("mqtt.wizard.advanced")}</summary>
        <div class="advanced">
          <label class="field"><span class="small muted">{t("mqtt.mapping.id")}</span><input class="input" value={id} oninput={(event) => { idTouched = true; id = event.currentTarget.value; }} data-testid="mqtt-wizard-id" /></label>
          {#if power}<p class="muted small"><code>{power.topic}</code>{power.path ? ` → ${power.path}` : ""}</p>{/if}
          <label class="field"><span class="small muted">{t("mqtt.mapping.factor")}</span><input class="input narrow" type="number" step="any" bind:value={factor} placeholder={String(unitFactor(powerUnit))} /></label>
          <button class="btn secondary small" type="button" onclick={() => device && onfree?.(device)} disabled={!device}><Icon name="wrench" size="sm" />{t("mqtt.wizard.openEditor")}</button>
        </div>
      </details>

      <div class="row-wrap">
        <button class="btn secondary small" type="button" onclick={preview} disabled={busy || !device} data-testid="mqtt-wizard-preview"><Icon name="eye" size="sm" />{t("mqtt.mapping.preview")}</button>
        <button class="btn small" type="button" onclick={() => device && onsave?.(device)} disabled={busy || !device || !name.trim()} data-testid="mqtt-wizard-save"><Icon name="save" size="sm" />{t("mqtt.mapping.save")}</button>
        <button class="btn secondary small" type="button" onclick={oncancel}>{t("mqtt.mapping.cancel")}</button>
      </div>
      {#if evaluation?.state}
        <p class="note ok" data-testid="mqtt-preview-state"><Icon name="circle-check" />{t("mqtt.mapping.wouldPublish")} <code>{JSON.stringify(evaluation.state)}</code></p>
      {:else if evaluation}
        <p class="note warn" data-testid="mqtt-preview-silent"><Icon name="triangle-alert" />{t("mqtt.mapping.wouldNotPublish", { reason: evaluation.reasonText || evaluation.reason })}</p>
      {/if}
    {/if}
  {/if}
</section>

<style>
  .wizard { display: grid; gap: var(--s3); }
  .kinds { display: grid; grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr)); gap: var(--s2); }
  .kind { display: flex; gap: var(--s2); align-items: flex-start; cursor: pointer; }
  .kind.chosen { outline: 2px solid var(--m-accent); }
  .words { display: grid; gap: 2px; }
  .field { display: grid; gap: 2px; min-width: 0; }
  .field.grow { flex: 1 1 16rem; }
  .field select { max-width: 100%; }
  .advanced { display: grid; gap: var(--s2); padding-top: var(--s2); }
  .narrow { width: 8rem; }
</style>
