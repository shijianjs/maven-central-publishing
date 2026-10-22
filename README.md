# maven-central-publishing

A release hub for Maven Central. Project releases **register** the archive they want published;
the hub downloads it, verifies it, drops coordinates that are already on Central, merges the rest
into one bundle, and uploads that bundle as a **single** Central deployment. Publishing is always a
manual action: there is no schedule and the default workflow run only builds and inspects the
bundle.

[English](README.md) | [简体中文](README.zh-CN.md)

The hub never compiles anything. It needs Python 3, the GitHub CLI (`gh`) and JReleaser, not
Gradle or Maven.

```
project release ──▶ registry/<owner>/<repo>/<module>.json ──▶ collect.py ──▶ stage.py
                                                                              │
                                          Central ◀── JReleaser ◀── staging/ ┘
                                                     │
                              delete satisfied registrations
```

## Registry

The registry is the source of truth. It holds **one JSON file per project + module**:

```
registry/
└── shijianjs/
    └── ktjs-iconify/
        ├── ktjs-iconify-types.json
        └── ktjs-iconify-utils.json
```

A registration for a newer version overwrites the same file, so the newest registration replaces
the previous one instead of piling up. After uploading the archive, commit the file (or open a pull
request); the workflow reads the registry on its next run. The file is small and meant to be edited
by hand; a typo or a stale link fails the next run with a message naming the file.

```json
{
  "schemaVersion": 1,
  "project": "shijianjs/ktjs-iconify",
  "module": "ktjs-iconify-types",
  "version": "3.1.7-kt.4",
  "release": "release-7",
  "coordinate": "io.github.shijianjs:ktjs-iconify-types:3.1.7-kt.4",
  "archive": {
    "url": "https://github.com/shijianjs/ktjs-iconify/releases/download/release-7/ktjs-iconify-types-3.1.7-kt.4.tar.xz",
    "sha256": "<64 lowercase hex characters>"
  }
}
```

| Field | Meaning |
| --- | --- |
| `schemaVersion` | `1` |
| `project` | GitHub repository, `owner/repo` |
| `module` | Maven artifact id; also the file name |
| `version` | Maven version being registered |
| `release` | Release identifier the archive was uploaded to; must match the tag of a GitHub asset URL |
| `coordinate` | Optional. If present it must equal the coordinate inside the archive |
| `archive.url` | HTTPS link to the archive |
| `archive.sha256` | SHA-256 of the archive, checked on every run |

The file must live at `registry/<owner>/<repo>/<module>.json`; a mismatch between the path and the
fields is an error. Duplicate project + module registrations cannot exist because the path is the
key.

GitHub Release asset URLs are downloaded with `gh`, so private repositories work when
`HUB_RELEASE_TOKEN` is configured. Any other HTTPS URL is fetched directly.

### One archive per module and version

Listing individual Maven files would mean dozens of links per module; a registration points at one
archive instead. Recommended formats:

- `.tar.xz` — recommended. Solid compression deduplicates the repeated content that Maven
  repository layouts contain (POM/module metadata, repeated signatures, similar jars);
- `.zip` — accepted, useful when the producer already builds a Portal bundle;
- `.tar.gz` / `.tgz` — accepted.

The archive must contain the Maven repository layout for **exactly one coordinate**:

```
io/github/shijianjs/ktjs-iconify-types/3.1.7-kt.4/ktjs-iconify-types-3.1.7-kt.4.pom
io/github/shijianjs/ktjs-iconify-types/3.1.7-kt.4/ktjs-iconify-types-3.1.7-kt.4.pom.asc
io/github/shijianjs/ktjs-iconify-types/3.1.7-kt.4/ktjs-iconify-types-3.1.7-kt.4.jar
...
```

Do not wrap the layout in an extra directory, and do not mix several modules or versions into one
archive. `tar -C dir -cJf out.tar.xz .` is fine; a leading `./` is ignored.

Every artifact must already be signed by the producer: each file needs a detached, non-empty
`.asc` sibling, and the POM itself is mandatory. The hub does not sign (`sign: false`) and does not
generate checksums (`checksums: false`).

Producing an archive from Gradle is the same flow as before: publish to a file repository such as
`build/staging-repo` (vanniktech has no "bundle only" switch), then package the directory. Maven
projects can use `central-publishing-maven-plugin` with `skipUpload=true`. Example:

```bash
tar -C build/staging-repo -cJf ktjs-iconify-types-3.1.7-kt.4.tar.xz .
sha256sum ktjs-iconify-types-3.1.7-kt.4.tar.xz
gh release upload release-7 ktjs-iconify-types-3.1.7-kt.4.tar.xz
```

All coordinates must belong to the namespace in `jreleaser.yml` (`io.github.shijianjs`); Central
rejects anything else.

## What the hub verifies

`scripts/collect.py` reads the registry and checks:

- schema, required fields, project/module path and `https` link;
- the archive is inside a known format and its sha256 matches the registration.

`scripts/stage.py` then checks, before writing anything:

- sha256 again (defence in depth);
- the archive contains exactly the registered module and version, and every entry sits under that
  coordinate directory (unsafe entries, links and device files in tar archives are rejected);
- the POM exists and every artifact has a non-empty `.asc` signature;
- probing `https://repo1.maven.org/maven2/.../<artifact>-<version>.pom`: existing coordinates are
  dropped for the whole registration, so one duplicate cannot fail the deployment;
- the same path coming from two projects must have identical bytes, otherwise the run fails.

JReleaser also asks Central whether every POM already exists before uploading, and fails the whole
deployment on a duplicate. The repo1 probe is what makes the run skip such coordinates instead of
hitting that wall; it is a defence-in-depth check, not a replacement.

It also drops files that Central does not need and that only consume File Count quota:
`maven-metadata*.xml`, `_remote.repositories`, hidden files and all checksum files (`.md5`,
`.sha1`, `.sha256`, `.sha512`, including checksums of `.asc` files).

When the registry is empty, or every coordinate is already published, the staging directory is
empty: the run reports zero pending coordinates, ends successfully, and creates no deployment.

## Local verification

The fixture builds two registrations: one coordinate that is already on Central (packaged as zip)
and one that cannot exist (packaged as tar.xz), plus noise the hub must drop. Generating the
fixture downloads the real published files from repo1; after that, collecting is local, while
`stage.py` still probes repo1 so the pruning path is exercised:

```bash
python3 tests/make-fixtures.py
python3 scripts/collect.py --local tests/fixtures
python3 scripts/stage.py
JRELEASER_PROJECT_VERSION=0.0.1 \
JRELEASER_DEPLOY_MAVEN_MAVENCENTRAL_HUB_USERNAME=dry-run \
JRELEASER_DEPLOY_MAVEN_MAVENCENTRAL_HUB_PASSWORD=dry-run \
jreleaser deploy --dry-run
```

The report is written to `out/staging-report.json`; the deletion list to
`out/registrations-to-delete.json`; the bundle to
`out/jreleaser/deploy/mavenCentral/hub/`. The fixture signatures are copies, so the fixture is only
for dry runs. Generated fixtures are gitignored.

## Publishing

1. Add the `maven-central` environment in repository settings and, preferably, required reviewers.
2. Add `MAVEN_CENTRAL_USERNAME` / `MAVEN_CENTRAL_PASSWORD` (Central user-token halves) as
   environment or repository secrets. For archives in private projects, add `HUB_RELEASE_TOKEN`
   with `repo` read access.
3. Run the workflow manually with `upload=true`.

The `bundle` job always runs first: it collects, verifies, prunes and creates the bundle with
`jreleaser deploy --dry-run`. The `upload` job then restores the verified staging directory, runs
`jreleaser deploy`, and — only after it succeeds — deletes the registrations it satisfied:

- registrations that were staged in this deployment, now on Central;
- registrations that were already published and got pruned.

The deletion is a commit pushed back to the branch that ran the workflow. The `upload` job declares
`permissions: contents: write` for it, and a protected branch must allow that push. If the remote
moved during the upload and the push is rejected, the upload still succeeded: run the workflow
again; the already-published registrations are pruned and deleted. A failed deployment never deletes
anything, and failed deployments are left in place for Sonatype Support.

Release Count is per deployment: every run that uploads creates one deployment regardless of how
many registrations it contains. File Count and Release Size are per artifact, which is why the hub
drops published coordinates and checksum files before uploading.

JReleaser is pinned in the workflow and its distribution is verified against the release's
`checksums_sha256.txt`.

## Withdrawing or correcting a registration

- To withdraw a pending version, delete its JSON file before the next run.
- To correct a link, hash or version, edit the file; the next run validates it.
- Replacing a pending registration with a different version discards the old one: only the newest
  registration for a project + module is published. Register a version only after its archive is
  on the release.

See [`registry/README.md`](registry/README.md) for the same contract from the producer's side.
