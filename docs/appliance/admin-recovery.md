# EMS Admin recovery from the Appliance Manager

The Appliance Manager runs outside Docker, so it can install, restart, repair
and roll back the EMS Admin container while that container is broken.

Open **Admin** in the navigation.

## What you see

| Card | Basic mode | Expert mode adds |
|---|---|---|
| Installed version | version, health, container state, health-check result | — |
| Image | — | repository, digest, revision, architecture, container ID |
| Known-good versions | current and previous version | previous digest |

## Restart, start, stop

Each action shows a preview, asks for confirmation and then reports progress and
a result. The Overview page has the same **Restart Admin** action.

## First installation on a freshly flashed appliance

A flashed appliance ships `/opt/ems-solarflow` as an empty directory: there is
no Admin container, no compose file, and nothing that would ever have created
one. The Admin page recognises that state and shows a single action instead of
the usual lifecycle, repair and rollback stages.

1. Open **Admin**. The page reads *No EMS Admin installation was found on this
   appliance*.
2. Choose the version. Under **Channels** only *Latest stable* is offered —
   there is no current version and no known-good history to fall back on — but
   the **Stable** and **Unstable** groups below it list every version the
   registry publishes, so a first installation can pin an older release just as
   readily as the newest one. A candidate this host will not accept is listed
   greyed out with the reason. Expert mode adds *Exact release tag* for a
   version the list does not carry.
3. Press **Install Admin**. The plan names the files it will create and the
   image digest it validated. Nothing is written yet.
4. Confirm. Only now does the appliance run the packaged installer
   (`/usr/lib/ems-appliance-manager/install-admin-console.sh`), pin the
   validated digest into the compose file it wrote, start Admin and verify it.

EMS itself is not deployed here. Once Admin is running, its own guided setup
installs EMS — the appliance deliberately has no second way to do that.

### What the appliance will not do

- **Overwrite a deployment.** The plan is bound to there being nothing. If a
  deployment appears between planning and confirming, execution stops with
  `deployment_appeared_since_plan` and nothing is written.
- **Adopt somebody else's installation.** A deployment root that already holds
  files keeps the owner it has. Only a root nothing was installed in is handed
  to the `ems-deploy` account the package creates.
- **Run the containers as root.** The hosted containers run as the owner of
  `/opt/ems-solarflow`. If that resolves to root, the plan is refused rather
  than started.
- **Roll back a first installation.** There is no previous Admin to restore. A
  failure reports `failed_recoverable` and says whether the deployment files
  were created, so a retry is either another first installation or a normal
  one. When the failure came from a command, the reported message carries that
  command's own last line of output, bounded and redacted: an operator has no
  shell here, so a refusal that dropped it left nobody able to find the cause.

## Install a specific Admin version

The list comes from the registry the Admin image is pulled from, so it names
the versions that exist for this appliance rather than the versions this
project has tagged. A version the host configuration will not accept is still
listed, greyed out, with the reason — a candidate that exists and is refused is
something to see, not something to hide. An operator running a mirror can point
`release_index_url` in `/etc/ems-appliance-manager/appliance.conf` at a JSON
index instead; that replaces the registry as the source of the list.

1. Open **Admin → Install version**.
2. Choose the version:
   - **Channels**: *Latest stable*, *Current stable (reinstall)*, *Previous
     known-good*.
   - **Stable**: every release the registry publishes, newest first.
   - **Unstable**: the release candidates, newest first. The group is always
     listed. Whether a candidate can be chosen is `allow_prerelease` in
     `/etc/ems-appliance-manager/allowed-images.conf`: a host that sets it to
     false shows every candidate greyed out with the reason, and the agent
     refuses the tag with `prerelease_not_allowed` even when it is typed by
     hand in Expert mode.
   - Expert mode adds *Exact release tag* — enter for example `v0.8.0`. Use it
     for a version the list does not carry, or when the registry cannot be
     reached.
3. Tick **Reinstall the same version** when you want to reinstall what is
   already running.
4. Press **Plan installation**.

A newly imaged appliance ships with candidates enabled: before 1.0 this project
publishes more Admin candidates than releases, and refusing them would leave the
version list with almost nothing in it. An appliance installed earlier keeps
whatever its own `/etc/ems-appliance-manager/allowed-images.conf` already says —
`ems-appliance-config-seed.service` creates that file once and never rewrites
it — so an existing appliance sees the candidates listed and greyed out until an
operator changes the line and restarts `ems-appliance-agent.service`.

