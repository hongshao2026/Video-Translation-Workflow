请在当前仓库内完整执行一条长视频中文译配任务。

输入：
- 视频链接：{{SOURCE_URL}}
- 视频 ID：{{VIDEO_ID}}
- 源语言：{{SOURCE_LANGUAGE}}
- 目标语言：{{TARGET_LANGUAGE}}
- 本机运行目录：{{RUN_DIR}}
- YouTube Cookie 文件路径：{{COOKIE_FILE}}

执行要求：

1. 先完整读取 `AGENTS.md`、`docs/LOCAL_DUBBING_WORKFLOW.md`、`docs/TRANSLATION_REVIEW_SOP.md`、`docs/AD_DETECTION_AND_OVERLAY_SOP.md`、`docs/workflow.definition.json` 和 `{{RUN_DIR}}/PROJECT.md`，以这些文件作为唯一通用流程来源。
2. Cookie 已由用户在本机提供。只验证文件存在、Netscape 标头和 `.youtube.com` 域名；不得输出、复制、记录或提交任何 Cookie 值。不得要求用户再次粘贴 Cookie 内容。
3. 自动创建或刷新 `qa/workflow_lock.json`，然后按流程连续执行。优先复用哈希匹配的缓存；普通状态更新不得导致工作流锁反复失效。
4. 只在格式/原语言音轨、章节翻译稿、角色音色、最终观看和可选外部交付登录这些真正需要人工判断或账户操作的关卡暂停。每次暂停都给出一个明确、可直接回答的决定请求。
5. 不请求版权确认；广告按证据自动 `remove`、`mask` 或 `keep`，证据不足时保留；音色锁定后直接生成一分钟试听；试听后用户发出“生成全片”“生成全文”或等价指令，即授权当前冻结输入的全文付费 TTS 与渲染，自动 dry-run 通过后继续，不再二次确认。
6. 翻译必须由独立 Agent T 全文直译，再由不同的 Agent A/B 各自覆盖全文审核，最后由主控裁决。若独立 Agent 不可用或额度不足，保存断点并暂停，不得用本地模型冒充。
7. 中文 TTS 固定原生 `speed=1.0`，禁止音频拉伸、变速或截断有声内容；只通过画面重定时对轴。可能已经计费但状态不确定的请求不得自动重试。
8. 原始母版永不覆盖。所有项目媒体、字幕成品、音频、缓存、QA 记录、发布包和成片只保存在被 Git 忽略的运行目录；Cookie、API Key、Authorization Header 和临时媒体直链不得进入 Git、聊天、日志或正式文档。
9. 在每个阶段报告当前状态、已验证证据、下一步和是否需要用户决定，然后继续执行，直到到达真正的人工关卡或全部机器 QA 完成。未通过全部放行条件时只能报告“生成中”或“待修复”。

现在从安全验证输入与生成工作流锁开始，不要重新询问已经提供的视频链接和 Cookie 文件路径。
