# Modern front ends style guide

The dashboard and the Admin console each have two front ends on one server and
one API. **Classic** is the hand-written page (`dashboard/static/`,
`admin/static/`) and follows the [Dashboard style guide](dashboard-style-guide.md)
or the Admin console's own stylesheet. **Modern** is a Svelte 5 application
built with Vite, one for each server:

| App | Source | Build output, committed | Served at |
| --- | --- | --- | --- |
| Modern dashboard | `frontend/dashboard-modern/` | `dashboard/static/modern/` | the dashboard's `/modern/` |
| Modern Admin console | `frontend/admin-modern/` | `admin/static/modern/` | the Admin server's `/modern/` |

Both are built from one design system in `frontend/shared/`, so they read as one
product -- and will keep doing so when one web container serves them both.

Modern is a visual system of its own, deliberately. The rule that dashboard work
must not introduce a new visual system binds Classic; Modern is the one
sanctioned exception, and this guide is its rule set, for both apps alike. A
change to Modern follows this page, a change to Classic follows its own guide,
and neither borrows the other's classes.

## One design system, two apps

`frontend/shared/` holds everything both apps show or do the same way, and an
app adds only what is its own:

| Shared (`frontend/shared/`) | Per app |
| --- | --- |
| `styles/tokens.css`, `styles/base.css`: every colour, distance and the base classes | its pages and widgets |
| `ui/`: Card, Segmented, Fact, Status, Bar, ConfirmDialog, Dialog, Sheet, Empty, Icon, BrandMark | its own `src/icons.json` beside `shared/icons.json` |
| `shell/`: side menu, top bar, phone bar, Appearance, the router and the page set | `app/pages.js` (the pages and the four of the phone bar) |
| `widgets/DiagnosisReport.svelte`: one EMS diagnosis, drawn once | its own `src/i18n/strings.js` |
| `i18n/`: `t`, `tn` (plural forms), `fmt` and the shell's own strings | the auth model of its server |
| `lib/`: `format.js`, `api.js`, `download.js` | |
| `vite/page.js`: the tab icon, the icon sprite, the licence notices and the build settings | |

- **The shell** gets what it needs from the app through one context
  (`shell/context.js`): the page set and router, the word under the folded mark,
  which pages need a sign-in, the session (signed-in state, its label and icon,
  sign in and out), the switch to Classic and extra links (the Admin console's
  link to the dashboard). The top bar takes the app's status pills as a snippet.
- **Strings** live in one table each: the shared parts' table and the app's.
  A key lives in one of them only; `app.name` and `app.sub` are the two every
  app defines for the shell. A text that depends on a count is a plural pair
  (`key.one`, `key.other`) asked for with `tn(key, count)`, never "device(s)".
- **Icons**: a shared part's icon is in `shared/icons.json`, an app's own in its
  `icons.json`, never in both; the sprite is their union.
- **Where something goes:** a part, a helper or a string that a second app could
  use the same way belongs in `frontend/shared/`. Copying one into an app is the
  pattern the shared token invariant forbids for colours, applied to everything
  else.

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

`frontend/shared/styles/tokens.css` is the only file that writes a colour value. A rule
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
`frontend/shared/state/prefs.svelte.js`; a scheme's name is not translated. The contract
test reads `THEMES` and requires a block that defines every role, and it holds
every scheme to WCAG AA contrast: 4.5:1 for each ink on the surfaces and fills
it is written on.

The energy colours mean the same thing everywhere and never change with the
scheme: PV is the warm one, battery the green one, output the cyan one, grid
the violet one. A series, a flow line and a tile that are about the grid wear
`--grid`; nothing else does. Text never wears an energy colour -- a small mark
beside the text, a dot or an icon, carries the identity.

The one exception is the product's mark: the inverter's bolt in a ring of all
four energy colours, each quarter where the flow diagram puts it. It is drawn
once, in `frontend/shared/brand/mark.js`, and shown in the side menu (with
"EMS" or "ADMIN" beneath it when the menu is folded), in the phone's top bar and
as the browser tab's icon, which the build makes from the same drawing with the
colours of `tokens.css`. The Admin console wears the same mark; its name under
the product name ("Admin console") is what tells the two apart.

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
  injects a sprite with only the icons listed in `shared/icons.json` and the
  app's `src/icons.json`, and each app's `tests/icons.test.js` requires every
  name a component uses to be listed once and to exist in the pinned package.

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

The energy flow draws Classic's pipes: a translucent track, a halo around it
and rounded dashes moving along it, faster and brighter in Classic's three
power buckets (150 W and 600 W). The dashes move with `element.animate()` on a
transform, one animated layer per straight run, and are painted into that
layer's own background with gradients, so each layer is rasterised once and
then only moved. The halo is three wider, fainter strokes in the static SVG,
never a blur. The flow layer carries no CSS animation, no `@keyframes` and no
`filter`: the reasons and the measurements are in
[Dashboard performance](../technical/dashboard-performance.md), and a contract
test holds the built stylesheet to it. Measured against the dots it replaced,
headless in Chromium and Firefox at two and eight devices, on a desktop and a
phone: the same frame rate and the same number of paint and raster events. The
number of animated layers is the same except in the System view on a phone,
which has seven instead of six, because the solar and battery lines now bend
twice on their way to the inverter. Motion stops when the operator turns it
off, when the server's `animation_mode` is `off`, and when the system asks for
reduced motion. The phone's sheet slides the same way, with `element.animate()`
on a transform, and stays still under the same three conditions.