Planning is where all validation happens, before anything changes:

```text
01 Validate the requested tag       (a mutable name such as "latest" is refused)
02 Resolve the image digest
03 Pull the target image
04 Verify the supported architecture
05 Verify the OCI source label
06 Verify the OCI version label
07 Verify that the tag and the image version agree
08 Record the operation plan
09 Ask for explicit confirmation
```

A release candidate is refused earlier still — at request validation, before
step 01 — with `prerelease_not_allowed`, on a host that disables prereleases.
The gate is on the tag an operator names: reinstalling the running version or
rolling back to the recorded known-good one does not name a tag and is not
gated, because both restore a version this appliance already ran.

An image is refused when the repository is not allowlisted, the architecture
does not match, the version label conflicts with the requested tag, a required
OCI label is missing (unless the tag is explicitly listed as legacy), the image
cannot be inspected, or the target is already installed and no reinstall was
requested.

The repository itself is host configuration
(`/etc/ems-appliance-manager/allowed-images.conf`). The browser sends a tag,
never an image reference.

5. Review the preview and confirm.

The plan shows the resolved **digest reference** (`repository@sha256:...`).
That immutable reference — not the tag — is what gets deployed. If no
canonical digest can be resolved the installation is refused with
`digest_unresolved` before anything is touched.

A confirmed plan is executed later, possibly by an agent that restarted in
between, so the record it leaves behind is the only authority the mutation has.
Before the first Docker or filesystem call, the persisted plan must still carry
a known operation type, a known schema version, and — when an Admin exists — a
complete versioned recovery identity: repository, digest, the canonical
`repository@digest` reference, version, compose path and hash, environment path
and hash, and the captured health state. The reference has to name exactly the
recorded repository and digest, both paths have to be absolute, and the
recovery fingerprints have to be the ones the plan was made against. A record
that lost or contradicts any of these is refused with
`operation_plan_requires_replanning` and reports `admin_untouched: true`;
nothing is inferred from the running system.

Execution is transactional:

```text
01 Preflight (Docker running, compose file, Admin service defined)
02 Save the current Admin metadata
03 Save the current Compose and environment files
04 Pull and inspect the target image
05 Record the current known-good digest
06 Write repository@sha256:... into the deployment
07 Stop the current Admin container
08 Recreate Admin from the immutable reference
09 Wait for the container health check
10 Verify the Admin API and its version on the loopback address
11 Mark the target as known-good
```

Step 06 happens **before** the running Admin is stopped. If the deployment
file cannot be written, the operation ends `failed_recoverable` and the
healthy container keeps running.

Nothing is deleted: EMS configuration, EMS runtime data, backups, Admin
persistent state, unrelated containers and Docker volumes are untouched.

## What happens when an update fails

If the new Admin does not become healthy, the appliance rolls back by itself:

```text
stop the failed target
restore the previous Compose and environment files byte for byte
re-pin the previous known-good digest and recreate that Admin
verify the restored Admin — HTTP availability, version *and* stored digest
record the rollback result
```

The digest is part of the verification on purpose: an image that carries the
expected version label and different bytes is not the Admin that was recorded
as known good, and must not count as a successful recovery.

The operation ends as `rolled_back` and shows the failure that caused it. The
Appliance Manager itself stays reachable the whole time.

If the previous image is no longer available locally and cannot be pulled, the
operation ends as `failed_terminal` with `rollback_failed` — it never reports a
success it did not achieve.

## Roll back manually

**Admin → Rollback** restores the previous known-good version by deploying the
**stored** `repository@sha256:...` reference. The tag is never resolved again,
so a tag that moved in the registry cannot change what a rollback installs. The
button is disabled when no previous known-good version exists.

### The plan is bound to what it was made against

A confirmed plan carries a fingerprint of everything the operation depends on:

```text
compose file path and hash
environment file path and hash
the running Admin's image digest and version
the target digest and its canonical repository@digest reference
```

Every field is revalidated immediately before the first mutation. A compose
file or an Admin environment file edited after planning, or a container that
was replaced in the meantime, stops the operation with `admin_untouched: true`
while the current Admin is still running. Nothing is stopped, and the plan is
simply made again.

The target itself has to be internally consistent, because the reference is
what reaches Docker and the repository and digest are what were recorded beside
it. An install or rollback is refused unless

```text
reference == repository@digest
repository is in the host image allowlist
digest is sha256:<64 hex>
tag is a release tag
architecture is one this appliance supports
compose_hash and environment_hash are canonical sha256 digests
```

