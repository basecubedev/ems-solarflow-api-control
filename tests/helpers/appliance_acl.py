# SPDX-License-Identifier: AGPL-3.0-or-later
"""A minimal POSIX ACL model shared by the packaged-script harnesses.

``setfacl``/``getfacl`` are stubbed as recording tools everywhere else, which
cannot show whether a purge removed an operator's ACL entry. This stub keeps a
real entry table, so "the operator's ACL survived" is an observable fact.
"""

ACL_STUB = r'''"""A minimal POSIX ACL model, enough to prove what a purge removes."""

import json
import os
import sys

DB = os.environ["EMS_STUB_ACL_DB"]


def load():
    try:
        with open(DB, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save(state):
    with open(DB, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)


def key_for(path):
    """Real setfacl acts on the inode, so /proc/self/fd/N is the directory."""

    return os.path.realpath(path)


def targets(path, recursive):
    """The objects a tool acts on, named the way the caller named them.

    Real getfacl reports the path it was given, so a walk below an open handle
    reports /proc/self/fd/N/... and not the canonical path that handle happens
    to resolve to right now. That distinction is the whole point of acting
    through a handle, so the stub keeps it.
    """

    if not recursive:
        return [path]
    found = [path]
    for directory, subdirectories, files in os.walk(path):
        for name in sorted(subdirectories) + sorted(files):
            found.append(os.path.join(directory, name))
    return found


def setfacl(arguments):
    recursive = default = False
    modify = remove = ""
    paths = []
    expect = ""
    for argument in arguments:
        if expect == "modify":
            modify, expect = argument, ""
            continue
        if expect == "remove":
            remove, expect = argument, ""
            continue
        if argument == "-R":
            recursive = True
        elif argument == "-d":
            default = True
        elif argument == "-m":
            expect = "modify"
        elif argument == "-x":
            expect = "remove"
        elif argument.startswith("-"):
            continue
        else:
            paths.append(argument)

    state = load()
    kind = "default" if default else "access"
    for path in paths:
        if not os.path.exists(path):
            return 1
        for item in map(key_for, targets(path, recursive)):
            entries = state.setdefault(item, {"access": {}, "default": {}})
            if modify:
                fields = modify.split(":")
                if len(fields) != 3 or fields[0] not in ("u", "user"):
                    continue
                # A default ACL only exists on a directory, as with real setfacl.
                if kind == "default" and not os.path.isdir(item):
                    continue
                entries[kind][fields[1]] = fields[2]
            elif remove:
                fields = remove.split(":")
                if len(fields) < 2 or fields[0] not in ("u", "user"):
                    continue
                entries[kind].pop(fields[1], None)
    save(state)
    return 0


def getfacl(arguments):
    recursive = False
    paths = []
    for argument in arguments:
        if argument == "-R":
            recursive = True
        elif argument.startswith("-"):
            continue
        else:
            paths.append(argument)

    state = load()
    for path in paths:
        if not os.path.exists(path):
            return 1
        for item in targets(path, recursive):
            entries = state.get(key_for(item)) or {"access": {}, "default": {}}
            print(f"# file: {item}")
            print("# owner: root")
            print("# group: root")
            print("user::rwx")
            for name, perms in sorted(entries.get("access", {}).items()):
                print(f"user:{name}:{perms}")
            print("group::r-x")
            print("other::r-x")
            for name, perms in sorted(entries.get("default", {}).items()):
                print(f"default:user:{name}:{perms}")
            print()
    return 0


tool = sys.argv[1]
sys.exit(setfacl(sys.argv[2:]) if tool == "setfacl" else getfacl(sys.argv[2:]))
'''
