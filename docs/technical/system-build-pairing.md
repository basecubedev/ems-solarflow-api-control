# Paired Admin/EMS system builds

The Admin Console and the EMS controller ship as **two images** but are supported
as **one system build**. A managed Docker installation selects a single system
build; that selection fixes both the Admin image and the EMS image, their Git
revision, build id, release channel and the Setup resources embedded in the Admin
image. Admin must align itself to the selected build **before** it installs or
upgrades EMS.

This supersedes the previous "loose" compatibility model where any Admin image
could drive any EMS release.

## What a system build is

A user selects one build tag, e.g.:

```text
v0.8.0
v0.8.0-RC1
latest                                   (bootstrap / rolling only)
dev-feature-zendure-mqtt-device-support-c05292c331-f7265fc-123456789-1 (immutable dev build)
local                                    (source checkout; build id records clean/dirty state)
```

The browser only ever sends a **tag**. Both image repositories are fixed
server-side and can never be supplied by the browser:

```text
ghcr.io/basecubedev/ems-solarflow-admin          (Admin)
ghcr.io/basecubedev/ems-solarflow-api-control    (EMS)
```

### Pair identity

Admin and EMS are the same system build only when these OCI labels match on both
images:

```text
org.opencontainers.image.version      (canonical selected tag)
org.opencontainers.image.revision     (git revision)
de.basecubedev.ems.build_id           (paired build id)
de.basecubedev.ems.channel            (release channel)
de.basecubedev.ems.release_tag        (canonical selected tag)
```

The version and release-tag labels must equal the selected canonical tag for
stable, RC, `latest`, development and local builds. Digests are **not** required
to be equal — Admin and EMS are separate images. `build_serial` (a GitHub run
number) is **not** used as pair identity or as a global version order.

Resolution and validation live in [`admin/system_build.py`](../../admin/system_build.py)
(`SystemBuild`, `SystemBuildResolver`, `decide_alignment`). A pair that fails
validation raises `system_build_mismatch`; the resolver never downloads or
installs any Setup resource before resolution succeeds.

### Tag policy

| Channel | Example | Installable? |
| --- | --- | --- |
| stable | `v0.8.0` | yes, must match `release_tag` |
| rc | `v0.8.0-RC1` | yes, must match `release_tag` |
| latest | `latest` | bootstrap / rolling only |
| dev (immutable) | `dev-<branch-slug>-<ref-hash>-<short-sha>-<run-id>-<attempt>` | yes |
| dev (floating alias) | `dev-<branch-slug>-<ref-hash>` | **rejected** (`system_build_dev_floating`) |
| local | `local` | local builds only |

Canonical development tags include the workflow attempt. A workflow retry for
the same commit therefore produces a new
`dev-<branch-slug>-<ref-hash>-<short-sha>-<run-id>-<attempt>` tag and cannot
overwrite the meaning of an earlier canonical install target. The stable hash
of the full, unsanitized branch ref also prevents cleanup for `feature/foo`
from matching `feature/foo-bar` or a different ref with the same truncated
slug. The floating development alias may stay published for convenience but is
not an install target.
The shorter conceptual form
`dev-<branch>-<short-sha>-<run-id>-<attempt>` refers to the same contract; the
publisher expands `<branch>` into the sanitized slug plus its collision-safe
ref hash.

### Build ID formats

Every consumer uses the same validated build-ID contract. Published stable and
release-candidate builds include their tag, revision and CI run identity (for
example, a build beginning `v0.8.0-…` or `v0.8.0-RC1-…`). Development builds
use their full canonical immutable tag as the build ID. A local clean checkout
uses `local-<short-sha>`; local changes use the explicit
`local-<short-sha>-dirty` suffix. Local metadata is derived from Git and does
not require GitHub Actions variables.

`deploy/admin/start-admin-setup.sh` builds the Admin and EMS images from that
same local identity. It tags them as the fixed server-side repositories with
`:local`, then starts the Admin Compose service. Consequently the embedded
descriptors name images that actually exist in the local Docker cache; resolving
`local` never pulls from a registry.

A bootstrap Admin may run from `latest`, while Setup persists the concrete
stable, RC or immutable development build selected by the user. If `latest` is
explicitly selected as a rolling build, its exact revision, build ID and image
digests are still persisted and health checked; the tag itself remains floating
and is never presented as an immutable development target.

## Two dimensions of alignment

Alignment is only complete when **both** hold:

1. **Runtime content** — the running Admin image digest matches the selected
   build's Admin digest.
2. **Persistent Compose reference** — the image ref written in
   `docker-compose.admin.yml` / `.env.admin` pins the canonical selected build.

`admin:latest` whose digest currently equals `v0.8.0` is **not** fully aligned
while the Compose file still says `latest`: the next pull/recreate could silently
move Admin to another version. `decide_alignment` returns:

