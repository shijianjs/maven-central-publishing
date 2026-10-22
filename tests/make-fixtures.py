#!/usr/bin/env python3
"""Build a local registry fixture that exercises the hub without touching GitHub Releases.

The fixture mimics two registrations for `shijianjs/ktjs-iconify`:

* `ktjs-iconify-types:2.0.0-kt.3`, already on Maven Central. The real files are downloaded from
  repo1, so the probe really returns "published" and the hub must drop the coordinate. Packaged as
  a `.zip` to keep the zip path covered.
* `ktjs-iconify-utils:9.9.9-kt.99`, a version that cannot exist on Central. Its files are the types
  files renamed, packaged as the recommended `.tar.xz`; the hub must keep them.

Both archives also carry noise Central does not need (`maven-metadata.xml`, `.md5`/`.sha1` files,
hidden files), which the hub must filter out. The signatures of the second coordinate are copies of
the first one, so this fixture is for dry runs only.

Generated files are gitignored. Use `scripts/collect.py --local tests/fixtures`.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

PROJECT = "shijianjs/ktjs-iconify"
RELEASE = "release-test"
GROUP = "io.github.shijianjs"
PUBLISHED_MODULE = "ktjs-iconify-types"
PUBLISHED_VERSION = "2.0.0-kt.3"
PENDING_MODULE = "ktjs-iconify-utils"
PENDING_VERSION = "9.9.9-kt.99"
REPO1 = "https://repo1.maven.org/maven2"
TEXT_SUFFIXES = (".pom", ".json", ".module")


def fetch(path: str) -> bytes:
    request = urllib.request.Request(f"{REPO1}/{path}", headers={"User-Agent": "hub-fixture/1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def published_files() -> dict[str, bytes]:
    directory = f"{GROUP.replace('.', '/')}/{PUBLISHED_MODULE}/{PUBLISHED_VERSION}/"
    page = fetch(directory).decode("utf-8")
    names = sorted(
        name
        for name in re.findall(r'href="([^"]+)"', page)
        if name.startswith(f"{PUBLISHED_MODULE}-{PUBLISHED_VERSION}") and not name.endswith("/")
    )
    return {name: fetch(f"{directory}{name}") for name in names}


def renamed_files(files: dict[str, bytes]) -> dict[str, bytes]:
    directory = f"{GROUP.replace('.', '/')}/{PENDING_MODULE}/{PENDING_VERSION}/"
    result = {}
    for name, data in files.items():
        renamed = name.replace(PUBLISHED_MODULE, PENDING_MODULE).replace(PUBLISHED_VERSION, PENDING_VERSION)
        if renamed.endswith(TEXT_SUFFIXES):
            data = data.replace(PUBLISHED_MODULE.encode(), PENDING_MODULE.encode())
            data = data.replace(PUBLISHED_VERSION.encode(), PENDING_VERSION.encode())
        result[f"{directory}{renamed}"] = data
    # Noise the hub must drop instead of uploading.
    result[f"{directory}{PENDING_MODULE}-{PENDING_VERSION}.jar.md5"] = b"0" * 32
    result[f"{directory}.DS_Store"] = b"noise"
    result[f"{GROUP.replace('.', '/')}/{PENDING_MODULE}/maven-metadata.xml"] = b"<metadata/>"
    return result


def make_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
    return buffer.getvalue()


def make_tar_xz(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz") as archive:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def write_registration(registry: Path, module: str, version: str, archive_name: str, payload: bytes) -> None:
    path = registry / PROJECT / f"{module}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    registration = {
        "schemaVersion": 1,
        "project": PROJECT,
        "module": module,
        "version": version,
        "release": RELEASE,
        "coordinate": f"{GROUP}:{module}:{version}",
        "archive": {
            "url": f"https://github.com/{PROJECT}/releases/download/{RELEASE}/{archive_name}",
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
    }
    path.write_text(json.dumps(registration, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    fixtures = Path(__file__).resolve().parent / "fixtures"
    registry = fixtures / "registry"
    archives = fixtures / "archives"
    for directory in (registry, archives, fixtures / "releases"):
        if directory.exists():
            shutil.rmtree(directory)
    registry.mkdir(parents=True)
    archives.mkdir(parents=True)

    published = published_files()
    if not published:
        print("error: repo1 returned no files for the published fixture coordinate", file=sys.stderr)
        return 1

    published_prefix = f"{GROUP.replace('.', '/')}/{PUBLISHED_MODULE}/{PUBLISHED_VERSION}/"
    published_archive = {
        f"{published_prefix}{name}": data for name, data in published.items()
    }
    published_archive[f"{GROUP.replace('.', '/')}/{PUBLISHED_MODULE}/maven-metadata.xml"] = b"<metadata/>"
    published_payload = make_zip(published_archive)
    published_name = f"{PUBLISHED_MODULE}-{PUBLISHED_VERSION}.zip"
    (archives / published_name).write_bytes(published_payload)
    write_registration(registry, PUBLISHED_MODULE, PUBLISHED_VERSION, published_name, published_payload)

    pending_archive = renamed_files(published)
    pending_payload = make_tar_xz(pending_archive)
    pending_name = f"{PENDING_MODULE}-{PENDING_VERSION}.tar.xz"
    (archives / pending_name).write_bytes(pending_payload)
    write_registration(registry, PENDING_MODULE, PENDING_VERSION, pending_name, pending_payload)

    print(f"fixture: {fixtures}")
    print(f"  {published_name}: {len(published_archive)} entries, {len(published_payload)} bytes")
    print(f"  {pending_name}: {len(pending_archive)} entries, {len(pending_payload)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
