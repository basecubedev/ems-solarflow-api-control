<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Empty from "../../../shared/ui/Empty.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import ConfirmDialog from "../../../shared/ui/ConfirmDialog.svelte";
  import OperatorGate from "../ui/OperatorGate.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { auth, authorizedSend, operatorGet } from "../state/auth.svelte.js";
  import { errorText } from "../lib/errors.js";
  import {
    PRESET_ORDER, WEEKDAYS, WINTER_MONTHS, STATUSES, actionById, newDraft, draftFromRule, rulePayload,
    valueLimit, whenParts, offsetText, shiftDate, daysBetween,
  } from "../lib/schedule.js";

  const TONES = { waiting: "c-ok", paused: "c-idle", orphaned: "c-warn", refused: "c-warn", finished: "c-idle", broken: "c-fault" };
  const KNOWN_ACTIONS = ["total_output_limit", "device_output_limit", "pv_priority", "offgrid_socket", "charge_from_grid", "surplus_charging", "device_surplus_charging", "winter_mode", "ems_control", "device_control", "light", "fan", "buzzer", "auto_heat", "minimum_output_limit"];
  const KNOWN_GROUPS = ["power", "offgrid_socket", "ac_charging", "battery", "control", "device_settings", "advanced"];

  let overview = $state.raw(null);
  let loadError = $state("");
  let message = $state("");
  let draft = $state(null);
  let preview = $state.raw(null);
  let previewOk = $state(false);
  let saving = $state(false);
  let deleting = $state(null);
  let previewTimer = null;
  let previewSequence = 0;

  const f = $derived(fmt());
  const action = $derived(draft ? actionById(overview, draft.action) : null);
  const zoneNote = $derived.by(() => {
    if (!overview) return "";
    if (overview.zone_message) return overview.zone_message;
    const browser = -new Date().getTimezoneOffset();
    if (overview.zone_offset_minutes != null && overview.zone_offset_minutes !== browser) {
      return t("schedule.zoneDiffers", { zone: overview.zone, ems: offsetText(overview.zone_offset_minutes), browser: offsetText(browser) });
    }
    return "";
  });

  async function load() {
    if (!auth.authenticated) return;
    try {
      overview = await operatorGet("/api/schedule");
      loadError = "";
    } catch (error) {
      overview = null;
      loadError = errorText(error, t);
    }
  }

  $effect(() => {
    void auth.authenticated;
    load();
  });

  $effect(() => {
    if (!draft) return;
    const snapshot = JSON.stringify(draft);
    clearTimeout(previewTimer);
    previewTimer = setTimeout(() => runPreview(snapshot), 250);
    return () => clearTimeout(previewTimer);
  });

  async function runPreview(snapshot) {
    if (!draft || !overview) return;
    const current = JSON.parse(snapshot);
    const sequence = ++previewSequence;
    try {
      const answer = await authorizedSend("/api/schedule/preview", { body: { rule: rulePayload(current, overview), id: current.id } });
      if (sequence !== previewSequence) return;
      preview = answer;
      previewOk = Boolean(answer?.ok);
    } catch (error) {
      if (sequence !== previewSequence) return;
      preview = { ok: false, message: errorText(error, t) };
      previewOk = false;
    }
  }

  function when(at) {
    const parts = whenParts(at);
    if (!parts) return String(at || "");
    return `${f.weekday(parts.date)} ${f.date(parts.date.toISOString().slice(0, 10), { short: true })} ${parts.time}`;
  }

  function actionLabel(item) {
    return KNOWN_ACTIONS.includes(item.id) ? t(`schedule.action.${item.id}`) : item.label;
  }

  function choiceLabel(item, choice) {
    const key = `schedule.choice.${choice}`;
    const translated = t(key);
    if (translated !== key) return translated;
    return item.choice_labels?.[choice] ?? choice;
  }

  function startNew() {
    draft = newDraft(overview);
    preview = null;
    previewOk = false;
  }

  function changeAction(id) {
    const next = actionById(overview, id);
    draft.action = id;
    draft.value = "";
    draft.end_value = "";
    draft.device = next?.per_device ? next.devices[0] || null : null;
  }

  function changeDate(value) {
    const previous = draft.date;
    draft.date = value;
    if (previous && value && draft.end_date) draft.end_date = shiftDate(draft.end_date, daysBetween(previous, value));
  }

  function toggleIn(list, value) {
    return list.includes(value) ? list.filter((item) => item !== value) : [...list, value];
  }

  async function save(event) {
    event.preventDefault();
    if (!draft || !previewOk || saving) return;
    saving = true;
    const payload = rulePayload(draft, overview);
    try {
      const answer = draft.id
        ? await authorizedSend(`/api/schedule/rules/${encodeURIComponent(draft.id)}`, { method: "PUT", body: { revision: draft.revision, rule: payload } })
        : await authorizedSend("/api/schedule/rules", { body: { rule: payload } });
      message = t("schedule.saved", { name: answer?.name || draft.name });
      draft = null;
      await load();
    } catch (error) {
      message = `${errorText(error, t)}${error?.status === 409 ? ` ${t("schedule.conflict")}` : ""}`;
    } finally {
      saving = false;
    }
  }

  async function ruleAction(kind, rule) {
    const path = `/api/schedule/rules/${encodeURIComponent(rule.id)}`;
    try {
      let answer = null;
      if (kind === "toggle") {
        answer = await authorizedSend(path, { method: "PUT", body: { revision: rule.revision, rule: { name: rule.name, enabled: !rule.enabled, target: rule.target, action: rule.action, when: rule.when } } });
      } else if (kind === "skip") {
        answer = await authorizedSend(`${path}/skip`, { body: { revision: rule.revision } });
      } else if (kind === "apply") {
        answer = await authorizedSend(`${path}/apply-now`, { body: {} });
        message = t("schedule.appliedNow", { text: answer?.text || "" });
      } else if (kind === "up" || kind === "down") {
        answer = await authorizedSend(`${path}/move`, { body: { direction: kind } });
      } else if (kind === "delete") {
        const revision = rule.revision ? `?revision=${encodeURIComponent(rule.revision)}` : "";
        answer = await authorizedSend(`${path}${revision}`, { method: "DELETE", body: {} });
      }
      if (kind !== "apply") message = "";
      return answer;
    } catch (error) {
      message = errorText(error, t);
    } finally {
      await load();
    }
  }

  async function setRunning(enabled) {
    try {
      overview = await authorizedSend("/api/schedule", { method: "PATCH", body: { enabled } });
      message = "";
    } catch (error) {
      message = errorText(error, t);
    }
  }