```text
aligned                    running content + persistent ref both match
retag_required             running content matches, persistent ref is stale
admin_recreate_required    local target image present, container needs recreate
admin_update_required      running content differs; pull + recreate needed
system_build_mismatch      the pair itself is invalid
```

## The alignment sequence

One orchestration service, [`admin/system_alignment.py`](../../admin/system_alignment.py)
(`SystemAlignmentService`), runs for every mode — `automated_setup`,
`fresh_install`, `guided_upgrade`, `align_existing_install` — so HTTP routes never
re-implement it:

```text
01 Select system build
02 Validate image pair          SystemBuildResolver
03 Align Admin                  persist single-use transition + hardened updater
04 Reconnect                    browser reconnects to the new Admin
05 Verify resources             EmbeddedReleaseResources (embedded, hash-verified)
06 Install or upgrade EMS       only after Admin is aligned
07 Verify system                health checks -> known-good
```

No config write, Compose deployment or EMS start happens before Admin alignment
and embedded-resource verification advances the operation to
`resources_verified`. The `resources_verified` gate is reached before any config
or Compose write is permitted.

### Staged transition record

Admin realignment persists a bounded-TTL, single-use transition record
(`pending-transition.json`, schema `state_version: 2`; see
[`admin/admin_update.py`](../../admin/admin_update.py) `TransitionRecord` /
`PendingTransitionStore`). Its explicit stages are:

```text
admin_update_pending
admin_reconnect_pending
admin_aligned
resources_verified
ems_operation_pending
ems_operation_running
healthcheck_pending
completed
failed_recoverable
cancelled
```

The record:

- has a bounded TTL and rejects expired records;
- rejects unknown `state_version`, malformed records and a tampered
  `build_id`/`admin_digest`;
- rejects resuming against a **different** running Admin build;
- keeps reconnect polling and same-stage resume idempotent;
- uses a cross-process file lock and durable claims for Admin update, embedded
  resource import and EMS work so those mutations cannot execute twice;
- cannot be silently overwritten while an active transition is in progress.

Admin reconnect only advances to `admin_aligned`; resource verification,
EMS execution and health checks remain pending. The operation becomes
`completed` only after health checks pass and known-good is persisted.

### Hardened updater

The Admin self-update sidecar (see [`admin/admin_update.py`](../../admin/admin_update.py)
`AdminUpdateLauncher` and [`admin/update_apply.py`](../../admin/update_apply.py)):

- runs with the same host permissions as the normal Admin container
  (`--user PUID:PGID`, `--group-add DOCKER_GID`), failing before launch on
  missing/invalid permission metadata;
- runs from the **current** Admin build, not the target tag, so it understands
  the pending-state format it was handed and never executes a stale cached
  target;
- pulls the target image **before** touching any persistent file;
- verifies that the pulled image digest still equals the digest resolved into
  the transition before rewriting Compose (a moved tag fails recoverably);
- writes Compose/Env inside a byte-for-byte transaction — a failed
  recreate/verify restores the original bytes exactly (removing files that did
  not exist before) and reports any rollback failure with the affected paths
  (never secret env contents).

## Embedded Setup resources

The Admin image bundles `/app/release-resources/` with `config.template.json`,
`docker-compose.example.yml`, `install-docker.sh`, `install-docker.ps1`,
`deploy/docker/`, plus `system-build.json` and `resource-manifest.json` (generated
at image build time, see [`scripts/generate_release_resources.py`](../../scripts/generate_release_resources.py)).

[`admin/embedded_resources.py`](../../admin/embedded_resources.py)
(`EmbeddedReleaseResources`) verifies the bundle against the running Admin build
and every file hash, rejects path traversal / symlink escapes, and imports the
verified files into the existing `ReleaseManager` cache — so Fresh/Automated Setup
work with **no GitHub access** once Admin is aligned. A cached release is trusted
only when its manifest matches the canonical tag, revision, build id and file
hashes; a tag-named directory alone is never trusted (`system_build_resources_invalid`).

## Running, selected and known-good state

These are distinct states and a selected/downloaded build never becomes the
installed baseline automatically:

```text
selected -> resolved -> Admin aligned -> EMS deployed -> health checked -> known good
```

Known-good ([`admin/known_good.py`](../../admin/known_good.py) `KnownGoodStore`) is
written only after Admin and EMS are verified and health checks pass.

### Installed release authority

The installed EMS release is resolved by
[`admin/installed_release.py`](../../admin/installed_release.py)
(`resolve_installed_release`) in a strict source-of-truth order:

```text
running EMS container (immutable image identity)
  -> identified   : that release is the installed baseline
  -> unidentified : installed release is UNKNOWN (stop; no fallback)
no running EMS container (absent / stopped / Docker unavailable)
  -> digest-pinned Compose image OCI labels
  -> digest-matching known-good record
  -> legacy concrete Compose tag
  -> unknown
```

