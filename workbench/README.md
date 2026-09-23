# 声轨工坊

声轨工坊是一个本地优先的视频翻译、配音和发布工作台。它把视频资料库、下载与媒体探测、T/A/B/C 翻译审核、角色选音、原生 1.0 倍速语音、画面重定时、机器 QA 和发布材料组织成可恢复的八阶段运行。

产品界面采用类似 Zotero 的资料库结构：左侧筛选项目，中间浏览视频和运行，右侧检查当前项目；后台任务集中在任务坞中。设置只负责管理 Provider、工作流预设和设备绑定，项目一旦开始运行就冻结所用配置，不会在处理中静默换模型或换语音服务。

> 当前状态：多项目资料库、持久化任务、Provider 抽象、请求账本、门禁、自动音频时间线、渲染和新版工作台界面均已有代码与自动化测试。2026-09-23 已用本机凭证完成 MiniMax 模型目录、最小 LLM 生成、音色目录、原生 `speed=1.0` 最小语音生成与只读视频任务查询；净化证据见 [MiniMax 连通性报告](reports/minimax_connectivity_2026-09-23.json)。同日还从全新 ASCII 临时副本完成真实前后端、SQLite、SSE、表单、键盘和窄屏浏览器验收，见 [浏览器 QA 报告](reports/browser_qa_2026-09-23.json)。这些软件级 smoke test 不等于某条完整长视频已经通过项目级生产验收。

## 能力边界

当前代码提供：

- 视频链接或本地文件进入资料库，项目内使用相对路径组织源文件、工作稿、QA 与交付物；
- 远程下载会冻结可用的源语言字幕选择并随任务下载、登记 SRT/VTT；没有可用字幕时明确停在“导入字幕或设备 ASR”，且不持久化临时字幕 URL；
- 内嵌字幕提取、广告剪除/无广告 remux、工作母版校验和封面 PNG 抽帧均为可恢复的 FFmpeg Worker 任务；
- 八阶段工作流锁、SQLite 任务状态、真实分母进度和 SSE 事件流；
- OpenAI-compatible 与 MiniMax 大模型适配器；
- OpenAI-compatible 与 MiniMax 同步语音适配器；
- T 翻译、A 中文表达审核、B 语义忠实审核、C 裁决的独立角色绑定；
- 正式中文 TTS 固定 `speed=1.0`，对轴只允许画面重定时；
- 从批准翻译与音色锁自动生成 TTS 段、48 kHz 中文整轨、SRT 和完整画面重定时计划；
- Provider 请求幂等账本，对结果不确定的可能计费请求停止自动重发；
- 工作流锁、广告、翻译、生产和发布包门禁的结构与哈希校验；
- 项目迁移包的导出、验签、导入和新设备重新绑定；
- 音色锁定后的有界一分钟试听，以及旧版选音、MiniMax 和外部配音包制作台兼容入口。

仍需按具体项目或设备完成：

- 下载源站所需的 Cookie 由使用者在本机提供，不进入 Git 或迁移包；
- 没有远程或内嵌字幕的视频需要在当前设备绑定 ASR 工具后导入其 SRT/VTT/JSON；工作台不会静默安装模型；
- 本地 Qwen3-TTS、Kokoro、CosyVoice 的模型、CUDA 与 Torch 环境按设备单独安装；
- 云端服务的模型名、音色 ID、价格和额度由对应供应商决定；
- 多设备采用“冷迁移 + 单 Worker 接管”，不是共享 SQLite 的同时在线协作；
- 最终机器 QA 通过后仍需人工抽看，才能视为正式批准。

## 安装

推荐使用仅含 ASCII 字符的短目录，例如 `C:\work\dub-workbench`。Python 和媒体处理支持 Unicode 项目名，但当前 Vinext Windows 工具链对包含中文或其他非 ASCII 字符的源码安装路径存在已知兼容性问题，可能在 `dev` 或 `build` 阶段错误解析模块路径。项目资料库仍可放在中文目录；源码与 Node 构建目录建议使用 ASCII 路径，直到上游问题被验证解决。

系统要求：

- Windows 11 / PowerShell 7；
- Python `3.12.x`；
- Node.js `22.13+`；
- FFmpeg 与 FFprobe；
- 下载网络视频时需要 yt-dlp。

在 `dub_workbench` 目录运行：

```powershell
.\scripts\bootstrap.ps1
.\scripts\diagnose.ps1 -Strict
```

默认安装开发与测试依赖。只安装核心运行依赖可使用 `-Profile core`；需要 CPU 媒体分析依赖可使用 `-Profile media`。不要从旧设备复制 `.venv`、`.venv_media` 或 `node_modules`。

更完整的设备绑定说明见 [安装与重新绑定](docs/INSTALLATION.md)。

## 启动

```powershell
.\start_workbench.ps1
```

