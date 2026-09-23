# 声轨工坊架构

## 1. 总览

声轨工坊是一个只绑定本机回环地址的前后端应用：Vinext/React 提供资料库界面，FastAPI 提供业务 API，本地 Worker 执行长任务，SQLite 保存索引、任务、事件和 Provider 请求账本，视频项目目录保存可迁移的正式制品。

```text
浏览器（127.0.0.1:3000）
        │ HTTP + SSE
        ▼
FastAPI（127.0.0.1:8765）
  ├─ 资料库 / 工作流 / Provider API
  ├─ 兼容制作台 API
  ├─ SQLite 状态与请求账本
  └─ LocalTaskRunner ── FFmpeg / FFprobe / yt-dlp
           │
           ├─ LLM Provider（T/A/B/C）
           ├─ Speech Provider（speed=1.0）
           └─ 项目目录（源文件、work、qa、deliverables）
```

应用当前没有登录、租户或公网鉴权。启动脚本必须继续绑定 `127.0.0.1`；不要把 8765 端口直接暴露到局域网或互联网。

## 2. 目录职责

| 目录 | 职责 | 是否进入 Git |
| --- | --- | --- |
| `app/` | React/Vinext 资料库、设置、兼容制作台 | 是 |
| `backend/providers/` | Provider 中立模型、HTTP 传输和内置适配器 | 是 |
| `backend/workbench/` | 资料库、SQLite、任务、工作流、门禁、渲染、迁移边界 | 是 |
| `backend/schemas/` | 门禁 JSON Schema | 是 |
| `backend/prompt_packs/` | 版本化内置提示词包 | 是 |
| `backend/app.py` | FastAPI 组合根与旧制作台接口 | 是 |
| `tests/` | Python 与前端渲染契约测试 | 是 |
| `docs/` | SOP、架构、Provider 与安装文档 | 是 |
| `runtime/`、`outputs/` | 临时运行数据和测试生成物 | 否 |
| `.venv*`、`node_modules/` | 设备依赖 | 否 |

项目资料库默认不在源码目录中。Windows 默认位置是用户视频目录下的 `DubWorkbench`；状态数据库默认位于 `%LOCALAPPDATA%\DubWorkbench`。

## 3. 前端

`app/WorkbenchShell.tsx` 提供资料库主壳：

- 三栏资料库与项目检查器；
- 项目搜索/筛选和导入；
- 翻译、语音、预设、存储和诊断设置；
- 任务坞与 SSE 连接；
- 后端不可用时明确标记的预览数据；
- 进入旧版制作台的兼容入口。

`app/page.tsx`、`MiniMaxPanel.tsx`、`WorkflowPanel.tsx` 仍承载旧版单项目制作台、MiniMax 试听与静态流程地图。迁移期间两套表面共存；新功能应优先落在资料库 API，旧接口只为已验证流程提供兼容。

前端默认后端地址为 `http://127.0.0.1:8765`，可通过构建环境的 `NEXT_PUBLIC_DUB_API` 改写。因为后端没有公网鉴权，不应把该值指向不可信远程服务。

### Vinext Windows 路径限制

当前 Vinext 工具链在 Windows 上对包含中文或其他非 ASCII 字符的源码路径存在已知兼容风险，可能在开发服务器或构建阶段错误解析模块路径。发布和新设备安装应把源码放在短 ASCII 路径，例如：

```text
C:\work\dub-workbench
```

项目资料库可以使用中文项目名，但源码、`.vinext`、`node_modules` 和构建输出建议保持在 ASCII 路径。该建议应保留到上游版本在中文路径上完成明确验证。

## 4. 后端组合

`backend/app.py` 创建 FastAPI 应用，并挂载 `create_workbench_router()` 返回的新工作台路由。主要服务对象：

- `WorkbenchSettings`：解析状态、资料库、数据库和外部工具路径；
- `WorkbenchDatabase`：初始化 SQLite、执行事务、序列化 JSON 与发出事件；
- `MediaArtifactJobs`：持久化执行内嵌字幕提取、广告工作母版生成和正式工作母版 PNG 抽帧；每次操作先冻结相对路径与输入哈希，失败不伪造门禁；
- `CredentialBroker`：默认使用系统密钥链；只有测试或明确的临时会话才允许显式启用进程内存凭证；
- `ProjectLibrary`：创建项目目录、清单、安全相对路径，以及验证后冷迁移项目的重新索引；
- `WorkflowService`：种子预设、八阶段运行与工作流锁；
- `MediaService`：媒体探测、格式选择和下载处理器；
- `ProductionJobs`：把冻结运行连接到翻译/语音管线；
- `LocalTaskRunner`：持久化任务、协作暂停/取消和 Worker 租约。

后端启动时恢复被中断任务：普通本地任务进入 `repair_required`；可能计费的 LLM/语音任务进入 `blocked_uncertain`。恢复不会自动重新发送 Provider 请求。

## 5. 状态与项目文件的分工

### SQLite 保存“现在发生什么”