A **running** EMS container is authoritative. Its identity is read from the
immutable image id (`docker container inspect .Image`), so a tag that was moved
after the container started cannot change the perceived running release. When
that running identity cannot be established (a digest-pinned image whose build
labels are missing, or an image that cannot be inspected), the installed release
is **unknown** — the Compose image and the known-good record are **not** allowed
to stand in for the actual running bits. Compose and known-good act as fallbacks
only when no EMS container is running. `resolve_installed_release` records which
source proved the release (`running_container`, `running_container_unknown`,
`compose_image`, `known_good`, `legacy_compose`, `unknown`), and
[`admin/releases.py`](../../admin/releases.py) `ReleaseManager` and the
Maintenance Overview both consume this single helper (and the shared
`admin/container_names.py` EMS container-name precedence) so they cannot disagree.

## Selecting a build vs verifying it

Selecting a System Build in the selector is **side-effect free**: it shows the
local catalogue preview (tag, channel, revision, build id) and nothing else.
Browsing several builds contacts neither the container registry nor Docker, so it
never consumes GitHub Container Registry (GHCR) requests and cannot hit a pull
rate limit.

The full verification — resolving the pair, **downloading or reusing** the Admin
and EMS images, and checking their OCI identity — runs only on the explicit
**Verify System Build** action (the Step 1 primary while a build is unverified).
That single action:

- pulls each **missing** image at most once. An exact digest-pinned image already
  present locally is reused after a local inspection, without a pull. A mutable
  tag (`latest`, a moving channel alias) is resolved to a digest exactly once for
  the operation, and that resolved digest is then fixed — later actions never
  silently refresh it;
- records the verified, digest-pinned pair server-side. **Continue** and **Update
  Admin Server**, a normal re-render, and a short reconnect resume that verified
  result instead of pulling or re-verifying the same build. Concurrent
  verification requests for the same build coalesce into one resolver operation;
- is invalidated by any change to the selection — tag, expected image reference,
  digest, revision, build id or channel — so a stale verification can never
  authorize a different build. Changing the selected build requires a new Verify.

A verification is reused only while the full selection fingerprint still matches;
none of the pair-identity, revision, build-id, channel, digest or embedded
resource checks above are skipped or relaxed by reuse. This is implemented by
[`admin/system_build.py`](../../admin/system_build.py) `CachingBuildResolver`
wrapping `SystemBuildResolver`, and by the durable transition record; see
`docs/user/admin-setup.md` for the operator-facing wording and
`docs/technical/troubleshooting-reference.md` for the GHCR rate-limit guidance.

**Fresh Install and Guided Upgrade use the same model.** Selecting a Target
System Build in Maintenance is likewise a local preview only; **Verify System
Build** resolves through the same `CachingBuildResolver`, so the Guided Upgrade
execute (`_handle_maintenance_upgrade_execute` → `SystemAlignmentService.resolve`)
and a reconnect resume reuse the verified, digest-pinned pair instead of pulling
again. `validate_upgrade_target` returns the same `selection_fingerprint` so the
upgrade plan is bound to the exact resolved pair; the Guided Upgrade preflight
(current-state, Zendure MQTT migration, backup readiness) is an
*installation-specific* check that runs in addition to — never instead of —
System Build verification, and both must succeed before **Upgrade system** runs.

Execute **requires** the verified `selection_fingerprint` and enforces it before
anything else. `_handle_maintenance_upgrade_execute` re-resolves the target,
recomputes the fingerprint through the shared
`SystemAlignmentService.selection_fingerprint` helper, and rejects a missing
(`system_build_verification_required`) or changed
(`system_build_verification_stale`) fingerprint with HTTP 409 **before** any
preflight, backup, migration, Compose write, deployment, or transition. Because
the resolver can re-resolve a mutable tag to a different digest between Verify and
Upgrade, this is what guarantees the executed pair is exactly the one the operator
verified: if a re-resolve occurs and the identity (digests, revision, build id, or
channel) changed, execution is rejected and re-verification is required. A cached
("prepared") release directory is only downloaded resources; it never counts as
System Build verification and never enables **Upgrade system** on its own.

## Runtime identity is the verified digest, not the tag

The release tag (`v0.8.0`, `latest`, an `-RC` or a `dev-…` alias) identifies the
selected System Build and stays the user-facing, catalogue, upgrade-history,
Known-Good and release-resource identity. After verification it is **display
metadata only**. The EMS image that Docker pulls and that Compose persists is the
exact verified digest:

```text
Release:        v0.8.0                 (target_release — release resources, UI)
Runtime image:  ghcr.io/basecubedev/ems-solarflow-api-control@sha256:…  (deployed)
```

