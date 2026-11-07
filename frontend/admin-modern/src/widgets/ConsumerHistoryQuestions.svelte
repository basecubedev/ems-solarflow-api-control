<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import Segmented from "../../../shared/ui/Segmented.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";

  /**
   * What happens to a consumer's recorded values when a change touches them,
   * one choice per question the server asked: carried along, or taken over,
   * or begun anew, the old values then staying as a former consumer.
   */
  let { questions = [], answers = {}, onanswer } = $props();

  const f = $derived(fmt());

  function line(question) {
    return t(`history.question.${question.kind}`, {
      name: question.name,
      previous: question.previous_name || question.name,
      history: question.history_name,
      energy: f.energy(question.kwh),
      date: f.date(question.last_date),
    });
  }

  function options(question) {
    return [
      { value: "keep", label: t(question.kind === "reused" ? "history.answer.takeOver" : "history.answer.carry") },
      { value: "new", label: t("history.answer.new") },
    ];
  }
</script>

<ul class="questions" data-testid="consumer-history">
  {#each questions as question (question.name)}
    <li data-consumer={question.name}>
      <p>{line(question)}</p>
      <Segmented options={options(question)} value={answers[question.name] ?? null} label={question.name} onchange={(value) => onanswer?.(question.name, value)} />
    </li>
  {/each}
</ul>
<p class="muted hint">{t("history.newHint")}</p>

<style>
  .questions { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--s3); }
  .questions li { display: flex; flex-direction: column; gap: var(--s2); }
  .questions p { margin: 0; overflow-wrap: anywhere; }
  .hint { font-size: 12.5px; margin: var(--s3) 0 0; }
</style>
