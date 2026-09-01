# 翻译 Agent 直译与双 Agent 强制审核 SOP

## 1. 适用范围与默认模式

本 SOP 适用于本工作区内所有外语视频的中文字幕和中文配音稿。默认采用质量优先模式，不依赖本地小模型生成第一版中文。

```text
冻结源文、稳定 ID、说话人与术语表
→ 启动独立翻译 Agent T 直接翻译全文
→ 结构检查并冻结 Agent T 候选稿
→ 同时启动审核 Agent A 和 B
→ A、B 各自独立覆盖全文
→ 主控合并去重并逐条裁决
→ 生成新版本并做全片回归
→ 按章节生成中文阅读稿并做完整性验证
→ 用户通读后发出明确修改意见或下游执行指令；下游指令自动绑定当前版本
→ translation_gate=pass
→ 才能进入角色、音色与 TTS
```

Agent T、Agent A、Agent B 和主控必须是不同角色。翻译者不能审核自己的稿件，主控不能伪装成独立审核 Agent，A/B 不能各审一半后合称全文审核。

如果 Codex Agent 暂不可用、额度不足或任务中断，必须保存进度并暂停/续跑。不得静默降级为本地模型初译。只有用户明确要求切换“额度优先模式”并重新生成 `workflow_lock.json` 后，才允许使用本地候选稿；双 Agent 全文审核仍不能取消。

## 2. 角色与独立性

### Agent T：直接翻译

Agent T 只接收冻结的源文、稳定 ID、说话人信息、上下文、项目术语表和用户明确决定，不读取本地模型旧译稿或其他历史中文候选，避免被低质量措辞锚定。

Agent T 必须：

- 覆盖全部 ID，不增删、不重排、不合并时间槽；
- 先理解话题、人物关系和全片术语，再按连续 ID 分批翻译；
- 每批读取足够的前后文，持续维护人物称呼、话题摘要和术语状态；
- 输出自然、口语化、适合中文配音的完整表达；
- 不为塞进原时间槽而删减事实、逻辑、语气或限定条件；
- 对不确定源文标记问题与依据，但仍给出最保守的完整候选；
- 记录 Agent 标识、模型、推理档位、源文/术语表哈希、覆盖范围、缺失 ID 和候选稿哈希。

Agent T 产出的候选稿经结构检查并冻结后，不再由 T 自行复审或直接修改。后续问题交给 A/B 和主控处理。

### Agent A：中文表达与上下文

Agent A 在不知道 B 结论的情况下全文检查：

- 中文是否自然、口语化、易听懂；
- 前后槽是否衔接，代词、问答和人物口吻是否一致；
- 是否存在残句、翻译腔、生造词、搭配不当、过度书面表达或标点误导；
- 录音稿是否适合自然朗读，但不得为了原视频时长强行压缩语义。

### Agent B：语义与扑克专业性

Agent B 在不知道 A 结论的情况下全文检查：

- 是否忠实于源文，数字、否定、程度、时间、因果、主客体是否正确；
- 扑克术语和策略逻辑是否专业、一致；
- 专名、缩写、比赛形式、牌型、位置、下注动作和资金单位是否正确；
- 是否有跨槽错位、漏译、重复或把相邻人物台词混在一起。

### 主控

主控负责冻结输入、为 T/A/B 提供相同的权威源文和术语标准、核验全文覆盖、合并 A/B 报告、查看上下文、裁决、生成正式新版本和执行回归。

A/B 只提交问题与建议，不直接修改冻结候选 JSON/SRT。A/B 提交前不能读取对方报告；两者都必须覆盖全部 ID，可以分批读取，但必须给出完整覆盖证明。

## 3. 翻译原则

- 准确性优先级：语义准确 > 扑克专业准确 > 上下文连贯 > 中文自然 > 字数或原时间槽长度。
- 中文时长超出原槽是视频重定时信号，不是删减翻译的理由。
- 项目术语表是冻结输入。以用户批准版本为准；例如扑克语境中的 `downswing` 使用“下风期”，不得写成“下跌期”。
- `spot` 等多义词按牌局语境翻成“局面”“节点”等自然表达，不能机械逐词替换。
- 字幕稿与录音稿分开。用户指定只在录音中替换的专名，只进入 TTS 派生稿，不得污染批准字幕稿。
- 不确定项必须由 A、B 读取至少前后各 5 条后独立给出候选和依据，再由主控裁决；不能把“建议回听”当成最终状态。

## 4. 本地模型的限定用途

本地 Ollama/Qwen 不再生成默认候选翻译。它只可在 Agent T 候选稿冻结后用于：

- ID、空句、数字、单位和标点的结构扫描；
- 源文与中文的漏译、重复和跨槽错位提示；
- 术语表机械回归；
- A/B 意见冲突时提供第三个参考候选。

