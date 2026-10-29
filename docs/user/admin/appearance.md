# Appearance, language and phone

## Purpose

Make the Admin Console read well where you use it: in your language, in the
colour scheme and density that suit the screen, with the side menu as wide as
you want it, and on a phone.

## When to use this workflow

- You want the console in German, Dutch or French.
- You prefer the other colour scheme, or want more on the screen at once.
- You manage the installation from a phone.

## Prerequisites

- The console open. The language can be chosen before you sign in; everything
  else is in the console. These are choices of your browser, not settings of
  the installation.

## Step-by-step instructions

### 1 — Open the Appearance menu

![Overview with the Appearance menu open at the top right: colour scheme, language, density, side menu and motion, and the switch to the classic view](../../assets/screenshots/admin/admin-appearance-menu.png)

**What you select:** the settings button at the right end of the top bar.

**What you see:** **Appearance** with five choices and the way to the old
design:

| Choice | Values |
| --- | --- |
| **Colour scheme** | **Graphite** (the default, neutral near-black) or **Midnight** (deep navy) |
| **Language** | **EN**, **DE**, **NL**, **FR** — numbers and dates follow the language |
| **Density** | **Normal** or **Compact** |
| **Side menu** | **Auto** (text beside the icons on a desktop, icons only below 1024 px), **With text** or **Icons** |
| **Motion** | The console's small movements, such as the sheet sliding in on a phone, **On** or **Off** |

**Switch to classic view** opens the same page in the deprecated Classic
console. **Hide menu text** at the foot of the side menu switches the text
beside the icons as well, and is kept like the choice in the menu.

### 2 — Choose a colour scheme

![The same Overview twice, side by side: on the left in Graphite, on the right in Midnight](../../assets/screenshots/admin/admin-appearance-themes.png)

Left **Graphite**, right **Midnight**. Only the surfaces and the accent colour
change; nothing moves. The colours that carry meaning are the same in both,
and the same as on the EMS Dashboard: a healthy state stays green, a warning
amber and a fault red.

### 3 — Choose a density

![The same Backups page twice, side by side: on the left at Normal density, on the right at Compact, where the cards take less room](../../assets/screenshots/admin/admin-appearance-density.png)

Left **Normal**, right **Compact**. Compact takes less room around and between
everything, so more fits on the screen at once. Colour and text size stay the
same.

### 4 — Choose a language

Select **DE**, **NL** or **FR** — in the Appearance menu, under **More** on a
phone, or on the sign-in card. Every page, menu and message changes at once,
and so do numbers and dates. Messages that come from the server itself, such as
a check's raw output or a refusal the server words, can stay in English; they
are still accurate.

## On a phone

![The Overview on a phone: the verdict, the four tiles in two columns, and the bar at the bottom with Overview, Updates, Settings, Services and More](../../assets/screenshots/admin/admin-overview-phone.png)

On a narrow screen the side menu gives way to a bar at the bottom with four
pages — **Overview**, **Updates**, **Settings** and **Services** until you
choose others — and **More**.

![The More sheet on a phone: the pages Assistant, Backups, Diagnostics and System, the quick settings for language and colour scheme, Sign out, Open the dashboard, Choose the pages in the bar and Switch to classic view](../../assets/screenshots/admin/admin-phone-more.png)

**More** opens a sheet with the other pages (**Assistant**, **Backups**,
**Diagnostics**, **System**), **Quick settings** for the language and the
colour scheme, **Sign out**, **Open the dashboard**, **Choose the pages in the
bar** and **Switch to classic view**. To change the bar, choose **Choose the
pages in the bar**, tap two pages to swap them — one **In the bar**, one
**Under More** — and **Done**; **Default pages** puts the four back. Swipe the
sheet down or tap its handle to close it.

## What happens in the background

- Each choice is stored by the browser you made it in, under the console's
  address. A phone and a laptop can each have their own. They are not
  installation state and change nothing on the host.
- The EMS Dashboard is a separate address, so its choices are separate too,
  although it offers the same ones.
- The Classic console keeps its own appearance; changing one never changes the
  other.
- A system that asks for reduced motion gets none, whatever **Motion** says.

## Expected result

The console opens in your language, colour scheme, density and menu width
every time, in this browser.

## Warnings and common problems

| Symptom | Meaning | What to do |
| --- | --- | --- |
| Another browser shows the default again | The choices belong to the browser they were made in | Choose them there too |
| A message is in English | It comes from the server and has no translation yet | It is still accurate; report it if it matters to you |
| The address opens the Classic console | An address that names a Classic page, `?ui=classic`, or a browser too old for Modern | See [The deprecated Classic console](index.md#the-deprecated-classic-console) |

## Recovery or next steps

- The pages and the menu → [Admin Console guides](index.md#the-pages)
- The same choices on the dashboard →
  [Dashboard appearance](../dashboard/appearance.md)
