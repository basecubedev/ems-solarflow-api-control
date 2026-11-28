# Modern front ends style guide

The dashboard, the Admin console and the Appliance Manager each have a
**Modern** front end, a Svelte 5 application built with Vite, one for each
server:

| App | Source | Build output, committed | Served at |
| --- | --- | --- | --- |
| Modern dashboard | `frontend/dashboard-modern/` | `dashboard/static/modern/` | the dashboard's `/` |
| Modern Admin console | `frontend/admin-modern/` | `admin/static/modern/` | the Admin server's `/` |
| Modern Appliance Manager | `frontend/appliance-modern/` | `appliance/static/modern/` | the Appliance Manager's `/` (Classic, deprecated, at `/classic/`) |

The Classic dashboard and Admin console, the hand-written pages that came
before, have been removed; `/classic/` on either server leads to `/`. The
Appliance Manager keeps its Classic page (`appliance/static/`), which follows
its own stylesheet.

A browser too old for the app -- one without the Popover API, which no engine
shipped before the syntax and the modal dialogs the apps also rely on -- is sent
by `fallback.js` to `unsupported.html`, which names the browser versions the
app needs in its four languages. Both files, and the page's stylesheet, come
from `frontend/shared/unsupported/` through the build recipe
(`modernPage({ unsupported: true })` in `frontend/shared/vite/page.js`), so the
dashboard and the Admin console ship the same ones.

All three are built from one design system in `frontend/shared/`, so they read
as one product -- and the dashboard and the Admin console will keep doing so
when one web container serves them both.

Modern is a visual system of its own, and this guide is its rule set, for every
app alike. It never borrows the Appliance Manager's Classic classes, nor they
Modern's.

## The console carries the dashboard's pages

The Admin console's app is the product's one address: its menu holds the
dashboard's pages (**Dashboard**) beside its own (**Admin**), with
`area: "ems"` or `"admin"` in `frontend/admin-modern/src/app/pages.js`. The
dashboard's page components are imported from `frontend/dashboard-modern/src/`
and stay the dashboard's: the EMS's own server still builds and serves the
dashboard app from the same sources, as the way in while the console is down,
and sends a reader on to the console (`src/app/MovedNotice.svelte`).

- **Data.** The pages call the EMS's API on the console's origin, and the
  console forwards it ([Admin architecture](../technical/admin-architecture.md)).
  `src/state/ems.svelte.js` asks the EMS whether it answers before the
  dashboard's live feed and session start; `src/app/EmsPage.svelte` shows a
  page while it does, and in its place why not and where to change that.
- **Sign-in.** The dashboard's pages are readable signed out; the console's
  need its session, and opening one shows the sign-in. The sign-in signs in to
  both servers; the dashboard's own sign-in dialog stays for a lapsed EMS
  session.
- **Words.** Both string tables are loaded; `useStrings(table, rank)` lets the
  console's words win where both name a key, and
  `frontend/admin-modern/tests/strings.test.js` lists every key where they
  differ. The dashboard's table ships as its own chunk,
  `dashboard-strings.js`.
- **Icons.** The console's sprite holds the dashboard's icons as well
  (`modernPage({ icons: [...] })`).
- **Folded pages.** The dashboard's **Diagnose** is part of the console's
  **Diagnostics**; its **Maintenance** (backups of the EMS) is not carried, as
  the console's **Backups** and **Updates** cover it. Their old addresses open
  the console's pages.

## One design system, two apps

`frontend/shared/` holds everything both apps show or do the same way, and an
app adds only what is its own:

| Shared (`frontend/shared/`) | Per app |
| --- | --- |
| `styles/tokens.css`, `styles/base.css`: every colour, distance and the base classes | its pages and widgets |
| `ui/`: Card, Segmented, Fact, Status, Bar, ConfirmDialog, Dialog, Sheet, Empty, SearchField, Icon, BrandMark | its own `src/icons.json` beside `shared/icons.json` |
| `shell/`: side menu, top bar, phone bar, Appearance, the router and the page set | `app/pages.js` (the pages and the four of the phone bar) |
| `widgets/DiagnosisReport.svelte`: one EMS diagnosis, drawn once; `widgets/About.svelte`: the About & licenses page every app registers as its hidden `about` page | its own `src/i18n/strings.js` |
| `i18n/`: `t`, `tn` (plural forms), `fmt` and the shell's own strings | the auth model of its server |
| `lib/`: `format.js`, `api.js`, `download.js`, `about.js` (the source links and notice list of the About page, and `versionText`, the line that names the running build where the side menu and the phone's **More** end: a release by its version, a `latest` build by its build number and short commit, a feature build as `dev` and its short commit; `format.js`'s `list` joins names as the reader's language does) | |
| `state/`: `prefs.svelte.js` (appearance and language, per browser), `build.svelte.js` (the image's `build.json`, read once and shared by that line and the About page; an app that names its version itself, the Appliance Manager, passes it to the side menu and the phone bar instead) | |
| `vite/page.js`: the tab icon, the icon sprite, the licence notices and the build settings | |

- **The shell** gets what it needs from the app through one context
  (`shell/context.js`): the page set and router, the word under the folded mark,
  which pages need a sign-in, the session (signed-in state, its label and icon,
  sign in and out), the Appliance Manager's switch to its Classic page, and
  extra links (the Admin console's link to the dashboard). The top bar takes the app's status pills as a snippet.
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
| The server's vocabulary: rule list, control codes, decision sentences, schedule presets and statuses, setup and workflow refusals | Appearance choices: Modern's keys carry the `ems-modern.` prefix |

The shared colours keep the names and values of the shared token contract
(`docs/developer/agent-rules.md`, *Shared design-token invariant*);
`tests/test_dashboard_modern.py` compares them with the Appliance Manager's
Classic `:root`. Every other Modern token is surface-specific and says so with
its `--m-` prefix.

A list of codes the server writes is paired with its source, the
paired-registry rule in `agent-rules.md`: the rule list with `rule_states` in
`dashboard/telemetry.py` (`tests/test_dashboard_modern.py`), and the control
codes, decision sentences, schedule vocabulary and the guided setup's refusals
with the Python modules that write them (`tests/server-codes.test.js` of each
app).

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
| `--m-accent`, `--m-accent-ink` | the one interactive colour: focus ring, selected navigation, primary button (a destructive one wears `--m-fault`, the start of a checked update `--m-ok`) |
| `--m-selected-bg`, `--m-selected-ink` | the chosen segment of a segmented control, and a badge's fill and ink |
| `--m-choice` | what is chosen: a switch that is on, a pressed month chip, the pressed option of a setting's `ui/Segmented` (`choice`), a stepper's bar, a ticked checkbox or radio (`.check`, `.chip`) in every app; a calm blue that marks the choice without drawing the eye |
| `--m-quiet-bg` | the fill of a badge that brings its own ink (a warning, the Admin's operating value): grey in Graphite, deep navy in Midnight |
| `--m-ok`, `--m-warn`, `--m-fault` | status, always with an icon and a word; `--m-fault` and `--m-ok` also fill the two primary buttons above |

A colour scheme is one value set for those roles, as an
`:root[data-m-theme="…"]` block. There are two: **Graphite** (the default,
neutral near-black) and **Midnight** (deep navy with a blue accent). A third is
one more block plus its id in `THEMES` and its name in `THEME_NAMES`, both in
`frontend/shared/state/prefs.svelte.js`; a scheme's name is not translated. The contract
test reads `THEMES` and requires a block that defines every role, and it holds
every scheme to WCAG AA contrast: 4.5:1 for each ink on the surfaces and fills
it is written on. A badge (`.badge`) writes `--m-selected-ink` on
`--m-selected-bg`, which is the accent blue in Midnight; a variant that changes
the ink brings its own fill or none, for example `--m-quiet-bg` (grey in
Graphite, deep navy in Midnight) as `.badge.warn` in `base.css` and the
Admin's operating value do, or a transparent one read on every surface. A
component restyles a badge only through a variant class, never a bare
`.badge` rule, which would outrank the variants in `base.css`.
The same test reads each badge variant a Svelte source names in a static
`class="badge …"` against both schemes, and fails on one whose ink or fill is
no token pair it can check. A state pill (`.status` with `c-ok`, `c-warn`,
`c-fault`, `c-acc`, `c-idle`) writes its tone on 12 % of that tone over the
card; the accent and idle tones write `--m-text` and `--m-muted` instead:
Midnight's accent blue on its own tint is not readable, and the idle grey
stayed just below 4.5:1 on a card's inner surface in both schemes.

The energy colours mean the same thing everywhere and never change with the
scheme: PV is the warm one, battery the green one, output the cyan one, grid
the violet one. A series, a flow line and a tile that are about the grid wear
`--grid`; nothing else does. Text never wears an energy colour -- a small mark
beside the text, a dot or an icon, carries the identity.

Three identity colours sit beside them, for marks that are about a thing
rather than an energy flow: `--inverter` (blue) wherever an inverter is shown
-- the flow's inverter node, a device card, a decision per device, an Admin
device; `--money` (pink) wherever savings are; `--soc` (violet) for the state
of charge, with `--soc-ink` as the lighter shade its text and settings,
limits and backups wear. They were picked against the energy, status and
accent colours for distance in normal vision and for contrast on every
surface. The system accent (`--m-accent`) is near-white in Graphite, the
colour of text, so an icon that stands for something wears one of these
instead; the accent stays for state and focus.

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
  `c-bat`, `c-out`, `c-grid` for energy, `c-inv`, `c-money`, `c-soc` for the
  identity colours, `c-ok`, `c-warn`, `c-fault` for status, `c-acc` and
  `c-idle` for neither. A rule paints with `var(--c)` instead of naming a
  colour. An option of `ui/Segmented` may carry an icon and a tone; a page's
  tabs use its `large` size.
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
| `ui/Card` | every panel; a title, an optional subtitle, an optional mark (`icon` and `tone`: an icon tile in a tone class beside the title, so no page reads black and white) and actions on the right; `fold` makes the title the button that folds a panel a page needs only now and then, with a `summary` line that says how it stands while folded |
| `ui/Segmented` | a choice between two to seven options that applies at once; an option may carry an icon or a dot, in a tone |
| `ui/Switch` | an on/off setting: a native checkbox in the role of a switch, over its track, so a label and the keyboard work as on a checkbox and a screen reader says on or off. On, the track is a gradient into `--m-choice` with a soft glow and a light thumb; off, an inset track with a grey thumb. In a field it stands right before its label; in a card's head at the right end, with the fold chevron at the left and no pill beside it that repeats its state |
| `ui/Stepper` | a compact number field, at most 20 rem wide: the number in the middle with its unit after it, **−**/**+** beside it (the arrow keys too), which move it by a step within min and max; it stays a number input to type any value into. `bar`, a [low, high] range, draws a narrow outlined track under the number, filled out of zero where the range holds it with a gradient that grows into `--m-choice` and glows at the value; a value beyond the range only fills it. An empty field shows the default as its placeholder, `note` (*(default)*) after it, and the default's fill in a paler `--m-choice`; `fill` ends that paler fill elsewhere, such as at the bar's top where an empty field means a device's maximum. `mark` ticks the default on the track and `markText` names it in a tip over the tick while the pointer rests on the stepper or the input has the focus; the input is described by it, so the Admin's settings field drops its **Default: …** badge there. No slider: a slider is imprecise on a phone and cannot reach a value off its step |
| `ui/MonthPicker` | months as twelve chips to press on and off, the chosen ones in `--m-choice`, read back as a season and a count (*Oct – Mar*, *6 months*); the last chosen month stays |
| `ui/Fact` | a label and a value in a row |
| `ui/Status` | a state pill: a tone class, a word and, where it helps, an icon; the word carries the meaning |
| `ui/Bar` | a share of a whole, e.g. SoC |
| `ui/TimeChart` | every time series; uPlot, colours resolved from the tokens; a drag zooms, and a refresh keeps the zoom |
| `ui/ConfirmDialog` | every write; a native `<dialog>` that says what will change |
| `ui/Sheet` | a panel from the bottom edge on a phone (the bar's **More**); a native modal `<dialog>` that Esc, the scrim and a tap or a swipe down on its handle close; the element marked `data-focus` takes the focus |
| `ui/OperatorGate` | a page or a panel that needs the operator to be signed in |
| `ui/Empty` | why there is nothing to show, and what would change that |
| `ui/SearchField` | a search box that narrows a list as you type: the settings, the live MQTT topics |

A page is a twelve-column grid of cards (`.content`, with `span-12`,
`span-8`, `span-6` and `span-4`). A card that only
groups things does not paint a fill of its own.

The Admin console edits every catalogue setting through one field,
`widgets/SettingField`, which picks the control from the field's type
(`controlFor` in `src/lib/settings.js`): a switch for a boolean,
`ui/Segmented` for two or three choices of a list or a number (a text field
with choices stays a list), a list beyond that, `ui/MonthPicker` for a month
list, `ui/Stepper` for a number with a unit or a bar, else a plain input, as
for a port, an address part or a factor of any sign. The catalogue names no
step, so `stepFor` takes it from the unit, the values and the bar. The bar's
range is the catalogue's `scale` (`STEPPER_BAR_RANGES` in
`ems/config_catalog.py`): generous, never a limit. Every number is decided
there, a bar or `STEPPER_WITHOUT_BAR` (a port, an address part, a version, a
factor of any sign, a power where 0 means no limit), and a test holds every
range inside the bounds the field enforces.

## Writing to the EMS

A write to the EMS sends:

- only the fields the operator changed (`src/lib/runtime.js`, `changedPayload`);
- after the confirmation the change needs, with its warnings;
- the `X-CSRF-Token` header from `/api/auth/status`, through
  `authorizedSend` in `src/state/auth.svelte.js`.

A disabled control stays disabled until the backend says otherwise. The server
is what authorizes a write; nothing in Modern decides it.

## Live data and motion

One `EventSource` on `/api/events`, falling back to polling `/api/live` every
two seconds when the stream does not answer within three, and promoted back to
the stream after thirty, in `src/lib/transport.js`. A hidden tab releases its stream after thirty seconds.

The energy flow draws pipes: a translucent track, a halo around it and rounded
dashes moving along it, faster and brighter in three power buckets (150 W and
600 W). The dashes move with `element.animate()` on a
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
  (`frontend/admin-modern/tests/`, `tests/test_admin_modern.py`).
- **The settings catalogue, translated by its English.** Labels, help texts,
  units, section and group titles and option values come from
  `ems/config_catalog.py`. `src/i18n/catalog.js` keys their German, Dutch and
  French by the English the catalogue sends, and option values by the stored
  value; `tests/test_admin_modern.py` walks both catalogue payloads, so a new or
  changed catalogue sentence fails it until it is translated, and an entry
  nothing uses any more fails it too.
- **Writes carry their proof.** Every write sends its `confirm` flags, plan ids
  and fingerprints through `adminSend` with the session's CSRF token. A
  confirmation dialog says what will change before anything is sent; a job is
  followed until it ends, and a read with no answer is retried and said so, as
  the job keeps running on the server.
- **Old addresses still arrive.** An address of the removed Classic console
  (`#maintenance-upgrade`, `#maintenance-settings-<tab>` and the like) opens
  the same page here (`LEGACY_ADDRESSES` in `src/app/pages.js`). A guided
  workflow that holds the console is shown on every page with the way to
  continue it.
- **The Guided Upgrade runs here.** `state/upgrade.svelte.js` holds the one run
  this console follows, beside the pages, and sends: validate, the migration review, execute, resume, the job poll and the
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
  `return_available`, `cancel_available`). Each new screen of the flow
  brings the update's card into view when it is above the window or, after
  its first screen, below its fold, and takes the focus. The page leads with **Versions and updates**
  (EMS and the Admin, then the bundled InfluxDB, each with its one button), the
  update's card below it, the InfluxDB card while a run of it has something to
  say, and the versions: the
  installable ones, grouped by channel, with a channel filter the browser
  keeps (`ems-admin.releaseChannel`). An InfluxDB run is also said where it
  was started, in its line or in the next step the finished update offers
  (`InfluxUpdateLine`): the step under way while it runs, then how it ended
  beside the version and its state, so a run whose card ends below the window
  is still seen. `influxRun` decides it from the plan and the run this browser
  follows; the next step follows only the run it started for its own update,
  and an outcome is said only while the plan still shows what the run did.
  While the
  device works (`WORKING_STAGES`), a banner in the stage's tone
  (`STAGE_TONES`) shows the turning loader, a clock and a moving line, or the
  share of the phases behind it once the run has started. The start of a
  checked update is green rather than `--m-accent`: it is the step that
  changes the device, offered only after the plan has been read.
- **The assistants share their parts** with the Appliance Manager's in
  `frontend/shared/assistant/`: `assistant.js` (the step states and their
  marks, `ems-modern.welcome`, `bareAddress`), `welcome.svelte.js` (the
  per-browser choice) and the components `AssistantWelcome`, `StepList`,
  `AssistantStep`, `TaskGrid`, `TaskTile` and `KeepChoice`. Each app keeps
  its own steps, tasks and words.
- **The assistant is the way in.** It is the page after the overview, which
  stays the first page and so the router's home. `lib/assistant.js` builds its
  checklist (install, devices, first backup, then the dashboard) and its tasks
  from the answers the overview already reads, and asks the server nothing of
  its own; `tests/assistant.test.js` holds each step to the fact that decides
  it. The menu marks the assistant with the shared `assistantMark`: `warning`
  while one of the app's `URGENT_STEPS` is left (EMS not installed here; EMS
  Admin or EMS not there or not running in the Appliance Manager), `info`
  while another step that is not optional is (the Manager's `markedSteps`
  leaves out a network that only lacks the internet, which an isolated LAN
  could never clear); the Admin's `App.svelte` reads
  the settings and the backups for it once an installation is there. A console
  opened at its bare address shows the assistant, once per page
  load, in `App.svelte`'s `resumeSetup()`: an unfinished guided setup goes
  first, then the assistant opens always while nothing is installed and
  otherwise while the browser's `ems-modern.welcome` is not `off`. An address
  with a page in it is what the reader asked for and stays; `data-landed` on
  `.app` says the choice is made. Its buttons open the place where the work is
  done: `#/settings/devices`, and `#/backups/create` or `#/backups/restore`,
  which scroll once to that card (the restore card at once, the create card
  when the listing has answered, as its button needs it) and focus its button
  if it can be pressed then.
- **The console answers at the root**; the Admin server maps the addresses as
  the dashboard does (`dashboard/static_files.py`) and sends an old
  `/modern/...` or `/classic/...` address to the root, never cached. The
  upgrade reloads at `/#/updates`.

## The Appliance Manager

The Modern Appliance Manager follows every rule above, and a few more that come
from what the Manager is: the recovery console of a Raspberry Pi, served by
`appliance/web.py` from its own `.deb`.

- **The assistant** comes after the overview, built from the shared parts
  (see the Admin console above). `src/lib/assistant.js` reads its five steps
  -- network, EMS Admin, the EMS, security updates and the optional backup
  access -- from the host status and the backup listing the pages read, Docker
  first, a container in the agent's `missing` state as absent;
  `tests/assistant.test.js` walks them. `App.svelte` lands a console opened at
  its bare address there once per load (always while nothing is installed,
  else unless `ems-modern.welcome` is `off`, which waits for the status), with
  `location.replace`, never after the reader left for Classic; `data-landed`
  marks the decision.

- **One bundle, no chunks.** The Manager updates itself as one package, and a
  page split across two versions of it is worse than a larger file: every page
  is in `assets/appliance.js`. `appliance/web.py` serves the build directory at
  the root by extension, under the same CSP as Classic, and
  `packaging/appliance/build-deb.sh` ships the tree and refuses to build
  without it.
- **Everything is behind the sign-in**, as in the Admin console: create the
  first password, sign in, or the recovery state of a missing password file,
  with the language switch on it. The session is the Manager's own
  (`/api/session`, the `X-Appliance-CSRF` header,
  `src/state/session.svelte.js`); a 401 anywhere brings the sign-in back and
  says why.
- **Every change is a plan.** `src/state/actions.svelte.js` is the one path:
  ask for a plan with a request id, show it in `widgets/PlanDialog.svelte`,
  confirm it with its token, follow the operation in
  `widgets/OperationBanner.svelte` until it is acknowledged. Closing the dialog
  cancels and acknowledges the plan; a plan whose answer was lost is picked up
  with `resume-plan`; a restart or shutdown of the Pi hands the page to a
  waiting screen. The bodies are Classic's, built in `src/lib/plans.js`.
- **The host state is polled, the rest is held.** Operations every two
  seconds, the status and the Manager state every fifth tick
  (`src/state/host.svelte.js`); the release lists, the Manager index, the
  backup state and the host settings are read once and dropped when an
  operation settles or the installed version moves, as in Classic.
- **The backend's words, translated by their codes.** Findings, operation types
  and states, verification failures, plan fields, Manager verdicts and
  outcomes, export states and log sources are closed sets the console has words
  for; `tests/test_appliance_modern.py` walks each against the backend. The
  executor's step names, blockers and refusals without a known code keep the
  appliance's English, marked `lang="en"`.
- **Basic and Expert** are Classic's detail levels, kept per browser
  (`ems-modern.detail`).
- **Modern answers at the root**, the deprecated Classic at `/classic/` (its
  page and three files); `appliance/web.py` sends an old `/modern/...` address
  to the root, never cached. Nothing is remembered. Modern's
  `public/fallback.js` sends `?ui=classic` and a browser without the Popover
  API to `/classic/`. A Manager version change reloads the page from the
  address, so the version that answers decides what loads. Classic's Playwright
  specs open `/classic/`; the Modern spec is
  `tests/e2e-appliance/modern.spec.ts`.

## What the Admin console only shows

Every workflow the removed Classic console had runs here;
`docs/developer/modern-parity.md` keeps the inventory taken before it was
removed. What remains are details shown without an action. Remove an entry in
the change that adds the action.

### Display-only details

| Page | Shown without an action | Way out today |
| --- | --- | --- |
| Updates | The Admin/EMS pair state (not a pair, not verified); the catalogue's warnings | The Guided Upgrade aligns the pair |
| Services | Docker not reachable; there is no start or stop per service, only **Restart EMS now** (sync) and **Repair analytics** | Fix Docker on the host |
| Diagnostics | The checks' results; the report cannot be copied or exported | — |
| System | Missing installation paths; broker and device problems from MQTT telemetry | Settings & devices, the Guided Setup |
| Backups | Encrypted backups cannot be created (existing ones can be inspected and restored) | The backend accepts `encrypt`/`password` on create (`admin/backup_restore.py`) |

### Missing from the console

- Changing the Admin password: the server has no endpoint for it. Only setup,
  login, logout and refresh exist.
- A backup schedule.
- Logs and a support bundle. The support archive exists only in the Appliance
  Manager (`appliance/support_archive.py`).

The server also has routes the console does not call. They are probably for the
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
npm run test:appliance-modern     # the Appliance Manager's
npm run build:appliance-modern    # writes appliance/static/modern/
```

A change in `frontend/shared/` changes every build; rebuild all three. The build
output is committed with stable file names, so the Python servers, the
Docker images and the Appliance Manager package need no Node at all. A change to
the source is committed together with its rebuilt output; CI rebuilds and
refuses a difference. The browser tests are `tests/e2e-dashboard/modern.spec.ts`,
`modern-operator.spec.ts` (what an operator does) and `modern-views.spec.ts`
(what the pages show) in the dashboard suite, `tests/e2e/admin-modern.spec.ts` and
`tests/e2e/admin-modern-upgrade.spec.ts` in the Admin suite and
`tests/e2e-appliance/modern.spec.ts` in the appliance suite. The checks every Modern
build has to pass live in `tests/modern_frontend.py`, the design system's own
(colour schemes, contrast) in `tests/test_modern_design_system.py`.

Each build's `licenses.json` lists every package its bundle carries; each must
appear in `THIRD_PARTY_LICENSES.md` with a permissive licence.