## Text and numbers

English is the default; German, Dutch and French are switchable. Every string
lives in the app's `src/i18n/strings.js` or in `shared/i18n/strings.js` as one
entry with all four languages, and the `strings.test.js` beside each requires
every language to be present, the placeholders to match, every key a component
asks for to exist, every counted key to have its plural forms and every key to
be used.

Numbers, dates, ages, file sizes and units go through
`frontend/shared/lib/format.js` with the reader's language: `1.85 kW` in
English, `1,85 kW` in German, "3 hours ago" and "vor 3 Stunden". A missing value
is an em dash, never `0`.

## Security

The server's CSP is `script-src 'self'; style-src 'self'`, and Modern lives with
it rather than loosening it: no inline script, no inline style, no `style=`
attribute in the built page. A style that depends on data is set through a
Svelte `style:` directive, which goes through the CSSOM and is allowed. Dynamic
text is always text; `{@html}` is not used.

## The Admin console

The Modern Admin console follows every rule above, and a few more that come
from what the Admin server is.

- **Everything is behind the sign-in.** The Admin has no read-only mode: signed
  out, the console is one sign-in card (create the first password, sign in, or
  the recovery state of an unreadable password file) with the language switch
  on it. A session that ends anywhere brings it back; a different
  `admin_instance_id` means another Admin process answers, and the page reloads
  from it.
- **The server's words, translated by their codes.** Findings, control states,
  container states, backup steps, check results and workflow states arrive as
  codes beside English sentences. A known code is said in the reader's language
  with the facts the payload carries filled in; an unknown one keeps the
  server's sentence, marked `lang="en"`, so a newer server is never silenced.
  Each list of codes is a paired registry, walked against the server's source
  (`frontend/admin-modern/tests/`, `tests/test_admin_modern.py`) and against
  Classic's tables (`tests/classic-parity.test.js`).
- **The same writes as Classic.** Every write sends exactly the body Classic
  sends to the same endpoint, with the same `confirm` flags, plan ids and
  fingerprints, through `adminSend` with the session's CSRF token. A
  confirmation dialog says what will change before anything is sent; a job is
  followed until it ends, and a read with no answer is retried and said so, as
  the job keeps running on the server.
- **Hand-overs to Classic.** A workflow Modern does not run yet -- Guided Setup,
  the settings editor, the MQTT migration -- is named and opened in Classic at
  its page (`/#maintenance-settings-devices` and so on). An address with a
  Classic page in it opens Classic whatever the browser chose, so a hand-over
  never changes the choice. A hand-over may carry what the reader chose: the
  Updates page's way to Classic's Guided Upgrade opens
  `/#maintenance-upgrade-<tag>` for the version on screen, which Classic reads
  once (`maintenanceUpgradeTagForHash`) and selects. A guided workflow that
  holds the console is shown on every page with the way to continue it.
- **The Guided Upgrade runs here.** `state/upgrade.svelte.js` holds the one run
  this console follows, beside the pages, and sends what Classic sends:
  validate, the migration review, execute, resume, the job poll and the
  system-alignment recovery routes. Choosing a version verifies it and reads
  the migration review at once, so the owner confirms one plan with one
  button. `lib/upgrade.js` turns the job's steps and the transition's stage
  into five phases (check, back up, Admin console, install, verify) and is
  walked against the executor's steps and options. When the Admin console is
  replaced, `auth.svelte.js` reloads the page from the new one, and
  `resumeUpgrade()` at sign-in finds the run in the durable transition and
  follows it on; the version before and the start time travel in
  `sessionStorage` (`ems-admin.upgrade`), and only the server's transition
  decides what happens (`follow()`). While the Admin console is replaced, the
  page never asks to resume: the old console would record the swap as failed,
  so it waits for the new one, which moves the run on by itself
  (`resume_pending_guided_upgrade`). A start that got no answer is watched for
  in the transition, a refusal says the update did not start, and the ways out
  of a failure are the ones the transition offers (`resume_available`,
  `return_available`, `cancel_available`). Each new screen of the flow is shown
  from the top of the page and takes the focus.
