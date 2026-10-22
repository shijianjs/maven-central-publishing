# AGENTS.md

## 仓库职责

本仓库是 Maven Central 的**汇总发布**入口，只做「注册 → 下载 → 校验 → 剪枝 → 合并 → 上传 →
删除已发布注册」，不编译任何项目，也不需要 Gradle/Maven。项目侧在创建 release 后，把「一个模块
一个版本」打成一个归档并提交一条注册；本仓库汇总后一次性上传。

一次运行最多产生一个 Central deployment。Release Count 按 deployment 计（与项目数、模块数无
关），File Count / Release Size 按实际构件计，所以「合并」和「剪枝」都不能省。

发布**没有定时任务**，只能手动触发。默认 `upload=false`，只收集、校验并产出 bundle；只有手动
勾选 `upload=true` 才进入上传 job。

## 布局

- `registry/<owner>/<repo>/<module>.json`：注册表，是唯一事实来源。一个项目 + 模块一个文件，
  新版本覆盖旧文件，因此天然是「替代」而不是「追加」。JSON 便于手工修改，且 Python 标准库能
  直接解析，不引入 PyYAML。
- `scripts/collect.py`：校验注册字段与文件路径，按 `archive.url` 下载（GitHub Release 链接走
  `gh`，其他 HTTPS 直接抓取），校验 sha256，写 `downloaded/collected.json`。
- `scripts/stage.py`：再次校验 sha256；支持 `.tar.xz` / `.txz` / `.tar.gz` / `.tgz` / `.zip`；
  检查归档里恰好只有注册的模块和版本、每个条目都在该坐标目录下、POM 存在、每个构件有非空
  `.asc`（tar 的链接/设备条目、`..` 等路径直接拒绝）；探测 `repo1` 上的 POM，已发布坐标整组
  剔除；合并进 `staging/`，同路径内容冲突即失败；生成：
  - `out/staging-report.json`：每个注册的状态（`pending` / `published`）、坐标、文件数、字节数；
  - `out/pending-coordinates.txt`；
  - `out/registrations-to-delete.json`：本次运行「已满足」的注册（本次将上传的 + 本来就已发布的），
    供上传成功后删除。
- `scripts/cleanup.py`：读取删除清单，重新读取工作区里的注册文件，只有版本与归档 sha256 都没变
  才 `git rm`；然后 commit，`--push` 时推回当前分支。这样即使上传期间有人替换了注册，也不会误删
  新版本。
- `jreleaser.yml`：只负责合并与上传。`stagingRepositories: [staging]`，`checksums: false`，
  `sign: false`（构件由项目侧签名）。
- `.github/workflows/hub.yml`：仅 `workflow_dispatch`。`bundle` job 默认 `jreleaser deploy
  --dry-run` 只产出 bundle；`upload` job 在 `upload=true` 时上传，并在成功后运行 cleanup。
  即使 `pending=0`，`upload=true` 也会运行 upload job，用于删除「已发布但注册还在」的条目。
- `tests/make-fixtures.py`：生成两份本地注册与归档：一个已发布坐标（zip，真实文件从 repo1
  下载），一个不可能存在的坐标（tar.xz，由真实文件改名），以及必须被过滤的噪音。生成物已
  gitignore。
- `registry/README.md`：给提交注册的项目看的契约说明。

## 命令

```bash
python3 tests/make-fixtures.py
python3 scripts/collect.py --local tests/fixtures
python3 scripts/stage.py
JRELEASER_PROJECT_VERSION=0.0.1 \
JRELEASER_DEPLOY_MAVEN_MAVENCENTRAL_HUB_USERNAME=dry-run \
JRELEASER_DEPLOY_MAVEN_MAVENCENTRAL_HUB_PASSWORD=dry-run \
jreleaser deploy --dry-run
```

只使用 Python 标准库，不要引入 requests / pyyaml 等依赖；YAML 只由 JReleaser 读取，脚本不解析它。
JReleaser 版本固定在工作流的 `JRELEASER_VERSION`，升级时同步更新 README 中的命令。

## 注册契约

- 一个注册 = 一个项目 + 一个模块 + 一个版本 + 一个归档。归档里只能有一个坐标，且 artifact 必须
  等于 `module`、版本必须等于 `version`；多模块必须拆成多条注册。
- 路径就是唯一键：`registry/<owner>/<repo>/<module>.json`。字段必须与路径一致；同名模块的不同
  项目可以各自注册（路径含 owner/repo）。
- 新版本覆盖旧文件。如果旧版本尚未发布就被覆盖，旧版本不会发布；项目应只注册准备发布的版本。
- `coordinate` 可选；写了就必须与归档内容一致。JReleaser 仍会检查 namespace，坐标必须属于
  `jreleaser.yml` 的 `project.languages.java.groupId`。
