# 外语长视频中文 AI 译配：本地生产工作流

## 1. 这份文档解决什么问题

这是一套可在 Windows、macOS 或 Linux 工作站复用的本地生产流程。输入是一个可合法处理的公开视频链接，输出是：

- 原始下载母版完整保留；除分析确认删除的广告片段外，正式工作母版画面全部保留；
- 保持源视频真实分辨率，本次为 1920×1080；
- 多角色中文 AI 配音；
- 中文语音始终使用模型生成的 1.0 原始速度，不做语音拉伸；
- 配音短则适当加速对应画面，配音长则适当放慢对应画面；
- 通过较长区块分摊极端倍率，并在区块内使用平滑时间映射；
- 原视频音轨按新时间轴保音调重定时，以低音量混入；
- 重新生成烧录字幕，同时保留可开关的中文字幕轨；
- 最终机器 QA 后生成无广告中文标题/简介、按中文成片重定位的章节，以及 16:9 和 4:3 两张封面；
- 人工关卡只保留翻译、音色和最终观看等真正需要主观判断的环节；广告处理、试听费用和“生成全片”后的全文付费调用不再额外等待批准；
- TTS 和视频渲染可从缓存续跑。

本流程是短语/句子级同步，不是嘴唇逐音素同步。它优先保证内容完整、中文自然、角色清楚和全片不累计漂移。

## 2. 本地权威文件

不要在多个说明之间猜哪个版本最新。当前按下面的顺序使用：

1. 总工作流（本文件）：`docs/LOCAL_DUBBING_WORKFLOW.md`
2. 机器可读状态模型：`docs/workflow.definition.json`
3. 高清下载 SOP：`docs/YOUTUBE_1080P_DOWNLOAD_WORKFLOW.md`
4. 广告检测、剪除与遮盖 SOP：`docs/AD_DETECTION_AND_OVERLAY_SOP.md`
5. 通用双 Agent 翻译审核 SOP：`docs/TRANSLATION_REVIEW_SOP.md`
6. 当前项目约束：`<video_id>_run/PROJECT.md`
7. 当前项目正式翻译稿、术语表、音色交接、QA 门禁和执行报告
8. 当前设备选定的媒体、ASR、TTS 与渲染执行器及版本记录

如果规则冲突，以用户对当前项目的明确决定、正式输入文件和较新的版本记录为准。不要无记录地覆盖已批准的 JSON、SRT 或成片。

开始任何生产动作前，主控必须把上述通用文件和当前 `PROJECT.md` 的路径、版本或 SHA-256 写入项目状态记录。未记录即视为没有加载工作流，不得进入翻译、付费 TTS 或渲染。

## 3. 强制 Agent 编制

流程定义 6 个长期角色，但不需要一直同时运行。质量优先翻译阶段先由 Agent T 直接翻译全文，随后审核阶段的峰值并发固定为 3 个：主控 Agent 加两个独立翻译审核 Agent。T、A、B 都是正式翻译的硬性角色；无法启动 T 时停在阶段 04，无法同时启动 A/B 时停在阶段 05。

| Agent | 何时开启 | 负责什么 | 不负责什么 |
|---|---|---|---|
| 主控 Agent | 全流程常驻 | 建目录、冻结版本、安排依赖、合并审核、处理人工关卡、记录哈希和放行结论 | 不替用户决定音色、翻译放行和最终发布 |
| 下载与媒体 Agent | 下载、ASR、TTS、渲染阶段 | yt-dlp、FFmpeg、Demucs、Whisper、说话人切分、TTS、时间映射、封装 | 不读取或传播 Cookie/API Key 内容；只接受本机路径或内存凭证 |
| 翻译 Agent T | 阶段 04 | 不参考本地旧译稿，直接根据冻结源文、说话人、上下文和术语表翻译全文 | 不审核自己的稿件，不直接形成正式稿，不为时长压缩语义 |
| 翻译审核 Agent A | 翻译候选稿完成后 | 独立全文检查中文口语、上下文、跨槽衔接、标点和 TTS 可读性 | 不直接改基准 JSON/SRT |
| 翻译审核 Agent B | 与 A 同时开启 | 独立全文检查源文忠实度、数字、否定、主客体、术语和专业逻辑 | 不参考 A 的结论，不直接改基准 JSON/SRT |
| 独立质检 Agent | 成片生成后 | ffprobe、全片解码、帧/DTS/字幕/音量/静音/哈希和抽帧抽听 | 不参与生成，避免自己证明自己的结果 |

### 强制调度

```text
主控 Agent
├─ 下载与媒体 Agent：02 → 03
├─ 翻译 Agent T：04（源文直译，全文）
├─ 翻译审核 Agent A：05（全文，独立） ┐
├─ 翻译审核 Agent B：05（全文，独立） ├─ 主控合并裁决
├─ 下载与媒体 Agent：06 → 07          ┘
└─ 独立质检 Agent：08
```

T、A、B 和主控必须是不同角色。Agent T 可以按连续 ID 分批翻译和续跑，但最终必须覆盖全文并维护统一上下文与术语状态。禁止把字幕平均切给 A/B 后各看一半；A、B 必须使用同一冻结候选并各自覆盖全文，可以分批读取，但不能漏 ID，且提交前不能读取对方结论。T 不能审核自己的稿件，主控自己复查不能冒充独立 Agent。

## 4. 必须由人完成的关卡

