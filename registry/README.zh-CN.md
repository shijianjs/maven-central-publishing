# 注册表

[English](README.md) | [简体中文](README.zh-CN.md)

**一个项目 + 模块一个 JSON 文件**：

```
registry/<owner>/<repo>/<module>.json
```

新版本覆盖同一个文件；汇总仓库不会为同一个模块追加第二条记录。路径就是唯一键，字段必须与路径
一致。

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

- `coordinate` 可选；写了就必须与归档里的坐标一致。
- 归档必须是一个模块一个版本的 Maven 仓库布局：`<group 路径>/<module>/<version>/…`，包含 POM，
  并且每个文件都有 `.asc` 签名。
- 推荐 `.tar.xz`（solid 压缩对重复内容更友好），也接受 `.zip` 和 `.tar.gz`。
- `archive.sha256` 每次运行都会重新校验，所以上传归档后一定要同步更新注册；哈希不对会让所有人的
  运行停下来。
- 想撤回一个待发布的版本，删掉这个 JSON 文件即可。上传成功后，汇总仓库会自动删除已经满足的注册。

注册表里的改动按普通代码评审对待：格式错误的文件会被当成硬错误，而不是跳过。

完整流程见根目录的 [README.zh-CN.md](../../README.zh-CN.md)。