</script>

{#snippet valueControl(name, value, onchange)}
  {#if action?.value_type === "switch"}
    <select class="input" value={String(value ?? "")} onchange={(e) => onchange(e.currentTarget.value)}>
      <option value="">{t("schedule.choose")}</option>
      <option value="true">{t("common.on")}</option>
      <option value="false">{t("common.off")}</option>
    </select>
  {:else if action?.value_type === "choice"}
    <select class="input" {value} onchange={(e) => onchange(e.currentTarget.value)}>
      <option value="">{t("schedule.choose")}</option>
      {#each action.choices as choice (choice)}<option value={choice}>{choiceLabel(action, choice)}</option>{/each}
    </select>
  {:else if action?.value_type === "factor"}
    <span class="number"><input class="input num" type="number" min="0.01" max="100" step="0.01" {value} oninput={(e) => onchange(e.currentTarget.value)} aria-label={name} /><span>×</span></span>
  {:else if action}
    <span class="number"><input class="input num" type="number" min="0" max={valueLimit(action, draft.device, overview)} step="1" {value} oninput={(e) => onchange(e.currentTarget.value)} aria-label={name} /><span>{action.value_type === "charge" ? t("schedule.wattsStop") : "W"}</span></span>
  {/if}
{/snippet}

<OperatorGate>
  {#if message}<div class="span-12 message" role="status">{message}</div>{/if}
  {#if !overview}
    {#if loadError}
      <div class="span-12"><Empty text={loadError} error={true}>{#snippet action()}<button class="btn small" type="button" onclick={load}>{t("common.retry")}</button>{/snippet}</Empty></div>
    {:else}
      <div class="span-12"><Empty text={t("common.loading")} icon="loader" /></div>
    {/if}
  {:else}
    <Card icon="calendar-clock" tone="c-pv" title={overview.enabled !== false ? t("schedule.on") : t("schedule.paused")} subtitle={t("schedule.sub", { zone: overview.zone || "" })} span="span-12">
      <div class="row-wrap">
        {#if !overview.clock_trusted}<Status tone="c-warn" icon="clock-alert" text={t("schedule.clockUntrusted")} />{/if}
        <button class="btn small" type="button" onclick={startNew} disabled={Boolean(overview.zone_message) || Boolean(draft)}><Icon name="plus" size="sm" />{t("schedule.new")}</button>
        <button class="btn secondary small" type="button" onclick={() => setRunning(overview.enabled === false)}>
          <Icon name={overview.enabled === false ? "play" : "pause"} size="sm" />{overview.enabled === false ? t("schedule.resume") : t("schedule.pauseAll")}
        </button>
      </div>
      {#if zoneNote}<p class="note"><Icon name="clock" />{zoneNote}</p>{/if}
    </Card>

    {#if draft}
      <form class="card span-12 editor" onsubmit={save} novalidate>
        <h2 class="card-title">{draft.id ? t("schedule.edit") : t("schedule.new")}</h2>
        <fieldset>
          <legend>1 · {t("schedule.what")}</legend>
          <div class="grid">
            <label class="field"><span>{t("schedule.name")}</span><input class="input" type="text" maxlength="80" bind:value={draft.name} placeholder={t("schedule.namePlaceholder")} /></label>
            <label class="field"><span>{t("schedule.actionLabel")}</span>
              <select class="input" value={draft.action} onchange={(e) => changeAction(e.currentTarget.value)}>
                {#each overview.groups || [] as group (group.id)}
                  {@const items = (overview.actions || []).filter((item) => item.group === group.id)}
                  {#if items.length}
                    <optgroup label={KNOWN_GROUPS.includes(group.id) ? t(`schedule.group.${group.id}`) : group.label}>
                      {#each items as item (item.id)}<option value={item.id} disabled={item.per_device && !item.devices.length}>{actionLabel(item)}</option>{/each}
                    </optgroup>
                  {/if}
                {/each}
              </select>
            </label>
            <label class="field"><span>{t("schedule.appliesTo")}</span>
              {#if action?.per_device}
                <select class="input" bind:value={draft.device}>
                  {#each action.devices as name (name)}<option value={name}>{name}</option>{/each}
                  {#if draft.device && !action.devices.includes(draft.device)}<option value={draft.device}>{t("schedule.notAvailable", { device: draft.device })}</option>{/if}
                </select>
              {:else}
                <span class="readonly">{t("schedule.wholeSystem")}</span>
              {/if}
            </label>
            <label class="field"><span>{t("schedule.setTo")}</span>{@render valueControl(t("schedule.setTo"), draft.value, (v) => (draft.value = v))}</label>
          </div>
          {#if action?.help}<p class="help">{action.help}</p>{/if}
        </fieldset>
        <fieldset>
          <legend>2 · {t("schedule.when")}</legend>
          <div class="chips" role="radiogroup" aria-label={t("schedule.when")}>
            <label class="chip"><input type="radio" value="point" bind:group={draft.mode} />{t("schedule.point")}</label>
            <label class="chip"><input type="radio" value="window" bind:group={draft.mode} />{t("schedule.window")}</label>
          </div>
          <div class="grid">
            <label class="field"><span>{draft.preset === "once" ? t("schedule.on") : t("schedule.starting")}</span><input class="input" type="date" value={draft.date} onchange={(e) => changeDate(e.currentTarget.value)} /></label>
            <label class="field"><span>{draft.mode === "window" ? t("schedule.from") : t("schedule.at")}</span><input class="input" type="time" bind:value={draft.from} /></label>
            {#if draft.mode === "window"}
              <label class="field"><span>{t("schedule.to")}</span><input class="input" type="time" bind:value={draft.to} /></label>
              <label class="field"><span>{draft.preset === "once" ? t("schedule.untilDate") : t("schedule.firstEnds")}</span><input class="input" type="date" bind:value={draft.end_date} /></label>
              <label class="field"><span>{t("schedule.thenSetTo")}</span>{@render valueControl(t("schedule.thenSetTo"), draft.end_value, (v) => (draft.end_value = v))}</label>
            {/if}
          </div>
          {#if draft.mode === "window"}<p class="help">{t("schedule.overnight")}</p>{/if}
        </fieldset>
        <fieldset>
          <legend>3 · {t("schedule.repeat")}</legend>
          <div class="chips">
            {#if draft.keptRepeat}<label class="chip"><input type="radio" value="kept" bind:group={draft.preset} />{t("schedule.kept")}</label>{/if}
            {#each PRESET_ORDER as name (name)}<label class="chip"><input type="radio" value={name} bind:group={draft.preset} />{t(`schedule.preset.${name}`)}</label>{/each}
          </div>
          {#if draft.preset === "kept"}<p class="help">{t("schedule.keptHelp")}</p>{/if}
          {#if draft.preset === "custom"}
            <div class="grid">
              <label class="field"><span>{t("schedule.every")}</span>
                <span class="number">
                  <input class="input num" type="number" min="1" max="366" step="1" bind:value={draft.custom.interval} />
                  <select class="input" bind:value={draft.custom.freq} aria-label={t("schedule.unit")}>
                    {#each ["daily", "weekly", "monthly", "yearly"] as freq (freq)}<option value={freq}>{t(`schedule.freq.${freq}`)}</option>{/each}
                  </select>
                </span>
              </label>
              {#if draft.custom.freq === "monthly" || draft.custom.freq === "yearly"}
                <label class="field"><span>{t("schedule.onDay")}</span>
                  <select class="input" bind:value={draft.custom.monthly}>
                    <option value="day">{t("schedule.monthly.day")}</option>
                    <option value="last">{t("schedule.monthly.last")}</option>
                    <option value="weekday">{t("schedule.monthly.weekday")}</option>
                  </select>
                </label>
                {#if draft.custom.monthly === "day"}<label class="field"><span>{t("schedule.day")}</span><input class="input num" type="number" min="1" max="31" bind:value={draft.custom.month_day} /></label>{/if}
                {#if draft.custom.monthly === "weekday"}
                  <label class="field"><span>{t("schedule.which")}</span>
                    <span class="number">
                      <select class="input" bind:value={draft.custom.set_pos}>
                        {#each [1, 2, 3, 4, -1] as pos (pos)}<option value={pos}>{t(`schedule.pos.${pos}`)}</option>{/each}
                      </select>
                      <select class="input" bind:value={draft.custom.set_day} aria-label={t("schedule.weekdayLabel")}>
                        {#each WEEKDAYS as day (day)}<option value={day}>{t(`schedule.weekday.${day}`)}</option>{/each}
                      </select>
                    </span>
                  </label>
                {/if}
              {/if}
            </div>
            {#if draft.custom.freq === "weekly" || draft.custom.freq === "daily"}
              <div class="chips" role="group" aria-label={t("schedule.onDays")}>
                {#each WEEKDAYS as day (day)}
                  <label class="chip"><input type="checkbox" checked={draft.custom.by_weekday.includes(day)} onchange={() => (draft.custom.by_weekday = toggleIn(draft.custom.by_weekday, day))} />{t(`schedule.weekday.${day}`)}</label>
                {/each}
              </div>
            {/if}
          {/if}
          {#if draft.preset !== "once" && draft.preset !== "kept"}
            <div class="chips" role="group" aria-label={t("schedule.onlyIn")}>
              <span class="muted small">{t("schedule.onlyIn")}</span>
              {#each Array.from({ length: 12 }, (_, i) => i + 1) as month (month)}
                <label class="chip"><input type="checkbox" checked={draft.months.includes(month)} onchange={() => (draft.months = toggleIn(draft.months, month))} />{f.month(month)}</label>
              {/each}
              <button class="btn secondary small" type="button" onclick={() => (draft.months = [...WINTER_MONTHS])}>{t("schedule.winterMonths")}</button>
              <button class="btn secondary small" type="button" onclick={() => (draft.months = [])}>{t("schedule.allMonths")}</button>
            </div>
            <div class="grid">
              <label class="field"><span>{t("schedule.ends")}</span>
                <select class="input" bind:value={draft.ends}>
                  <option value="never">{t("schedule.ends.never")}</option>
                  <option value="until">{t("schedule.ends.until")}</option>
                  <option value="count">{t("schedule.ends.count")}</option>
                </select>
              </label>
              {#if draft.ends === "until"}<label class="field"><span>{t("schedule.lastDate")}</span><input class="input" type="date" bind:value={draft.until} /></label>{/if}
              {#if draft.ends === "count"}<label class="field"><span>{t("schedule.times")}</span><input class="input num" type="number" min="1" max="1000" bind:value={draft.count} /></label>{/if}
            </div>
          {/if}
        </fieldset>
        <fieldset>
          <legend>4 · {t("schedule.check")}</legend>
          <div class="preview inner" role="status" aria-live="polite" data-testid="schedule-preview">
            {#if !preview}
              <span class="muted">{t("schedule.checking")}</span>
            {:else if !preview.ok}
              <Status tone="c-warn" icon="triangle-alert" text={t("schedule.cannotSave")} /> <span>{preview.message || preview.error || t("schedule.refused")}</span>
            {:else}
              <p>{preview.summary}</p>
              <p>{preview.action}</p>
              <ul>
                {#each (preview.next || []).slice(0, 5) as point, index (index)}<li class="num">{when(point.at)} → {point.text}</li>{:else}<li class="muted">{t("schedule.nothingSoon")}</li>{/each}
              </ul>
              {#if preview.first_action}<p class="help">{t("schedule.firstAction", { at: when(preview.first_action) })}</p>{/if}
            {/if}
          </div>
        </fieldset>
        <div class="row-wrap">
          <button class="btn" type="submit" disabled={!previewOk || saving} data-testid="schedule-save">{t("schedule.save")}</button>
          <button class="btn secondary" type="button" onclick={() => (draft = null)}>{t("auth.cancel")}</button>
        </div>
      </form>
    {/if}

    <Card icon="clock" tone="c-pv" title={t("schedule.next")} span="span-4">
      <ul class="next">
        {#each overview.next || [] as point, index (index)}
          <li><span class="num when">{when(point.at)}</span><span>{point.name}</span><span class="muted">→ {point.text}</span></li>
        {:else}
          <li class="muted">{t("schedule.nothingSoon")}</li>
        {/each}
      </ul>
    </Card>

    <Card icon="list" tone="c-pv" title={t("schedule.rules")} span="span-8">
      {#if !(overview.rules || []).length}
        <Empty text={t("schedule.noRules")} />
      {:else}
        <div class="rules">
          {#each overview.rules as rule, index (rule.id || index)}
            <div class="rule inner" data-rule={rule.id}>
              <div class="rule-head">
                <span class="rule-name">{rule.status === "broken" ? rule.id || t("schedule.unreadable") : rule.name}</span>
                <Status tone={TONES[rule.status] || "c-idle"} text={STATUSES.includes(rule.status) ? t(`schedule.status.${rule.status}`) : rule.status || ""} />
              </div>
              {#if rule.status === "broken"}
                <p class="muted">{rule.message}</p>
              {:else}
                <p>{rule.summary}</p>
                <p class="muted">{rule.action_summary}</p>
                {#if rule.message}<p class="muted">{rule.message}</p>{/if}
                <p class="small">{rule.next ? t("schedule.nextPoint", { at: when(rule.next.at), text: rule.next.text }) : t("schedule.noNext")}</p>
              {/if}
              <div class="row-wrap actions">
                {#if rule.status !== "broken"}
                  <button class="btn secondary small" type="button" onclick={() => { draft = draftFromRule(rule, overview); preview = null; previewOk = false; }}><Icon name="pencil" size="sm" />{t("schedule.editShort")}</button>
                  <button class="btn secondary small" type="button" onclick={() => ruleAction("toggle", rule)}>{rule.enabled ? t("schedule.switchOff") : t("schedule.switchOn")}</button>
                  <button class="btn secondary small" type="button" disabled={!rule.next} onclick={() => ruleAction("skip", rule)}><Icon name="skip-forward" size="sm" />{t("schedule.skip")}</button>
                  <button class="btn secondary small" type="button" disabled={rule.status === "paused"} onclick={() => ruleAction("apply", rule)}>{t("schedule.applyNow")}</button>
                  <button class="icon-btn" type="button" disabled={index === 0} aria-label={t("schedule.up", { name: rule.name })} onclick={() => ruleAction("up", rule)}><Icon name="arrow-up" size="sm" /></button>
                  <button class="icon-btn" type="button" aria-label={t("schedule.down", { name: rule.name })} onclick={() => ruleAction("down", rule)}><Icon name="arrow-down" size="sm" /></button>
                {/if}
                <button class="btn danger small" type="button" onclick={() => (deleting = rule)}><Icon name="trash-2" size="sm" />{t("schedule.delete")}</button>
              </div>
            </div>
          {/each}
        </div>
      {/if}
    </Card>
  {/if}
</OperatorGate>

<ConfirmDialog
  open={Boolean(deleting)}
  title={t("schedule.delete")}
  lines={[t("schedule.deleteText", { name: deleting?.name || deleting?.id || "" })]}
  confirmLabel={t("schedule.delete")}
  danger={true}
  onconfirm={() => {
    const rule = deleting;
    deleting = null;
    ruleAction("delete", rule);
  }}
  oncancel={() => (deleting = null)}
/>

<style>
  .message { padding: var(--s3) var(--s4); border-radius: var(--m-radius-inner); background: var(--m-surface-2); border: 1px solid var(--m-line-strong); }
  .editor { display: flex; flex-direction: column; gap: var(--s4); }
  fieldset { border: 1px solid var(--m-line); border-radius: var(--m-radius-inner); padding: var(--s3) var(--s4) var(--s4); margin: 0; display: flex; flex-direction: column; gap: var(--s3); min-width: 0; }
  legend { font-size: 12.5px; color: var(--m-muted); padding: 0 6px; font-weight: 600; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: var(--s3); }
  .number { display: inline-flex; gap: 6px; align-items: center; }
  .number .input { width: auto; min-width: 80px; }
  .readonly { font-size: 13px; padding: 8px 0; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 999px; border: 1px solid var(--m-line-strong); font-size: 12.5px; cursor: pointer; }
  .chip input { accent-color: var(--m-accent); }
  .help { margin: 0; font-size: 12px; color: var(--m-muted); }
  .preview p { margin: 0 0 6px; }
  .preview ul, .next { margin: 0; padding-left: 18px; font-size: 13px; display: flex; flex-direction: column; gap: 4px; }
  .next { list-style: none; padding: 0; }
  .next li { display: grid; gap: 2px; }
  .when { font-weight: 600; }
  .rules { display: flex; flex-direction: column; gap: var(--s3); }
  .rule p { margin: 4px 0; font-size: 13px; }
  .rule-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
  .rule-name { font-weight: 620; }
  .actions { margin-top: var(--s2); gap: 6px; }
  .small { font-size: 12px; }
</style>