| 关卡 | 必须由人做的决定 | Agent 可以做的准备 |
|---|---|---|
| Cookie | 在目标 YouTube 页面导出 Netscape Cookie；只给出本机文件路径 | 只检查域名是否含 `.youtube.com`，不打印 Cookie 值 |
| 翻译放行 | 阅读按章节排版的全文中文稿，并批准与其哈希绑定的 `translation_final_vN.json/.srt` 为正式配音输入 | Agent T 直接全文翻译，两个审核 Agent 全文独立检查，主控合并、终检并生成章节阅读稿 |
| 音色 | 为每个角色选择声音并锁定映射 | 角色聚类、推荐候选、缓存试听文件；映射锁定后直接生成一分钟试听，不再单独请求费用或生成批准 |
| 最终观看 | 抽看片头、中段、片尾、高风险句和角色切换，并确认发布标题、简介、章节和两张封面 | 输出机器验收报告、抽查时间点、可播放文件和已通过发布包门禁的文案/封面 |
| 外部交付 | 如需网盘，由账号本人登录并确认目标目录 | 上传已批准的最终成片；失败只重传，不重渲染 |

Cookie 与 API Key 都是敏感凭证：不粘贴到聊天、不写进文档、不进入 Git、不出现在命令输出。API Key 只通过进程环境变量、系统密钥链、后端内存或仓库外凭证文件提供。

下载格式与原语言音轨不再是人工关卡。媒体 Agent 在 `yt-dlp -F` 和格式探测 QA 通过后，按冻结的源语言、`original (default)` 标记、真实分辨率、项目目标规格、编码/容器兼容性、帧率与码率自动排序，生成 `qa/media_format_selection_vN.json` 并直接下载。不得要求用户回复格式编号或组合；多音轨元数据冲突时先自动下载短探针并做语言验证，仍无法判定则记录机器门禁失败并报告“待修复”。

## 5. 目录约定

每条视频一个独立运行目录：

```text
<video_id>_run/
├─ source/                 # 原始画面、音频和合并后的高清源文件
├─ work/                   # ASR、时间槽、翻译 JSON/SRT、背景轨
├─ qa/                     # 审核报告、裁决、复核表、机器验收
├─ deliverables/           # 按章节中文阅读稿、标题/简介/最终章节及其他人工审阅件
│  └─ covers/              # 16:9 与 4:3 版本化 PNG 封面
├─ scripts/                # 该项目可复用的下载/翻译辅助脚本
└─ full_dub_vN/            # 正式 TTS、时间轴、视频区块和最终成片
   ├─ tts/
   │  ├─ manifest.json
   │  ├─ raw/
   │  └─ timestamps/
   ├─ video_blocks/
   ├─ original_audio_blocks/
   ├─ role_map.json
   ├─ timeline_vN.json
   ├─ video_retime_plan.json
   ├─ subtitles_zh_vN.srt
   ├─ execution_report_vN.md
   └─ <video_id>_zh_dub_vN_1080p.mp4
```

版本规则：

- 翻译、字幕、角色图、成片、发布材料和封面都使用递增版本号；
- 新版本不得覆盖已批准版本；
- 每个正式输入与输出记录 SHA-256；
- 变更角色、音色、批次边界或翻译稿后，使用新的输出目录，避免旧缓存与新计划混用。

## 6. 八阶段主流程

### 01 立项与工作流锁

输入：公开视频链接、目标语言、期望角色数量、成片规格。

主控 Agent：

1. 提取 `video_id`，建立 `<video_id>_run`；
2. 记录输入链接和用户明确的创作目标；
3. 记录本次同步策略、原声音量、字幕方式和目标分辨率；
4. 完整读取 `AGENTS.md`、本文件、`AD_DETECTION_AND_OVERLAY_SOP.md`、`TRANSLATION_REVIEW_SOP.md`、`workflow.definition.json` 和当前 `PROJECT.md`；
5. 生成 `qa/workflow_lock.json`，记录上述文件路径和 SHA-256，并固定以下字段：

```text
ad_policy = detect_then_apply_evidence_based
media_format_selection = automatic_after_probe
translation_mode = codex_agent_direct_quality_first
translation_review = two_independent_agents_full_coverage
chapter_reading_review = required_before_translation_gate
chapter_reading_layout = sentence_aligned_verbatim
chinese_tts_speed = 1.0
chinese_offline_rate = 1.0
sync_strategy = video_retime_only
preserve_all_formal_working_master_frames = true
subtitle_timeline = rebuilt_from_chinese_audio
audition_authorization = voice_selection_implies_audition
full_tts_authorization = user_generate_full_command
publication_package = required_after_final_machine_qa
publication_text_format = utf8_txt_only
cover_variants = 16x9_and_4x3
```


完成标志：项目目录存在，任务目标冻结，`qa/workflow_lock.json.status == "pass"` 且文档哈希齐全。通用规则文件、冻结输入或 `PROJECT.md` 中的项目约束发生实质变化后必须重新生成工作流锁；仅追加运行状态不应触发新的工作流锁版本。

### 02 高清下载与源文件验收

严格执行根目录的 `YOUTUBE_1080P_DOWNLOAD_WORKFLOW.md`。核心交接是：

```text
人工：目标视频页导出 Cookie
→ 媒体 Agent：yt-dlp -F
→ 媒体 Agent：自动选择画面格式和原语言音轨并记录 QA
→ 媒体 Agent：下载独立画面/音频
→ FFmpeg -c copy 合并
→ ffprobe + 全片解码 + SHA-256
```

Cookie 文件只传路径，例如：