核心表包括：

- `projects`：资料库索引和项目相对位置；
- `workflow_presets`：版本化提示词包元数据；
- `provider_profiles`：不含密钥的 Provider 配置；
- `workflow_runs`：阶段、Provider 锁和提示词锁；
- `stage_runs`：阶段状态；
- `tasks`：长任务、进度、错误和结果；
- `provider_requests`：可能计费请求的幂等账本；
- `artifacts`：产物索引；
- `events`：SSE 的追加事件；
- `worker_leases`：每个运行的单 Worker 租约。

SQLite 使用 WAL，适合单机进程并发，不适合多台机器通过同步盘共同写。

### 项目目录保存“什么可以审计和迁移”

典型项目布局：

```text
<project>/
  PROJECT.md
  project.json
  source/
  work/
  qa/
  deliverables/
  full_dub_vN/
```

工作流锁、门禁、正式翻译、章节阅读稿、TTS 清单、渲染计划和发布材料都写入项目目录，并通过路径与 SHA-256 互相绑定。任务数据库可以重建索引，但正式放行不应只依赖数据库行。

所有生产任务输入都使用项目相对路径。后端解析后必须确认结果仍位于项目根目录，拒绝绝对路径和目录穿越。

## 6. 工作流运行

创建运行时，`WorkflowService`：

1. 读取内置 `quality-zh-v1` 提示词包；
2. 校验所选 Provider 配置存在且启用；
3. 冻结 T/A/B/C、语音配置、模型、接口与能力；
4. 冻结提示词包版本和 SHA-256；
5. 建立八阶段记录；
6. 对 SOP、机器定义和 `PROJECT.md` 计算 SHA-256；
7. 写入 `qa/workflow_lock.json`。

任务执行前再次比较当前 Provider 配置与冻结快照。配置发生变化时拒绝执行，而不是继续使用新配置。

### 翻译数据流

```text
冻结源文 + 术语 + 项目上下文
            │
            ▼
        T 分批翻译
            │ 候选稿冻结
       ┌────┴────┐
       ▼         ▼
   A 全文审核  B 全文审核
       └────┬────┘
            ▼
         C 全文裁决
            │
            ▼
结构/稳定 ID/术语回归 → 章节阅读稿 → 用户批准 → translation_gate
```

A 与 B 使用独立输入包并覆盖全部稳定 ID。C 只裁决 T/A/B 产生的问题。工作流不把本地旧译稿当作 T 的底稿，也不为原时间槽压缩语义。

### 语音与渲染数据流

```text
translation_gate + 音色锁
            │
            ├─ 有界一分钟试听（独立授权范围）
            │
            ▼
明确“生成全片/全文”指令 + 自动生成的 TTS 段落
            │
            ▼
      dry-run / 费用估算
            │
            ▼
speed=1.0 逐段生成 + 缓存 + 请求账本
            │
            ▼
48 kHz 中文整轨 + SRT + 完整画面重定时计划
            │
            ▼
 广告遮盖 + 字幕顶层 + FFmpeg 渲染
            │
            ▼
 成片 → ffprobe / DTS / 全片解码 QA → 发布包
```

`backend/workbench/audio_timeline.py` 从批准翻译、音色锁和已验证的逐段语音自动生成整轨、字幕与连续重定时计划；`backend/workbench/rendering.py` 再生成滤镜图、生产门禁并执行机器 QA。资料库界面已经调用对应的试听、全文 TTS 与渲染 API；任何一次具体项目是否可放行，仍由该项目的门禁和端到端证据决定，不能从“按钮存在”推断。

## 7. Provider 与请求账本

Provider 配置通过显式注册表创建，不能从项目目录动态执行插件。适配器接收运行时解析出的 Key，但序列化对象和 API 响应不含 Key。

`with_provider_ledger()` 包装 LLM/语音实例。每次生成先根据规范化输入计算 SHA-256 并保留幂等键，再发送网络请求。成功记录用量和供应商请求 ID；明确失败记录安全错误；网络结果不确定则锁定该键并阻止重放。

详细字段和扩展规则见 [PROVIDER_API.md](PROVIDER_API.md)。

## 8. 任务执行与租约

`LocalTaskRunner` 使用本机线程池执行处理器，任务状态始终先写 SQLite。处理器通过：

- `ProgressReporter.exact()` 报告真实分母；
- `checkpointed()` 报告离散检查点；
- `indeterminate()` 报告不可量化等待；
- `TaskToken.checkpoint()` 在安全位置响应暂停/取消。

每个工作流运行同一时刻只允许一个 Worker 租约。Worker 周期性续租；另一设备或进程发现租约有效时把任务置为 `waiting_worker`，不重复执行。租约减少误操作，但不是跨互联网的分布式锁；迁移前仍应关闭旧设备进程。

## 9. 事件流

SQLite 每次重要状态变化追加 `events`。`GET /api/events` 以 SSE 发送：

```text
id: 123
event: task.updated
data: { ... }
```

