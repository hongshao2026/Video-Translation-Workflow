# Provider API 与适配器契约

本文描述声轨工坊的 Provider 配置、HTTP API 和 Python 适配器边界。接口目前面向本机工作台，不是承诺长期兼容的公网 API；修改字段时应同步更新前端、测试与本文。

## 1. 设计目标

Provider 层把工作流与具体厂商分开，但不把不同厂商假设成完全相同。统一层只提供两类最小能力：

- `LLMProvider`：连接探测、模型列表、非流式文本生成；
- `SpeechProvider`：连接探测、音色列表、费用估算、同步语音生成。

统一层同时强制：

- 配置与凭证分离；
- 远程地址使用 HTTPS；
- 运行开始后冻结 Provider；
- 结构化输出在本地严格解析；
- 正式中文语音固定 `speed=1.0`；
- 可能计费的请求进入持久化幂等账本；
- 不支持的能力明确报错，不静默降级或换厂商。

## 2. 内置适配器

| `provider_id` | `service_kind` | 协议 | 默认地址 |
| --- | --- | --- | --- |
| `openai-compatible` | `llm` | `GET /models`、`POST /chat/completions` 的保守公共子集 | 必填 |
| `minimax-llm` | `llm` | MiniMax Chat Completions 兼容端点 | `https://api.minimax.cn/v1` |
| `openai-compatible-speech` | `speech` | `GET /models`、`POST /audio/speech` | 必填 |
| `minimax-speech` | `speech` | MiniMax 音色列表与同步语音 | `https://api.minimax.cn` |

国际账号可在 Provider 设置中覆盖为 `https://api.minimax.io/v1`（LLM）或 `https://api.minimax.io`（语音）。

`openai-compatible` 当前使用 Chat Completions 风格，不是 OpenAI Responses API 的通用实现。其他厂商即使自称兼容，也需要用连接探测和最小真实生成验证模型名、结构化输出、鉴权和错误语义。

查询当前安装的适配器：

```http
GET /api/providers/catalog
```

保存配置后，读取该配置实际可见的模型或音色目录：

```http
GET /api/providers/profiles/{profile_id}/catalog
```

该接口使用本机凭证执行非生成目录查询。若语音厂商没有音色目录端点，返回配置中的显式 `voices`；响应不含凭证。

## 3. Provider 配置

创建或更新配置：

```http
POST /api/providers/profiles
Content-Type: application/json
```

LLM 示例：

```json
{
  "id": "llm-primary",
  "service_kind": "llm",
  "provider_id": "openai-compatible",
  "display_name": "主翻译模型",
  "base_url": "https://provider.example/v1",
  "model": "model-name",
  "api_key": "仅本次请求出现的密钥",
  "config": {
    "structured_output_mode": "prompt_json",
    "model_listing": true
  },
  "enabled": true
}
```

MiniMax 语音示例：

```json
{
  "id": "speech-main",
  "service_kind": "speech",
  "provider_id": "minimax-speech",
  "display_name": "MiniMax 正式配音",
  "model": "<当前账号可用模型>",
  "api_key": "仅本次请求出现的密钥",
  "config": {},
  "enabled": true
}
```

规则：

- `id` 只能使用字母、数字、点、下划线、冒号和连字符，长度 3–120；
- MiniMax 可省略 `base_url`，其他内置兼容适配器必须显式填写；
- 远程地址只接受 HTTPS；`localhost`、`127.0.0.1`、`::1` 可用 HTTP；
- 地址不能包含用户名、密码、查询参数或 URL fragment；
- `config` 不能包含 `api_key`、`authorization`、`headers`、`token`、`secret` 等敏感字段；
- 更新配置时省略 `api_key` 会保留已有凭证引用；传入新的 Key 会替换本机凭证。

响应不会返回 Key 或内部凭证引用，只返回：

```json
{
  "id": "llm-primary",
  "service_kind": "llm",
  "provider_id": "openai-compatible",
  "display_name": "主翻译模型",
  "base_url": "https://provider.example/v1",
  "model": "model-name",
  "config": {},
  "capability": {},
  "enabled": true,
  "credential": {
    "configured": true,
    "persistent": true,
    "backend": "os_keyring"
  }
}
```

实际响应还可能包含不敏感的创建/更新时间字段。前端必须容忍这些扩展字段。

列出配置：

```http
GET /api/providers/profiles
```

删除一个配置的凭证（不删除配置本身）：

```http
DELETE /api/providers/profiles/{profile_id}/credential
```

## 4. 凭证生命周期

后端默认使用操作系统密钥链，服务名为 `dub-workbench`，配置表只保存形如 `provider:<profile_id>` 的不透明引用。若 Python 环境没有可用的 `keyring` 后端，服务会拒绝保存，避免用户误以为凭证已经持久化。只有显式设置 `DUB_CREDENTIAL_BACKEND=memory` 时才使用进程内存：

- API 响应中 `persistent=false`；
- 服务重启后需要重新输入 Key；
- SQLite、项目文件和迁移包仍不保存明文 Key。

