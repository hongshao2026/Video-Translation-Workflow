import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";


test("keeps the local dubbing workbench as the primary product surface", async () => {
  const [page, layout, css] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);

  assert.match(layout, /声轨工坊｜本地 AI 视频译配工作台/);
  assert.match(layout, /lang="zh-CN"/);
  assert.match(page, /http:\/\/127\.0\.0\.1:8765/);
  assert.match(page, /让每个角色/);
  assert.match(page, /固定原始起点/);
  assert.match(page, /保持自然语速，不做后期变速/);
  assert.doesNotMatch(page, /自动压缩句长|最大语速调整/);
  assert.match(css, /\.voiceLibrary/);
  assert.doesNotMatch(page, /SkeletonPreview|Your site is taking shape/);
});


test("exposes all three local speech engines in the voice picker", async () => {
  const [page, catalog, worker] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../backend/voice_catalog.py", import.meta.url), "utf8"),
    readFile(new URL("../backend/generate_multi_engine_audition.py", import.meta.url), "utf8"),
  ]);

  for (const engine of ["qwen", "kokoro", "cosyvoice"]) {
    assert.match(page, new RegExp(`"${engine}"`));
  }
  assert.match(catalog, /engine="qwen"/);
  assert.match(catalog, /"engine": "kokoro"/);
  assert.match(catalog, /engine="cosyvoice"/);
  assert.match(page, /CosyVoice 300M SFT · 7 个官方声音/);
  assert.match(catalog, /CosyVoice 300M SFT/);
  assert.match(worker, /generate_cosyvoice_segments/);
  assert.match(worker, /cosyvoice_raw/);
});


test("supports an external sentence-level dubbing handoff", async () => {
  const [page, api, worker] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../backend/app.py", import.meta.url), "utf8"),
    readFile(new URL("../backend/generate_external_audition.py", import.meta.url), "utf8"),
  ]);

  assert.match(page, /外部配音包/);
  assert.match(page, /下载配音任务包/);
  assert.match(page, /上传并检查时间轴/);
  assert.match(page, /外部配音逐句检查结果/);
  assert.match(api, /\/api\/external-packs\/template/);
  assert.match(api, /\/api\/jobs\/external-audition/);
  assert.match(api, /normalize_audio/);
  assert.match(worker, /external_progress/);
  assert.match(worker, /start_seconds/);
});


test("provides a parameterized MiniMax provider module", async () => {
  const [page, panel, client, api] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/MiniMaxPanel.tsx", import.meta.url), "utf8"),
    readFile(new URL("../backend/minimax_client.py", import.meta.url), "utf8"),
    readFile(new URL("../backend/app.py", import.meta.url), "utf8"),
  ]);

  assert.match(page, /MiniMax API/);
  assert.match(page, /\/api\/jobs\/minimax-audition/);
  assert.match(panel, /保存并测试连接/);
  assert.match(panel, /生成试听/);
  assert.match(panel, /本地普通话试听库/);
  assert.match(panel, /不会调用 MiniMax API/);
  assert.match(page, /playMiniMaxVoice/);
  assert.match(page, /本项目候选/);
  assert.match(page, /保存并锁定音色/);
  assert.match(page, /保存后直接解锁一分钟试听/);
  assert.match(page, /\/api\/voice-selection/);
  assert.match(panel, /type="range"/);
  assert.match(panel, /type=\{showKey \? "text" : "password"\}/);
  assert.doesNotMatch(panel, /localStorage/);
  assert.match(client, /output_format.*hex/s);
  assert.match(client, /bytes\.fromhex/);
  assert.match(api, /\/api\/minimax\/preview/);
});


