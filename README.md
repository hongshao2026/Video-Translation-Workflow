# Video Translation Workflow

这是一个可跨设备复用的长视频中文译配工作流与本地工作台仓库。它保存流程规则、SOP、项目模板、安全辅助脚本，以及 `workbench/` 中可运行的 Zotero 式视频资料库软件；不保存任何源视频、成片、音频、Cookie、API Key、模型权重或具体项目运行记录。

## 能完成什么

流程覆盖：高清源文件获取、广告检测与证据化自动处理、源语言转写、稳定字幕槽、独立 Agent 全文翻译、双 Agent 全文审核、章节阅读稿、角色与音色锁定、原生 1.0 语速 TTS、仅通过画面重定时对轴、字幕/软字幕封装、全片机器 QA 和发布材料生成。

人工只在真正需要主观判断或账户交互时介入：必要的 Cookie 导出、翻译通读、角色音色选择、最终观看，以及可选的外部交付登录。阅读稿展示后，“开始选音色”“启动工作台”等明确下游指令自动批准当前展示的翻译版本，系统自行绑定哈希，不再要求用户复制 SHA 或重复确认。格式/原语言音轨在探测 QA 后按冻结规则自动选择并继续下载；版权确认、广告逐项批准、锁定音色后的试听批准和“生成全片/全文”指令后的二次确认也都不属于门禁。

## 仓库内容

- `workbench/`：完整本地工作台；包含 React/Vinext 前端、FastAPI 后端、SQLite 任务状态、可替换 LLM/语音 Provider、下载、T/A/B/C、门禁、TTS、自动音频时间线、渲染、发布包与迁移工具。
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

先安装 Git、Python 3.12、FFmpeg/ffprobe 和 Node.js 22.13+。本地可选 ASR/TTS 模型按设备能力安装，模型权重必须放在仓库外。

```powershell
git clone https://github.com/hongshao2026/Video-Translation-Workflow.git
Set-Location Video-Translation-Workflow
python scripts/audit_repository.py
Set-Location workbench
.\scripts\bootstrap.ps1
.\scripts\diagnose.ps1 -Strict
.\start_workbench.ps1
```

启动后访问 `http://127.0.0.1:3000/`。在设置中分别保存翻译与语音 Provider；API Key 只进入本机系统密钥链。内置支持 MiniMax 和保守的 OpenAI-compatible 契约，不同项目可冻结不同模型或厂商。工作台的完整说明见 [workbench/README.md](workbench/README.md)。

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
也不要再次询问链接或 Cookie 内容。自动生成工作流锁并连续执行；格式探测通过后按冻结规则
自动选择画面和原语言音轨并直接下载；章节翻译稿展示后，明确的选音/工作台指令自动绑定并批准当前版本，仅在需要修改翻译、角色音色、最终观看和可选外部交付登录暂停。

不请求版权确认、格式组合确认或翻译哈希复述；广告按证据自动删除、遮盖或保留；锁定音色后直接生成一分钟试听；
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

工作流 schema 10 使用事件驱动执行，详见 [执行与轮询约束](docs/EVENT_DRIVEN_EXECUTION_SOP.md)。`scripts/workflow_runtime.py` 串联本地机器步骤，原任务通过完成/失败事件恢复；正常运行中的进度不调用模型。`scripts/build_review_packet.py` 生成绑定原文件哈希的精简源文/审核包，保持稳定 ID 和文本不变。

初始化项目级反轮询护栏：

```powershell
python scripts/install_workflow_hooks.py --workspace .
```

随后在 Codex `/hooks` 审阅并信任新钩子。安装器不改变宿主信任或审批设置。钩子不能截获已有终端会话的 `write_stdin`；执行器用独立进程持有长任务，避免给模型留下媒体轮询会话。不能宣称钩子是完全的工具隔离。

执行计划包含 `schema_version=1`、唯一 `job_id`、绑定 `path/sha256/status` 的 `workflow_lock` 和 `steps`。每个机器步骤声明 `id`、`kind=machine`、参数数组 `command`、`requires`、`outputs`、`timeout_seconds`、`effects` 和有限重试。命令可用 `{python}` 表示当前 Python。语义/人工步骤使用 `kind=agent/human`、`receipt`、`requires` 和 `outputs`；回执必须绑定事件 ID 和计划哈希。生产执行器仍负责原有完整广告/翻译/音色/授权/速度/生产门禁，不可用通用运行器的成功状态替代。

```powershell
python scripts/workflow_runtime.py start --run-dir .\VIDEO_ID_run --plan .\VIDEO_ID_run\work\machine_plan_v1.json --notify-thread CURRENT_TASK_UUID
```

事件会通过本地 `codex queue` 提交给指定的现有任务，记录接受和消费两种状态；不确定提交不重试。任务响应后使用 `ack-event --job JOB_PATH --event-id EVENT_UUID` 确认消费；完成所需语义/用户决定并生成回执后用 `resume --job JOB_PATH` 继续。失败恢复还需要 `--repair-evidence PATH`；不确定付费请求不能用该命令重试。没有通知通道时仅可明确选择 `--manual-events`，并报告需要人工恢复。

所有运行状态、事件、日志和断点位于被忽略的 `<video_id>_run/runtime/`。首次加载规范后保留上下文的续跑只校验哈希；单纯阶段变化不使工作流锁失效。`create_workflow_lock.py --rules-root PATH` 也支持含 `dub_workbench/docs` 的本地工作区。已存在的项目与用户批准不会被安装或规范更新自动重写。

`workbench/` 是本仓库默认、可测试的生产执行器；根目录脚本继续提供无界面的初始化、事件驱动编排和安全审计。`docs/LOCAL_DUBBING_WORKFLOW.md` 中保留的 `your_pipeline` 只代表第三方或项目专用执行器兼容接口，不再表示本仓库缺少实现。无论使用默认工作台还是外部执行器，都必须满足稳定 ID、哈希链、TTS 1.0 原速、视频重定时和 QA 条件。

## 安全边界

- 凭证只通过系统密钥链、进程环境变量、后端内存或仓库外文件提供。
- 不打印 Cookie、API Key、Authorization Header 或带签名的媒体直链。
- 不提交任何 `*_run/` 目录和任何媒体、字幕、模型、缓存、QA、发布包。
- 可能已经计费但状态不确定的 TTS 请求不得自动重试。
- 每次提交前运行 `python scripts/audit_repository.py`；检查不通过时不要推送。