`.env.example` 只是变量名说明，不会自动加载。旧兼容接口可能读取 `MINIMAX_API_KEY` 等进程环境变量；新版 Provider 配置应通过本机凭证代理保存。任何方式都不得把 Key 提交到 Git。

## 5. 连接探测

```http
POST /api/providers/profiles/{profile_id}/probe
```

探测被设计为非生成请求：LLM/OpenAI-compatible speech 使用模型列表，MiniMax speech 使用音色列表。成功响应包含延迟、数量和能力快照：

```json
{
  "profile": {},
  "report": {
    "ok": true,
    "provider_id": "minimax-speech",
    "profile_id": "speech-main",
    "latency_ms": 180,
    "message": "连接、凭证与音色目录验证成功",
    "model_count": null,
    "voice_count": 58,
    "capabilities": {}
  }
}
```

探测成功只证明当时的地址、凭证和目录端点可用，不证明生成端点、余额、指定模型或指定音色一定可用。发布前仍需一次最小真实生成测试。

## 6. LLM 配置选项

`openai-compatible` 支持以下非敏感选项：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `models_path` | `/models` | 非生成模型列表端点 |
| `chat_path` | `/chat/completions` | 非流式生成端点 |
| `model_listing` | `true` | 是否声明支持模型列表 |
| `structured_output_mode` | `none` | `none` / `prompt_json` / `json_object` / `json_schema` |
| `max_tokens_field` | `max_tokens` | `max_tokens` 或 `max_completion_tokens` |
| `tool_calls` | `false` | 能力声明；当前翻译流程不依赖 |
| `multimodal_input` | `false` | 能力声明；当前翻译流程输入为文本槽 |

MiniMax LLM 默认覆盖为：

- `chat_path=/chat/completions`；
- `models_path=/models`；
- `max_tokens_field=max_completion_tokens`；
- `structured_output_mode=prompt_json`。

正式翻译需要结构化输出。`prompt_json` 会把 JSON Schema 注入 system 消息，并在本机严格解析结果；`json_object` 和 `json_schema` 只有供应商实际支持时才可启用。解析失败不能把普通文本当作合格结果继续门禁。

## 7. 语音配置选项

`openai-compatible-speech` 使用同步 `/audio/speech` 契约，常用选项：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `models_path` | `/models` | 探测端点 |
| `speech_path` | `/audio/speech` | 生成端点 |
| `model_listing` | `true` | 是否支持模型列表 |
| `voices` | `[]` | 厂商未提供目录时，显式配置的音色数组 |
| `audio_formats` | `mp3,wav,flac,opus,aac,pcm` | 声明支持的格式 |
| `price_per_million_characters` | 未设置 | 可选费用估算单价 |
| `currency` | `USD` | 费用估算币种 |

每个 `voices` 元素可为字符串，或对象：

```json
{
  "voice_id": "voice-name",
  "name": "显示名称",
  "description": "可选说明",
  "category": "configured",
  "language": "zh-CN"
}
```

MiniMax speech 的模型、音色与价格可能随账号和供应商政策变化，代码不应在文档中写死“永远可用”的模型名或数量。以探测报告和本次真实测试为准。

## 8. 冻结到工作流运行

创建运行：

```http
POST /api/library/projects/{project_id}/runs
Content-Type: application/json
```

```json
{
  "preset_id": "quality-zh-v1",
  "role_bindings": {
    "T": "llm-primary",
    "A": "llm-review-a",
    "B": "llm-review-b",
    "C": "llm-judge"
  },
  "speech_profile_id": "speech-main"
}
```

运行快照记录 Provider ID、服务类型、模型、接口地址、能力和创建时是否已绑定凭证；不会复制 Key。执行任务时后端再次读取当前配置并与快照比较。任何关键字段变化都会拒绝当前运行，用户必须创建新运行。

翻译任务要求 T/A/B/C 四个绑定全部存在：

```http
POST /api/workflows/runs/{run_id}/translate
Content-Type: application/json
```

```json
{
  "slots_path": "work/source_slots.json",
  "glossary_path": "work/glossary.json",
  "project_context": "可选项目上下文",
  "version": 1,
  "batch_size": 40
}
```

所有路径必须相对项目根目录，绝对路径或 `..` 越界会被拒绝。

### 本地媒体任务（不调用 Provider）

下列端点同样创建 SQLite 持久化任务，由本地 Worker 持有 FFmpeg；它们不调用大模型或语音 Provider：

```text
POST /api/workflows/runs/{run_id}/ingest/embedded-subtitle
POST /api/workflows/runs/{run_id}/ad-edit
POST /api/workflows/runs/{run_id}/cover-source
```

内嵌字幕任务冻结 SRT/VTT、稳定 ID JSON 和纯文本投影。广告任务只接受已经完成语义/画面证据校验的 `remove/mask/keep` 决定：`remove` 生成新的音画同步工作母版；`no_ads_detected` 也执行独立 remux，禁止硬链接或复用原母版路径；遮盖留到最终渲染。封面源帧任务校验 `ad_edit_gate` 中的工作母版路径与 SHA-256 后才抽取 PNG。所有任务拒绝覆盖版本化制品，重启恢复只复用输入签名和输出哈希完全一致的结果。

