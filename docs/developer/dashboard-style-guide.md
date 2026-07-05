# Dashboard Style Guide

This guide documents the visual primitives used by the live dashboard. New
dashboard work should extend these patterns instead of adding another card or
form language.

## Core Layout

- Dashboard panels use the existing glass panel shell with a `16px` radius.
- Dense operational views should use compact grids with `8px` gaps.
- Repeated dashboard tiles should keep stable dimensions so live values,
  validation text, hover states, and actions do not shift the layout.
- Avoid unrelated color palettes. Use the existing CSS variables in
  `dashboard/static/styles.css`, especially `--output`, `--accent2`, `--muted`,
  `--text`, `--battery`, and `--danger`.

## Control And Energy Tiles

Control pipeline and energy statistic cards are the reference style for dense
dashboard controls:

- Use `.control-pipeline-stage` or a close equivalent for compact stage cards.
- Use `.control-stage-head` and `.control-stage-header` for the tile header.
- Stage headers include a numbered `.control-stage-step`, an icon
  `.control-stage-dot`, a `.control-stage-title`, and an optional
  `.control-stage-subtitle`.
- Titles are uppercase, `11px`, and high weight.
- Small labels are uppercase, `10px`, and use `.08em` letter spacing.
- Compact values use pill/fact rows such as `.control-pipeline-fact` or
  `.control-fact`, with a minimum height of about `32px`.
- Result or command areas should reuse `.control-result` or the existing
  `.primary-button.compact` styling.

## Runtime Write Controls

Runtime write controls live in the Control tab and must look like the Control
pipeline below them.

- Keep unauthenticated and auth-not-configured states as compact read-only
  messages. Do not expose form controls unless the dashboard reports an
  authenticated session.
- Authenticated runtime forms use `.runtime-form.control-pipeline-stage` and
  `.runtime-stage-card`.
- Every runtime card has a numbered header, icon dot, uppercase title, and
  muted subtitle:
  - EMS / System: `Global runtime limits and loop control`
  - Device cards: `Device runtime write values`
  - Winter Mode: `Seasonal charging behavior`
  - Home Assistant: `External publishing and helper control`
- Runtime fields keep semantic `label`, `input`, and `select` elements for
  keyboard and screen-reader use, but are visually styled as compact
  `.control-pipeline-fact` rows.
- Apply actions use `.primary-button.compact` at the bottom of each card.
- Feedback must be written with `textContent`, never by injecting untrusted HTML.

## Diagnose And Logs Tabs

The Diagnose and Logs tabs use the **Control / Energy stage style**. Diagnose
sections reuse `.control-pipeline-stage` cards and the existing tone pills
(`tone-send` / `tone-warn` / `tone-blocked`) for status; the Logs view is a
compact monospace region using the existing color tokens for level accents. They
do not introduce a new visual system. Both are operator-only and render the
configure-password / login-required empty states the same way the runtime panel
does.

## Security And Data Handling

- Do not bypass the existing authentication, session, CSRF, or runtime write
  validation paths.
- Do not introduce `innerHTML` with unescaped device names, runtime values, or
  server-provided messages.
- Dynamic HTML generated in `dashboard/static/app.js` must pass user/device
  values through `escapeHtml()`.
- Runtime write API behavior belongs in the backend runtime write modules, not
  in visual style work.
