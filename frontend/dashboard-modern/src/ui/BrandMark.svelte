<script>
  import { MARK_ARC, MARK_BOLT, MARK_RING, MARK_VIEWBOX } from "../brand/mark.js";
</script>

<svg class="brand-mark" viewBox={MARK_VIEWBOX} aria-hidden="true">
  <g fill="none" stroke-width={MARK_ARC.width} stroke-linecap="round">
    {#each MARK_RING as arc (arc.token)}
      <path d={arc.d} stroke={`var(${arc.token})`} pathLength={MARK_ARC.length} stroke-dasharray={MARK_ARC.dash} stroke-dashoffset={MARK_ARC.offset}></path>
    {/each}
  </g>
  <path class="bolt" d={MARK_BOLT}></path>
</svg>

<style>
  .bolt { fill: var(--m-text); }
</style>