`GuidedUpgradeExecutor` builds the runtime reference once via the shared
[`digest_pinned_ref`](../../admin/system_build.py) helper
(`repository@sha256:<ems_digest>`, tag stripped, registry host port preserved,
official repository required) and uses it for the pull, the post-pull digest
check and the Compose `image:` line. Because the deploy is by digest:

- a registry tag moved **after** verification (while the resolver cache still
  holds the verified pair) cannot change the installed image — the cache returns
  the verified digest and the executor deploys it;
- a tag moved **after** the execute-time fingerprint comparison cannot change the
  pull — the pull is by digest, not by tag;
- a later `pull`/recreate/recovery cannot drift to a newer image behind the old
  tag, because the persisted Compose ref is the digest, not the tag.

Execute **ensures the exact verified image is locally available** rather than
always contacting the registry: verification already pulled the EMS image, so an
exact `repository@sha256:<digest>` already present locally is reused with **zero**
registry requests (the `pull_image` step is recorded as skipped, "Verified image
already available locally."). Only a **missing** digest is pulled by digest and
then inspected. A matching mutable tag is never accepted as local proof, and a
local image whose content digest differs is never reused. This keeps the mandatory
deploy semantics — Compose is still digest-pinned and EMS is still recreated —
while removing redundant GHCR pulls (and the associated rate-limit pressure) for
an already-verified image. When the digest is absent and its pull is rate-limited,
the run fails closed (no Compose write, no recreate) and the verified target stays
retryable.

After a pull the executor inspects the pulled reference and requires the actual
content digest to equal the verified digest; a mismatch fails closed with a typed
`target_digest_mismatch` result **before** Compose is updated, the EMS container
is recreated, or the build is marked Known-Good. A matching tag is never accepted
as proof. This mirrors the Admin updater's own expected-digest check after its
final pull.

### Recovering the installed release from a digest-pinned Compose

Because the persisted Compose ref carries no readable tag, the installed release
is recovered through one shared helper
([`admin/installed_release.py`](../../admin/installed_release.py)), used by both
`ReleaseManager` and the Maintenance Overview so they cannot disagree. The
source-of-truth order is:

1. the **running** EMS container's OCI labels (inspected by its immutable image
   ID, never the mutable `docker ps` string) — `release_tag`, then `version`;
2. the **digest-pinned Compose image**'s OCI labels, when that exact ref is
   locally inspectable;
3. a **known-good** record whose `ems_digest` equals the Compose digest;
4. a **legacy concrete Compose tag** (`repository:v0.7.0`);
5. otherwise **unknown**.

A release tag is never derived from the digest text, `latest` stays non-concrete,
and a **prepared** (downloaded, not installed) release never becomes the installed
baseline: the release catalogue's `active_release`, the `active` flags and the
downgrade baseline all use the installed release, so preparing a newer build can
no longer make a genuine forward upgrade look like a downgrade. Release resources
stay keyed by `target_release` (`releases/<tag>`), never by the digest.

A resume after the Admin container is replaced reconstructs the runtime reference
from the durable transition record (which pins the verified `ems_digest`), never
from a fresh resolve of the mutable tag — the replacement Admin's resolver cache
is empty, so re-resolving could otherwise pick up a moved digest.

## Partial transitions

A transition where Admin is on the target build but EMS is still the old build is
allowed only temporarily. While mismatched, the UI shows an upgrade-pending state
and only allows: resume the EMS upgrade, return Admin to the running EMS build,
read-only diagnostics and support bundle. Unrelated mutating Maintenance actions
are blocked until resolved (`SystemAlignmentService.is_transition_pending()`).

An embedded-resource, EMS deployment or health-check failure moves the operation
to `failed_recoverable`, retaining the target Admin identity, the previous
known-good/running EMS identity, the failed stage and its safe resume stage.
Resume retries from that committed stage. **Return Admin to running build** first
inspects the actual EMS container and only uses the last known-good pair when its
EMS digest, revision and build ID match that running container; it then starts an
Admin reconnect without accepting an image repository from the browser. Neither
recovery action marks the target known-good.

## CI

The stable ([`.github/workflows/docker-publish.yml`](../../.github/workflows/docker-publish.yml))
and feature ([`.github/workflows/docker-feature-publish.yml`](../../.github/workflows/docker-feature-publish.yml))
workflows pass the **same** revision/build_id/channel/release_tag build args to
both the Admin and EMS image builds, and a verification step fails the build if
the Admin OCI labels, the Admin embedded `system-build.json`/`resource-manifest.json`
and the EMS build identity disagree. There is one Admin image per paired system
build — an older Admin image is never reused for a new EMS release.
Both workflows derive the revision from `git rev-parse HEAD` after checkout, so
a manually selected feature ref or scheduled `main` checkout cannot be stamped
with the workflow trigger's different SHA.
