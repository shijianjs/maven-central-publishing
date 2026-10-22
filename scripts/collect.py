#!/usr/bin/env python3
"""Collect the archives named by the registry under `registry/`.

The registry is the source of truth. It holds one JSON file per project + module at
`registry/<owner>/<repo>/<module>.json`; publishing a new version overwrites that file, so the
newest registration replaces the previous one instead of piling up. Files are meant to be edited by
hand as well, which is why the registry is plain JSON rather than a build output.

This script only downloads and verifies transport: it downloads the archive each registration
names (a GitHub Release asset or any HTTPS URL), compares its sha256, and writes
`downloaded/collected.json` for `scripts/stage.py`. It never contacts Maven Central.

Registration schema (schemaVersion 1):

    {
      "schemaVersion": 1,
      "project": "owner/repo",
      "module": "artifact-id",
      "version": "1.2.3-kt.4",
      "release": "release-7",
      "coordinate": "group:artifact-id:1.2.3-kt.4",   # optional; verified by stage.py
      "archive": {
        "url": "https://github.com/owner/repo/releases/download/release-7/artifact-id-1.2.3-kt.4.tar.xz",
        "sha256": "<64 lowercase hex characters>"
      }
    }

Exactly one archive per module and version. Only Python's standard library is used; the workflow
runs this on a stock ubuntu-latest image.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

PROJECT_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MODULE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
VERSION_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# GitHub release assets are downloaded with `gh` so private projects work and the token is never
# forwarded to the redirect host that actually serves the bytes.
GITHUB_ASSET_RE = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)/releases/download/([^/]+)/([^/?#]+)$"
)
ARCHIVE_SUFFIXES = (".zip", ".tar.xz", ".txz", ".tar.gz", ".tgz")
USER_AGENT = "maven-central-publishing-hub/1"


class CollectionError(RuntimeError):
    pass


@dataclass
class Registration:
    file: str
    project: str
    module: str
    version: str
    release: str
    url: str
    sha256: str
    coordinate: str | None

    @property
    def owner(self) -> str:
        return self.project.split("/", 1)[0]

    @property
    def repository(self) -> str:
        return self.project.split("/", 1)[1]

    @property
    def archive_name(self) -> str:
        return urllib.parse.unquote(urllib.parse.urlsplit(self.url).path.rsplit("/", 1)[-1])


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_registration(path: Path, registry: Path) -> Registration:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CollectionError(f"{path}: cannot read registration: {error}") from error
    if not isinstance(data, dict):
        raise CollectionError(f"{path}: registration must be a JSON object")
    if data.get("schemaVersion") != 1:
        raise CollectionError(f"{path}: schemaVersion must be 1")

    project = require_string(data, "project", path)
    module = require_string(data, "module", path)
    version = require_string(data, "version", path)
    release = require_string(data, "release", path)
    coordinate = data.get("coordinate")
    if coordinate is not None and (not isinstance(coordinate, str) or not coordinate):
        raise CollectionError(f"{path}: coordinate must be a non-empty string when present")

    if not PROJECT_RE.match(project) or any(part in {".", ".."} for part in project.split("/")):
        raise CollectionError(f"{path}: project must look like owner/repo, got {project!r}")
    if not MODULE_RE.match(module) or module in {".", ".."}:
        raise CollectionError(f"{path}: module must be a plain artifact id, got {module!r}")
    if not VERSION_RE.match(version):
        raise CollectionError(f"{path}: invalid version {version!r}")
    if not release or any(char.isspace() for char in release):
        raise CollectionError(f"{path}: release must be a non-empty string without whitespace")

    relative = path.relative_to(registry)
    expected = Path(*project.split("/")) / f"{module}.json"
    if relative.as_posix() != expected.as_posix():
        raise CollectionError(
            f"{path}: a registration for {project} {module} must live at "
            f"registry/{expected.as_posix()}"
        )

    archive = data.get("archive")
    if not isinstance(archive, dict):
        raise CollectionError(f"{path}: archive must be an object with url and sha256")
    url = require_string(archive, "url", path)
    expected_hash = require_string(archive, "sha256", path)
    if not SHA256_RE.match(expected_hash):
        raise CollectionError(f"{path}: archive.sha256 must be 64 lowercase hex characters")

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise CollectionError(f"{path}: archive.url must be an absolute https URL")
    name = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        raise CollectionError(f"{path}: archive.url must end in a plain file name")
    if not name.endswith(ARCHIVE_SUFFIXES):
        raise CollectionError(
            f"{path}: archive must end with one of {', '.join(ARCHIVE_SUFFIXES)}"
        )
    github = GITHUB_ASSET_RE.match(url)
    if github is not None:
        tag = urllib.parse.unquote(github.group(3))
        if tag != release:
            raise CollectionError(
                f"{path}: archive.url points at release {tag!r}, but the registration says {release!r}"
            )

    return Registration(
        file=path.as_posix(),
        project=project,
        module=module,
        version=version,
        release=release,
        url=url,
        sha256=expected_hash,
        coordinate=coordinate,
    )


def require_string(data: dict, name: str, path: Path) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value:
        raise CollectionError(f"{path}: {name} must be a non-empty string")
    return value


def load_registrations(registry: Path) -> list[Registration]:
    if not registry.is_dir():
        raise CollectionError(f"registry {registry} does not exist")
    registrations = []
    seen: dict[tuple[str, str], str] = {}
    for path in sorted(registry.rglob("*.json")):
        if path.is_symlink() or not path.is_file():
            raise CollectionError(f"{path}: registry entries must be regular files")
        registration = read_registration(path, registry)
        key = (registration.project, registration.module)
        if key in seen:
            raise CollectionError(
                f"{path}: {registration.project} {registration.module} is also registered in {seen[key]}"
            )
        seen[key] = registration.file
        registrations.append(registration)
    return registrations


def run(command: list[str]) -> None:
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise CollectionError(f"{' '.join(command)} failed: {detail}")


def download(registration: Registration, destination: Path, local: Path | None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)

    if local is not None:
        source = local / "archives" / registration.archive_name
        if not source.is_file():
            raise CollectionError(f"{registration.file}: fixture archive {source} is missing")
        shutil.copy2(source, destination)
        return

    match = GITHUB_ASSET_RE.match(registration.url)
    if match:
        owner, repo, tag, asset = (urllib.parse.unquote(part) for part in match.groups())
        run(
            [
                "gh",
                "release",
                "download",
                tag,
                "--repo",
                f"{owner}/{repo}",
                "--pattern",
                asset,
                "--dir",
                str(destination.parent),
                "--clobber",
            ]
        )
        downloaded = destination.parent / asset
        if downloaded != destination:
            downloaded.replace(destination)
        return

    request = urllib.request.Request(registration.url, headers={"User-Agent": USER_AGENT})
    temporary = destination.with_name(destination.name + ".part")
    try:
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        temporary.replace(destination)
    except Exception as error:
        temporary.unlink(missing_ok=True)
        raise CollectionError(f"{registration.file}: download failed: {error}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--registry", type=Path, help="registry root (default: registry, or DIR/registry with --local)")
    parser.add_argument("--out", type=Path, default=Path("downloaded"))
    parser.add_argument(
        "--local",
        type=Path,
        metavar="DIR",
        help="fixture mode: read DIR/registry and DIR/archives instead of downloading",
    )
    args = parser.parse_args()
    if args.registry is None:
        args.registry = args.local / "registry" if args.local else Path("registry")

    try:
        registrations = load_registrations(args.registry)
    except CollectionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.out.exists():
        shutil.rmtree(args.out)
    args.out.mkdir(parents=True)

    if not registrations:
        print("warning: the registry is empty; nothing to collect")
        write_collected(args.out, [])
        return 0

    collected = []
    failures = 0
    for registration in registrations:
        relative = Path(f"{registration.owner}__{registration.repository}") / registration.module / registration.archive_name
        destination = args.out / relative
        try:
            download(registration, destination, args.local)
            actual = sha256_file(destination)
            if actual != registration.sha256:
                destination.unlink(missing_ok=True)
                raise CollectionError(
                    f"{registration.archive_name}: sha256 mismatch "
                    f"(registration {registration.sha256}, actual {actual})"
                )
        except CollectionError as error:
            failures += 1
            print(f"error: {error}", file=sys.stderr)
            continue

        collected.append(
            {
                "file": registration.file,
                "project": registration.project,
                "module": registration.module,
                "version": registration.version,
                "release": registration.release,
                "coordinate": registration.coordinate,
                "url": registration.url,
                "sha256": registration.sha256,
                "archive": relative.as_posix(),
            }
        )
        print(
            f"collected {registration.project} {registration.module}:{registration.version} "
            f"from {registration.release} ({destination.stat().st_size} bytes, sha256 verified)"
        )

    if failures:
        return 1

    write_collected(args.out, collected)
    print(f"collected.json: {args.out / 'collected.json'}")
    return 0


def write_collected(out: Path, registrations: list[dict]) -> None:
    (out / "collected.json").write_text(
        json.dumps({"schemaVersion": 1, "registrations": registrations}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    sys.exit(main())