端点支持查询参数 `after` 和 `Last-Event-ID`，空闲约 15 秒发送注释心跳。前端收到事件后重新读取权威项目/任务状态，而不是把事件对象直接当作完整状态。

SSE 使用命名事件；前端必须注册相应监听或统一转发。只设置 `EventSource.onmessage` 不一定能收到命名事件，这一项属于必须执行的联调测试。

## 10. 门禁与验证

`backend/workbench/gates.py` 负责：

- 加载并校验门禁 JSON Schema；
- 对制品路径与 SHA-256 做语义检查；
- 生成句对齐、可逐槽重建的章节阅读稿；
- 验证稳定 ID 完整、唯一和顺序一致；
- 把用户明确下游指令绑定到当前翻译/阅读稿哈希。

`backend/workbench/rendering.py` 负责：

- 检查视频重定时完整覆盖正式工作母版；
- 把广告遮盖从源时轴映射到成片时轴；
- 固定“画面 → 遮盖 → 烧录字幕”的层级；
- 生成生产门禁和机器 QA 命令；
- 检查流、分辨率、帧率、时长、DTS 与全片解码结果。

JSON Schema 通过是必要条件，不是充分条件；代码还执行跨字段、文件存在与哈希绑定检查。

## 11. 多设备迁移

迁移分为两条通道：

```text
设备 A Git checkout ──push/pull──> 设备 B Git checkout
设备 A 项目目录 ──迁移包/私有对象存储──> 设备 B 项目目录
设备 A 凭证 ──不迁移；在设备 B 重新输入
```

迁移包默认排除媒体、密钥、Cookie、设备配置、活动 runtime、数据库、压缩包和符号链接；每个条目使用相对路径、大小和 SHA-256 验证。可显式选择包含媒体，但仍不包含敏感或设备级状态。导入后的项目只能从资料库根目录内执行 `attach`：后端重新校验项目清单、工作流锁与阶段制品，恢复同一个项目/运行标识和冻结 Provider 元数据，同时明确不恢复凭证、旧设备活动任务、Worker 租约或状态不确定的付费请求。

当前不支持：

- 正在运行的进程热迁移；
- 多设备共享一个 SQLite；
- 自动解决两台机器同时修改同一项目；
- 自动迁移供应商计费状态；
- Codex task、hook 或通知通道迁移。

## 12. 配置

新版核心读取：

| 环境变量 | 含义 |
| --- | --- |
| `DUB_WORKBENCH_STATE_DIR` | 状态目录 |
| `DUB_WORKBENCH_LIBRARY_DIR` | 项目资料库根目录 |
| `DUB_WORKBENCH_DB` | SQLite 文件 |
| `DUB_FFMPEG` | FFmpeg 可执行文件 |
| `DUB_FFPROBE` | FFprobe 可执行文件 |
| `DUB_YT_DLP` | yt-dlp 可执行文件 |
| `DUB_WORKER_ID` | 本机 Worker 名称前缀 |
| `DUB_CREDENTIAL_BACKEND` | `keyring`（默认）或 `memory` |

兼容启动脚本和旧制作台还可能读取 `DUB_PROJECT_CONFIG`、`DUB_WORKBENCH_PYTHON` 与本地模型路径。所有环境值都是设备绑定，不应进入项目迁移包。

## 13. 测试分层

- 单元测试：Provider 解析、任务状态、格式选择、门禁、渲染计划、迁移安全；
- API/集成测试：资料库、配置、任务、SSE 和凭证不落盘；
- 前端契约：Vinext build、关键文案/结构、浏览器存储约束和实际 HTTP/SSE 联调；
- 真实 smoke test：一个最小 LLM 生成和一个最小语音生成；
- 项目端到端：下载、T/A/B/C、阅读稿批准、TTS、渲染、机器 QA、发布包、人工抽看。

前四层通过仍不代表任意视频项目已经完成最终交付；项目端到端必须逐门禁留存证据。

## 14. 已知集成风险

- 新资料库前端与后端字段必须保持一致；静态渲染测试不能替代实际 HTTP 与浏览器验证；
- SSE 使用命名事件；回归测试必须继续覆盖断线后 `Last-Event-ID` 续接；
- 密钥链依赖未安装或系统后端不可用时，默认启动会失败并给出修复指引；仅显式设置 `DUB_CREDENTIAL_BACKEND=memory` 才启用重启即失效的临时凭证；
- 云 Provider 的接口、模型和价格会变化，连接/生成验证应在发布时重跑；
- Vinext 中文源码路径兼容性尚未完全解决；
- 八阶段操作已从资料库 UI 接到后端任务 API，但完整长视频是否通过仍必须以项目级端到端门禁和人工抽看为准；
- 广告语义/画面检测与无字幕视频的 ASR 仍由上游证据流程或设备绑定工具提供；工作台只执行已经校验的广告决定，不把机械 FFmpeg 任务冒充语义检测；
- 旧制作台与新版 Provider 配置暂时共存，不能把旧内存凭证状态误认为新版配置已经保存。
