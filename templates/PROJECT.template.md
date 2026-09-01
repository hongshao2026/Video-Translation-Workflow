# {{VIDEO_ID}} 中文译配项目

## 冻结输入

- 视频 ID：`{{VIDEO_ID}}`
- 源链接：`{{SOURCE_URL}}`
- 源语言：`{{SOURCE_LANGUAGE}}`
- 目标语言：`{{TARGET_LANGUAGE}}`
- 建立时间：`{{CREATED_AT}}`
- 人名显示：源文核定的拉丁字母拼写，不作中文音译；TTS 发音提示只写入独立录音输入。
- 原始下载母版永久保留且不得覆盖。

## 强制生产策略

- `media_format_selection=automatic_after_probe`
- `ad_policy=detect_then_apply_evidence_based`
- `translation_mode=codex_agent_direct_quality_first`
- `translation_review=two_independent_agents_full_coverage`
- `chapter_reading_review=required_before_translation_gate`
- `chapter_reading_layout=sentence_aligned_verbatim`
- `chinese_tts_speed=1.0`
- `chinese_offline_rate=1.0`
- `sync_strategy=video_retime_only`
- `preserve_all_formal_working_master_frames=true`
- `subtitle_timeline=rebuilt_from_chinese_audio`
- `audition_authorization=voice_selection_implies_audition`
- `full_tts_authorization=user_generate_full_command`
- `publication_package=required_after_final_machine_qa`
- `publication_text_format=utf8_txt_only`
- `cover_variants=16x9_and_4x3`

版权/权限确认不设执行门禁。格式探测通过后按冻结规则自动选择画面和原语言音轨并直接下载，不请求格式组合确认。广告按证据自动 `remove`、`mask` 或 `keep`；证据不足时保留。角色与音色锁定后可直接生成一分钟试听。试听后用户发出“生成全片”“生成全文”或等价指令，即授权当前冻结输入的全文付费 TTS 与渲染；自动 dry-run 通过后直接继续，不再二次确认。

## 当前设备

- 操作系统：待记录
- Python：待记录
- FFmpeg / ffprobe：待记录
- yt-dlp：待记录
- GPU / CPU：待记录
- ASR / 分离 / 说话人执行器：待记录
- TTS 执行器：待记录
- 渲染执行器：待记录

不得在本文件记录 Cookie、API Key、Authorization Header、临时媒体直链或其他凭证值。

## 当前状态

- 当前阶段：`intake`
- 下一道放行门：`workflow_lock`
- 已完成：项目目录初始化。
- 待完成：生成并通过 `qa/workflow_lock.json`，然后自动查询、选择当前视频的格式与原语言音轨并继续下载。

> 本节仅记录进度。只修改本节时不改变冻结约束哈希，也不要求重新生成工作流锁。