本地模型的输出只能进入机器 QA 或争议参考文件，不能覆盖 Agent T 候选稿、不能直接形成正式稿，也不能替代任何一个 Agent。

## 5. 必须产出的证据

每次翻译与审核至少产生：

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

Agent T 清单至少记录：Agent 标识、模型、推理档位、冻结源文路径及 SHA-256、术语表路径及 SHA-256、总条数、ID 范围、实际覆盖 ID、缺失 ID、分批进度、候选稿路径及 SHA-256。

两份审核报告至少记录：审核 Agent 标识、模型、推理档位、冻结源文和候选稿 SHA-256、总条数、ID 范围、实际覆盖 ID、问题 ID、建议、依据、严重度和置信度。

主控裁决表至少记录：每个唯一问题 ID、A/B 意见、最终决定、修改前后文本、裁决依据和正式稿版本。

## 6. 按章节中文阅读稿与自动记录批准

主控完成裁决与全文回归后，必须先生成供用户连续阅读的章节稿，不能直接要求用户批准 JSON/SRT。

生成要求：

- 输入必须是准备放行的 `translation_final_vN.json`，并记录其 SHA-256；
- 章节优先使用用户提供或源视频已有章节；没有章节时可按话题切换生成候选边界，但必须标明 `chapter_source=generated`；
- 原章节时间必须通过当前正式 `source_to_edit_timeline` 映射到工作母版，同时保留原视频时间供核对；
- 每个稳定 ID 必须且只能分配到一个章节，顺序不得改变，缺失和重复均为 0；
- 阅读正文只能逐条拼接正式稿的 `subtitle_zh`，只允许增加标题、时间标签和段落换行；不得改写、压缩、补写或删除；
- 默认采用 `chapter_reading_layout=sentence_aligned_verbatim`：同一章节内连续拼接相邻稳定槽，只在完整句末或章节结束处分段，不能因为字幕槽边界把同一句话拆成多行；稳定 ID 以内联不可见标记完整、唯一、按序保留，不得跨章节移动；
- 验证报告必须证明每个 `subtitle_zh` 可按稳定 ID 逐字重建、章节可见正文无增删、非章节末的句中换段为 0。只改换行且正式字幕/TTS 文本不变属于纯展示层修改，可以不重开 T/A/B，但必须生成递增阅读稿并使旧批准失效；用户针对新展示版本发出的明确下游执行指令可自动形成新的哈希绑定批准；
- 不得使用 `recording_zh`、TTS 发音替换或尚未冻结的说话人标注污染阅读稿；
- 阅读稿时间属于工作母版审阅时轴，不得冒充视频重定时后的最终发布章节时间。

必须产出：

```text
deliverables/<video_id>_中文阅读版_按章节_vN.md
qa/chapter_reading_validation_vN.json
qa/translation_approval_vN.json
```

验证报告必须记录正式翻译、章节来源、时轴映射和阅读稿的路径及 SHA-256，并证明章节数、槽数、分配数、缺失数、重复数和文本一致性。当前阅读稿与正式翻译已完整展示且不存在待决修改时，用户说“开始选音色”“启动工作台”“进入选音”或等价的明确下游执行指令，即授权当前展示版本；主控自行计算并同时绑定阅读稿与正式翻译的路径及 SHA-256，记录原始指令、时间和 `capture_mode=explicit_downstream_command`，不得要求用户复制哈希或追加“我批准”。任一输入变化时旧记录立即失效，必须展示新版本后重新捕获明确下游指令。泛泛的“继续”只有在当前版本明确、紧邻完整展示且无其他可能指向时才可按等价下游指令处理；否则不得推断批准。

### 6.1 成片后的标题、简介与发布章节

发布标题、简介和最终章节属于成片后的发布材料，不是阶段 05 的章节阅读稿，也不替代 `translation_gate`：

- 中文标题和简介以批准的正式翻译、项目术语表、源标题/简介及用户最新要求为依据，可以为了发布可读性进行摘要和自然改写，但不得歪曲主题、人物关系、数字或结论；默认标题候选为 3—5 个，用户明确要求一个时可交付一个。
- 外文人名、赛事名、组织名和书名必须复用批准的核名/术语结果；用户要求保留拉丁字母时不得重新音译或混用拼写。
- 简介必须删除与内容无关的广告、课程/产品推广、优惠或跟踪链接、订阅/站点导流、无关标签和关键词堆砌；保留原视频链接和有内容价值的主题/嘉宾信息。
- 最终章节标题可以自然翻译，但章节时间必须在最终机器 QA 通过后根据当前成片的实际重定时映射重新计算。章节阅读稿中的工作母版时间只用于翻译审阅，不能复制到发布简介。
- 发布材料的纯展示层修改不需要重新启动 T/A/B 或使正式翻译失效；但必须创建新的 `deliverables/<video_id>_发布材料_vN.md` 和 `qa/publication_materials_vN.json`，记录输入、输出和哈希。若修改反向影响字幕或 TTS 文本，则仍按第 8 节重新进入翻译审核。

