# Modern dashboard style guide

The dashboard has two front ends on one server and one API. **Classic** is the
hand-written page in `dashboard/static/` and follows the
[Dashboard style guide](dashboard-style-guide.md). **Modern** is a Svelte 5
application built with Vite: its source lives in `frontend/dashboard-modern/`,
its build output is committed to `dashboard/static/modern/`, and the server
answers it at `/modern/`.

Modern is a visual system of its own, deliberately. The rule that dashboard work
must not introduce a new visual system binds Classic; Modern is the one
sanctioned exception, and this guide is its rule set. A change to Modern follows
this page, a change to Classic follows the other one, and neither borrows the
other's classes.

## What the two share, and what they do not

| Shared, one value | Separate |
| --- | --- |
| The API, the auth session and the CSRF header | Markup, components and CSS |
| The energy and status colours `--pv`, `--battery`, `--output`, `--grid`, `--danger` | Surface tokens: Modern's carry the `--m-` prefix |
| Classic's vocabulary: rule list, control codes, decision sentences, firmware enums, schedule presets, analytics series, flow hysteresis and scale ladder | Appearance choices: Modern's keys carry the `ems-modern.` prefix |

The shared colours keep the names and values of the shared token contract
(`docs/developer/agent-rules.md`, *Shared design-token invariant*);
`tests/test_dashboard_modern.py` compares them with Classic's `:root`. Every
other Modern token is surface-specific and says so with its `--m-` prefix.

The vocabulary is copied, not imported, because Classic is not a module.
`frontend/dashboard-modern/tests/classic-parity.test.js` walks Classic's own
lists out of `dashboard/static/app.js` and fails when the two drift -- the
paired-registry rule in `agent-rules.md` applied to a second front end.

## Colour

`src/styles/tokens.css` is the only file that writes a colour value. A rule
names a role; a contract test refuses a colour literal anywhere else in the
built stylesheet.

| Role | Used for |
| --- | --- |
| `--m-bg`, `--m-well` | the page, and anything cut into a surface |
| `--m-surface-1` … `--m-surface-3` | card, something on a card, something under the pointer |
| `--m-line`, `--m-line-strong` | hairlines and control borders |
| `--m-text`, `--m-muted`, `--m-muted-2` | three levels of ink |
| `--m-accent`, `--m-accent-ink` | the one interactive colour: focus ring, selected navigation, primary button |
| `--m-selected-bg`, `--m-selected-ink` | the chosen segment of a segmented control |
| `--m-ok`, `--m-warn`, `--m-fault` | status, always with an icon and a word |

A colour scheme is one value set for those roles, as an
`:root[data-m-theme="…"]` block. There are two: **Graphite** (the default,
neutral near-black) and **Midnight** (deep navy with a blue accent). A third is
one more block plus its id in `THEMES` and its name in `THEME_NAMES`, both in
`src/state/prefs.svelte.js`; a scheme's name is not translated. The contract
test reads `THEMES` and requires a block that defines every role, and it holds
every scheme to WCAG AA contrast: 4.5:1 for each ink on the surfaces and fills
it is written on.

The energy colours mean the same thing everywhere and never change with the
scheme: PV is the warm one, battery the green one, output the cyan one, grid
the violet one. A series, a flow line and a tile that are about the grid wear
`--grid`; nothing else does. Text never wears an energy colour -- a small mark
beside the text, a dot or an icon, carries the identity.

## Space and type

- The layout's distances -- a card's padding, the gaps between cards and
  between sections, the page gutter -- come from `--s1` … `--s6`, which
  multiply by `--d`. Compact density sets `--d` to `0.78`. The small distances
  inside one element (icon to text, a pill's, a control's or a row's own
  padding) stay fixed, because they are fitted to the text they hold: density
  tightens the page, not its controls, and type size does not change.
- A tone class says what a mark is about and sets `--c` for it: `c-pv`,
  `c-bat`, `c-out`, `c-grid` for energy, `c-ok`, `c-warn`, `c-fault` for
  status, `c-acc` and `c-idle` for neither. A rule paints with `var(--c)`
  instead of naming a colour.
- Corners name their role: `--m-radius-card`, `--m-radius-inner`,
  `--m-radius-control`, `--m-radius-pill`.
