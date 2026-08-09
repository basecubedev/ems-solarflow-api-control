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

## Install a specific Admin version

1. Open **Admin → Install version**.
2. Choose the version:
   - Basic mode: *Latest stable*, *Current stable (reinstall)*, *Previous
     known-good*.
   - Expert mode adds *Exact release tag* — enter for example `v0.8.0`. An
     approved prerelease tag is only accepted when the host configuration
     enables prereleases.
3. Tick **Reinstall the same version** when you want to reinstall what is
   already running.
4. Press **Plan installation**.

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

An image is refused when the repository is not allowlisted, the architecture
does not match, the version label conflicts with the requested tag, a required
OCI label is missing (unless the tag is explicitly listed as legacy), the image
cannot be inspected, or the target is already installed and no reinstall was
requested.

The repository itself is host configuration
(`/etc/ems-appliance-manager/allowed-images.conf`). The browser sends a tag,
never an image reference.

5. Review the preview and confirm.

Execution is transactional:

```text
01 Preflight (Docker running, compose file, Admin service defined)
02 Save the current Admin metadata
03 Save the current Compose and environment files
04 Pull and inspect the target image
05 Record the current known-good digest
06 Stop the current Admin container
07 Recreate Admin with the target image
08 Wait for the container health check
09 Verify the Admin API and its version on the loopback address
10 Mark the target as known-good
```

Nothing is deleted: EMS configuration, EMS runtime data, backups, Admin
persistent state, unrelated containers and Docker volumes are untouched.

## What happens when an update fails

If the new Admin does not become healthy, the appliance rolls back by itself:

```text
stop the failed target
restore the previous Compose and environment files byte for byte
re-pin the previous known-good digest and recreate that Admin
verify the restored Admin
record the rollback result
```

The operation ends as `rolled_back` and shows the failure that caused it. The
Appliance Manager itself stays reachable the whole time.

If the previous image is no longer available locally and cannot be pulled, the
operation ends as `failed_terminal` with `rollback_failed` — it never reports a
success it did not achieve.

## Roll back manually

**Admin → Rollback** restores the previous known-good version. Rollback is
digest-pinned: the recorded `sha256:` digest is restored, not just a mutable
tag. The button is disabled when no previous known-good version exists.

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

## Repair

**Admin → Repair** inspects and shows a preview before it changes anything:

| Finding | Suggestion |
|---|---|
| Docker is stopped | Start Docker |
| Admin container is missing | Reinstall the selected Admin version |
| Container exists but is stopped | Start Admin |
| Container restarts repeatedly | Review the logs, then reinstall |
| Compose file is missing | Regenerate the Admin section from the appliance template |
| Admin service is not defined | Regenerate the Admin section |
| Environment file is missing | Recreate the Admin environment file |
| Bind path is missing | Recreate the required empty directory after confirmation |
| Port is occupied | The conflicting process is shown; it is never killed automatically |

Repair also reports image availability, container state, health-check state and
file permissions.

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

1. Open `http://ems-solarflow.local:8080` (works even when Admin is down).
2. **Overview** shows the Admin warning; **Admin** shows the failed operation.
3. Read the error, then **Acknowledge** the result.
4. If the automatic rollback already restored the previous version, you are
   done — verify the health badge.
5. Otherwise use **Repair** (preview first), or **Install version → Previous
   known-good**.
6. If Docker itself is down, start it from the repair preview and retry.
