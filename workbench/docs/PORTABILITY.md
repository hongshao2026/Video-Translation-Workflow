# 项目迁移、完整性与多设备边界

GitHub 保存软件源码、工作流定义、依赖锁和测试；视频项目使用独立迁移包或私有对象存储。两者不能互相替代。

## 迁移包内容

迁移包格式为 ZIP，根目录包含 `transfer-manifest.json`。清单中的每个文件都有：

- 相对于项目根目录的 POSIX 路径；
- 文件字节数；
- SHA-256；
- 是否为媒体制品。

默认包含 `PROJECT.md`、字幕、翻译、术语表、QA、门禁、授权回执、发布文字和项目脚本等非媒体文件。

`project.json` 使用版本 2 的可迁移源绑定：公开视频页保存无凭证、非临时的原始 HTTP(S) URL；复制进项目的本地媒体只保存 `source/` 下的相对路径、大小和 SHA-256；没有复制的本地源只保存文件名/大小并标记 `rebind_required`。`googlevideo` 等临时媒体直链、签名参数、访问令牌、原设备绝对路径和凭证不会写入该文件。

默认排除：

- API Key、Cookie、私钥、凭证文件；
- `.env` 和设备本地 `workbench_config*.json`；
- `.git`、虚拟环境、`node_modules`、缓存和活动 runtime；
- 音视频、图片和成片；
- ZIP、7z、RAR 等无法安全检查内容的不透明压缩包；
- 符号链接和目录联接，避免把项目外文件意外打包。

即使开启媒体，凭证、设备绑定、活动 runtime、不透明压缩包和链接仍然排除。工作流本身禁止把 Key 或 Cookie 写入普通 JSON/文本制品；迁移工具不会打开已识别为凭证的文件。

## 导出与验证

不带媒体：

```powershell
.\scripts\export-project.ps1 `
  -ProjectRoot <项目运行目录> `
  -Archive <项目迁移包.zip>
```

带媒体：

```powershell
.\scripts\export-project.ps1 `
  -ProjectRoot <项目运行目录> `
  -Archive <项目迁移包.zip> `
  -IncludeMedia
```

迁移包必须放在项目目录之外。导出采用临时文件并在完成前再次逐文件验签。需要独立验证时运行：

```powershell
python -m backend.portability.cli verify <项目迁移包.zip>
```

## 导入与重新绑定

```powershell
.\scripts\import-project.ps1 `
  -Archive <项目迁移包.zip> `
  -Destination <新设备资料库目录\项目目录>
```

导入先验证清单、相对路径、文件大小和 SHA-256，再解压到同盘临时目录，最后原子安装。目标必须不存在或为空；工具不会覆盖已有项目。成功后生成不含绝对路径的 `.dub-workbench/import-receipt.json`。

导入只完成文件安装。要让项目出现在 Zotero 式资料库并恢复运行索引，目标必须位于 `DUB_WORKBENCH_LIBRARY_DIR` 内，然后登记：

```powershell
python -m backend.portability.cli attach <新设备资料库目录\项目目录>
```

也可以让 CLI 在导入后立即登记：

```powershell
python -m backend.portability.cli import <项目迁移包.zip> <新设备资料库目录\项目目录> --attach
```

应用 API 的等价入口是 `POST /api/library/projects/attach`，请求体只接受相对于资料库根目录的 `library_path`。登记会再次拒绝越界路径、符号链接/目录联接、重复 project/run ID、重复项目目录、被改写的 `project.json`/`workflow_lock.json`、提示词哈希不一致和不安全的 Provider URL。

登记事务从 `project.json` 与 `qa/workflow_lock.json` 恢复项目索引、原 `run_id`、冻结 Provider 配置、冻结提示词制品和八阶段基本状态。恢复的 Provider 配置没有 `credential_ref`；新设备必须使用原 profile ID 重新保存 Key。活动任务、Worker 租约、旧事件、明文凭证和 Provider 请求账本（尤其是 `uncertain` 付费请求）一律不恢复，冷迁移也不会自动启动任何任务。若源媒体没有随包迁移，项目会显示“需要重新绑定”，而不是使用旧设备绝对路径。

随后在新设备上：

1. 对标记为 `source_rebind_required` 的项目，在界面中重新选择本地视频或调用 `POST /api/library/projects/{project_id}/source-binding`；
2. 使用恢复回执列出的相同 Provider profile ID 重新输入 API Key；Cookie 同样只在新设备本地配置；
3. 重新绑定本地模型和 FFmpeg/yt-dlp；
4. 运行严格诊断与 `rebind`；
5. 确认没有其他设备正在执行同一项目后再续跑。

```powershell
.\scripts\diagnose.ps1 -RequireProject -Provider minimax -Strict
python -m backend.portability.cli rebind <新项目目录> --workbench-root . --provider minimax
```

## 多设备限制

当前迁移包是经过校验的冷快照，不是活动进程迁移。以下内容不会接管：

- 正在运行的下载、翻译、TTS 或渲染进程；
- PID、端口和临时队列状态；
- 状态不确定的付费请求；
- Codex task、hook 信任和本机通知通道。

同一项目同一时刻只能由一台设备执行。付费请求状态不确定时，必须先向供应商核对，不能因为换设备而重发。未来若支持同时在线监督，需要共享制品存储、主机租约和服务端幂等账本，不能直接让多台机器共同写一个 SQLite 文件。

登记完成不等于可以越过生产门禁。恢复器只根据在新设备上仍能通过结构与哈希验证的门禁推进八阶段骨架；缺失媒体或哈希失配会保守停在较早阶段。任何无法安全验证的恢复都会整体回滚 SQLite 登记并报错。

## GitHub 发布前检查

在 `dub_workbench` 仓库中至少确认：

```powershell
git status --short --ignored
git check-ignore .venv_media\Scripts\python.exe
git check-ignore backend\data\minimax_previews\catalog\manifest.json
git check-ignore .env.local
npm test
python -m pytest tests\test_portability.py
```

不得使用未经检查的 `git add .`。先确认待提交列表中没有媒体、模型、虚拟环境、运行数据库、API Key、Cookie 或机器绝对路径。