```text
$env:USERPROFILE\Downloads\www.youtube.com_cookies.txt
```

自动选择规则：先满足项目目标分辨率和源语言；同分辨率优先剪辑兼容的 MP4/H.264 画面与 M4A/AAC 原声，再比较帧率和码率。若更高真实分辨率只提供 VP9/AV1，保留最高画质下载母版，并在需要时另建兼容工作副本，不以人工确认代替自动决策。格式探测、候选排序、选中编号、语言证据和选择理由必须写入 `qa/media_format_selection_vN.json`。

完成标志：`qa/media_format_selection_vN.json.status == "pass"`，且 `source/` 中有通过全片解码的高清 MP4，音轨语言与分辨率已确认，哈希已记录。

恢复策略：

- `googlevideo.com` 临时直链过期：只重新执行 `--get-url`；
- 独立画面或音频已完整下载：不要重复下载；
- 合并失败：保留两个独立流，只重跑 FFmpeg；
- Cookie 失效：人工重新导出，不尝试绕过 Chrome 安全机制。

### 03 广告检测、分离、转写与时间槽

媒体 Agent 顺序执行：

1. 对原始母版做初步 ASR、章节分析和全片画面抽帧/OCR 扫描；
2. 按 `AD_DETECTION_AND_OVERLAY_SOP.md` 同时识别可整段删除的口播广告和需要遮盖的画面广告；
3. 输出 `qa/ad_detection.json`，按冻结的证据决策规则自动标记 `remove`、`mask` 或 `keep`；只有语义、边界与前后文证据足够时才删除，证据不足时自动保留；
4. 生成 `qa/ad_decisions.json`、`qa/ad_overlay_plan.json` 和 `qa/source_to_edit_timeline.json`；原始母版不覆盖，另建正式工作母版；
5. 对工作母版重新运行 Demucs，分离对白与背景；
6. faster-whisper 重新生成源语言词级时间戳；
7. `make_slots.py` 按停顿生成稳定的绝对时间槽；
8. 验证 ID 连续、时间单调、句子非空和工作母版全片覆盖；
9. 生成 `qa/ad_edit_gate.json`。未检测到广告时也必须记录 `decision=no_ads_detected`，不能省略门禁。

通用示例命令（把 `<video_id>` 和源语言代码替换为当前项目值）：

```powershell
Set-Location "<工作区>\<video_id>_run"

python scripts\transcribe.py `
  work\demucs_full\htdemucs\episode_source\vocals.wav `
  --output work\asr_full `
  --model large-v3-turbo `
  --language es `
  --device cuda `
  --compute-type float16 `
  --batch-size 16

python scripts\make_slots.py `
  work\asr_full.json `
  --output work\slots_es `
  --max-seconds 8.4
```

完成标志：原始母版与工作母版分开保存，`ad_edit_gate.json.status == "pass"`，所有广告候选均已按证据规则自动决定，时间映射与遮盖计划已记录；基于工作母版的 `asr_full.json/.srt`、正式 slots 和背景轨存在并通过结构检查。广告门禁未通过时禁止开始阶段 04，但不再将广告处置暂停为人工批准关卡。

### 04 Codex 翻译 Agent 直接全文翻译

严格执行 `TRANSLATION_REVIEW_SOP.md` 的质量优先模式。主控先冻结工作母版对应的正式 slots、源文、说话人信息、术语表和用户决定，并记录 SHA-256；然后启动独立翻译 Agent T。

Agent T：

1. 不读取本地模型旧译稿或历史中文候选；
2. 先理解全片话题、人物关系和术语，再按连续 ID 分批翻译；
3. 每批保留足够的前后文，持续维护人物称呼、话题摘要和术语状态；
4. 覆盖全部稳定 ID，不增删、不重排、不合并时间槽；
5. 输出自然、完整、适合中文配音的表达，不为原时间槽压缩语义；
6. 将分批进度和最终证据写入 `qa/translation_agent_t_manifest_vN.json`。

必须产出：

```text
qa/translation_agent_t_manifest_vN.json
work/translation_candidate_agent_t_vN.json
work/translation_candidate_agent_t_vN.srt
```

候选稿完成后运行 ID、空句、数字、术语、跨槽和 JSON/SRT 结构检查。Ollama/Qwen 可作为可选辅助扫描或争议句第三参考，但不能生成默认候选稿、覆盖 Agent T 输出或替代任何 Agent。

Codex 额度不足或 Agent T 中断时保存已验证批次并等待续跑；不得把剩余片段交给本地模型混入同一候选版本。

完成标志：Agent T 与主控/A/B 角色不同；源文、术语表和候选稿哈希已记录；Agent T 全文覆盖、缺失 ID 为 0；候选 JSON/SRT 结构正确。这里产出的仍是“候选稿”，不得跳过阶段 05。

### 05 双 Agent 全文审核

严格执行 `docs/TRANSLATION_REVIEW_SOP.md`：

1. 主控核验 `translation_agent_t_manifest_vN.json`，冻结 Agent T 候选稿并记录源文、术语表、候选稿路径、哈希、条数和 ID 范围；
2. 同时开启审核 Agent A 和 B；
3. 两者都覆盖全文，独立输出问题、建议、依据和置信度；
4. 主控按 ID 合并去重，逐条查看上下文并裁决；
5. 只由主控应用修改，生成新的 `translation_final_vN`；
6. 运行术语、数字、否定、主客体、跨槽、结构和 SRT 回归；
7. 基于裁决后的 `subtitle_zh` 和批准的章节边界，生成 `deliverables/<video_id>_中文阅读版_按章节_vN.md`；
8. 生成 `qa/chapter_reading_validation_vN.json`，验证章节、槽位、文本和时轴映射；
9. 暂停并把章节阅读稿交给用户整体阅读；用户批准必须绑定阅读稿与正式翻译的 SHA-256；
10. 生成 `qa/translation_gate.json`，满足下面全部条件才解除角色识别、选音和 TTS 锁定。