- 归档推荐 `.tar.xz`（solid 压缩对重复内容更友好），也接受 `.zip` / `.tar.gz`。归档必须已签名，
  且只放 Central 需要的文件；校验和、`maven-metadata.xml` 和隐藏文件会被丢弃。
- `archive.sha256` 是必填的 64 位小写十六进制；每次运行都会重算，因此上传归档后要同步更新注册。
- 修改字段名或 `schemaVersion` 属于跨仓库破坏性变更：所有生产者都要同步升级，优先加可选字段。

## 清理语义

- 上传失败时绝不删除注册：`set -euo pipefail` 让 cleanup 步骤不执行，注册保留给下次重试。
- 上传成功后，删除清单里的**所有**注册都会被删除，包括本次上传的和本来就已发布的（被剪枝的）。
  它们的内容都已在 Central 上，保留注册只会让下次运行反复探测。
- cleanup 对比的是注册文件里的 `version` 和 `archive.sha256`，不是文件字节；重新格式化 JSON 不会
  阻止删除，替换成新版本则会跳过。
- upload job 需要 `contents: write` 才能推回删除提交；受保护分支要允许该推送。上传期间远端若
  有新提交导致 push 被拒，上传本身已经成功：再跑一次即可，已发布注册会在下一次被剪枝并删除。

## 安全约束

- **默认不上传**。只有 `workflow_dispatch` 且 `inputs.upload` 才进入 `upload` job；`bundle` job
  里 JReleaser 必须带 `--dry-run`。不要重新加定时任务。
- `upload` job 挂在 `maven-central` environment 上（建议配置 required reviewers）。
- Central 凭据只出现在 `upload` job 的 env 中，变量名由 JReleaser 的键名决定：
  `JRELEASER_DEPLOY_MAVEN_MAVENCENTRAL_HUB_USERNAME` / `..._PASSWORD`。注意 `mavenCentral` 在
  环境变量里是 `MAVENCENTRAL`，没有下划线。
- dry-run 仍需通过配置校验，所以 `bundle` job 用 `dry-run` 假凭据；dry-run 不会访问 Central。
- 不要 drop 失败的 deployment：保留给 Sonatype Support 排查。上传失败时让 job 失败，注册保持
  不动，下次运行会重试。
- 不要改用 curl 直接调 Publisher API，也不要换成官方 Maven 插件的 `PublishMojo`：前者等于重写
  JReleaser 已有的 SDK，后者只能从当前 Maven reactor 收集构件，无法上传现成的 staging 目录。

## JReleaser 行为（容易踩的点）

- `jreleaser deploy --dry-run` 会执行 `createDeploymentBundle()`，把 `stagingRepositories` 里所有
  目录合并、忽略隐藏文件和 `maven-metadata.xml`，输出
  `out/jreleaser/deploy/mavenCentral/hub/<groupId>-<project>-<version>-bundle.zip`，但不发任何请求。
- `project.version` 写在配置里就不会被环境变量覆盖，且默认按 SemVer 校验。所以 `jreleaser.yml`
  不写 version，由工作流设置 `JRELEASER_PROJECT_VERSION=0.0.<run_number>`；它只影响 bundle 文件名。
- `project.java.groupId` 已弃用，改用 `project.languages.java.groupId`。groupId 同时用作 Central
  namespace，并与 deployer 的 namespace 回退逻辑相关。
- 默认 `checksums` 为 true，会为每个构件生成 `.md5/.sha1/.sha256/.sha512` 并放进 bundle（即使
  staging 里没有）。必须显式 `checksums: false`，否则 File Count 会翻四倍。
- 上传前 JReleaser 会对每个 POM 调 Central 的 `isPublished`，发现已存在直接整个失败，不会替你跳过。
  因此预剪枝必须做对，repo1 的 POM 是唯一依据；repo1 有 CDN 延迟，刚上传完不要立刻重跑。
- `authorization: BASIC` 表示 `Bearer base64(username:password)`，正好是 Central user token 的用法；
  `BEARER` 要求 password 里放已编码 token，容易配错。

## 生产者接入

项目侧目前还没有自动化产出注册；在本仓库依赖真实注册之前，先用 `tests/make-fixtures.py` 验证流程。
接入时：

- Gradle 用 `maven-publish` 发到 `build/staging-repo`，再 `tar -C build/staging-repo -cJf
  <module>-<version>.tar.xz .`（vanniktech 没有只打包不上传的开关）；
- Maven 用 `central-publishing-maven-plugin` 的 `skipUpload=true` 生成 `central-bundle.zip`；
- 构件必须已签名；归档里只放 Central 需要的文件；上传后计算 sha256 并提交注册。