也可以在需要兼容旧项目配置时指定：

```powershell
.\start_workbench.ps1 -ProjectConfig <设备本地项目配置.json>
```

启动成功后访问 `http://127.0.0.1:3000/`；后端 API 位于 `http://127.0.0.1:8765/`。关闭工作台：

```powershell
.\stop_workbench.ps1
```

## 首次配置

1. 打开“设置 → 翻译服务”，创建 LLM Provider 配置并执行连接探测。
2. 打开“设置 → 语音服务”，创建语音 Provider 配置并执行连接探测。
3. API Key 只交给本机后端，并保存到系统密钥链。若当前系统没有可用密钥链，后端会明确拒绝保存；仅在用户显式设置 `DUB_CREDENTIAL_BACKEND=memory` 时才使用重启即失效的内存凭证。
4. 导入视频，创建工作流运行，并为 T/A/B/C 和语音分别冻结 Provider 配置。
5. 在项目检查器和任务坞查看真实进度、人工关卡和错误恢复动作。

Provider 的配置字段、接口和安全约束见 [Provider API](docs/PROVIDER_API.md)。

## 测试

运行 Python 自动化测试：

```powershell
python -m pytest
```

运行前端构建与渲染契约测试：

```powershell
npm test
```

静态检查：

```powershell
python -m ruff check .
npm run lint
```

严格环境诊断：

```powershell
.\scripts\diagnose.ps1 -Strict
```

仅当本机已经安全配置相应凭证时，才执行真实 Provider 连通性测试。连接探测使用非生成端点；一次最小生成测试仍可能产生费用。任何测试都不得把 Key、Cookie、Authorization 头或临时媒体直链写入日志、截图、测试夹具或 Git。

MiniMax 的有界验证脚本默认只探测模型与音色目录；明确加 `--live` 时仅发送一条极短文本和一条原生 `speed=1.0` 语音，且不会自动重试：

```powershell
python scripts/verify_minimax_connectivity.py --credential-file <本机密钥文件> --region cn --live --probe-video
```

净化后的报告和短音频写入 Git 忽略的 `runtime/connectivity/`。`--probe-video` 只读取已有视频任务列表，不创建视频生成任务；整个脚本不会发布视频或上传项目媒体。

## 多设备迁移

GitHub 只保存软件源码、工作流定义、依赖锁与测试，不保存视频项目。项目状态通过带清单与逐文件 SHA-256 的迁移包转移：

```powershell
.\scripts\export-project.ps1 `
  -ProjectRoot <项目运行目录> `
  -Archive <项目迁移包.zip>

.\scripts\import-project.ps1 `
  -Archive <项目迁移包.zip> `
  -Destination <资料库目录\新项目目录>

python -m backend.portability.cli attach <资料库目录\新项目目录>
```

默认不包含媒体；明确需要时才在导出命令增加 `-IncludeMedia`。API Key、Cookie、设备配置、活动进程和运行数据库不会进入迁移包。`attach` 会严格校验项目目录、`project.json`、工作流锁和哈希链，然后恢复原 project/run ID、冻结配置与八阶段骨架到新设备 SQLite；不会恢复任务、租约、凭证或 Provider 请求账本。新设备仍须用相同 Provider profile ID 重新保存 Key、重新绑定缺失源媒体/工具/本地模型，并确认旧设备已经停止该项目的 Worker。也可使用 `python -m backend.portability.cli import <包> <资料库项目目录> --attach` 一步导入并登记。

详见 [项目迁移与多设备边界](docs/PORTABILITY.md)。

## 文档导航

- [产品定义与当前范围](PRODUCT.md)
- [界面与交互契约](UX-CONTRACT.md)
- [系统架构](docs/ARCHITECTURE.md)
- [Provider API](docs/PROVIDER_API.md)
- [完整生产工作流](docs/LOCAL_DUBBING_WORKFLOW.md)
- [双 Agent 翻译审核 SOP](docs/TRANSLATION_REVIEW_SOP.md)
- [广告检测与遮盖 SOP](docs/AD_DETECTION_AND_OVERLAY_SOP.md)
- [事件驱动执行 SOP](docs/EVENT_DRIVEN_EXECUTION_SOP.md)
- [机器可读工作流定义](docs/workflow.definition.json)
- [视觉设计系统](DESIGN.md)

## 安全原则

- 不提交 API Key、Cookie、私钥、凭证文件、运行数据库、模型或媒体；
- 远程自定义 Provider 必须使用 HTTPS，本机 `localhost` 才允许 HTTP；
- Provider 配置和运行快照不含密钥明文；
- 正式任务不自动切换 Provider；配置变化必须创建新的运行；
- 付费请求结果不确定时进入阻塞状态，先向供应商核对，禁止自动重试；
- 工作流门禁和产物哈希是放行依据，界面上的静态说明或预览数据不是生产证据。