```text
高严重度未解决：0
全部待定项：0
术语/数字/上下文回归：通过
JSON/SRT 结构验收：通过
Agent A 全文覆盖且报告哈希已记录：通过
Agent B 全文覆盖且报告哈希已记录：通过
Agent T/A/B/主控角色互不重复：通过
Agent T 源文直译、全文覆盖且清单哈希已记录：通过
正式翻译哈希与审核输入链一致：通过
章节阅读稿验证：通过
章节阅读稿完整且唯一覆盖全部正式字幕槽：通过
阅读稿只使用正式稿 subtitle_zh，未改写/压缩/加入朗读替换：通过
用户批准当前章节阅读稿及其绑定的正式翻译版本：是
```

#### 5.1 按章节中文阅读稿

章节阅读稿是翻译放行的强制人工审阅件，不是最终发布章节文件。生成规则：

1. 章节边界优先采用用户提供或源视频已有的章节；章节标题翻译成自然中文并记录原文。
2. 若源视频没有章节，主控可依据话题切换生成候选章节，但必须记录 `chapter_source=generated` 并允许用户在翻译放行时调整。
3. 原章节时间先通过 `qa/source_to_edit_timeline.json` 映射到正式工作母版；阅读稿同时显示工作母版时轴与原视频时轴。
4. 每个正式稳定 ID 按开始时间分配到且只分配到一个章节；缺失数和重复数都必须为 0。
5. 正文只按原顺序拼接裁决稿中的 `subtitle_zh`。允许增加章节标题、时间标签和段落换行，不允许重写、压缩、补写、删除或改变字幕顺序。
6. 默认采用 `chapter_reading_layout=sentence_aligned_verbatim`：同一章节内连续拼接相邻字幕槽，只在完整句末或章节结束处分段，不得因稳定槽边界把同一句话拆成多个阅读段落。稳定 ID 以内联不可见标记保留，每个 ID 必须且只能出现一次；验证器必须证明每槽 `subtitle_zh` 可逐字重建、章节可见正文无增删、非章节末的句中换段为 0。不得为了整句排版跨章节移动稳定 ID。
7. 只改变阅读稿换行且正式字幕/TTS 文本不变时，属于纯展示层修订，可以不重开 T/A/B；仍须生成递增版本阅读稿和验证报告，并重新取得绑定新阅读稿与未变化正式翻译哈希的用户批准。
8. 不得使用 `recording_zh` 替换显示文本，不得加入专名朗读替换，也不得提前加入尚未冻结的说话人标注。
9. 阅读稿中的工作母版时间只服务于翻译审阅。正式 TTS 生成新的中文时间轴并完成视频重定时后，发布用标题、简介和章节时间必须按渲染器的实际时间映射另行重算。

`qa/chapter_reading_validation_vN.json` 至少记录：

```text
status = pass
input_translation_path / input_translation_sha256
chapter_source / chapter_count
source_to_edit_timeline_path / source_to_edit_timeline_sha256
output_path / output_sha256
slot_count / assigned_slot_count
missing_slot_count = 0
duplicate_slot_count = 0
subtitle_text_used_verbatim = true
subtitle_text_reconstructable_per_stable_id = true
chapter_visible_text_insertions_or_deletions = 0
nonterminal_nonchapter_final_line_breaks = 0
chapter_reading_layout = sentence_aligned_verbatim
translation_rewritten = false
translation_compressed = false
recording_or_pronunciation_text_used = false
```

用户批准记录至少包含阅读稿路径与 SHA-256、正式翻译路径与 SHA-256、批准时间和明确决定。阅读稿或正式翻译任一哈希变化时，旧批准和旧 `translation_gate` 立即失效。

必须落盘以下证据：

```text
qa/translation_agent_t_manifest_vN.json
work/translation_candidate_agent_t_vN.json
work/translation_candidate_agent_t_vN.srt
qa/translation_audit_agent_a_vN.json
qa/translation_audit_agent_b_vN.json
qa/translation_audit_decisions_vN.json
qa/translation_regression_vN.json
qa/chapter_reading_validation_vN.json
qa/translation_approval_vN.json
qa/translation_gate.json
work/translation_final_vN.json
work/translation_final_vN.srt
deliverables/<video_id>_中文阅读版_按章节_vN.md
```

完成标志：`translation_gate.json.status == "pass"` 且模式为 `codex_agent_direct_quality_first`；T、A、B 角色互不重复，三者缺失 ID 均为 0，所有清单/报告和正式稿哈希匹配；章节阅读稿验证为 `pass`、完整且唯一覆盖全部槽位，并且用户批准与阅读稿及正式翻译哈希绑定的明确版本。任一条件不满足，后续阶段必须停止。

### 06 角色识别、选音与自动一分钟试听

媒体 Agent 用正式翻译稿和源视频生成 `role_map.json`。主控把角色、示例台词和候选音色放进声轨工坊。

人工步骤：