- **Modern is the default**, as on the dashboard: `admin/static/admin-theme.js`
  redirects `/` to `/modern/` unless the browser chose Classic
  (`ems-admin-ui`), the address names a Classic page, or the browser lacks the
  Popover API. The guided setup and Classic's guided upgrade keep their
  `#setup` / `#maintenance-upgrade` address, so a reload or the reconnect after
  an Admin update stays in Classic; Modern's own upgrade reloads at
  `/modern/#/updates`. Classic's Playwright specs choose Classic first
  through the configs' `use.storageState` (`tests/e2e/helpers/classic-ui.ts`,
  see [e2e-playwright.md](e2e-playwright.md)); the appearance gallery photographs
  Classic at `/?ui=classic`.

## What the Modern Admin only shows or hands to Classic

The goal is a Modern console that works on its own. Until then, this list
holds everything Modern only shows or opens in Classic, as of 2026-10-21. The
Guided Upgrade was the first to move: Modern runs it itself.
Classic stays the reference for each item. Remove an entry in the change that
makes it native.

### Workflows Modern hands to Classic

| # | Workflow | Modern today | Classic endpoints a native version needs |
| --- | --- | --- | --- |
| 1 | **Settings editor**: devices, grid meter, features, safety, expert; add an inverter, MQTT or E3/DC device; **Put live values back** | Settings shows everything read-only, with the overridden badges. Each card opens the matching Classic tab (`/#maintenance-settings-<tab>`) | POST `/api/admin/maintenance/config/{preview,apply,reset-runtime}`; the discovery routes for adding devices (`/api/discovery/scan`, refreshes, `connections/mqtt-credentials`) |
| 2 | **Older MQTT setup migration**: review and apply | System names the waiting migration and opens `/#maintenance-status`. The Classic section opens collapsed there | GET `/api/admin/maintenance/zendure-mqtt/migration-review`; POST `.../migration-apply` with `{revision, confirm, backup}` |
| 3 | **Guided Setup**: system build, discovery, MQTT brokers and credentials, Zendure cloud key, device plan, config, deployment, abandon | Overview offers **Set up in the Classic view**. Classic never opens the wizard from an address, so it lands on the start page, where **Fresh install** is the first choice | `/api/setup/{workflow,releases,device-plan,config-preview,config/*,deployment/*,abandon}`, `/api/setup/system-build/*`, `/api/admin/system-alignment/validate`, `/api/discovery/*` with the `/api/setup/discovery/*` aliases |
| 4 | **Start gate**: choose the start path, replace an existing install, migrate a root `config.json` to the standard layout | Not reachable from Modern. System shows a root `config.json` (`legacy_root_config`) without a way to migrate it | POST `/api/admin/start-path`; POST `/api/admin/config/migrate-legacy` |

### Display-only details inside native pages

| Page | Shown without an action | Way out today |
| --- | --- | --- |
| Updates | The Admin/EMS pair state (not a pair, not verified); the catalogue's warnings | The Guided Upgrade aligns the pair |
| Services | Docker not reachable; there is no start or stop per service, only **Restart EMS now** (sync) and **Repair analytics** | Fix Docker on the host |
| Diagnostics | The checks' results; the report cannot be copied or exported | Classic has no export either |
| System | Missing installation paths; broker and device problems from MQTT telemetry | The settings editor (1), the Guided Setup (3) |
| Backups | Encrypted backups cannot be created (existing ones can be inspected and restored) | Classic cannot create them either; the backend accepts `encrypt`/`password` on create (`admin/backup_restore.py`) |

### Missing from both consoles

These are not Modern gaps; neither console has them:

- Changing the Admin password: the server has no endpoint for it. Only setup,
  login, logout and refresh exist.
- A backup schedule.
- Logs and a support bundle. The support archive exists only in the Appliance
  Manager (`appliance/support_archive.py`).

The server also has routes neither console calls. They are probably for the
CLI; check before building on them:

- GET `admin-update/{status,resume}` and POST `admin-update/{plan,execute}`
- `/api/setup/releases/prepare`
- `discovery/connections/local-api`
- `discovery/connections/mqtt-brokers` POST and DELETE
- `/api/admin/maintenance/backups/diff`

## Building and testing

```bash
npm run test:frontend-shared      # the design system's unit tests
npm run test:dashboard-modern     # the dashboard's (node --test), no browser
npm run test:admin-modern         # the Admin console's
npm run build:dashboard-modern    # writes dashboard/static/modern/
npm run build:admin-modern        # writes admin/static/modern/
```

A change in `frontend/shared/` changes both builds; rebuild both. The build
output is committed with stable file names, so the Python servers and the
Docker images need no Node at all. A change to the source is committed together
with its rebuilt output; CI rebuilds and refuses a difference. The browser tests
are `tests/e2e-dashboard/modern.spec.ts` in the dashboard suite and
`tests/e2e/admin-modern.spec.ts` and `tests/e2e/admin-modern-upgrade.spec.ts` in
the Admin suite. The checks every Modern
build has to pass live in `tests/modern_frontend.py`, the design system's own
(colour schemes, contrast) in `tests/test_modern_design_system.py`.

Each build's `licenses.json` lists every package its bundle carries; each must
appear in `THIRD_PARTY_LICENSES.md` with a permissive licence.