test("documents and exposes the resumable full production workflow", async () => {
  const [page, panel, definition, handbook, api, design] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/WorkflowPanel.tsx", import.meta.url), "utf8"),
    readFile(new URL("../docs/workflow.definition.json", import.meta.url), "utf8"),
    readFile(new URL("../docs/LOCAL_DUBBING_WORKFLOW.md", import.meta.url), "utf8"),
    readFile(new URL("../backend/app.py", import.meta.url), "utf8"),
    readFile(new URL("../DESIGN.md", import.meta.url), "utf8"),
  ]);

  const workflow = JSON.parse(definition);
  assert.equal(workflow.stages.length, 8);
  assert.equal(workflow.agents.length, 6);
  assert.ok(workflow.agents.some((agent) => agent.id === "translator"));
  assert.equal(workflow.stages.find((stage) => stage.id === "translate")?.owner, "translator");
  assert.ok(workflow.stages.find((stage) => stage.id === "audit")?.required_gate.conditions.includes("mode=provider_agent_direct_quality_first"));
  assert.ok(workflow.manual_gates.some((gate) => gate.id === "cookie" && gate.sensitive));
  assert.ok(workflow.manual_gates.some((gate) => gate.id === "voices"));
  assert.deepEqual(
    workflow.manual_gates.map((gate) => gate.id),
    ["cookie", "translation", "voices", "final_review", "delivery_login"],
  );
  assert.ok(workflow.global_hard_rules.some((rule) => rule.includes("自动选择画面与音频格式") && rule.includes("不请求人工格式批准")));
  assert.ok(workflow.global_hard_rules.includes("角色音色锁定后直接允许生成一分钟试听，不设试听费用或生成批准。"));
  assert.ok(workflow.stages.find((stage) => stage.id === "synthesize")?.pre_render_gate.conditions.includes("user_generate_full_command_matches_frozen_inputs=true"));
  assert.match(page, /全流程地图/);
  assert.match(panel, /你来做决定/);
  assert.match(panel, /localStorage/);
  assert.match(handbook, /双 Agent 全文审核/);
  assert.match(handbook, /自动生成 dry-run/);
  assert.match(handbook, /原声音量 \| 8%/);
  assert.match(handbook, /生成全片\/全文/);
  assert.match(api, /app\.mount\("\/docs"/);
  assert.match(design, /Zotero 式三栏.*八阶段生产轨/);
});


