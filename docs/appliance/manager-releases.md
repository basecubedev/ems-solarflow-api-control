# Releasing the Appliance Manager

How a new Appliance Manager reaches an appliance, and what has to be true before
it can. This is the package that *is* the appliance's web interface: shipping a
new UI and shipping a new Manager release are the same operation.

It is deliberately not the same operation as building an image. An image built
on a hosted runner is refused at signing time with
`builder_environment_untrusted`, because `packaging/appliance/vm/base-images.lock.json`
approves exactly one builder. The `.deb` is exempt, and that is not an oversight:
`packaging/appliance/build-deb.sh` is reproducible from `SOURCE_DATE_EPOCH` and a
pinned compressor, so two builds of one commit are the same bytes and anybody can
re-derive the artefact and compare. An unattested builder is no objection to
something you can rebuild yourself.

## Creating the identity

Once, before any of this works. The shipped
`packaging/appliance/config/release-keyring.gpg` is a public key; whoever holds
its secret half is the only one who can sign a release, and if nobody does, the
identity has to be made:

```bash
scripts/appliance-new-release-identity.sh --force \
    --uid "EMS SolarFlow Appliance Releases <you@example.org>" \
    --secret-out ~/appliance-signing-subkey.b64
```

`--force` is needed here and only here. The keyring in the tree is not empty —
it holds the hand-made placeholder this project started with, whose primary
carries `scSC` and whose secret half is on nobody's machine — and the script
refuses to replace an identity without being told to, because normally doing so
strands every appliance already flashed. Nothing has been flashed yet, which is
what makes this the one safe moment to do it.

**Before any image reaches the Releases page.** The fingerprint a card pins is
frozen when it is flashed: `appliance.conf` ships to `/usr/share`, it is not a
dpkg conffile, and config-seed leaves an existing `/etc` copy unread. A card
flashed against an identity nobody can sign with will never install a Manager
package again — not an upgrade, and not the downgrade that is its only recovery
— and the repair is a root console on every unit. `scripts/appliance-check-release-identity.py`
enforces this: the image workflow's publish job and the release workflow's first
job both refuse while the placeholder is in the tree. Building is deliberately
left alone, because hardware validation needs images and an image nobody
downloads strands nobody.

It writes the public keyring, pins the primary in the shipped configuration, and
exports the signing subkey for GitHub — the three artefacts that have to agree.
It refuses to overwrite an existing identity without `--force`, because
replacing one strands every appliance already flashed with the old one, and it
refuses to write the secret anywhere inside the repository.

The primary it makes is **certify-only**, so "the primary never signs" is true
of the key rather than of whoever is at the keyboard.

## The one thing a person has to create first

An environment named `appliance-manager-signing`, with **required reviewers**,
holding:

| Kind | Name | Value |
|---|---|---|
| Secret | `APPLIANCE_MANAGER_SIGNING_KEY` | the ASCII-armored signing **subkey**, base64 encoded |
| Variable | `APPLIANCE_MANAGER_SIGNING_FINGERPRINT` | the **subkey's** fingerprint |

**Two different keys, and it is worth being sure which is which.** The variable
above names the *subkey*, because that is the only secret the runner is given —
naming the primary there fails with "no secret key". The pin in
`packaging/appliance/config/appliance.conf` names the *primary*, because `gpg`
reports the primary for a subkey signature. Swapping them breaks signing in one
direction and every future rotation in the other.

Nothing else in this repository reads a secret, and a test asserts that. The key
must be one the shipped `packaging/appliance/config/release-keyring.gpg` already
trusts: the workflow verifies its own signature against that keyring with
`gpgv`, the same program the appliance runs, so a key the fleet would refuse
fails on a runner rather than in the field.

Export the subkey for the secret with:

```bash
gpg --armor --export-secret-subkeys <SUBKEY>! | base64 -w0
```

