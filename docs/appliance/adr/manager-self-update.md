# ADR: the Appliance Manager updates itself as a signed package

- Status: accepted
- Date: 2026-08-26
- Scope: how the `ems-appliance-manager` package reaches an appliance and what
  happens when the one that arrives does not work. Not the operating system,
  not the EMS containers, not the A/B image.

## Context

The appliance had two update paths and neither covered the manager itself.

**A/B OS updates** replace a whole slot and commit only after a trial boot
proves itself. Under that design, an appliance that said nothing after an update
rebooted into the slot it came from: the boot selector enforced the safe answer
and no code had to run for it to happen. That property — *inaction is safe* — is
the reason the A/B design is worth its complexity.

It is also the reason it cannot ship here. Building an A/B image needs KVM, and
the free GitHub runners do not have it; on the paid runners that do, the
four-image matrix is 8.33 GiB of real data per build. A release path that cannot
run on the infrastructure this project has is not a release path.

**`apt`** patches the OS in place on a single-slot appliance. It has never
touched the Appliance Manager, because the manager is not in any Debian archive
this appliance trusts.

So the manager could be built, signed and published, and there was no way to
install it on a running appliance except SSH and `dpkg` by hand.

## Decision

**Ship the manager as a versioned, signed `.deb`, fetched over HTTPS and
installed on an operator's button.**

- **One trust anchor.** The package manifest is verified with the same
  `SignatureVerifier` and the same shipped keyring as an OS release. Two
  artefact classes, one keyring.
- **Never automatic.** The manager updates when an operator asks. An automatic
  update distributes an untested package to every appliance at once, and the
  revert this path provides has to be a decision somebody made.
- **Going back is a first-class outcome.** The same control installs an older
  package as readily as a newer one, and `previous.deb` is retained before
  anything is unpacked. A single-slot appliance has no other recovery.
- **The refusals happen before dpkg runs.** Signature, digest, architecture and
  state-schema compatibility are all checked while this project's Python is
  still the code that started the process. Afterwards the module files are the
  new ones.

## No inaction-safe fallback

This is the part that is worse than what it replaces, and it is written here in
those words so nobody has to rediscover it.

**Under A/B, doing nothing reverted. Here, doing nothing commits.** dpkg
replaces the manager, systemd restarts it, and if nothing comes back then
nothing goes back either. There is no boot selector, no second slot and no
firmware-level authority that acts when the software does not.

What replaces it is a deadline, and a deadline is not equivalent:

- It is armed by the outgoing package before the install starts, and the
  reverter it runs is a copy taken out of that package — not one the incoming
  install brought with it.
- A repeating timer, not a one-shot: a reboot inside the window would cancel a
  single `OnActiveSec=` firing, and rebooting is exactly what an operator does
  when the console stops answering.
- Its health gate is narrow and the script says so: the package dpkg reports is
  the one the install promised, and the two units that make the appliance
  reachable are running. It is not a functional test of the manager.
- **It is software.** A kernel that will not boot, a filesystem that will not
  mount or an init that never reaches the timer all defeat it, and A/B's boot
  selector would not have been defeated by any of them.

The remaining backstop is a person at a keyboard:
[../console-recovery.md](../console-recovery.md).

## What was given up

- **The trial boot.** No update is tested before it becomes the running one.
- **The inactive slot.** There is nothing to fall back into and nothing to stage
  into; a bad package is undone by installing the previous package, which is a
  strictly weaker guarantee than switching to a root that was already working.
- **OS coverage.** `previous.deb` covers the manager. It does not cover the
  kernel, the firmware or the operating system, and `apt` on this appliance is
  deliberately unrestricted. A kernel that does not boot is a re-flash.

## Consequences

- A published package is **re-derivable**: `SOURCE_DATE_EPOCH` from the tagged
  commit and a pinned compressor make two builds byte-identical, which is what
  replaces the builder attestation an image carries.
- The version string is tilde-form. Verified on a real host:
  `dpkg --compare-versions 0.1.0-rc1 gt 0.1.0` is **true**, because `-rc1` is a
  Debian revision and sorts *above* the release, while this project's
  comparator ranks it below. `0.1.0~rc1` makes them agree, and a manifest
  spelling a pre-release with a hyphen is refused.
- A rescue account ships with a documented default password, and changing it is
  optional. The trade is stated once in
  [../console-recovery.md](../console-recovery.md) and not argued again.
- **A/B is not removed by this decision.** Adding the new path and deleting the
  old one are two changes, and doing them in that order is what keeps a working
  update path at every point. The deletion is its own branch and its own review.
- **None of this has run on a device.** The suite is green and the artefacts are
  built and inspected; no appliance has installed a manager package over HTTPS.
  What is proven is recorded in
  [../ab-hardware-validation.md](../ab-hardware-validation.md).
