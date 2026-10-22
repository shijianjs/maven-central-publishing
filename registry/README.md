# Registry

[English](README.md) | [简体中文](README.zh-CN.md)

One JSON file per **project + module**:

```
registry/<owner>/<repo>/<module>.json
```

A newer version overwrites the same file; the hub never appends a second entry for the same module.
The path is the uniqueness key, and the fields must match it.

```json
{
  "schemaVersion": 1,
  "project": "owner/repo",
  "module": "artifact-id",
  "version": "1.2.3-kt.4",
  "release": "release-7",
  "coordinate": "group:artifact-id:1.2.3-kt.4",
  "archive": {
    "url": "https://github.com/owner/repo/releases/download/release-7/artifact-id-1.2.3-kt.4.tar.xz",
    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }
}
```

- `coordinate` is optional; when present it must match the coordinate inside the archive.
- The archive holds the Maven repository layout for exactly one module and version:
  `<group path>/<module>/<version>/…`, including the POM and a `.asc` signature for every file.
- `.tar.xz` is recommended (solid compression deduplicates repeated content); `.zip` and
  `.tar.gz` are accepted.
- Every run re-checks `archive.sha256`. A wrong hash stops the run for everyone, so verify it after
  uploading the archive.
- Delete the JSON file to withdraw a pending version. The hub deletes it automatically after a
  successful upload.

Registrations are reviewed like any other change to this repository; the workflow treats a malformed
file as a hard error rather than skipping it.

See the root [README](../README.md) for the full verification and publishing flow.
