#!/usr/bin/env python3
"""Verify the collected archives, drop coordinates that are already published, and merge the rest
into one Maven repository layout directory for JReleaser.

Input is `downloaded/collected.json`, written by `scripts/collect.py` from the registry under
`registry/`. Each registration describes exactly one module and one version, packaged as one
archive (`.tar.xz` recommended, `.zip` accepted) in Maven repository layout.

Checks performed before anything is written to `staging/`:

* the archive on disk still matches the registered sha256 (defence in depth after collect.py);
* every archive entry lives under a coordinate directory, and the archive contains exactly the
  registered module and version;
* the POM of the coordinate exists and every artifact has a detached, non-empty `.asc` signature;
* `repo1` has no POM for the coordinate (otherwise Central rejects the whole deployment with a
  duplicate-version error).

After the merge, `staging/` is checked against JReleaser's `.jar` prerequisite as well: a packaging
outside pom/jar/aar/war/zip must ship the same-named jar or be exempted in `jreleaser.yml` under
`artifactOverrides`. Running it here names the offending coordinate instead of letting JReleaser
abort the bundle build.

Files that Central does not need are dropped instead of uploaded: `maven-metadata*.xml`,
`_remote.repositories`, hidden entries and all checksum files (`.md5`, `.sha1`, `.sha256`,
`.sha512`, including checksums of `.asc` files). They only consume File Count quota.

The reports are written outside `staging/`, because everything inside `staging/` ends up in the
uploaded bundle. `out/registrations-to-delete.json` lists every registration whose content is
published after a successful upload: registrations that were just staged and registrations that
were already on Central and got pruned. `scripts/cleanup.py` deletes them from the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import re
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COORDINATE_RE = re.compile(r"^[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+:[A-Za-z0-9_.+-]+$")
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# JReleaser's Maven Central prerequisite only skips the "same-named .jar" rule for these packagings.
JARLESS_PACKAGING = {"pom", "jar", "aar", "war", "zip"}
USER_AGENT = "maven-central-publishing-hub/1"


class StageError(RuntimeError):
    pass


@dataclass
class Registration:
    file: str
    project: str
    module: str
    version: str
    release: str
    coordinate: str | None
    url: str
    sha256: str
    archive: Path

    @property
    def key(self) -> str:
        return f"{self.project}:{self.module}"


@dataclass
class Entry:
    coordinate: str
    name: str
    size: int


@dataclass
class Inspected:
    registration: Registration
    coordinate: str
    entries: list[Entry]
    ignored_entries: int
    files: int
    bytes: int


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_ignored(entry: str) -> bool:
    parts = entry.split("/")
    if any(part.startswith(".") or part == "__MACOSX" for part in parts):
        return True
    base = parts[-1]
    if base.startswith("maven-metadata") and base.endswith(".xml"):
        return True
    if base == "_remote.repositories":
        return True
    # Checksums are optional for Central and count toward the monthly file quota.
    return base.endswith((".md5", ".sha1", ".sha256", ".sha512"))


def normalize_entry(entry: str) -> str:
    # `tar -C dir .` stores members as `./io/...`; that prefix is not part of the Maven path.
    while entry.startswith("./"):
        entry = entry[2:]
    return entry


def validate_entry_name(entry: str) -> None:
    if "\\" in entry or entry.startswith("/") or entry == "":
        raise StageError(f"unsafe archive entry {entry!r}")
    parts = entry.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise StageError(f"unsafe archive entry {entry!r}")


def coordinate_of(entry: str) -> str:
    parts = entry.split("/")
    if len(parts) < 4:
        raise StageError(f"archive entry {entry!r} is not inside a coordinate directory")
    group, artifact, version = ".".join(parts[:-3]), parts[-3], parts[-2]
    if not group:
        raise StageError(f"archive entry {entry!r} has an empty group id")
    expected_prefix = f"{artifact}-{version}"
    if not parts[-1].startswith(expected_prefix):
        raise StageError(f"archive entry {entry!r} does not start with {expected_prefix!r}")
    return f"{group}:{artifact}:{version}"


def iter_archive(path: Path) -> Iterator[tuple[str, int, object]]:
    """Yield (name, size, stream) for every regular file. Callers must close the stream."""
    lower = path.name.lower()
    if lower.endswith(".zip"):
        try:
            archive = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as error:
            raise StageError(f"{path.name}: not a valid zip: {error}") from error
        with archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                yield info.filename, info.file_size, archive.open(info)
        return

    if not lower.endswith((".tar.xz", ".txz", ".tar.gz", ".tgz")):
        raise StageError(f"{path.name}: unsupported archive format (use .tar.xz or .zip)")
    try:
        archive = tarfile.open(path, "r:*")
    except (tarfile.TarError, OSError, EOFError, lzma.LZMAError) as error:
        raise StageError(f"{path.name}: not a valid tar archive: {error}") from error
    with archive:
        for member in archive:
            if member.isdir():
                continue
            if not member.isfile():
                raise StageError(
                    f"{path.name}: {member.name!r} is not a regular file "
                    "(links and device entries are not allowed)"
                )
            stream = archive.extractfile(member)
            if stream is None:
                raise StageError(f"{path.name}: cannot read {member.name!r}")
            yield member.name, member.size, stream


def load_registrations(downloaded: Path) -> list[Registration]:
    path = downloaded / "collected.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise StageError(f"{path}: missing; run scripts/collect.py first") from None
    except (OSError, json.JSONDecodeError) as error:
        raise StageError(f"{path}: cannot read collected archives: {error}") from error
    if not isinstance(data, dict) or data.get("schemaVersion") != 1:
        raise StageError(f"{path}: schemaVersion must be 1")
    raw_registrations = data.get("registrations")
    if not isinstance(raw_registrations, list):
        raise StageError(f"{path}: registrations must be a list")

    registrations = []
    for raw in raw_registrations:
        if not isinstance(raw, dict):
            raise StageError(f"{path}: every registration must be an object")
        fields = {}
        for name in ("file", "project", "module", "version", "release", "url", "sha256", "archive"):
            value = raw.get(name)
            if not isinstance(value, str) or not value:
                raise StageError(f"{path}: {name} must be a non-empty string")
            fields[name] = value
        if not SHA256_RE.match(fields["sha256"]):
            raise StageError(f"{path}: sha256 must be 64 lowercase hex characters")
        coordinate = raw.get("coordinate")
        if coordinate is not None and (not isinstance(coordinate, str) or not COORDINATE_RE.match(coordinate)):
            raise StageError(f"{path}: invalid coordinate {coordinate!r}")

        relative = Path(fields["archive"])
        if relative.is_absolute() or any(part in {"..", ""} for part in relative.parts):
            raise StageError(f"{path}: archive path must stay inside {downloaded}")
        archive = downloaded / relative
        if not archive.is_file():
            raise StageError(f"{path}: archive {archive} is missing")

        registrations.append(
            Registration(
                file=fields["file"],
                project=fields["project"],
                module=fields["module"],
                version=fields["version"],
                release=fields["release"],
                coordinate=coordinate,
                url=fields["url"],
                sha256=fields["sha256"],
                archive=archive,
            )
        )
    return registrations


def inspect(registration: Registration) -> Inspected:
    actual_hash = sha256_file(registration.archive)
    if actual_hash != registration.sha256:
        raise StageError(
            f"{registration.archive.name}: sha256 mismatch "
            f"(registered {registration.sha256}, actual {actual_hash})"
        )

    entries: list[Entry] = []
    names: dict[str, int] = {}
    derived: set[str] = set()
    ignored = 0
    for name, size, stream in iter_archive(registration.archive):
        try:
            name = normalize_entry(name)
            validate_entry_name(name)
            if is_ignored(name):
                ignored += 1
                continue
            coordinate = coordinate_of(name)
            derived.add(coordinate)
            entries.append(Entry(coordinate=coordinate, name=name, size=size))
            names[name] = size
        finally:
            stream.close()

    if not entries:
        raise StageError(f"{registration.archive.name}: no Maven artifacts after filtering")
    if len(derived) != 1:
        raise StageError(
            f"{registration.archive.name}: expected exactly one coordinate, found "
            f"{', '.join(sorted(derived))}"
        )
    coordinate = next(iter(derived))
    group, artifact, version = coordinate.split(":")
    if artifact != registration.module:
        raise StageError(
            f"{registration.archive.name}: contains {artifact!r}, "
            f"but the registration is for module {registration.module!r}"
        )
    if version != registration.version:
        raise StageError(
            f"{registration.archive.name}: contains version {version!r}, "
            f"but the registration is for version {registration.version!r}"
        )
    if registration.coordinate is not None and registration.coordinate != coordinate:
        raise StageError(
            f"{registration.archive.name}: contains {coordinate}, "
            f"but the registration declares {registration.coordinate}"
        )

    prefix = f"{group.replace('.', '/')}/{artifact}/{version}/"
    pom = prefix + f"{artifact}-{version}.pom"
    if pom not in names:
        raise StageError(f"{registration.archive.name}: missing {artifact}-{version}.pom")
    for entry in entries:
        if entry.name.endswith(".asc"):
            if entry.size == 0:
                raise StageError(f"{registration.archive.name}: empty signature {entry.name}")
            continue
        if entry.name + ".asc" not in names:
            raise StageError(f"{registration.archive.name}: {entry.name} has no .asc signature")

    return Inspected(
        registration=registration,
        coordinate=coordinate,
        entries=entries,
        ignored_entries=ignored,
        files=len(entries),
        bytes=sum(entry.size for entry in entries),
    )


def pom_url(repo1: str, coordinate: str) -> str:
    group, artifact, version = coordinate.split(":")
    path = f"{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}.pom"
    return f"{repo1.rstrip('/')}/{path}"


def http_exists(url: str) -> bool:
    for attempt in range(4):
        request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return 200 <= response.status < 300
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return False
            if error.code in {403, 405}:
                return http_exists_with_get(url)
            if error.code in RETRYABLE_STATUS and attempt < 3:
                time.sleep(2**attempt)
                continue
            raise StageError(f"HEAD {url} returned HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            if attempt < 3:
                time.sleep(2**attempt)
                continue
            raise StageError(f"HEAD {url} failed: {error}") from error
    raise StageError(f"HEAD {url} failed after retries")


def http_exists_with_get(url: str) -> bool:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise StageError(f"GET {url} returned HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise StageError(f"GET {url} failed: {error}") from error


def probe_coordinates(repo1: str, coordinates: set[str]) -> dict[str, bool]:
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = executor.map(
            lambda coordinate: (coordinate, http_exists(pom_url(repo1, coordinate))),
            sorted(coordinates),
        )
        return dict(results)


def reset_staging(staging: Path) -> None:
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)


def merge(registrations: list[Inspected], pending: set[str], staging: Path) -> None:
    reset_staging(staging)
    origins: dict[Path, str] = {}

    for registration in registrations:
        if registration.coordinate not in pending:
            continue
        origin = f"{registration.registration.key}@{registration.registration.release}"
        for name, _size, stream in iter_archive(registration.registration.archive):
            try:
                name = normalize_entry(name)
                if is_ignored(name):
                    continue
                destination = staging.joinpath(*name.split("/"))
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_name(destination.name + ".part")

                digest = hashlib.sha256()
                with temporary.open("wb") as output:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                        output.write(chunk)
                new_hash = digest.hexdigest()

                if destination.exists():
                    temporary.unlink()
                    if sha256_file(destination) != new_hash:
                        raise StageError(
                            f"conflict for {destination.relative_to(staging)}: "
                            f"{origins[destination]} and {origin} differ"
                        )
                    continue

                temporary.replace(destination)
                origins[destination] = origin
            finally:
                stream.close()


def validate_staging(staging: Path, pending: set[str]) -> tuple[int, int]:
    total_files = 0
    total_bytes = 0
    for coordinate in sorted(pending):
        group, artifact, version = coordinate.split(":")
        directory = staging.joinpath(*group.split("."), artifact, version)
        if not directory.is_dir():
            raise StageError(f"{coordinate}: nothing was extracted")
        pom = directory / f"{artifact}-{version}.pom"
        if not pom.is_file():
            raise StageError(f"{coordinate}: missing {pom.name}")
        files = [path for path in directory.rglob("*") if path.is_file()]
        for path in files:
            relative = path.relative_to(staging)
            total_files += 1
            total_bytes += path.stat().st_size
            if path.name.endswith(".asc"):
                if path.stat().st_size == 0:
                    raise StageError(f"{coordinate}: empty signature {relative}")
                continue
            signature = path.with_name(path.name + ".asc")
            if not signature.is_file():
                raise StageError(f"{coordinate}: {relative} has no .asc signature")
    return total_files, total_bytes


def read_packaging(pom: Path) -> str:
    try:
        root = xml.etree.ElementTree.parse(pom).getroot()
    except (xml.etree.ElementTree.ParseError, OSError) as error:
        raise StageError(f"{pom.name}: cannot read packaging: {error}") from error
    namespace = root.tag[: root.tag.index("}") + 1] if root.tag.startswith("{") else ""
    node = root.find(f"{namespace}packaging")
    value = (node.text or "").strip() if node is not None else ""
    return value or "jar"


def jar_exemptions(config: Path) -> set[tuple[str, str]]:
    """Return the (groupId, artifactId) pairs that artifactOverrides exempts from the jar rule.

    jreleaser.yml is read as text: it is one hand-maintained block and adding a YAML parser to the
    hub is not worth it. Every entry in that block counts, because the block exists only to pass
    `jar: false`.
    """
    try:
        text = config.read_text(encoding="utf-8")
    except OSError:
        return set()
    match = re.search(r"^(?P<indent>[ \t]*)artifactOverrides:[ \t]*$", text, re.MULTILINE)
    if match is None:
        return set()
    indent = len(match.group("indent"))
    block: list[str] = []
    for line in text[match.end() :].splitlines():
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        block.append(line)
    exempt: set[tuple[str, str]] = set()
    for item in re.split(r"^[ \t]*-[ \t]+", "\n".join(block), flags=re.MULTILINE)[1:]:
        group = re.search(r"^[ \t]*groupId:[ \t]*[\"']?([^\"'\s]+)", item, re.MULTILINE)
        artifact = re.search(r"^[ \t]*artifactId:[ \t]*[\"']?([^\"'\s]+)", item, re.MULTILINE)
        if group and artifact:
            exempt.add((group.group(1), artifact.group(1)))
    return exempt


def check_jar_prerequisite(staging: Path, pending: set[str], config: Path) -> None:
    """Fail before JReleaser does when a coordinate has no jar and no artifactOverrides entry.

    JReleaser aborts the whole deployment for every packaging outside JARLESS_PACKAGING when the
    same-named `.jar` is missing, and the only opt-out is an artifactOverrides entry keyed by the
    exact groupId/artifactId. Kotlin Multiplatform JS publications ship a `.klib`, so a new KMP
    producer that forgets its entry used to surface only in the JReleaser dry run.
    """
    exempt = jar_exemptions(config)
    missing: list[str] = []
    for coordinate in sorted(pending):
        group, artifact, version = coordinate.split(":")
        directory = staging.joinpath(*group.split("."), artifact, version)
        packaging = read_packaging(directory / f"{artifact}-{version}.pom")
        if packaging in JARLESS_PACKAGING:
            continue
        if (directory / f"{artifact}-{version}.jar").is_file():
            continue
        if (group, artifact) in exempt:
            continue
        missing.append(f"{coordinate} (packaging {packaging})")
    if missing:
        raise StageError(
            "JReleaser requires a .jar for these coordinates and jreleaser.yml has no matching "
            "artifactOverrides entry:\n  "
            + "\n  ".join(missing)
            + "\nShip the jar, or add an entry with `jar: false` under "
            "deploy.maven.mavenCentral.hub.artifactOverrides."
        )


def write_reports(
    report_path: Path,
    report: dict,
    pending: list[str],
    delete: list[dict],
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (report_path.parent / "pending-coordinates.txt").write_text(
        "".join(f"{coordinate}\n" for coordinate in pending), encoding="utf-8"
    )
    (report_path.parent / "registrations-to-delete.json").write_text(
        json.dumps({"schemaVersion": 1, "registrations": delete}, indent=2) + "\n",
        encoding="utf-8",
    )


def empty_report(staging: Path, report_path: Path, repo1: str, offline: bool) -> int:
    reset_staging(staging)
    report = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo1": repo1,
        "offline": offline,
        "registrations": [],
        "coordinates": [],
        "pending": [],
        "files": 0,
        "bytes": 0,
    }
    write_reports(report_path, report, [], [])
    print("warning: the registry is empty; nothing to publish")
    print(f"report: {report_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--downloaded", type=Path, default=Path("downloaded"))
    parser.add_argument("--staging", type=Path, default=Path("staging"))
    parser.add_argument("--report", type=Path, default=Path("out/staging-report.json"))
    parser.add_argument(
        "--jreleaser-config",
        type=Path,
        default=Path("jreleaser.yml"),
        help="read artifactOverrides from this file to check JReleaser's .jar prerequisite",
    )
    parser.add_argument("--repo1", default="https://repo1.maven.org/maven2")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="skip repo1 probes; every coordinate is treated as pending (for local experiments)",
    )
    args = parser.parse_args()

    try:
        registrations = load_registrations(args.downloaded)
    except StageError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if not registrations:
        return empty_report(args.staging, args.report, args.repo1, args.offline)

    try:
        inspected = [inspect(registration) for registration in registrations]
        coordinates = {registration.coordinate for registration in inspected}
        print(f"inspected {len(inspected)} registration(s) with {len(coordinates)} coordinate(s)")

        if args.offline:
            published: dict[str, bool] = {}
            print("warning: --offline, skipping repo1 probes")
        else:
            print(f"probing {args.repo1} for published POMs")
            published = probe_coordinates(args.repo1, coordinates)

        statuses = {coordinate: published.get(coordinate, False) for coordinate in coordinates}
        pending = {coordinate for coordinate, is_published in statuses.items() if not is_published}
        for coordinate in sorted(coordinates):
            state = "already published, dropped" if statuses[coordinate] else "pending"
            print(f"  {coordinate}: {state}")

        merge(inspected, pending, args.staging)
        files, size = validate_staging(args.staging, pending)
        check_jar_prerequisite(args.staging, pending, args.jreleaser_config)

        report = {
            "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "repo1": args.repo1,
            "offline": args.offline,
            "registrations": [
                {
                    "file": item.registration.file,
                    "project": item.registration.project,
                    "module": item.registration.module,
                    "version": item.registration.version,
                    "release": item.registration.release,
                    "coordinate": item.coordinate,
                    "archive": item.registration.archive.name,
                    "sha256": item.registration.sha256,
                    "status": "pending" if item.coordinate in pending else "published",
                    "files": item.files,
                    "bytes": item.bytes,
                    "ignoredEntries": item.ignored_entries,
                }
                for item in sorted(inspected, key=lambda entry: entry.registration.key)
            ],
            "coordinates": [
                {
                    "coordinate": coordinate,
                    "status": "pending" if coordinate in pending else "published",
                }
                for coordinate in sorted(coordinates)
            ],
            "pending": sorted(pending),
            "files": files,
            "bytes": size,
        }
        # Every registration is satisfied once this deployment succeeds: staged ones are published
        # by it, already-published ones were pruned. cleanup.py removes them after a successful
        # upload; a failed upload leaves the registry untouched for the next attempt.
        delete = [
            {
                "file": item.registration.file,
                "project": item.registration.project,
                "module": item.registration.module,
                "version": item.registration.version,
                "sha256": item.registration.sha256,
            }
            for item in sorted(inspected, key=lambda entry: entry.registration.key)
        ]
        write_reports(args.report, report, sorted(pending), delete)

        print(f"staged {len(pending)} coordinate(s): {files} file(s), {size} byte(s)")
        print(f"report: {args.report}")
        print(f"pending: {args.report.parent / 'pending-coordinates.txt'}")
        return 0
    except StageError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
