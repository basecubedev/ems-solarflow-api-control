# Overview page

## Purpose

The **Overview** says what the Raspberry Pi is doing and whether anything needs
you, and carries the actions you need most often. It is also where this guide
explains how every change on the appliance works: a plan you read and confirm,
and a banner that follows it to the end.

## When to use this workflow

- You want to know whether the appliance is healthy.
- The pill in the top bar or a dot in the menu says something needs attention.
- You want to restart or repair EMS Admin, install security updates, or restart
  or shut down the Raspberry Pi.

## Prerequisites

- You are signed in — see [First start](first-start.md). The console opens on
  the [assistant](assistant.md) until you turn that off; **Overview** is the
  first entry of the side menu.

## Step-by-step instructions

![The Overview: the verdict that the appliance needs a little attention with the security updates waiting and Open Updates, the six tiles for the Raspberry Pi, the EMS, EMS Admin, updates, Docker and the network, and the quick actions for EMS Admin, updates and power](../../assets/screenshots/appliance/appliance-overview.png)

### 1 — Read the verdict

**What you see:** one sentence at the top that is the appliance's own judgement,
the same one the pill in the top bar shortens:

| The verdict | The pill |
| --- | --- |
| *This appliance is healthy.* | **Healthy** |
| *This appliance is running and needs a little attention.* | **Attention** |
| *Something on this appliance is not working.* | **Problem** |
| *This appliance could not be read.* | **Not readable** |
| *This appliance could not be classified.* | **Unclear** |
| *Reading the appliance…* | **Reading** |

Under it, how many things need your attention — or *Nothing needs your
attention.* — and when the page last read the appliance: *read at 15:30:00*.
The console reads it again every few seconds by itself; the refresh button in
the top bar asks at once and reads the lists the pages hold, too.

**What it changes:** nothing.

### 2 — Act on what needs your attention

**What you see:** below the verdict, everything that needs you, worst first:
errors, then warnings, then notes. Each entry says what it is, what was
observed and what to do about it, and carries a button to the page that can act
on it — **Open Updates**, **Go to the EMS Admin page** (the EMS Admin page of
this console, not the EMS Admin console itself), **Open Diagnostics**, **Open
Settings**. An appliance with nothing to
report shows nothing here.

The menu carries the same information: a page with something waiting gets a
dot, coloured by the worst entry, and a screen reader hears *needs attention*
beside the page's name. On a phone the dot also sits on **More** when the page
is under it.

If the appliance could not be read at all, that is itself the only entry —
*The appliance status could not be read* — and it says that nothing on the page
came from the appliance. The tiles below then show dashes, and you are told why
before you read them.

Messages the appliance words itself and this console has no translation for
are shown as the appliance wrote them, in English.

### 3 — Read the tiles

**What you see:** six tiles, in this order. A state is never a colour alone:
every pill also carries a word.

| Tile | Reading it |
| --- | --- |
| **Raspberry Pi** | The board model; the operating system, how long it has been running, its temperature and the free storage, with a bar for the storage in use. The pill is the appliance's overall level: **Healthy**, **Needs attention** or **Not working**. Above 80 °C the board throttles; check its ventilation |
| **EMS** | *The controller that runs the battery*: its container and whether it runs. Before the EMS is set up: *No EMS container exists yet. The EMS is set up in the EMS Admin console's guided setup.* A stopped EMS says that nothing controls the battery |
| **EMS Admin** | *The console that sets up and runs EMS*: the installed version, the container state, and **Healthy**, **Not answering** or **Missing**. Expert adds the image digest |
| **Updates** | *Raspberry Pi OS packages*: how many security and other updates wait, how old the package index is, and whether a restart is required |
| **Docker** | *The container engine EMS and EMS Admin run in*: **Running**, **Stopped** or **Not installed**. Expert adds the engine version |
| **Network** | The hostname; the local name (mDNS), the active connection and the addresses |

Two things are deliberately elsewhere, because they belong to a page that can
act on them: the Appliance Manager's own version and the pending packages are
on [Updates](updates.md), and the backup export is on
[SSH & backup access](backup.md).

### 4 — Use a quick action

**What you see:** **Quick actions** — *Every action shows its plan first and
waits for your confirmation* — in three groups. They are alternatives, not
steps; use the one that applies.

