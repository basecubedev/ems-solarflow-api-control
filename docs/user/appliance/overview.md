# Overview page

What the main page tells you, and what you can do from it.

![The overview page with status tiles for system, network, Docker, Admin, updates and backup](../../assets/screenshots/appliance/appliance-overview.png)

## The tiles

| Tile | Reading it |
| --- | --- |
| **System** | Board model, uptime, temperature, memory. A temperature above 80 °C throttles the board; check ventilation |
| **Network** | Address, connection, whether the `.local` name is being announced |
| **Docker** | Whether the container engine is running, and which of the expected containers are up |
| **Admin** | The EMS Admin Console: installed version, whether it is healthy. Says *not installed* until you install it |
| **Updates** | Either package updates, or — on an appliance image — the A/B slot state |
| **Backup** | Whether the read-only file export is active |

A tile is never coloured alone. Every state also carries a word, so a colour you
cannot distinguish is never the only signal.

## The operation banner

Anything that changes the box runs as an *operation*, and one appears at the top
while it runs: what it is doing, which step it reached, and what it ended as.

The important property: **nothing starts without you confirming a plan.** You
press an action, the appliance works out what it would do, shows you that, and
only acts once you agree. A plan is not a promise that it will succeed — it is a
statement of what will be attempted.

When an operation ends, its result stays on the page until you acknowledge it.
That is deliberate: a result nobody read is a result nobody acted on.

## Quick actions

| Action | What it does |
| --- | --- |
| **Restart Admin** | Restarts the Admin container. First thing to try when Admin is unreachable but the box is fine |
| **Repair Admin** | Inspects the Admin deployment and previews what it would fix |
| **Install Admin** | Appears only while no Admin is installed |
| **Reboot** / **Shut down** | The whole box. EMS control stops while it is down |

Always use **Shut down** before pulling power. A card that loses power
mid-write is the most common way an appliance breaks.

## Basic and Expert

The switch at the top right changes how much is shown. Expert adds digests,
container IDs, exact release tags and the recovery details. It does not unlock
anything — the same actions are available in both.

## Next

- [Updates](updates.md)
- [Network](network.md)
- [Backups](backup.md)
