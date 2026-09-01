# Video Translation Workflow

这是一个可跨设备复用的长视频中文译配工作流仓库。它保存流程规则、SOP、项目模板和安全辅助脚本，不保存任何源视频、成片、音频、字幕成品、Cookie、API Key、模型权重或具体项目运行记录。

## 能完成什么

流程覆盖：高清源文件获取、广告检测与证据化自动处理、源语言转写、稳定字幕槽、独立 Agent 全文翻译、双 Agent 全文审核、章节阅读稿、角色与音色锁定、原生 1.0 语速 TTS、仅通过画面重定时对轴、字幕/软字幕封装、全片机器 QA 和发布材料生成。

人工只在真正需要主观判断或账户交互时介入：必要的 Cookie 导出、格式/语言选择、翻译通读、角色音色选择、最终观看，以及可选的外部交付登录。版权确认、广告逐项批准、锁定音色后的试听批准和“生成全片/全文”指令后的二次确认均不属于门禁。

## 仓库内容

- `AGENTS.md`：给 Codex 或其他编排 Agent 的强制执行规则。
- `docs/workflow.definition.json`：机器可读的阶段、角色和门禁定义。
- `docs/LOCAL_DUBBING_WORKFLOW.md`：完整生产流程。
- `docs/TRANSLATION_REVIEW_SOP.md`：翻译、双审核和章节阅读稿规范。
- `docs/AD_DETECTION_AND_OVERLAY_SOP.md`：广告检测、自动决策、剪辑和遮盖规范。
- `docs/YOUTUBE_1080P_DOWNLOAD_WORKFLOW.md`：高清源文件下载与验收 SOP。
- `templates/PROJECT.template.md`：新视频项目模板。
- `scripts/init_project.py`：创建本机运行目录。
- `scripts/create_workflow_lock.py`：生成或核验工作流锁。
- `scripts/audit_repository.py`：提交前检查敏感信息、媒体、大文件和项目数据。

## 新设备快速开始

先安装 Git、Python 3.11+、FFmpeg/ffprobe、Node.js 和 yt-dlp。Demucs、faster-whisper、说话人识别模型及 TTS 引擎按设备能力选择；模型权重必须放在仓库外。

```powershell
git clone https://github.com/hongshao2026/Video-Translation-Workflow.git
Set-Location Video-Translation-Workflow
python -m pip install -r requirements-core.txt
python scripts/audit_repository.py
```

初始化一条视频的本地运行目录：

```powershell
python scripts/init_project.py VIDEO_ID `
  --url "https://www.youtube.com/watch?v=VIDEO_ID" `
  --source-language en
```

该命令创建 `<video_id>_run/`。整个目录已被 `.gitignore` 排除，不会被上传。随后生成首个工作流锁：

```powershell
python scripts/create_workflow_lock.py `
  --run-dir .\VIDEO_ID_run `
  --stage intake `
  --next-gate format
```

在 Codex 中打开仓库并使用下面的执行请求即可：

```text
请完整读取 AGENTS.md、docs 下的全部强制工作流文件和 VIDEO_ID_run/PROJECT.md，
严格按机器门禁与断点续跑规则处理这个视频。任何 Cookie、API Key、临时媒体直链、
源媒体、音频、字幕成品、QA 运行记录和成片都不得提交到 Git。
```

## 执行器说明

这个仓库是工作流与质量门禁的权威来源，不绑定某一台机器的绝对路径或某一个 TTS/ASR 实现。`docs/LOCAL_DUBBING_WORKFLOW.md` 中的 `your_pipeline` 是当前设备的执行器接口占位名。Codex 可以在每个被忽略的 `<video_id>_run/scripts/` 目录内生成项目专用执行器，也可以接入已有容器或本地工具；无论实现怎样替换，都必须满足 `workflow.definition.json` 的稳定 ID、哈希链、TTS 1.0 原速、视频重定时和 QA 条件。

## 安全边界

- 凭证只通过系统密钥链、进程环境变量、后端内存或仓库外文件提供。
- 不打印 Cookie、API Key、Authorization Header 或带签名的媒体直链。
- 不提交任何 `*_run/` 目录和任何媒体、字幕、模型、缓存、QA、发布包。
- 可能已经计费但状态不确定的 TTS 请求不得自动重试。
- 每次提交前运行 `python scripts/audit_repository.py`；检查不通过时不要推送。