| Group | Buttons | What they do |
| --- | --- | --- |
| **EMS Admin recovery** | **Restart**, **Repair**, **Go to the EMS Admin page** | Restart the EMS Admin container — the first thing to try when EMS Admin does not answer but the box is fine; inspect the deployment and preview what a repair would fix; open the EMS Admin page |
| **Install EMS Admin** | **Install EMS Admin** | Takes the place of the group above while nothing is installed (*Nothing is deployed yet, so there is nothing to restart or repair*) and opens the EMS Admin page |
| **Updates** | **Open Updates**, **Install security updates** | Open the Updates page; plan the waiting security updates. With none waiting the second button is greyed out, and the reason says so |
| **Power** | **Restart Raspberry Pi**, **Shut down** | The whole box. EMS control stops while it is down |

Always use **Shut down** before pulling power. A card that loses power
mid-write is the most common way an appliance breaks.

## Changing something

Every action that changes the appliance — here, on any other page, and in the
assistant's targets — works the same way.

1. **Press the button** for what you want, for example **Restart Raspberry Pi**.
2. **Read the plan.** The appliance works out what it would do and the page
   shows it in a dialog: *Nothing has changed yet. Read the plan, then confirm
   it to start.* — what will happen, to what, and what is kept as it is. A plan
   the appliance refuses says *This plan cannot run: the appliance refuses it
   for the reasons below. Nothing has changed.*, lists the reasons and cannot
   be confirmed.
3. **Confirm or drop it.** The confirm button names the act — **Confirm**,
   **Install**, **Apply**, **Restart**, **Shut down**, **Roll back** — and is
   red for an act worth a second thought: one that can cut the page off, go
   back to an older version or open a root login. **Cancel**, the close button
   or Esc drops the plan, and nothing is left behind.
4. **Follow it.** The operation shows at the top of every page: what it is, the
   stage it reached, its last steps with their times, and its state —
   **Running**, **Verifying**, then **Completed**, **Incomplete**, **Failed**,
   **Rolled back**, **Manual action required** or **Cancelled**. It survives a reload or a closed browser,
   because it runs on the appliance, not in the page. An operation that has
   started cannot be interrupted, and the banner says so.
5. **Acknowledge it.** Once it ended, the banner stays until you press
   **Acknowledge**, so a result is never missed. A support archive is
   downloaded from there.

A plan is not a promise that it will succeed; it is a statement of what will be
attempted. The appliance runs one operation at a time: while one runs or waits
for confirmation, another plan is refused with *Another operation is running or
waiting for confirmation*, and the banner can cancel one that waits.

A restart or shutdown of the Raspberry Pi turns the page into *Waiting for the
appliance*, which looks for the appliance by itself (**Check now** asks at
once). After a restart you sign in again. When another Appliance Manager
version starts answering — after an update, or after going back to the kept
package — the page loads itself again from that version, so it never runs
against a Manager it was not made for.

## Basic and Expert

The **Basic** / **Expert** switch in the top bar changes how much is shown; on
a phone it is on **Settings** under **Detail level**. Expert adds the image
digests, container IDs, plan fingerprints and image references, every log
source, all operating-system updates with **Plan all updates**, the exact
release tag, and the package-manager repairs, which Basic shows too as soon as
the package manager needs one. It does not unlock anything a Basic reader
needs: the same actions are there in both. The choice is kept in this browser
only.

## What happens in the background

- The page reads the appliance's status every two seconds while you are signed
  in and the appliance answers. Nothing on the page is decided in the browser:
  the verdict, the findings and every state are the appliance's.
- A plan is made by the appliance's privileged agent and sealed with a token;
  only the plan shown can be confirmed.

## Expected result

You know whether the appliance is healthy, what needs you and which page fixes
it, and every change you make goes through a plan you read first.

## Warnings and common problems

| Symptom | Meaning | What to do |
| --- | --- | --- |
| *The appliance did not answer in time* after pressing a button | The plan took longer than the browser waited | Wait: the plan opens by itself once the appliance has made it |
| *The appliance is holding a plan this page did not ask for* | A plan from another tab or browser waits for confirmation | Cancel it in the banner, then plan again |
| Every tile shows dashes | The status could not be read | Read the entry at the top; open **Diagnostics** and create a support archive |
| *Your session ended* | The session timed out or the password was changed | Sign in again |

## While the appliance is down

The EMS is what tells your battery and inverter what to do, and they keep the
last instruction until they get a new one. Whenever the appliance restarts —
a reboot, a shutdown, an operating-system update that needs one — that
instruction stays in force and nothing replaces it with a safe default.

Nothing is damaged by this; the hardware simply carries on doing what it was
last told. It is worth knowing before you start an update at a moment when the
setpoint matters.

## Recovery or next steps

- What is still to do on this appliance → [The assistant](assistant.md)
- Updates in detail → [Updates](updates.md)
- WLAN, hostname, time zone → [Network](network.md)
- When something stops working → [When it stops working](recovery.md)