语音任务：

```http
POST /api/workflows/runs/{run_id}/synthesize
Content-Type: application/json
```

```json
{
  "segments_path": "work/tts_segments_v1.json",
  "authorization_path": "qa/full_tts_authorization_v1.json",
  "translation_gate_path": "qa/translation_gate.json",
  "profile_id": "speech-main",
  "output_dir": "full_dub_v1/tts"
}
```

后端先验证翻译门禁与全文授权，再执行 dry-run、缓存复用和逐片段生成。

### 一分钟试听

音色锁定后，`voice-lock` 已经构成当前冻结翻译、音色映射和语音 Provider 的付费试听授权；不需要再提交费用确认。启动本次版本的有界试听：

```http
POST /api/workflows/runs/{run_id}/audition
Content-Type: application/json
```

```json
{
  "version": 1
}
```

后端只接受由同一版本 `voice-lock` 自动生成的 `work/tts_segments_vN.json`，按角色全覆盖、约一分钟和固定字符上限确定性选段。任务类型为 `provider.speech.audition`，进度可通过 `GET /api/jobs/{task_id}` 或事件流观察。所有请求保持 Provider 原生 `speed=1.0`，并使用请求账本和试听专用 TTS manifest 续跑；可能已计费但结果不确定时进入 `blocked_uncertain`，不会自动重发。

成功结果位于：

```text
work/audition_segments_vN.json
qa/audition_authorization_vN.json
auditions/audition_vN/tts/manifest.json
auditions/audition_vN/audition_vN.wav
auditions/audition_vN/audition_manifest_vN.json
```

当前试听输出是经 FFmpeg 组装和完整解码验证的 48 kHz mono PCM WAV（音频样片，不生成视频）。试听授权范围固定为 `audition_current_inputs`，不能用于全文 TTS；全文仍只由 `generate-full` 的明确“生成全片/生成全文”指令授权。版本制品存在且绑定一致时直接复用；同一版本输入不同或输出无法验证时拒绝覆盖，必须使用新版本。

## 9. 请求账本与幂等性

每个可能计费的 LLM/语音请求在发送前写入 `provider_requests`：

- 配置、模型与规范化输入的 SHA-256；
- 幂等键；
- `sending/completed/failed/uncertain` 状态；
- 供应商请求标识、用量和不含密钥的错误元数据。

如果调用方没有提供幂等键，工作台根据配置与输入哈希生成稳定键。发现相同键时：

- 已完成：从已验证制品恢复，禁止再次计费；
- 发送中或不确定：进入阻塞，禁止重发；
- 明确失败：需要修复证据和新任务，不能在原任务上盲重试。

查看账本：

```http
GET /api/providers/requests
GET /api/providers/requests?task_id={task_id}
GET /api/jobs/{task_id}
```

账本不得保存 Authorization 头、Key、Cookie 或完整临时媒体 URL。

## 10. 错误语义

Provider 适配器将错误归一为认证、限流、无效请求、响应格式、网络传输、不支持能力等类别。错误对象可能包含：

- `code`：安全错误码；
- `status_code`：供应商业务或 HTTP 状态；
- `trace_id`：供应商追踪标识；
- `retryable`：技术上是否可重试；
- `uncertain_completion`：供应商是否可能已经受理。

`retryable=true` 不代表工作台应自动重试。只要 `uncertain_completion=true` 或请求可能已计费，任务都必须进入 `blocked_uncertain`，由用户先核对供应商状态。

## 11. 新增适配器

新增厂商时：

1. 继承 `LLMProvider` 或 `SpeechProvider`；
2. 在 `ProviderCapabilities` 中只声明真实验证的能力；
3. 通过统一传输层发送请求，不在日志中输出头或密钥；
4. 将厂商错误映射为 Provider 错误并正确设置 `uncertain_completion`；
5. 在 `backend/providers/registry.py` 显式注册；
6. 在 API catalog 中增加公开元数据；
7. 为探测、成功、认证失败、限流、格式异常、超时和密钥不落盘编写测试；
8. 使用最小真实请求验证后，才在 README 中标记为已验证。

禁止扫描目录并自动加载任意 Python Provider 插件。扩展必须经过代码审查和显式注册。

## 12. 最小验证顺序

安全的发布验证顺序是：

1. 使用模拟传输运行单元测试；
2. 保存配置，确认 API 响应和 SQLite 不含 Key；
3. 执行非生成 `probe`；
4. LLM 发送要求固定短 JSON 的最小请求；
5. 语音生成极短测试文本，保存到 Git 忽略的 runtime/outputs 目录；
6. 校验返回格式与非空字节；
7. 检查请求账本只有哈希、用量和追踪 ID；
8. 清点 `git status`，确认没有凭证或测试媒体待提交。

真实请求可能计费。只有明确授权且本机凭证可用时执行；不要为了测试自动选择更贵的模型或创建长视频。