- One typeface, Inter (variable, Latin subset), shipped beside the bundle. Every
  number sits in an element with the `num` class: tabular figures, so a value
  that changes does not make its neighbours move.
- Icons are Lucide. A component names an icon by its Lucide name; the build
  injects a sprite with only the icons listed in `src/icons.json`, and
  `tests/icons.test.js` requires every name a component uses to be listed and
  to exist in the pinned package.

## Building blocks

| Component | For |
| --- | --- |
| `ui/Card` | every panel; a title, an optional subtitle and actions on the right |
| `ui/Segmented` | a choice between two to seven options that applies at once |
| `ui/Fact` | a label and a value in a row |
| `ui/Status` | a state pill: a tone class, a word and, where it helps, an icon; the word carries the meaning |
| `ui/Bar` | a share of a whole, e.g. SoC |
| `ui/TimeChart` | every time series; uPlot, colours resolved from the tokens; a drag zooms, and a refresh keeps the zoom |
| `ui/ConfirmDialog` | every write; a native `<dialog>` that says what will change |
| `ui/Sheet` | a panel from the bottom edge on a phone (the bar's **More**); a native modal `<dialog>` that Esc, the scrim and a tap or a swipe down on its handle close; the element marked `data-focus` takes the focus |
| `ui/OperatorGate` | a page or a panel that needs the operator to be signed in |
| `ui/Empty` | why there is nothing to show, and what would change that |

A page is a twelve-column grid of cards (`.content`, with `span-12`,
`span-8`, `span-6` and `span-4`). A card that only
groups things does not paint a fill of its own.

## Writing to the EMS

Modern sends exactly what Classic sends, to the same endpoints:

- only the fields the operator changed (`src/lib/runtime.js`, `changedPayload`);
- the confirmation Classic asks for, with the same warnings;
- the `X-CSRF-Token` header from `/api/auth/status`, through
  `authorizedSend` in `src/state/auth.svelte.js`.

A disabled control stays disabled until the backend says otherwise. The server
is what authorizes a write; nothing in Modern decides it.

## Live data and motion

One `EventSource` on `/api/events`, falling back to polling `/api/live` every
two seconds when the stream does not answer within three, and promoted back to
the stream after thirty -- the transport contract of Classic, in
`src/lib/transport.js`. A hidden tab releases its stream after thirty seconds.

The energy flow moves its dots with `element.animate()` on a transform. The
flow layer carries no CSS animation, no `@keyframes` and no `filter`: the
reasons and the measurements are in
[Dashboard performance](../technical/dashboard-performance.md), and a contract
test holds the built stylesheet to it. Motion stops when the operator turns it
off, when the server's `animation_mode` is `off`, and when the system asks for
reduced motion. The phone's sheet slides the same way, with `element.animate()`
on a transform, and stays still under the same three conditions.

## Text and numbers

English is the default; German, Dutch and French are switchable. Every string
lives in `src/i18n/strings.js` as one entry with all four languages, and
`tests/strings.test.js` requires every language to be present, the placeholders
to match, every key a component asks for to exist and every key to be used.

Numbers, dates and units go through `src/lib/format.js` with the reader's
language: `1.85 kW` in English, `1,85 kW` in German. A missing value is an em
dash, never `0`.

## Security

The server's CSP is `script-src 'self'; style-src 'self'`, and Modern lives with
it rather than loosening it: no inline script, no inline style, no `style=`
attribute in the built page. A style that depends on data is set through a
Svelte `style:` directive, which goes through the CSSOM and is allowed. Dynamic
text is always text; `{@html}` is not used.

## Building and testing

```bash
npm run test:dashboard-modern     # unit tests (node --test), no browser
npm run build:dashboard-modern    # writes dashboard/static/modern/
```

The build output is committed with stable file names, so the Python server and
the Docker image need no Node at all. A change to the source is committed
together with its rebuilt output; CI rebuilds and refuses a difference. The
browser tests are `tests/e2e-dashboard/modern.spec.ts` in the dashboard suite.

`dashboard/static/modern/licenses.json` lists every package the bundle
carries; each must appear in `THIRD_PARTY_LICENSES.md` with a permissive
licence.