1. 打开工作台；
2. 为每个角色修改可读名称；
3. 试听本地缓存音色；
4. 为每个角色选定并锁定音色；
5. 工作台自动生成 dry-run、显示字符数与预估费用，然后直接生成一分钟试听，不再请求试听费用或生成批准；
6. 用户可检查片头回音、角色分配、语气、停顿、字幕与原声音量；
7. 用户在试听后发出“生成全片”“生成全文”或等价指令时，该指令即作为当前冻结音色、翻译和参数的全文 TTS 与渲染授权，不再追加费用确认。

MiniMax 的本地试听缓存可以反复播放，不消耗额度。锁定角色音色后生成的一分钟试听会调用接口并显示估算费用，但不设独立人工批准。若改用剪映等外部语音，使用工作台的“外部配音包”模式：下载逐句任务包，外部生成后按固定文件名回传。

完成标志：`role_map.json`、锁定的角色到音色映射和已通过机器检查的一分钟试听均存在。不需要单独的试听批准文件。

### 07 原速 TTS 与平滑画面重定时

先在仓库根目录打开 PowerShell，并把占位符替换为当前项目值。正式执行器可以由项目脚本、容器或任务编排器提供，但必须满足本节参数与门禁约束：

```powershell
$projectRoot = (Get-Location).Path
$workbench = $projectRoot
$python = "python"
$runDir = Join-Path $projectRoot "<video_id>_run"
$translation = Join-Path $runDir "work\translation_final_vN.json"
$source = Join-Path $runDir "source\working_master_vN.mp4"
$output = Join-Path $runDir "full_dub_vN"
$keyFile = "<仓库外安全路径>\tts_api_key.txt"

Set-Location $workbench
```

`your_pipeline` 是接口占位名，不是本仓库内置模块。当前设备可用项目脚本、容器或已有媒体流水线实现它；实现必须接受等价输入并产出本节列出的门禁与工件。

#### 7.1 说话人切分

```powershell
& $python -m your_pipeline `
  --translation $translation `
  --source $source `
  --output $output `
  diarize
```

输出：`role_map.json` 和可复用的说话人 embedding。

#### 7.2 全片指令后自动做 dry-run

运行 dry-run 前先验证：

```text
qa/translation_gate.json.status == pass
translation_gate.final_translation_sha256 == 本次 --translation 文件 SHA-256
角色与音色映射已锁定
已记录用户针对当前冻结输入发出的“生成全片/全文”指令
本次使用新的或与上述输入哈希完全匹配的输出目录
```

验证失败时不得通过“用户说继续”推断为放弃门禁；必须修复记录或回到上游阶段。

```powershell
& $python -m your_pipeline `
  --translation $translation `
  --source $source `
  --output $output `
  synthesize `
  --api-key-file $keyFile `
  --rpm 10 `
  --pause 0.30 `
  --dry-run
```

检查批次数、角色数量、预估计费字符、等价费用和缓存复用数。检查通过后在同一次“生成全片/全文”执行中自动去掉 `--dry-run` 继续，不再暂停等待费用或生成确认。如冻结翻译、角色、音色或 TTS 参数已变化，必须重新取得针对新输入的“生成全片/全文”指令。

#### 7.3 正式 TTS

```powershell
& $python -m your_pipeline `
  --translation $translation `
  --source $source `
  --output $output `
  synthesize `
  --api-key-file $keyFile `
  --rpm 10 `
  --pause 0.30
```

脚本行为：

- 模型 `speech-2.8-hd`；
- 中文速度固定 `1.0`；
- 逐批写入 `tts/manifest.json`；
- 已有音频与字级时间戳的 `ready` 批次自动跳过；
- 小时/频率限制会指数退避等待，最长单次等待 15 分钟，然后续跑；
- 可能已经计费但返回不确定的请求不会盲目自动重试，清单会记录 `uncertain`。

中文轨的禁止项：

- 任何 TTS 请求参数 `speed` 不等于 `1.0`；
- 对中文成品或片段使用 `atempo`、`rubberband`、`librosa.time_stretch`、采样率伪变速或其他时长拉伸；
- 为塞进原时间槽而截断有声内容、覆盖相邻台词或把逐句中文按旧绝对时间直接 `amix`；
- 为满足时长而启用旧的压缩翻译或“短句改写”，除非用户明确要求修改语义风格且重新通过翻译门禁。

允许的中文音频处理仅包括不改变语速的重采样、声道转换、去除首尾静音和极短淡入淡出；任何离线处理都必须记录 `offline_rate=1.0`。

如果更换翻译、角色、音色或批次规划，不要复用旧 `tts/`；创建新的版本输出目录。

#### 7.4 组装中文原速时间轴

```powershell
& $python -m your_pipeline `
  --translation $translation `
  --source $source `
  --output $output `
  assemble-audio `
  --inter-batch-silence 0.38
```

输出 `chinese_voice.wav`、`timeline_v5.json` 和 `subtitles_zh_v5.srt`。

#### 7.5 计算视频重定时计划

```powershell
& $python -m your_pipeline `
  --translation $translation `
  --source $source `
  --output $output `
  plan-video `
  --min-block-seconds 9 `
  --max-block-seconds 22
```

策略：

- 所有原画面完整保留；
- 中文短则画面加速，中文长则画面减速；
- 极短且倍率激进的字幕槽与相邻槽合并；
- 区块内使用三次 Hermite 单调时间映射，使边界速度连续；
- 字幕完全按新中文时间轴重建。

#### 7.5a 渲染前生产门禁

