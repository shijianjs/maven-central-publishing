# maven-central-publishing

Maven Central 的汇总发布仓库。项目在自己的 release 里放好归档后，向本仓库**提交一条注册**；
本仓库下载归档、校验、剔除已经在 Central 上的坐标、把剩下的合并成一个 bundle，并以**一次**
Central deployment 上传。发布永远是手动动作：没有定时任务，默认的工作流运行只构建并检查 bundle。

[English](README.md) | [简体中文](README.zh-CN.md)

本仓库不编译任何东西。它只需要 Python 3、GitHub CLI（`gh`）和 JReleaser，不需要 Gradle 或 Maven。

```
项目 release ──▶ registry/<owner>/<repo>/<module>.json ──▶ collect.py ──▶ stage.py
                                                                             │
                                          Central ◀── JReleaser ◀── staging/ ┘
                                                     │
                                          删除已满足的注册
```

## 注册表

注册表是唯一事实来源，**一个项目 + 模块一个 JSON 文件**：

```
registry/
└── shijianjs/
    └── ktjs-iconify/
        ├── ktjs-iconify-types.json
        └── ktjs-iconify-utils.json
```

新版本覆盖同一个文件，所以最新的注册是**替代**而不是追加。上传归档后提交这个文件（或开一个
PR），工作流下次运行就会读取它。文件很小、可以手工编辑；写错的字段或过期的链接会让下次运行
失败，并指出是哪个文件。

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
    "sha256": "<64 位小写十六进制>"
  }
}
```

| 字段 | 含义 |
| --- | --- |
| `schemaVersion` | `1` |
| `project` | GitHub 仓库，`owner/repo` |
| `module` | Maven artifact id，同时也是文件名 |
| `version` | 本次注册的 Maven 版本 |
| `release` | 归档所在的 release 标识；GitHub 资源链接的 tag 必须与它一致 |
| `coordinate` | 可选。写了就必须等于归档里的坐标 |
| `archive.url` | 归档的 HTTPS 链接 |
| `archive.sha256` | 归档的 SHA-256，每次运行都会重新校验 |

文件必须放在 `registry/<owner>/<repo>/<module>.json`；路径与字段不一致就是错误。由于路径就是
唯一键，同一个项目 + 模块不可能存在两条注册。

GitHub Release 资源链接用 `gh` 下载，所以配置了 `HUB_RELEASE_TOKEN` 就能读私有仓库；其他
HTTPS 链接直接抓取。

### 一个模块的一个版本一个归档

如果逐个列 Maven 文件，一个模块就会有几十条链接；一条注册只指向一个归档。推荐格式：

- `.tar.xz` —— 推荐。solid 压缩对 Maven 仓库布局里的重复内容（POM/module 元数据、重复的签名、
  相近的 jar）优化更好；
- `.zip` —— 支持，生产者已经产出 Portal bundle 时很方便；
- `.tar.gz` / `.tgz` —— 支持。

归档里必须是**恰好一个坐标**的 Maven 仓库布局：

```
io/github/shijianjs/ktjs-iconify-types/3.1.7-kt.4/ktjs-iconify-types-3.1.7-kt.4.pom
io/github/shijianjs/ktjs-iconify-types/3.1.7-kt.4/ktjs-iconify-types-3.1.7-kt.4.pom.asc
io/github/shijianjs/ktjs-iconify-types/3.1.7-kt.4/ktjs-iconify-types-3.1.7-kt.4.jar
...
```

不要用额外的目录把布局包一层，也不要把多个模块或多个版本混进一个归档。
`tar -C dir -cJf out.tar.xz .` 可以用，开头的 `./` 会被忽略。

构件必须由生产者预先签名：每个文件都要有非空的分离式 `.asc` 兄弟文件，POM 本身也是必需的。
本仓库不签名（`sign: false`），也不生成校验和（`checksums: false`）。

Gradle 产出归档的流程与之前相同：先发布到 `build/staging-repo` 这类 file repository（vanniktech
没有「只打包不上传」的开关），再打包目录。Maven 项目可以用 `central-publishing-maven-plugin`
的 `skipUpload=true`。示例：

```bash
tar -C build/staging-repo -cJf ktjs-iconify-types-3.1.7-kt.4.tar.xz .
sha256sum ktjs-iconify-types-3.1.7-kt.4.tar.xz
gh release upload release-7 ktjs-iconify-types-3.1.7-kt.4.tar.xz
```

所有坐标必须属于 `jreleaser.yml` 里配置的 namespace（`io.github.shijianjs`），Central 会拒绝其他
group。

## 本仓库校验什么

`scripts/collect.py` 读取注册表并检查：

- schema、必填字段、project/module 与路径是否一致、链接是否为 `https`；
- 归档格式是否受支持，sha256 是否与注册一致。

`scripts/stage.py` 在写入任何东西之前再检查：

- 重新算一次 sha256（纵深防御）；
- 归档里恰好是注册的模块和版本，且每个条目都在该坐标目录下（不安全的路径条目、tar 里的链接和
  设备文件直接拒绝）；
- POM 存在，且每个构件都有非空 `.asc` 签名；
- 探测 `https://repo1.maven.org/maven2/.../<artifact>-<version>.pom`：已存在的坐标把整条注册
  剔除，因此一个重复版本不会拖垮整个 deployment；
