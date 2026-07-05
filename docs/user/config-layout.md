# Config Layout

All supported setups converge on one standard layout in the install root:

```text
./config/config.json      your setup (local, git-ignored)
./data/                   runtime state, dashboard history, optional analytics
./docker-compose.yml      Docker/Admin deployments
```

The versioned template lives at:

```text
./config/config.template.json
```

## Standard vs legacy

New setups (Admin, Docker, and developer) create their config at
`config/config.json`. Older native checkouts may still use a root
`config.json`. That legacy location is read as a fallback so existing installs
keep working, but new setups should not create it.

Path resolution prefers the standard location:

```text
If ./config/config.json exists  -> use it
Else if ./config.json exists    -> use it (legacy fallback)
Else (new config generation)    -> create ./config/config.json
```

`./config/config.template.json` is the canonical template shipped in the
repository. Template resolution prefers it and falls back to a legacy root
`./config.template.json` (kept only for older installs) and, inside the Docker
image, to `/app/config.template.json` (copied from the canonical file at build
time).

The committed template is generated from `ems/config_catalog.py`. Change the
catalog, not the generated JSON, when adding defaults, comments, variants, or
configuration metadata. Regenerate and verify it with:

```bash
python tools/build_config_template.py
python tools/build_config_template.py --check
```

## Legacy migration states

Tools classify how the two locations coexist so they never overwrite an
existing config silently:

| State | Meaning |
| --- | --- |
| `none` | No config yet; new setups create `config/config.json`. |
| `legacy_root_only` | Only `./config.json` exists (a native checkout). It can be used as the source for the standard layout; the active Docker/Admin config should live at `config/config.json`. |
| `standard_only` | Only `config/config.json` exists (the standard layout). |
| `both_same` | Both exist with identical contents (e.g. mid-migration). |
| `both_different` | Both exist and differ; the standard `config/config.json` is the active Docker/Admin config. |

## Migrating a legacy root config

The root `./config.json` is treated as a **legacy source**. When Admin detects a
`legacy_root_config` install, its **Manage my existing system** flow offers a
first maintenance step that migrates it into the standard layout:

```text
Source: ./config.json
Target: ./config/config.json
```

The migration is copy-only and non-destructive. It validates the source is a
JSON object, backs it up (under `./data/admin/backups/config/`), writes
`config/config.json` atomically, and never deletes `./data/`, never removes the
legacy source, never creates symlinks, and never overwrites an existing
`config/config.json` without explicit confirmation. After migration,
`config/config.json` is the active Docker/Admin config.

There is no automatic symlink migration. To move an existing native config into
the standard layout by hand instead, copy it explicitly:

```bash
mkdir -p config
cp config.json config/config.json
```

`config init` does not overwrite a legacy `config.json` silently: when only the
legacy root config exists it prints a notice and keeps editing that file unless
you pass an explicit `--config` output path.