`plan-video` 完成后、`render-video` 开始前，主控必须生成 `qa/production_gate.json`。只有以下检查全部通过，状态才能为 `pass`：

```text
ad_edit_gate：pass，且工作母版/遮盖计划哈希与实际输入一致
translation_gate：pass，且正式翻译哈希与实际输入一致
TTS 请求 speed：全部 1.0
中文离线处理 offline_rate：全部 1.0
中文 tempo/stretch/filter：0 个
中文实际发声区间重叠：0 个
video_retime_plan：存在且完整覆盖全部源画面
源/目标区块：时间单调，无缺口、无重叠
字幕：由中文新时间轴生成，不复用旧绝对时间
画面叠加顺序：视频 → 广告遮盖 → 烧录字幕
所有 mask 区间：坐标、起止时间和抽帧验证均通过
```

生产门禁必须记录翻译、角色、音色、TTS manifest、中文时间轴和视频重定时计划的路径与 SHA-256。缺文件、缺字段、哈希不匹配或任一检查失败都必须停止渲染，不能只写警告后继续。

#### 7.6 渲染与封装

渲染滤镜顺序必须固定：先重定时视频，再应用 `ad_overlay_plan` 中的广告遮盖，最后烧录中文字幕；字幕是最上层。可开关中文字幕同时作为独立字幕轨封装。不得在烧录字幕后再叠加广告遮盖、角标或覆盖图形。

```powershell
& $python -m your_pipeline `
  --translation $translation `
  --source $source `
  --output $output `
  render-video `
  --original-volume 0.08
```

默认使用 NVIDIA NVENC。没有可用 NVIDIA 编码器时加 `--cpu`。脚本会复用已经完成的视频区块和原音轨区块；不要为了续跑加 `--force`，除非确认缓存损坏或计划已经改变。

完成标志：输出目录中存在可播放的最终 MP4、中文音轨、字幕、时间轴和重定时计划。

#### 7.7 成片后修正少量说话人（增量模式）

如果精细说话人审核把原槽拆成多人台词，不要重新生成全片 TTS。使用
当前设备提供的增量说话人修补执行器：

```powershell
$python = "python"

# 先核对受影响槽、请求数和费用，不调用 API
& $python scripts\your_incremental_patch.py dry-run

# 一次执行付费增量 TTS、局部重定时、混流和验收
& $python scripts\your_incremental_patch.py `
  --rpm 20 `
  --original-volume 0.08 `
  all
```

增量脚本必须满足：

- 用拆分稿自带的 SHA-256 校验正式翻译和旧角色图；
- 通过单人台词共识映射真人与既有音色，不靠姓名猜测；
- 只把发生人物切换或角色纠正的台词合并成少量付费批次；
- 每次付费响应先保存音频、trace 和时间戳 URL，再下载非计费时间戳；
- 中文始终保持 1.0×，多人切换默认保留 0.4 秒停顿；
- 新语音比旧槽长或短时，只改变包含该槽的视频区块时长；后续区块整体平移；
- 未修改中文 PCM 逐样本复用，未变化视频区块使用硬链接或文件复制复用；
- 只重新编码时长或本地字幕发生变化的区块；
- 输出到独立的 `full_dub_vN_speaker_patch/`，绝不覆盖上一版成片；
- 最终 QA 同时检查三媒体流、人物数量、111 类增量范围、PCM 复用、台词无重叠、
  全片 packet 扫描、旧版未变和 SHA-256。

如果 MiniMax 触发 1002/1039 限额，保留 manifest 自动等待；若返回完成状态不确定，
停止并按 trace 人工查账，不能盲目重试。已有 `ready` 批次再次运行只读本地缓存。

### 08 独立验收、发布包与交付

质检 Agent 至少执行：

```powershell
$final = "<工作区>\<video_id>_run\full_dub_vN\<video_id>_zh_dub_vN.mp4"

ffprobe -v error `
  -show_entries "format=filename,duration,size,bit_rate:stream=index,codec_name,codec_type,width,height,r_frame_rate,sample_rate,channels,duration,nb_frames:stream_tags=language" `
  -of json `
  $final

ffmpeg -v error -i $final -map 0:v:0 -map 0:a:0 -f null NUL

