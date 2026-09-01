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
- `templates/CODEX_PROMPT.template.md`：可直接交给 Codex 的主控提示词模板。
- `scripts/build_prompt.py`：接收视频链接和本机 Cookie 文件路径，自动校验、初始化项目并生成提示词。
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

## 链接 + Cookie 自动生成提示词

Cookie 不要上传到 GitHub，也不要把内容粘贴到 Codex。只需要在本机选择导出的 Netscape Cookie 文件，并把本机文件路径传给脚本：

```powershell
python scripts/build_prompt.py `
  --url "https://www.youtube.com/watch?v=VIDEO_ID" `
  --cookie-file "$env:USERPROFILE\Downloads\www.youtube.com_cookies.txt" `
  --source-language en
```

这条命令会自动完成：

1. 从普通链接、短链接、Shorts、Embed 或 Live 链接中提取视频 ID，并去掉播放列表和跟踪参数；
2. 只检查 Cookie 文件是否存在、是否有 Netscape 标头和 `.youtube.com` 域名，不输出任何 Cookie 值；
3. 首次运行时创建 `<video_id>_run/` 和 `PROJECT.md`；
4. 生成 `<video_id>_run/CODEX_PROMPT.md`。

`<video_id>_run/` 已被 `.gitignore` 排除，因此提示词里的本机 Cookie 路径、项目文件和后续所有媒体都不会上传。要同时在终端查看生成结果，可加 `--print`；要用新输入替换已有提示词，可加 `--force`。

Windows 下可把生成的提示词复制到剪贴板：

```powershell
Get-Content -Raw .\VIDEO_ID_run\CODEX_PROMPT.md | Set-Clipboard
```

随后把剪贴板内容作为新任务发给 Codex。生成器已经把链接、Cookie 文件路径、运行目录、人工边界、安全规则和完成条件组合完整，不需要再手写长提示词。

### 生成的主控提示词

README 中保留下面这份可人工填写的版本；脚本实际使用的权威模板是 `templates/CODEX_PROMPT.template.md`：

```text
请在当前仓库内完整执行一条长视频中文译配任务。

输入：
- 视频链接：<视频链接>
- 视频 ID：<视频 ID>
- 源语言：<源语言或 auto>
- 目标语言：zh-CN
- 本机运行目录：<video_id>_run
- YouTube Cookie 文件路径：<只写本机路径，不写 Cookie 内容>

先完整读取 AGENTS.md、docs 下的全部强制工作流文件和当前 PROJECT.md。
Cookie 已在本机提供，只验证文件、Netscape 标头和 youtube.com 域名，不输出值，
也不要再次询问链接或 Cookie 内容。自动生成工作流锁并连续执行，仅在格式/原语言音轨、
章节翻译稿、角色音色、最终观看和可选外部交付登录这些真正的人工关卡暂停。

不请求版权确认；广告按证据自动删除、遮盖或保留；锁定音色后直接生成一分钟试听；
试听后“生成全片/全文”指令直接授权当前冻结输入的全文 TTS 与渲染，自动 dry-run 后继续，
不再二次确认。翻译使用独立 T 全文直译和独立 A/B 双全文审核。中文 TTS 固定原生 1.0，
只通过画面重定时对轴。原始母版不覆盖，凭证、临时直链、媒体、QA 和成片不进入 Git。

现在从安全验证输入与生成工作流锁开始。
```

这份提示词采用“目标 + 输入上下文 + 行动边界 + 成功条件”的紧凑结构，并把长期规则留在 `AGENTS.md`，避免在每次任务里重复整套说明。该做法与[官方 OpenAI 提示词建议](https://developers.openai.com/api/docs/guides/latest-model#prompting-best-practices)中“精简提示词、每条规则只写一次、明确自主执行与批准边界”的原则一致。

### 仅手动初始化项目

如果暂时没有 Cookie，也可以只创建本地运行目录：

```powershell
python scripts/init_project.py VIDEO_ID `
  --url "https://www.youtube.com/watch?v=VIDEO_ID" `
  --source-language en
```

随后可手动生成首个工作流锁：

```powershell
python scripts/create_workflow_lock.py `
  --run-dir .\VIDEO_ID_run `
  --stage intake `
  --next-gate format
```

## 执行器说明

这个仓库是工作流与质量门禁的权威来源，不绑定某一台机器的绝对路径或某一个 TTS/ASR 实现。`docs/LOCAL_DUBBING_WORKFLOW.md` 中的 `your_pipeline` 是当前设备的执行器接口占位名。Codex 可以在每个被忽略的 `<video_id>_run/scripts/` 目录内生成项目专用执行器，也可以接入已有容器或本地工具；无论实现怎样替换，都必须满足 `workflow.definition.json` 的稳定 ID、哈希链、TTS 1.0 原速、视频重定时和 QA 条件。

## 安全边界

- 凭证只通过系统密钥链、进程环境变量、后端内存或仓库外文件提供。
- 不打印 Cookie、API Key、Authorization Header 或带签名的媒体直链。
- 不提交任何 `*_run/` 目录和任何媒体、字幕、模型、缓存、QA、发布包。
- 可能已经计费但状态不确定的 TTS 请求不得自动重试。
- 每次提交前运行 `python scripts/audit_repository.py`；检查不通过时不要推送。