The `!` matters twice over. On the export it keeps the primary at home, which is
the whole point of having a subkey. And the workflow signs with
`--local-user "<fingerprint>!"` for a second reason: without it, `gpg` reads a
fingerprint as naming a *key* and then signs with whichever of that key's
signing subkeys it prefers — measurably the newest one, not the one named. With
one subkey that is invisible; with two it silently signs with the wrong one, and
a rotation is precisely what creates the second.

## Cutting one

1. **Bump the version.** `appliance/version.py`, the single source it comes
   from. Spell a pre-release with a tilde if you ever need one — but see below,
   they cannot be published.
2. **Tag it** `appliance-manager-v<version>`. The prefix keeps it out of the EMS
   `v*` namespace, which matters: `admin/releases.py` offers every non-draft
   release of this repository as an EMS system-build target and decides
   eligibility by parsing the tag.
3. **Push the tag** — from a commit that already contains the keyring. The sign
   job verifies against `packaging/appliance/config/release-keyring.gpg` *as of
   the tag*, so tagging a commit older than the identity fails after a reviewer
   has already released the signing key. The *Appliance Manager release*
   workflow starts, builds the package and the manifest, and then waits for that
   approval.
4. **Approve it.** The signature is made and verified, the release is published,
   and the index is rebuilt to name every version including this one.

The tag and `appliance/version.py` have to agree; the workflow refuses a
mismatch before it builds anything, because the package version comes from the
file and the release is named after the tag.

## What comes out

At `releases/download/appliance-manager-v<version>/`:

| File | What it is |
|---|---|
| `ems-appliance-manager_<version>_arm64.deb` | the package |
| `…_arm64.deb.sha256` | its checksum |
| `…_arm64.build.json` | what it was built from, and with |
| `ems-appliance-manager-<version>-arm64.manifest.json` | what gets signed |
| `…manifest.json.asc` | the detached signature that decides everything |

And, rewritten at a tag that never moves,
`releases/download/appliance-manager-index/manager-packages.json` — the index
`packaging/appliance/config/appliance.conf` points every appliance at.

## Why the index carries every version

Going back to an earlier package is the *whole* recovery this path provides.
There is no second slot behind it: `dpkg` runs, and what is installed is
installed. An index naming only the newest package would take that away from
every appliance that did not keep a copy locally, so `--keep` is left unset and
a test refuses to let one appear on a command line.

Asset URLs are pinned to the release that carries them rather than to
`/releases/latest`, because an index entry written today has to still resolve
years from now, when it is somebody's way back.

The floor on how far back an operator can go is
`appliance/persistent_state.py`'s `RETIRED_SCHEMAS`: retiring a state format
makes every later package uninstallable on an appliance that recorded it. That
list may only be added to by *removing* a format, never by adding one.

## Candidates cannot be published, and that is structural

A pre-release is spelled with a tilde — `0.2.0~rc1` — because that is the only
form on which `dpkg` and `appliance/version.py`'s comparator agree about order.
`appliance/artifact_trust.py`'s `RELEASE_ID` grammar admits no tilde. So a
candidate has no publishable release id, and the release workflow says so where
the version is chosen rather than failing five steps later as
`invalid_release_id`, which is a message about the wrong thing.

If publishing candidates ever becomes necessary, that grammar is the thing to
change, and the change reaches every appliance's parser.

## What the image does with all this

The weekly image build reads the same index and bakes in the newest **stable**
package it names, verified the same way — see
[`../developer/testing.md`](../developer/testing.md). Until a Manager release
exists it builds its own package from the checkout and says so; that is exit 3
from `scripts/appliance-fetch-manager-package.py`, deliberately distinct from a
verification failure, which fails the build.

So the ordering for a first release is: create the identity, cut the Manager
release, then let the next weekly image pick it up. An image *built* before
there is anything to pick up is fine — it carries a package with no published
counterpart, which is only a missing convenience. An image *published* before
the identity exists is a different thing entirely, and is what the gate above
refuses.