and the nested recovery identity satisfies the same rules plus its own types:
booleans are booleans, schema versions are numbers, hashes are digests, and its
deployment paths are the ones the plan was made against.

### The confirmation is bound to the plan that was shown

The record is durable and is confirmed later, so what it holds is the whole
authority of the mutation. When planning finishes, the appliance seals it: a
canonical SHA-256 over the operation id, its type, its schema version, the
complete requested target and the hash of the rendered plan. The value is
stored in the record and shown in the plan.

Confirmation and execution both recompute it. A record whose target changed
after the plan was rendered — a partial write, a hand-edited file — is refused
at the confirmation:

```text
operation_plan_changed: this plan is not the one that was confirmed; plan again
admin_untouched: true
```

This is an integrity check against accidental or partial corruption. It is not
a defence against a privileged process that can rewrite every field of the
record, including the hash.

### The image is inspected again, not trusted from the record

`architecture`, `source` and `revision` are strings in a file. Before the
running Admin is touched, the appliance inspects the image the canonical
reference resolves to and compares its digest, architecture and OCI labels with
what the plan recorded. An install additionally revalidates the full OCI label
set against the requested tag; a rollback deploys an image this appliance
already validated once, so its digest and architecture are what must still
hold.

### The Admin that must come back is captured before anything changes

Automatic rollback has to be able to prove *what* came back, not only that
something did. At preflight the appliance therefore captures the running
Admin's immutable identity — image digest, canonical reference and version —
independently of the known-good history, because an Admin installed before this
appliance, or one that never became healthy, has no known-good record at all.

Recovery restores that identity and verifies it: the restored digest, the
restored version and a reachable HTTP endpoint. An image carrying the same
version label but different bytes fails recovery.

If the current Admin is healthy but its digest cannot be resolved, the
operation does not start:

```text
recovery_identity_unavailable: the running Admin cannot be identified by an
image digest, so an automatic rollback could not be verified
```

The appliance does not present transactional safety it cannot provide.

### Preflight comes before any downtime

Everything that can fail is done while the current Admin keeps running:

```text
01 load the stored known-good record
02 validate its repository, digest and canonical reference
03 make sure the deployment file is still the one the plan was made against
04 make sure the immutable image is present locally, pulling it by digest if needed
05 snapshot the Compose and environment files
06 write the rollback reference — proving the deployment can be updated
07 only now: stop the running Admin
08 recreate it from the rollback deployment
09 verify HTTP availability, digest and version
```

Installing a specific version takes the same route: its target image and its
deployment file are revalidated after the confirmation and before anything is
stopped, so a plan that went stale between preview and confirmation costs no
downtime.

If any of steps 01–06 fails, **the running Admin is never stopped**. The
operation reports `admin_untouched` and the UI says so explicitly, so nobody
goes looking for an outage that did not happen:

| Preflight failure | Result |
|---|---|
| The stored record has no valid digest or its reference disagrees | `failed_terminal`, `invalid_known_good_record` — refused already at plan time |
| The stored image is gone and cannot be pulled by its digest | `failed_terminal`, `known_good_image_unavailable` |
| The deployment file changed after the plan was created | `failed_terminal`, `deployment_changed_since_plan` — plan again |
| The Compose or environment file cannot be written | `failed_recoverable`, the snapshot is restored |

A mutable tag is never a fallback: if the immutable reference cannot be
prepared, the operation ends and the current Admin keeps running.

Once step 07 has run, a failure is no longer free. The appliance then puts the
snapshot back and recreates the Admin that was running before, and the result
carries a `recovery` block saying whether that worked — it never claims a
restore it did not achieve.

The known-good history keeps at least the current and the previous verified
Admin:

```json
{
  "admin_image": "ghcr.io/basecubedev/ems-solarflow-admin:v0.8.0",
  "admin_digest": "sha256:...",
  "admin_version": "v0.8.0",
  "revision": "...",
  "compose_hash": "...",
  "verified_at": "...",
  "healthcheck": "passed"
}
```

## When Admin is replacing itself

Two layers can write the same Admin deployment. This page is one of them; the
Admin console is the other, through System Build and Guided Upgrade. Admin is
the only side that can be halfway through — with a worker running and a durable
record of where it got to — so the appliance yields to it.

While that record is live, **Install, Roll back, Repair, Start, Stop and
Restart are refused** with `admin_transition_in_flight`. The message names the
stage Admin reached and the file holding it. Reading still works: status,
version, health and logs are unaffected.

