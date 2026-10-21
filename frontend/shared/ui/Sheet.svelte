<script>
  import { tick, untrack } from "svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { prefs } from "../state/prefs.svelte.js";

  /**
   * A panel that slides up from the bottom edge. It is a native modal
   * <dialog>, so focus stays inside and Esc closes it; the scrim, a tap on the
   * handle and a swipe down on the handle close it as well. The content
   * scrolls on its own, and the element marked data-focus takes the focus.
   */
  let { open = false, label = "", onclose, children } = $props();

  const SWIPE_CLOSE_PX = 60;
  const TAP_SLOP_PX = 6;
  const SLIDE = { duration: 220, easing: "cubic-bezier(0.2, 0.8, 0.2, 1)" };

  let dialog = $state();
  let drag = $state(0);
  let startY = null;
  let moved = false;
  let closing = false;
  let animation = null;

  $effect(() => {
    if (!dialog) return;
    if (open && !dialog.open) untrack(show);
    if (!open && dialog.open) untrack(dismiss);
  });

  function slide(from, to, fill = "none") {
    animation?.cancel();
    animation = null;
    if (!prefs.animate || typeof dialog.animate !== "function") return null;
    animation = dialog.animate([{ transform: from }, { transform: to }], { ...SLIDE, fill });
    return animation;
  }

  async function show() {
    closing = false;
    moved = false;
    drag = 0;
    dialog.showModal();
    slide("translateY(100%)", "none");
    await tick();
    dialog.querySelector("[data-focus]")?.focus();
  }

  function dismiss() {
    if (closing || !dialog?.open) return;
    closing = true;
    const run = slide(getComputedStyle(dialog).transform, "translateY(100%)", "forwards");
    if (!run) {
      dialog.close();
      return;
    }
    run.finished.then(
      () => dialog?.close(),
      () => dialog?.close(),
    );
  }

  function closed() {
    animation?.cancel();
    animation = null;
    closing = false;
    drag = 0;
    startY = null;
    onclose?.();
  }

  function grab(event) {
    if (event.button > 0) return;
    startY = event.clientY;
    moved = false;
  }

  function pull(event) {
    if (startY !== null && event.buttons === 0) startY = null;
    if (startY === null) return;
    const offset = event.clientY - startY;
    if (!moved && Math.abs(offset) > TAP_SLOP_PX) {
      moved = true;
      event.currentTarget.setPointerCapture?.(event.pointerId);
    }
    if (moved) drag = Math.max(0, offset);
  }

  function release() {
    if (startY === null) return;
    startY = null;
    if (drag > SWIPE_CLOSE_PX) {
      dismiss();
      return;
    }
    if (drag) {
      const from = `translateY(${drag}px)`;
      drag = 0;
      slide(from, "none");
    }
  }

  function tapHandle(event) {
    if (moved && event.detail > 0) {
      moved = false;
      return;
    }
    dismiss();
  }
</script>

<dialog
  class="sheet"
  bind:this={dialog}
  aria-label={label}
  style:transform={drag ? `translateY(${drag}px)` : null}
  oncancel={(event) => {
    event.preventDefault();
    dismiss();
  }}
  onclose={closed}
  onclick={(event) => event.target === dialog && dismiss()}
>
  <div class="sheet-body">
    <div class="sheet-grab" onpointerdown={grab} onpointermove={pull} onpointerup={release} onpointercancel={release} role="presentation">
      <button class="sheet-handle" type="button" aria-label={t("common.close")} title={t("common.close")} onclick={tapHandle} data-testid="sheet-handle"><span></span></button>
    </div>
    <div class="sheet-content">{@render children?.()}</div>
  </div>
</dialog>

<style>
  dialog.sheet {
    position: fixed; inset: auto 0 0 0; margin: 0 auto; padding: 0;
    width: min(100%, 560px); max-width: none; max-height: 78dvh; overflow: hidden;
    border: 1px solid var(--m-line-strong); border-bottom: 0;
    border-radius: var(--m-radius-card) var(--m-radius-card) 0 0;
    background: var(--m-surface-2); color: var(--m-text); box-shadow: var(--m-shadow-pop);
  }
  dialog.sheet::backdrop { background: var(--m-scrim, rgba(3, 5, 8, 0.72)); }
  .sheet-body { display: flex; flex-direction: column; max-height: 78dvh; }
  .sheet-grab { flex: none; display: flex; justify-content: center; padding: 6px 0 2px; touch-action: none; cursor: grab; }
  .sheet-handle { width: 72px; height: 24px; display: grid; place-items: center; border: 0; border-radius: 12px; background: none; cursor: pointer; }
  .sheet-handle span { width: 40px; height: 4px; border-radius: 2px; background: var(--m-muted-2); }
  .sheet-content {
    overflow-y: auto; overscroll-behavior: contain;
    padding: var(--s2) var(--s4) calc(var(--s4) + env(safe-area-inset-bottom));
  }
</style>