test("renders the Zotero-style library shell without persisting credentials in the browser", async () => {
  const [shell, page, css] = await Promise.all([
    readFile(new URL("../app/WorkbenchShell.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);

  assert.match(page, /WorkbenchShell/);
  assert.match(shell, /全部视频/);
  assert.match(shell, /项目检查器/);
  assert.match(shell, /八阶段生产轨/);
  assert.match(shell, /任务坞/);
  assert.match(shell, /翻译服务/);
  assert.match(shell, /语音服务/);
  assert.match(shell, /工作流预设/);
  assert.match(shell, /存储与迁移/);
  assert.match(shell, /高级与诊断/);
  assert.match(shell, /\/api\/library\/projects/);
  assert.match(shell, /\/api\/providers\/profiles/);
  assert.match(shell, /\/api\/events/);
  assert.match(shell, /type=\{showApiKey \? "text" : "password"\}/);
  assert.doesNotMatch(shell, /localStorage|sessionStorage/);
  assert.match(shell, /当前阶段没有可量化进度 · 不估算虚假百分比/);
  assert.match(css, /\.wb-layout/);
  assert.match(css, /@media \(max-width: 640px\)/);
  assert.match(css, /prefers-reduced-motion/);
});


test("uses the backend workbench contracts for providers, runs, raw state, and named events", async () => {
  const shell = await readFile(new URL("../app/WorkbenchShell.tsx", import.meta.url), "utf8");

  for (const field of ["display_name", "service_kind", "provider_id", "base_url", "credential"]) {
    assert.match(shell, new RegExp(`row\\.${field}`));
  }
  assert.match(shell, /credential\.configured/);
  assert.match(shell, /service_kind: providerDraft\.service_kind/);
  assert.match(shell, /provider_id: providerDraft\.provider_id/);
  assert.match(shell, /display_name: providerDraft\.display_name\.trim\(\)/);
  assert.match(shell, /base_url: providerDraft\.base_url\.trim\(\) \|\| null/);
  assert.match(shell, /api_key: providerDraft\.api_key/);
  assert.match(shell, /config: providerDraft\.config/);

  for (const providerId of [
    "openai-compatible",
    "minimax-llm",
    "openai-compatible-speech",
    "minimax-speech",
  ]) {
    assert.match(shell, new RegExp(`"${providerId}"`));
  }
  assert.doesNotMatch(shell, /<option value="(?:custom|local|openai|minimax)">/);
  assert.match(shell, /https:\/\/api\.minimax\.cn\/v1/);
  assert.match(shell, /https:\/\/api\.minimax\.cn/);
  assert.match(shell, /\/api\/providers\/profiles\/\$\{encodeURIComponent\(next\.id\)\}\/probe/);
  assert.match(shell, /配置已保存，但连接探测失败/);
  assert.match(shell, /配置已保存，连接探测通过/);

  for (const field of ["current_stage", "progress_current", "progress_total", "progress_unit", "source_display"]) {
    assert.match(shell, new RegExp(`row\\.${field}`));
  }
  for (const field of ["run_id", "stage_key", "kind", "detail"]) {
    assert.match(shell, new RegExp(`row\\.${field}`));
  }

  assert.match(shell, /preset_id: presetId/);
  assert.match(shell, /role_bindings: effectiveRoleBindings/);
  assert.match(shell, /speech_profile_id: effectiveSpeechProfileId/);
  assert.doesNotMatch(shell, /resume: true/);
  assert.match(shell, /尚未为 \$\{missingRoles\.join\(" \/ "\)\} 绑定已配置的翻译 Provider/);
  assert.match(shell, /尚未绑定已配置的语音 Provider/);
  assert.match(shell, /\["T", "A", "B", "C"\] as TranslationRole\[\]/);

  for (const eventName of [
    "project.created",
    "run.created",
    "task.created",
    "task.updated",
    "provider.saved",
    "provider_request.updated",
  ]) {
    assert.match(shell, new RegExp(`"${eventName.replace(".", "\\.")}"`));
  }
  assert.match(shell, /events\.addEventListener\(eventName, refreshLibrary\)/);
  assert.match(shell, /events\.addEventListener\("provider\.saved", refreshProviders\)/);
  assert.match(shell, /event\.lastEventId/);
  assert.match(shell, /\?after=\$\{lastEventIdRef\.current\}/);
  assert.doesNotMatch(shell, /events\.onmessage/);
});


test("exposes gate-aware stage operations for a selected durable run", async () => {
  const [shell, css] = await Promise.all([
    readFile(new URL("../app/WorkbenchShell.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);

  for (const endpoint of [
    "probe",
    "download",
    "ingest/source-transcript",
    "ingest/embedded-subtitle",
    "ad-edit",
    "translate",
    "chapter-reading",
    "approve-translation",
    "voice-lock",
    "audition",
    "generate-full",
    "render",
    "cover-source",
    "publication-package",
  ]) {
    assert.match(shell, new RegExp(`"${endpoint.replaceAll("/", "\\/")}"`));
  }

  assert.match(shell, /api\/library\/projects\/\$\{encodeURIComponent\(projectId\)\}\/runs/);
  assert.match(shell, /api\/workflows\/runs\/\$\{encodeURIComponent\(runId\)\}/);
  assert.match(shell, /id="wb-current-run"/);
  assert.match(shell, /按钮随门禁放行/);
  assert.match(shell, /disabled=\{workflowWritesDisabled \|\| !adGatePassed \|\| translationComplete\}/);
  assert.match(shell, /disabled=\{workflowWritesDisabled \|\| !translationGatePassed \|\| voiceLockPassed\}/);
  assert.match(shell, /disabled=\{workflowWritesDisabled \|\| !voiceLockPassed \|\| auditionComplete\}/);
  assert.match(shell, /disabled=\{workflowWritesDisabled \|\| !voiceLockPassed \|\| speechComplete\}/);
  assert.match(shell, /disabled=\{workflowWritesDisabled \|\| !machineQaPassed\}/);

  assert.match(shell, /完整扫描：无广告/);
  assert.match(shell, /operator_confirmed_full_content_visual_semantic_scan/);
  assert.match(shell, /这不是自动检测结果/);
  assert.doesNotMatch(shell, /自动检测完成.*no_ads_detected/s);

  assert.match(shell, /profiles\/\$\{encodeURIComponent\(profileId\)\}\/catalog/);
  assert.match(shell, /generation_performed !== false/);
  assert.match(shell, /读取非生成目录/);
  assert.match(shell, /voice_id/);
  assert.match(shell, /result\.segments_path/);
  assert.match(shell, /provider\.speech\.audition/);
  assert.match(shell, /音色锁即为本次试听授权/);
  assert.match(shell, /正式中文语音固定原生 1\.0×/);

  assert.match(shell, /copyable_text/);
  assert.match(shell, /UTF-8 发布文字/);
  assert.match(css, /\.wb-workflow-card/);
  assert.match(css, /\.wb-copyable-publication/);
  assert.match(css, /textarea[^}]*resize: none/s);
});