The appliance never edits or deletes that record. It belongs to Admin.

### It yields to a live transition, not to a stuck one

The appliance is what an operator reaches for when Admin is broken, so a
transition that has passed its own expiry does **not** block anything, and
neither does one the appliance cannot read. Those are the wedged states this
page exists to fix, and refusing to help then would make the recovery tool part
of the problem.

In practice:

| Admin's record | Appliance |
|---|---|
| none | works normally |
| live, within its expiry | refuses Admin-mutating operations |
| past its expiry | works normally |
| corrupt or unreadable | works normally |

If Admin is genuinely stuck inside a live transition, either wait for the
expiry or delete
`<install root>/data/admin/state/pending-transition.json` yourself.

## Repair

**Admin → Repair** inspects and shows a preview before it changes anything:

| Finding | Suggestion |
|---|---|
| Docker is stopped | Start Docker |
| Admin container is missing | Reinstall the selected Admin version |
| Container exists but is stopped | Start Admin |
| Container restarts repeatedly | Review the logs, then reinstall |
| Compose file is missing | Manual: recreate it with `install-admin-console.sh`. With no Admin container either, the Admin page offers **Install Admin** instead |
| Admin service is not defined | Manual: add the service with `install-admin-console.sh` |
| Environment file is missing | Manual: recreate it with `install-admin-console.sh` |
| Bind path is missing | Recreate the required empty directory after confirmation |
| Port is occupied | The conflicting process is shown; it is never killed automatically |

Repair also reports image availability, container state, health-check state and
file permissions.

### What the repair result means

Repair performs the action and then inspects the host again. The result is the
state the appliance is really in afterwards:

| Result | Meaning |
|---|---|
| `succeeded` | The action ran and no blocking finding remains |
| `failed_recoverable` | The action ran but at least one finding still blocks a healthy Admin |
| `manual_action_required` | Nothing could be repaired automatically; the listed steps are yours |
| `failed_terminal` | The operation could not proceed safely |
| `cancelled` | Cancelled before anything was changed |

Starting Docker is only reported as repaired when the daemon **API** answers
afterwards, not when the start command was merely accepted.

A check that could not run is shown as `not checked`, never as a pass. The
Admin port check is the case that matters: if `ss` is missing or fails, the
appliance reports that the port could not be inspected instead of reporting it
as available.

An action that ran but could not be verified makes the repair fail even when the
re-inspection finds nothing else wrong; `unverified_actions` in the result names
which action failed and why.

## What "verified" means

Start, restart, recreate and repair share one verification. A Docker exit code
is not evidence, so each of these facts is checked:

```text
the container exists
the container is running
the active image digest matches the expected known-good digest
the Admin HTTP endpoint answers on the loopback address
the reported Admin version can be read
that version matches the expected target when one is known
```

A container **without a Docker health check does not count as healthy** just
because its process is running: the HTTP endpoint has to answer. The version is
read from the health payload, and from the running image's
`org.opencontainers.image.version` label when the payload does not carry one.

For **Stop**, the appliance verifies that the container really stopped. A
container that a restart policy brings straight back up is reported as
`container_still_running`, not as a successful stop.

None of these report `succeeded`:

| Failure | Reported as |
|---|---|
| `api_unreachable` | the Docker command worked, the Admin did not answer |
| `image_mismatch` | a different image than the recorded known-good one is running |
| `version_mismatch` | the running Admin reports a different version |
| `version_unreadable` | neither the health payload nor the image label names a version |
| `container_missing` | there is nothing to start |
| `container_still_running` | the stop did not take effect |

`container_missing` and `image_mismatch` end as `manual_action_required` — a
retry cannot fix either. The others end as `failed_recoverable`.

## Logs

**Admin → Admin container log** shows bounded, redacted output. Log output has a
maximum line count and byte size, escapes all dynamic content and redacts
credential-looking values.

## From the console

```bash
sudo ems-appliance status
sudo ems-appliance repair
sudo ems-appliance repair --apply
```

## Recovering a failed Admin update — checklist

1. Open `http://ems-solarflow.local:8088` (works even when Admin is down).
2. **Overview** shows the Admin warning; **Admin** shows the failed operation.
3. Read the error, then **Acknowledge** the result.
4. If the automatic rollback already restored the previous version, you are
   done — verify the health badge.
5. Otherwise use **Repair** (preview first), or **Install version → Previous
   known-good**.
6. If Docker itself is down, start it from the repair preview and retry.
