# Updates

Installing a newer operating-system image, and what protects you if it goes
wrong.

## How it works

The card carries **two complete system slots**. One runs; the other is spare.
An update writes the new system into the spare slot and never touches the one
you are running.

The new slot then gets **one trial boot**. If it comes up and proves itself
healthy, it becomes the permanent choice. If it does not, the next ordinary boot
returns to the slot you were on, unchanged.

Your configuration, data and backups live on a third area shared by both slots,
so they survive either outcome.

> This is the part that has never run on real hardware. The mechanism is tested
> in full offline, but the firmware's one-shot trial boot is a Raspberry Pi
> behaviour that only a Pi can confirm. See
> [what "not confirmed" means](index.md#what-not-confirmed-means).

![The system updates page showing the current and inactive slot, trial status and update readiness](../../assets/screenshots/appliance/appliance-ab-slots.png)

## Before you can update

The **Updates** page lists prerequisites and marks each one. All of them have to
hold before it will plan anything — there is no override.

Two are worth explaining:

- **Release keyring** — an OS image is only installed if it carries a signature
  this appliance trusts. No key ships with the appliance, on purpose: a trust
  anchor the device brings itself is one whoever shipped the device controls.
  You install the public key of whoever signs your releases, once.
- **EMS deployment** — a trial slot has to be able to rebuild your application.
  If the appliance cannot prove what you are running, there is nothing to
  rebuild, and it says so rather than guessing.

## Getting an image onto the box

Two ways:

- **Download it.** If a release index is configured, **Download an OS release**
  lists what it offers. The index only names candidates; what may actually be
  installed is decided by the signature on each release.
- **Copy it in.** Place the release files in the release directory yourself.
  Nothing about the installation differs afterwards.

If no index is configured, the page says so rather than implying an update could
arrive on its own.

### After a power cut, wait before downloading

The Raspberry Pi has no battery-backed clock. After a cold start it believes it
is somewhere in the past until it reaches a time server. Certificate checks and
signature validity both depend on the time, so a download started too early
fails with errors that mention everything except the clock. The appliance
refuses to start one until the time is confirmed.

![A plan dialog naming the target version and digest, waiting for confirmation](../../assets/screenshots/appliance/appliance-update-plan.png)

## Doing the update

1. Open **Updates**.
2. Pick a release and press **Plan**.
3. Read the plan. It names the target version, the slot it will write, and what
   it preserves.
4. Confirm. Writing takes several minutes; the page follows along — a banner
   names the stage it is in, and it survives a reload or a closed browser.

   ![An operation in flight, with a banner naming the stage it has reached](../../assets/screenshots/appliance/appliance-update-running.png)

5. The appliance reboots into the trial slot on its own.
6. Wait. The health check runs after the system has come up and settled — up to
   half an hour on a slow card, because everything it judges has to have had its
   chance to start first.

## How you know it worked

The Updates page shows the current slot and its version. After a successful
commit, the version is the new one and no trial is pending.

If the trial failed, you are back on the old slot with the old version, and the
page reports the fallback. Nothing is retried on its own — you acknowledge it
and decide.

## Rolling back on purpose

**Roll back** returns to the previous known-good slot. It is not an arbitrary
version list: with two slots, the only thing to go back to is the other one, and
only if its exact build was recorded when it was good.

## Related

- [When it stops working](recovery.md)
- [A/B updates, in technical detail](../../appliance/ab-os-updates.md)
