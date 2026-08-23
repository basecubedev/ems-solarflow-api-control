# Appliance release-candidate evidence — run `2026-08-09-rc`

Bounded evidence for one run of the A/B release-candidate gates. The artefacts
themselves — images, update archives, kits — are **not** here and are not in
Git: a single image is about 16.5 GiB. What is here identifies them logically,
by revision, build id and digest, so a reviewer can tell whether a file they
were handed is the file this run produced.

Nothing in this directory contains a key, a token, a password or a host path
that is not part of the repository.

## Files

| File | What it records |
|---|---|
| `result.json` | The run's top-level status per gate, and the revision each verdict belongs to |
| `media-sizing.json` | The measured medium requirement and the supported-minimum policy that follows from it |

Files this run did not produce, and why, are listed in `result.json` under
`not_run` with the exact prerequisite. A gate with no evidence file here did not
run; an absent file is never a pass.

## How to reproduce

```sh
# repository hygiene
python3 scripts/check_repository_hygiene.py --json

# the measured medium requirement
python3 -c "import json; from appliance import media_sizing; \
    print(json.dumps(media_sizing.requirements(), indent=2, sort_keys=True))"

# the packaged runtime gate, in a real booted guest
sh scripts/appliance-smoke-vm-amd64.sh

# builder qualification (needs the disposable builder guest)
sh scripts/appliance-builder-vm.sh --profile rpi5 --profile rpi4 --release-gate

# production finalization (needs a signing key, never in the builder)
sh scripts/appliance-finalize-rpi-release.sh --sign-key <KEYID> --keyring <FILE> \
    --trusted-fingerprint <FPR> --source-bundle <BUNDLE>
```
