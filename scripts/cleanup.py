#!/usr/bin/env python3
"""Remove registrations whose content has been published.

`scripts/stage.py` writes `out/registrations-to-delete.json` while it builds the bundle: every
registration it saw is either staged for this deployment or already present on Central. Once the
upload succeeds, those registrations have served their purpose and are deleted from the repository.

The file is re-read from the checkout before deletion. If it changed after the bundle was staged
(for example a project replaced it with a newer version), the registration is kept: a newer
registration must not be deleted by an older run. Only tracked files are touched.

The workflow commits and pushes the deletion; it configures the git identity first. A failed
upload never reaches this step, so the registry stays intact for the next attempt.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


class CleanupError(RuntimeError):
    pass


def git(*arguments: str) -> str:
    result = subprocess.run(["git", *arguments], text=True, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise CleanupError(f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout.strip()


def is_tracked(relative: str) -> bool:
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", relative],
        text=True,
        capture_output=True,
    )
    return result.returncode == 0


def load_entries(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CleanupError(f"{path}: missing; run scripts/stage.py first") from None
    except (OSError, json.JSONDecodeError) as error:
        raise CleanupError(f"{path}: cannot read deletion list: {error}") from error
    if not isinstance(data, dict) or data.get("schemaVersion") != 1:
        raise CleanupError(f"{path}: schemaVersion must be 1")
    entries = data.get("registrations")
    if not isinstance(entries, list):
        raise CleanupError(f"{path}: registrations must be a list")

    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise CleanupError(f"{path}: every entry must be an object")
        for name in ("file", "project", "module", "version", "sha256"):
            value = entry.get(name)
            if not isinstance(value, str) or not value:
                raise CleanupError(f"{path}: {name} must be a non-empty string")
        result.append(entry)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", type=Path, default=Path("out/registrations-to-delete.json"))
    parser.add_argument("--dry-run", action="store_true", help="print what would be deleted")
    parser.add_argument("--push", action="store_true", help="push the deletion commit to origin")
    parser.add_argument("--branch", help="branch to push to (default: the current branch)")
    parser.add_argument(
        "--message",
        default="Remove published registrations",
        help="commit subject",
    )
    args = parser.parse_args()

    try:
        entries = load_entries(args.list)
        root = Path(git("rev-parse", "--show-toplevel")).resolve()

        deletable = []
        for entry in entries:
            path = (root / entry["file"]).resolve()
            if path != root and root not in path.parents:
                raise CleanupError(f"{entry['file']}: outside the repository {root}")
            relative = path.relative_to(root).as_posix()

            if not path.is_file():
                print(f"skip {relative}: already gone")
                continue
            if not is_tracked(relative):
                raise CleanupError(f"{relative}: not tracked by git")

            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise CleanupError(f"{relative}: cannot read registration: {error}") from error
            archive = current.get("archive") if isinstance(current, dict) else None
            same = (
                isinstance(current, dict)
                and current.get("project") == entry["project"]
                and current.get("module") == entry["module"]
                and current.get("version") == entry["version"]
                and isinstance(archive, dict)
                and archive.get("sha256") == entry["sha256"]
            )
            if not same:
                print(f"skip {relative}: changed since the bundle was staged; keeping the newer registration")
                continue
            deletable.append((relative, entry))

        if not deletable:
            print("nothing to delete")
            return 0

        for relative, entry in deletable:
            print(f"delete {relative} ({entry['project']} {entry['module']}:{entry['version']})")
        if args.dry_run:
            print("dry run: no files were removed")
            return 0

        git("rm", "--quiet", "--", *[relative for relative, _ in deletable])
        body = "\n".join(
            f"- {entry['project']} {entry['module']}:{entry['version']}" for _, entry in deletable
        )
        git("commit", "--quiet", "-m", f"{args.message}\n\nThese coordinates are on Maven Central:\n\n{body}")

        if args.push:
            branch = args.branch or git("rev-parse", "--abbrev-ref", "HEAD")
            if branch == "HEAD":
                raise CleanupError("detached HEAD: pass --branch")
            git("push", "origin", f"HEAD:{branch}")
            print(f"pushed deletion commit to {branch}")

        return 0
    except CleanupError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