Get-FileHash -Algorithm SHA256 $final
```

#### 8.1 最终机器 QA

还要检查：

- 视频、音频、字幕三条流都存在；
- 分辨率与源视频一致；
- 成片时长与重定时计划一致；
- 字幕数量与正式翻译条数一致；
- 视频包 DTS 严格递增；
- 音轨没有削波、异常长静音或只剩单边；
- 片头、中段、片尾抽帧正常；
- 人工抽听至少覆盖角色第一次出现、高风险翻译句、快速问答和结尾。

最终机器 QA 报告必须记录成片路径、字节数和 SHA-256，且状态为 `pass`。状态非 `pass` 时不得生成正式发布包；修复成片后必须使用新版本，并使依赖旧成片哈希的章节、发布材料、封面 QA 和发布包门禁全部失效。

#### 8.2 中文标题、无广告简介与最终章节

主控只在最终机器 QA 通过后生成正式发布材料：

1. 根据源标题、源简介、批准的全文翻译、项目术语表和用户最新要求，写出自然、准确、不夸张的简体中文标题。默认提供 3—5 个候选；用户明确只要一个标题时可交付一个。
2. 中文简介保留原视频链接、主题摘要和有内容价值的嘉宾介绍；删除课程/产品推广、优惠或跟踪链接、订阅引导、站点导流、无关标签和关键词堆砌。不得把源简介附带但对白未实际提到的书或产品写成“片中提到”。
3. 外文人名、赛事名、组织名和书名沿用批准术语表与核名结果；用户要求保留拉丁字母时不得重新音译。
4. 将每个源章节时间先通过 `qa/source_to_edit_timeline.json` 映射到正式工作母版，再通过最终 `video_retime_plan` 或等价的实际映射换算到当前中文成片。不得直接复用原视频、章节阅读稿或旧成片的时间。
5. 最终章节数量与批准章节源一致，时间严格递增、第一章为 `0:00`（若项目明确另有开场规则则记录例外）、最后一章不超过成片时长；每项同时记录源时间、工作母版时间和中文成片时间以便审计。

必须产出：

```text
deliverables/<video_id>_发布材料_vN.txt
deliverables/<video_id>_中文简介与章节_vN.txt
deliverables/<video_id>_片中书单_vN.txt
qa/chapter_timeline_vN.json
qa/publication_materials_vN.json
```

面向用户的正式发布文字必须是 UTF-8 纯文本 `.txt`，不得再把 `.md` 作为正式交付件；标题、简介、重定位章节和书单还必须在交付时完整贴到聊天框。`qa/publication_materials_vN.json` 至少记录最终成片、最终机器 QA、章节映射和全部 TXT 发布材料的路径及 SHA-256，以及标题数、章节数、时间严格递增检查、原视频链接保留状态、广告/推广链接残留数、UTF-8 TXT 格式检查和外文人名规则检查结果。

#### 8.3 16:9 与 4:3 封面

每条正式成片默认生成同一视觉系列的两张 PNG：

```text
deliverables/covers/<video_id>_中文封面_16x9_vN.png   # 1920×1080
deliverables/covers/<video_id>_中文封面_4x3_vN.png    # 1440×1080
qa/cover_art_vN.json
```

规则：

- 输入可以是用户有权使用的原封面、源视频抽帧、项目人物参考图或新生成视觉；输入来源和 SHA-256 必须记录。人物身份、外文姓名和节目/频道名须与项目核名结果一致。
- 封面主标题从当前发布标题或用户指定的短标题派生，中文字样必须人工可读并逐字核对；不得出现多字、错字、伪字、重复字或未经批准的附加文案。
- 不得保留课程推广、网址、二维码、优惠信息、订阅引导、广告角标、无关标签或水印；节目自身 Logo 仅在用户有权使用且明确作为节目识别时允许保留。
- 16:9 输出固定为 `1920×1080`；4:3 输出固定为 `1440×1080`。4:3 应重新排版人物、Logo 和文字，不得仅靠中心裁切导致重要内容被截断。
- 两张图必须检查像素尺寸、宽高比、文件可解码、人物与文字完整性、安全边距、风格一致性和 SHA-256；修订时递增版本，不覆盖旧封面。

`qa/cover_art_vN.json` 至少记录生成/编辑方式、最终提示词或设计说明、参考输入、两张输出的路径/尺寸/字节数/SHA-256，以及人物、文字、广告残留、安全边距和 4:3 重排检查结果。

#### 8.4 发布包门禁与人工交付

生成 `qa/publication_package_gate_vN.json`。只有以下检查全部通过才能写为 `pass`：

```text
final_machine_qa = pass
final_media_sha256 = 当前交付成片实际 SHA-256
publication_materials_qa = pass
chapter_timeline_source_to_output_mapping_valid = true
chapter_times_strictly_increasing = true
chapter_count_matches_approved_source = true
promotional_links_remaining = 0
cover_art_qa = pass
cover_16x9 = 1920x1080 PNG
cover_4x3 = 1440x1080 PNG
cover_text_and_identity_verified = true
cover_advertising_residue = 0
all_artifact_hashes_match = true
old_versions_preserved = true
```

人工最终确认当前成片及其绑定的标题、简介、最终章节和两张封面后，才复制/上传正式交付包。用户只要求修改文案或封面时，成片、字幕和 TTS 不需要重做，但必须生成新的发布材料/封面版本并重新通过发布包门禁。上传失败不需要重新配音或重渲染，只重试上传。

## 7. 断点续跑与变更规则

| 发生什么 | 从哪里继续 | 哪些不能复用 |
|---|---|---|
| 下载直链过期 | 重新 `--get-url` | 过期直链 |
| Cookie 失效 | 人工重新导出 Cookie | 旧 Cookie |
| ASR/Agent T 翻译中断 | 已验证的连续 ID 批次和 `translation_agent_t_manifest` | 未验证的最后一批；不得用本地模型补齐同一候选版本 |
| 翻译稿修改 | 新建 `translation_final_vN+1`，从双 Agent 回归和后续重新开始 | 旧版本对应的正式 TTS/时间轴 |
| 章节阅读稿缺槽、重复或用户要求改译 | 修正正式翻译或章节映射，生成新阅读稿与验证报告并重新请求批准 | 旧阅读稿批准、旧 `translation_gate` 及其后续 TTS |
| 只改角色名 | 重新导出交接文件；音色 ID 不变时可按情况保留音频 | 与角色清单冲突的 manifest |
| 改音色或批次边界 | 新输出目录，重新 TTS | 旧 `tts/manifest.json` 和语音文件 |
| MiniMax 限额 | 保留 `tts/manifest.json`，原命令续跑 | 不要删除 ready 批次 |
| 某批返回不确定 | 人工查 trace_id 与账户记录后决定 | 不自动重试可能已计费的批次 |
| 视频渲染中断 | 原命令续跑，复用 `video_blocks/` 和 `original_audio_blocks/` | 损坏或与新 plan 不一致的区块 |
| 最终成片版本或哈希变化 | 从最终机器 QA、章节重定位、发布材料和封面 QA 重新开始 | 旧成片绑定的章节、发布材料、封面 QA 和发布包门禁 |
| 只改中文标题/简介/章节措辞 | 新建发布材料版本并重跑发布材料 QA 和发布包门禁 | 旧发布材料版本；成片/TTS/字幕可保留 |
| 只改封面设计或文字 | 新建封面版本并重跑封面 QA 和发布包门禁 | 旧封面版本；成片/TTS/字幕可保留 |
| 最终上传中断 | 从本地最终 MP4 重传 | 不重跑媒体流程 |

## 8. 费用、Token 与敏感数据

- yt-dlp、FFmpeg、Demucs、faster-whisper、可选 Ollama/Qwen 辅助检查和本地语音模型在本机运行，不产生 Codex 用量；会消耗本机时间、电力和磁盘。
- Agent T 全文直译以及 A/B 全文审核会消耗 Codex 使用额度。质量优先模式以翻译质量为第一目标；额度不足时保存进度等待恢复，不静默降低模型或切换本地初译。
- MiniMax 正式语音按语音字符或套餐规则消耗额度。每次正式生成前都先自动 dry-run 并记录估算；音色锁定后的一分钟试听不再需要单独费用批准，用户的“生成全片/全文”指令直接授权当前冻结输入的全文付费 TTS 与渲染。具体扣费以账号当时规则和接口返回为准。
- 本地缓存试听不会再次调用 MiniMax。
- 工作台参数变化不会自动调用接口，只有明确点击付费试听/生成或运行正式 `synthesize` 才调用。
- Cookie、API Key、临时媒体直链不进入文档、Git、截图、日志或网盘。

## 9. 可移植性要求

- 仓库只保存工作流、模板、脚本和空目录占位，不保存任何源媒体、成片、音频、字幕成品、运行缓存或项目 QA 历史；
- 每台设备在本机 `PROJECT.md` 中记录工具路径、模型版本、GPU/CPU 能力和实际执行器；不得把用户目录写回通用文档；
- TTS 服务、模型和下载工具可以替换，但稳定 ID、哈希链、原生 `speed=1.0`、视频重定时、广告证据决策和全部 QA 门禁不得省略；
- 执行器命令若与示例不同，以 `docs/workflow.definition.json` 的约束为准，并把差异写入当前项目记录。

## 10. 完成定义

一条新视频只有同时满足以下条件才算完成：

- [ ] `qa/workflow_lock.json` 为 `pass`，通用规则和当前项目文件的哈希与实际文件一致；
- [ ] 高清源视频与正确原语言音轨通过全片解码；
- [ ] `qa/ad_edit_gate.json` 为 `pass`；原始母版未覆盖，全部广告候选已按证据决定，删除/遮盖区间和原新时轴映射已验证；
- [ ] ASR、槽位和正式翻译 ID 连续且时间单调；
- [ ] `qa/translation_gate.json` 为 `pass` 且模式为 `codex_agent_direct_quality_first`；翻译 Agent T 直接覆盖全文，两个独立审核 Agent A/B 都覆盖全文，三者身份不同且清单/报告哈希已记录；
- [ ] 高严重度和待定项均为 0；
- [ ] 已按章节生成中文阅读稿，`qa/chapter_reading_validation_vN.json` 为 `pass`，全部正式字幕槽完整且唯一覆盖，正文逐字来自正式稿 `subtitle_zh`；
- [ ] 用户已阅读并批准与章节阅读稿及正式翻译 SHA-256 绑定的明确版本；
- [ ] 每个角色的音色已锁定，一分钟试听已生成并通过机器检查；
- [ ] 已自动记录 dry-run 的付费字符、批次数、RPM、估算费用与缓存复用，且存在针对当前冻结输入的“生成全片/全文”用户指令记录；
- [ ] `qa/production_gate.json` 为 `pass`，所有原画面完整保留，中文 TTS 与离线处理均为 1.0，中文处理链无 tempo/stretch；
- [ ] 新字幕与中文时间轴一致；
- [ ] 所有画面遮盖仅在批准区间内出现，字幕清晰位于遮盖层之上，章节与简介时间跳转已按编辑后时轴重定位；
- [ ] 原视频音轨按新时间轴处理并以批准音量混入；
- [ ] 最终 MP4 通过 ffprobe、全片解码、字幕、帧、音量与哈希验收；
- [ ] 已生成自然中文标题和无广告中文简介；原视频链接保留，推广链接、订阅引导、无关标签和关键词堆砌残留为 0；
- [ ] 最终发布章节已从原视频经工作母版映射到当前中文成片，时间严格递增、数量匹配且不超过成片时长；
- [ ] 已生成 `1920×1080` 的 16:9 PNG 封面与 `1440×1080` 的 4:3 PNG 封面；人物、外文人名、中文字样、安全边距和广告残留检查通过；
- [ ] `qa/publication_materials_vN.json`、`qa/cover_art_vN.json` 和 `qa/publication_package_gate_vN.json` 均为 `pass`，全部路径与 SHA-256 匹配当前成片和交付文件；
- [ ] 用户完成人工抽看并批准交付；
- [ ] 执行报告记录输入版本、音色、费用/字符、耗时、输出路径与哈希。