- 两个项目提供同一路径时字节必须完全相同，否则运行失败。

JReleaser 上传前也会就每个 POM 询问 Central 是否已存在，发现重复会让整个 deployment 失败。
repo1 探测的作用是让运行跳过这类坐标，而不是撞上那堵墙；它是纵深防御，不能相互替代。

同时丢弃 Central 不需要、只消耗 File Count 额度的文件：`maven-metadata*.xml`、
`_remote.repositories`、隐藏文件以及所有校验和文件（`.md5`、`.sha1`、`.sha256`、`.sha512`，
包括 `.asc` 的校验和）。

注册表为空，或者所有坐标都已发布时，staging 目录是空的：运行报告零个待发布坐标，正常结束，
不创建 deployment。

## 本地验证

fixture 生成两条注册：一个已经在 Central 上的坐标（打成 zip）和一个不可能存在的坐标（打成
tar.xz），再加上必须被丢弃的噪音。生成 fixture 时会从 repo1 下载真实的已发布文件；之后收集
环节是本地的，而 `stage.py` 仍会探测 repo1，因此剪枝路径也被覆盖：

```bash
python3 tests/make-fixtures.py
python3 scripts/collect.py --local tests/fixtures
python3 scripts/stage.py
JRELEASER_PROJECT_VERSION=0.0.1 \
JRELEASER_DEPLOY_MAVEN_MAVENCENTRAL_HUB_USERNAME=dry-run \
JRELEASER_DEPLOY_MAVEN_MAVENCENTRAL_HUB_PASSWORD=dry-run \
jreleaser deploy --dry-run
```

报告写在 `out/staging-report.json`，删除清单写在 `out/registrations-to-delete.json`，bundle 写在
`out/jreleaser/deploy/mavenCentral/hub/`。fixture 的签名是复制出来的，所以它只能用于 dry run。
生成物已 gitignore。

## 发布

1. 在仓库设置里添加 `maven-central` environment，最好再配置 required reviewers。
2. 把 Central user token 的两部分作为 `MAVEN_CENTRAL_USERNAME` / `MAVEN_CENTRAL_PASSWORD` 添加为
   environment 或仓库 secrets。私有项目的归档需要另外配置有 `repo` 读权限的
   `HUB_RELEASE_TOKEN`。
3. 手动运行工作流并勾选 `upload=true`。

`bundle` job 永远先跑：收集、校验、剪枝，并用 `jreleaser deploy --dry-run` 产出 bundle。
`upload` job 随后恢复已校验的 staging 目录，执行 `jreleaser deploy`，并且只在成功后删除本次已
满足的注册：

- 本次进入 deployment、现在已经发布在 Central 的注册；
- 本来就已在 Central、被剪枝掉的注册。

删除是一个推回运行工作流的那个分支的提交；该 job 声明了 `permissions: contents: write`，受保护
分支需要允许这次推送。如果上传期间远端有了新提交导致 push 被拒，上传本身已经成功：再跑一次
即可，已发布的注册会被剪枝并删除。部署失败时不会删除任何东西，失败的 deployment 会保留下来
供 Sonatype Support 排查。

Release Count 按 deployment 计：只要上传，一次运行就产生一个 deployment，与包含多少条注册无关。
File Count 与 Release Size 按构件累计，这就是本仓库在上传前剔除已发布坐标和校验和文件的原因。

JReleaser 的版本固定在工作流里，并且会用 release 附带的 `checksums_sha256.txt` 校验安装包。

## 撤回或修改注册

- 想撤回一个待发布版本，在下次运行前删掉它的 JSON 文件。
- 想修正链接、哈希或版本，直接编辑该文件，下次运行会校验。
- 用另一个版本覆盖待发布的注册会丢弃旧版本：一个项目 + 模块只有最新的注册会被发布。请在归档
  已经挂到 release 上之后再注册对应版本。

从生产者视角看同一份契约，见 [`registry/README.zh-CN.md`](registry/README.zh-CN.md)。