## 7. `translation_gate.json` 放行门

只有下列条件全部满足，状态才能写为 `pass`：

- T、A、B 是三个不同的 Agent，且主控不冒充其中任何一个；
- Agent T 直接从冻结源文完成全文翻译，缺失 ID 为 0；
- T 的源文/术语表哈希与项目冻结输入一致，候选稿和清单哈希已记录；
- A、B 使用同一个冻结候选稿哈希，且各自覆盖全部 ID，缺失 ID 均为 0；
- A/B 两份报告存在且 SHA-256 已记录；
- 主控裁决覆盖两份报告的全部唯一问题 ID；
- 高严重度未解决数为 0，全部待定项为 0；
- 术语、数字、否定、主客体、跨槽、结构和 SRT 回归全部通过；
- 正式 JSON/SRT 的版本和 SHA-256 已记录；
- `chapter_reading_validation_vN.json.status == "pass"`，全部稳定 ID 完整且唯一覆盖，正文逐字来自正式稿 `subtitle_zh`；
- 章节阅读稿采用 `sentence_aligned_verbatim`，每槽文本可逐字重建，章节可见正文增删为 0，非章节末句中换段为 0；
- 章节阅读稿、验证报告和时轴映射的路径及 SHA-256 已记录；
- 用户批准记录同时绑定章节阅读稿与正式翻译的路径及 SHA-256，且批准由当前版本展示后的明确下游执行指令自动捕获。

建议结构：

```json
{
  "schema_version": 3,
  "status": "pass",
  "mode": "codex_agent_direct_quality_first",
  "translator": {
    "id": "agent-t",
    "model": "...",
    "reasoning_effort": "...",
    "coverage": "0-1592",
    "missing": 0,
    "source_sha256": "...",
    "glossary_sha256": "...",
    "candidate": "work/translation_candidate_agent_t_vN.json",
    "candidate_sha256": "...",
    "manifest": "qa/translation_agent_t_manifest_vN.json",
    "manifest_sha256": "..."
  },
  "reviewers": [
    {"id": "agent-a", "coverage": "0-1592", "missing": 0, "report": "...", "report_sha256": "..."},
    {"id": "agent-b", "coverage": "0-1592", "missing": 0, "report": "...", "report_sha256": "..."}
  ],
  "roles_are_distinct": true,
  "unresolved_high": 0,
  "unresolved_total": 0,
  "regression_status": "pass",
  "final_translation": "work/translation_final_vN.json",
  "final_translation_sha256": "...",
  "chapter_reading": {
    "path": "deliverables/<video_id>_中文阅读版_按章节_vN.md",
    "sha256": "...",
    "validation": "qa/chapter_reading_validation_vN.json",
    "validation_sha256": "...",
    "status": "pass",
    "missing_slots": 0,
    "duplicate_slots": 0,
    "subtitle_text_used_verbatim": true
  },
  "user_approval": {
    "artifact": "qa/translation_approval_vN.json",
    "artifact_sha256": "...",
    "reading_sha256": "...",
    "final_translation_sha256": "...",
    "capture_mode": "explicit_downstream_command",
    "command": "开始选音色",
    "approved": true
  }
}
```

状态不是 `pass`、角色不独立、章节阅读稿未通过、明确下游指令未被自动绑定到当前哈希、文件缺失或哈希与生产输入不一致时，角色识别、选音、付费 TTS 和渲染全部锁定。只要绑定条件满足，主控必须自动生成批准记录与门禁并进入用户要求的下游阶段，不得追加一轮批准问答。

## 8. 中断、额度与修改回归

Agent T 可以按连续 ID 分批落盘和续跑。每批完成后记录范围与候选哈希；续跑时复用已验证批次，从第一个缺失 ID 开始，不重复翻译已经冻结的批次。

Codex 额度不足时保持当前状态并等待恢复，不能把剩余片段交给本地模型后混入同一候选版本。若用户明确切换模式，必须新建候选版本、记录不同生成来源，并重新运行完整 A/B 审核。

正式翻译一旦修改，必须：

1. 新建递增版本，不覆盖旧版；
2. 使旧 `translation_gate.json` 立即失效；
3. 对受影响条目及全片术语/结构重新回归；
4. 任何语义或术语修改重新进入 A/B 独立审核；
5. 重新生成按章节中文阅读稿与验证报告；
6. 展示新阅读稿；用户发出明确下游执行指令时，自动绑定新阅读稿和新正式翻译哈希，并生成新的 `translation_gate.json`；
7. 翻译哈希变化后，不得复用与旧翻译绑定的 TTS 或时间轴缓存。

只有纯展示层修改且不改变字幕/TTS 文本时，才可以不重开翻译审核；原因和影响范围仍须写入项目状态记录。
