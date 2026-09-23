"use client";

import {
  type FormEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

const API = process.env.NEXT_PUBLIC_DUB_API || "http://127.0.0.1:8765";

const STAGES = [
  { id: "ingest", short: "导入", label: "下载与媒体探测" },
  { id: "prepare", short: "预处理", label: "转写、说话人与广告检测" },
  { id: "translate", short: "翻译", label: "质量优先全文翻译" },
  { id: "review", short: "双审", label: "表达与忠实度双重审核" },
  { id: "voices", short: "选音", label: "角色识别与音色锁定" },
  { id: "synthesize", short: "配音", label: "原生 1.0× 语音生成" },
  { id: "render", short: "成片", label: "画面重定时与字幕合成" },
  { id: "publish", short: "发布", label: "机器验收与发布包" },
] as const;

type Surface = "library" | "settings" | "studio";
type LibraryFilter = "all" | "running" | "waiting_user" | "error" | "complete";
type ProjectState =
  | "draft"
  | "queued"
  | "running"
  | "waiting_user"
  | "waiting_provider"
  | "paused"
  | "blocked_uncertain"
  | "repair_required"
  | "machine_passed"
  | "completed"
  | "cancelled";
type JobState =
  | ProjectState
  | "ready"
  | "pausing"
  | "cancel_requested"
  | "invalidated"
  | "superseded"
  | "retry_wait"
  | "waiting_worker";
type InspectorTab = "overview" | "translation" | "voices" | "qa" | "history";
type SettingSection = "translation" | "speech" | "presets" | "storage" | "advanced";
type TranslationRole = "T" | "A" | "B" | "C";
type ServiceKind = "llm" | "speech";
type ProviderId = "openai-compatible" | "minimax-llm" | "openai-compatible-speech" | "minimax-speech";

type Progress = {
  current: number;
  total: number;
  unit: string;
  label: string;
};

type ProjectRecord = {
  id: string;
  title: string;
  source: string;
  source_value: string;
  source_kind: "video_url" | "local_file";
  duration_label: string;
  resolution: string;
  status: ProjectState;
  stage_index: number;
  stages_passed: number;
  progress: Progress | null;
  updated_at: string;
  language: string;
  issue?: string;
};

type JobRecord = {
  id: string;
  run_id: string;
  project_id: string;
  title: string;
  stage: string;
  stage_key: string;
  kind: string;
  status: JobState;
  progress: Progress | null;
  elapsed_label: string;
  issue?: string;
};

type ProviderProfile = {
  id: string;
  display_name: string;
  service_kind: ServiceKind;
  provider_id: ProviderId;
  model: string;
  base_url?: string;
  credential: {
    configured: boolean;
    persistent?: boolean;
    backend?: string;
  };
  capability: Record<string, unknown>;
  enabled: boolean;
};

type ProviderCatalogEntry = {
  provider_id: ProviderId;
  service_kind: ServiceKind;
  label: string;
  default_base_url?: string;
  base_url_required?: boolean;
  recommended_config: Record<string, unknown>;
};

type ProviderChoiceCatalog = {
  profile_id: string;
  service_kind: ServiceKind;
  models: { model_id: string; label?: string }[];
  voices: { voice_id: string; name?: string; description?: string; category?: string; language?: string }[];
  generation_performed: false;
};

type StageRecord = {
  key: string;
  title: string;
  status: JobState;
  detail: string;
  progress_mode: string;
  progress_current: number | null;
  progress_total: number | null;
  progress_unit: string;
};

type WorkflowRun = {
  id: string;
  project_id: string;
  preset_id: string;
  status: JobState;
  current_stage: string;
  created_at: string;
  updated_at: string;
  provider_lock: Record<string, unknown>;
  prompt_lock: Record<string, unknown>;
  stages: StageRecord[];
};

type WorkflowPreset = {
  id: string;
  name: string;
  version: string;
  locked: boolean;
  description: string;
};

type WorkflowDraft = {
  expected_language: string;
  cookie_file: string;
  transcript_path: string;
  embedded_stream_index: string;
  embedded_output_path: string;
  source_language: string;
  version: string;
  source_master_path: string;
  working_master_path: string;
  source_duration: string;
  frame_width: string;
  frame_height: string;
  frozen_source_path: string;
  ad_input_mode: "no_ads" | "advanced";
  no_ads_scan_confirmed: boolean;
  ad_analysis_json: string;
  slots_path: string;
  glossary_path: string;
  project_context: string;
  batch_size: string;
  translation_path: string;
  chapters_json: string;
  timeline_path: string;
  approval_command: string;
  reading_path: string;
  validation_path: string;
  voice_assignments: string;
  role_names_json: string;
  voice_role_id: string;
  voice_role_name: string;
  voice_id: string;
  translation_gate_path: string;
  segments_path: string;
  voice_lock_path: string;
  speech_output_dir: string;
  full_command: string;
  render_json: string;
  publication_json: string;
  cover_source_at_seconds: string;
};

function createWorkflowDraft(): WorkflowDraft {
  return {
    expected_language: "en",
    cookie_file: "",
    transcript_path: "work/source.srt",
    embedded_stream_index: "0",
    embedded_output_path: "work/embedded_source_v1.srt",
    source_language: "en",
    version: "1",
    source_master_path: "source/original_master.mp4",
    working_master_path: "work/working_master_v1.mp4",
    source_duration: "",
    frame_width: "1920",
    frame_height: "1080",
    frozen_source_path: "work/frozen_source_v1.json",
    ad_input_mode: "no_ads",
    no_ads_scan_confirmed: false,
    ad_analysis_json: JSON.stringify({
      status: "analysis_required",
      content_scan_complete: false,
      visual_scan_complete: false,
      semantic_analysis_required: true,
      semantic_analysis_complete: false,
      candidates: [],
    }, null, 2),
    slots_path: "work/frozen_source_v1.json",
    glossary_path: "",
    project_context: "",
    batch_size: "40",
    translation_path: "work/translation_final_v1.json",
    chapters_json: "[]",
    timeline_path: "qa/source_to_edit_timeline.json",
    approval_command: "",
    reading_path: "",
    validation_path: "",
    voice_assignments: "narrator=",
    role_names_json: JSON.stringify({ narrator: "旁白" }, null, 2),
    voice_role_id: "narrator",
    voice_role_name: "旁白",
    voice_id: "",
    translation_gate_path: "qa/translation_gate.json",
    segments_path: "work/tts_segments_v1.json",
    voice_lock_path: "work/voice_selection_locked_v1.json",
    speech_output_dir: "full_dub_v1/tts",
    full_command: "",
    render_json: JSON.stringify({
      working_master_path: "work/working_master_v1.mp4",
      output_path: "deliverables/final_dub_v1.mp4",
      ad_edit_gate_path: "qa/ad_edit_gate.json",
      translation_gate_path: "qa/translation_gate.json",
      voice_lock_path: "work/voice_selection_locked_v1.json",
      authorization_path: "qa/full_tts_authorization_v1.json",
      translation_path: "work/translation_final_v1.json",
      tts_manifest_path: "full_dub_v1/tts/manifest.json",
      video_codec: "libx264",
      version: 1,
    }, null, 2),
    publication_json: JSON.stringify({
      video_id: "video",
      version: 1,
      final_video_path: "deliverables/final_dub_v1.mp4",
      final_machine_qa_path: "qa/final_machine_qa_v1.json",
      production_gate_path: "qa/production_gate.json",
      ad_edit_gate_path: "qa/ad_edit_gate.json",
      source_chapters: [{ title: "完整视频", start: 0 }],
      source_to_working_path: "qa/source_to_edit_timeline.json",
      working_to_final_path: "work/video_retime_plan_v1.json",
      titles: ["中文标题候选一", "中文标题候选二", "中文标题候选三"],
      description: "请填写去除推广信息后的中文简介，并保留原视频链接。",
      books: [],
      original_video_url: "",
      allow_single_title: false,
      foreign_names_verified: false,
      removed_promotion_categories: [],
      cover_source_path: "work/cover_source.png",
      cover_title: "中文封面标题",
      cover_source_authorized: false,
      cover_source_clean_verified: false,
      cover_identity_verified: false,
      cover_text_verified: false,
      cover_design_notes: "授权源帧与标题分区重排，保留完整源帧内容。",
    }, null, 2),
    cover_source_at_seconds: "0",
  };
}

function createWorkflowDraftForProject(project: ProjectRecord): WorkflowDraft {
  const next = createWorkflowDraft();
  const publication = parseJsonObject(next.publication_json, "发布包计划");
  publication.video_id = project.id.replace(/^prj_/, "") || "video";
  publication.original_video_url = project.source_kind === "video_url" ? project.source_value : "";
  next.publication_json = JSON.stringify(publication, null, 2);
  return next;
}

const PREVIEW_PROJECTS: ProjectRecord[] = [
  {
    id: "preview-poker-01",
    title: "职业牌手如何拆解河牌决策",
    source: "界面预览项目 · 未写入资料库",
    source_value: "",
    source_kind: "video_url",
    duration_label: "01:03:20",
    resolution: "1920×1080",
    status: "running",
    stage_index: 3,
    stages_passed: 3,
    progress: { current: 672, total: 1420, unit: "句", label: "审核 A 全文覆盖" },
    updated_at: "刚刚",
    language: "English → 简体中文",
  },
  {
    id: "preview-interview-02",
    title: "访谈节目：长期主义与内容创作",
    source: "界面预览项目 · 未写入资料库",
    source_value: "",
    source_kind: "local_file",
    duration_label: "00:42:18",
    resolution: "3840×2160",
    status: "waiting_user",
    stage_index: 4,
    stages_passed: 4,
    progress: null,
    updated_at: "12 分钟前",
    language: "日本語 → 简体中文",
    issue: "章节阅读稿已生成，等待整体审阅后进入选音。",
  },
  {
    id: "preview-course-03",
    title: "产品设计中的十个常见误区",
    source: "界面预览项目 · 未写入资料库",
    source_value: "",
    source_kind: "video_url",
    duration_label: "00:28:44",
    resolution: "1920×1080",
    status: "repair_required",
    stage_index: 5,
    stages_passed: 5,
    progress: { current: 128, total: 640, unit: "句", label: "语音片段已验证" },
    updated_at: "35 分钟前",
    language: "English → 简体中文",
    issue: "MiniMax 返回限流；已生成的 128 句安全保留，可从下一批继续。",
  },
  {
    id: "preview-science-04",
    title: "睡眠如何改变记忆形成",
    source: "界面预览项目 · 未写入资料库",
    source_value: "",
    source_kind: "video_url",
    duration_label: "00:19:07",
    resolution: "1920×1080",
    status: "machine_passed",
    stage_index: 7,
    stages_passed: 8,
    progress: { current: 8, total: 8, unit: "项", label: "机器验收" },
    updated_at: "昨天 21:40",
    language: "English → 简体中文",
  },
];

const PREVIEW_JOBS: JobRecord[] = [
  {
    id: "preview-job-a",
    run_id: "preview-run-a",
    project_id: "preview-poker-01",
    title: "职业牌手如何拆解河牌决策",
    stage: "审核 A",
    stage_key: "05",
    kind: "provider.llm.role_a",
    status: "running",
    progress: { current: 672, total: 1420, unit: "句", label: "中文表达与上下文" },
    elapsed_label: "已运行 08:16",
  },
  {
    id: "preview-job-b",
    run_id: "preview-run-b",
    project_id: "preview-course-03",
    title: "产品设计中的十个常见误区",
    stage: "MiniMax 配音",
    stage_key: "07",
    kind: "provider.speech.synthesize",
    status: "repair_required",
    progress: { current: 128, total: 640, unit: "句", label: "已验证并缓存" },
    elapsed_label: "暂停于 14:32",
    issue: "服务限流；不会自动重发可能计费的请求。",
  },
];

const FALLBACK_PRESETS: WorkflowPreset[] = [
  {
    id: "quality-zh-v1",
    name: "质量优先译配",
    version: "v1 · 内置锁定",
    locked: true,
    description: "T 全文翻译、A/B 独立全文审核、主控裁决、章节阅读稿与生产门禁。",
  },
];

const FILTER_LABELS: Record<LibraryFilter, string> = {
  all: "全部视频",
  running: "进行中",
  waiting_user: "待我处理",
  error: "需修复",
  complete: "已完成",
};

const STATUS_META: Record<ProjectState, { label: string; tone: string }> = {
  draft: { label: "待创建运行", tone: "neutral" },
  queued: { label: "排队中", tone: "neutral" },
  running: { label: "运行中", tone: "active" },
  waiting_user: { label: "待你处理", tone: "warning" },
  waiting_provider: { label: "等待服务", tone: "warning" },
  paused: { label: "已暂停", tone: "neutral" },
  blocked_uncertain: { label: "计费待核对", tone: "danger" },
  repair_required: { label: "需修复", tone: "danger" },
  machine_passed: { label: "机器验收通过", tone: "success" },
  completed: { label: "已完成", tone: "success" },
  cancelled: { label: "已取消", tone: "neutral" },
};

const PROJECT_STATES = new Set<ProjectState>([
  "draft",
  "queued",
  "running",
  "waiting_user",
  "waiting_provider",
  "paused",
  "blocked_uncertain",
  "repair_required",
  "machine_passed",
  "completed",
  "cancelled",
]);

const JOB_STATES = new Set<JobState>([
  ...PROJECT_STATES,
  "ready",
  "pausing",
  "cancel_requested",
  "invalidated",
  "superseded",
  "retry_wait",
  "waiting_worker",
]);

const PROVIDER_LABELS: Record<ProviderId, string> = {
  "openai-compatible": "OpenAI-compatible 翻译模型",
  "minimax-llm": "MiniMax 翻译模型",
  "openai-compatible-speech": "OpenAI-compatible 语音",
  "minimax-speech": "MiniMax 语音",
};

const PROVIDER_DEFAULT_URLS: Partial<Record<ProviderId, string>> = {
  "minimax-llm": "https://api.minimax.cn/v1",
  "minimax-speech": "https://api.minimax.cn",
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function textValue(value: unknown, fallback = ""): string {
  return typeof value === "string" && value.trim() ? value : fallback;
}

function numberValue(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function errorMessage(payload: unknown, fallback: string): string {
  if (!isRecord(payload)) return fallback;
  if (typeof payload.detail === "string" && payload.detail.trim()) return payload.detail;
  if (Array.isArray(payload.detail)) {
    const messages = payload.detail.flatMap((item) => {
      if (!isRecord(item)) return [];
      const location = Array.isArray(item.loc) ? item.loc.slice(1).join(".") : "输入";
      const message = textValue(item.msg);
      return message ? [`${location || "输入"}：${message}`] : [];
    });
    if (messages.length) return messages.join("；");
  }
  if (isRecord(payload.detail) && typeof payload.detail.message === "string" && payload.detail.message.trim()) {
    return payload.detail.message;
  }
  return fallback;
}

function formatDuration(value: unknown): string {
  const seconds = numberValue(value, -1);
  if (seconds < 0) return "—";
  const rounded = Math.round(seconds);
  const hours = Math.floor(rounded / 3600);
  const minutes = Math.floor((rounded % 3600) / 60);
  const remainder = rounded % 60;
  return [hours, minutes, remainder].map((part) => String(part).padStart(2, "0")).join(":");
}

function stageIndex(value: unknown): number {
  if (typeof value === "string") {
    const numeric = Number.parseInt(value, 10);
    if (Number.isFinite(numeric)) return Math.min(7, Math.max(0, numeric - 1));
    const legacy = STAGES.findIndex((stage) => stage.id === value);
    if (legacy >= 0) return legacy;
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    return Math.min(7, Math.max(0, Math.trunc(value) - 1));
  }
  return 0;
}

function displayTimestamp(value: unknown): string {
  const raw = textValue(value);
  if (!raw) return "等待更新";
  const parsed = new Date(raw);
  if (Number.isNaN(parsed.getTime())) return raw;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed);
}

function projectIssue(status: ProjectState, needsAttention: boolean): string | undefined {
  if (status === "blocked_uncertain") return "外部请求结果可能已产生费用；请核对请求账本，系统不会自动重试。";
  if (status === "repair_required" || needsAttention) return "当前运行需要修复证据后才能继续。";
  if (status === "waiting_provider") return "Provider 暂时不可用；已保存的检查点不会丢失。";
  if (status === "waiting_user") return "当前阶段正在等待人工审阅或决定。";
  return undefined;
}

function normalizeProject(value: unknown, index: number): ProjectRecord {
  const row = isRecord(value) ? value : {};
  const rawStatus = textValue(row.status, "draft") as ProjectState;
  const status: ProjectState = PROJECT_STATES.has(rawStatus) ? rawStatus : "draft";
  const metadata = isRecord(row.metadata) ? row.metadata : {};
  const probe = isRecord(metadata.media_probe) ? metadata.media_probe : {};
  const currentStageIndex = stageIndex(row.current_stage);
  const progressTotal = numberValue(row.progress_total);
  const progress = progressTotal > 0
    ? {
        current: numberValue(row.progress_current),
        total: progressTotal,
        unit: textValue(row.progress_unit, "项"),
        label: STAGES[currentStageIndex]?.label || "当前步骤",
      }
    : null;
  const isPassed = status === "machine_passed" || status === "completed";
  const width = numberValue(probe.width);
  const height = numberValue(probe.height);
  return {
    id: textValue(row.id, `project-${index + 1}`),
    title: textValue(row.title, "未命名视频"),
    source: textValue(row.source_display, "本地资料库"),
    source_value: textValue(row.source),
    source_kind: row.source_kind === "local_file" ? "local_file" : "video_url",
    duration_label: formatDuration(probe.duration),
    resolution: width > 0 && height > 0 ? `${width}×${height}` : "—",
    status,
    stage_index: currentStageIndex,
    stages_passed: isPassed ? 8 : currentStageIndex,
    progress,
    updated_at: displayTimestamp(row.updated_at),
    language: textValue(probe.audio_language, "待探测") === "待探测"
      ? "待探测"
      : `${textValue(probe.audio_language)} → 简体中文`,
    issue: projectIssue(status, row.needs_attention === true),
  };
}

type RunOwner = { project_id: string; title: string };

function jobStageLabel(stageKey: string, kind: string): string {
  const numeric = Number.parseInt(stageKey, 10);
  const stage = Number.isFinite(numeric) ? STAGES[numeric - 1] : undefined;
  const role = kind.match(/role_([tabc])$/i)?.[1]?.toUpperCase();
  if (role) return `${stage?.short || "翻译"} ${role}`;
  return stage ? `${stageKey} · ${stage.short}` : kind || "等待运行";
}

function normalizeJob(value: unknown, index: number, runOwners: Map<string, RunOwner>): JobRecord {
  const row = isRecord(value) ? value : {};
  const rawStatus = textValue(row.status, "queued") as JobState;
  const status = JOB_STATES.has(rawStatus) ? rawStatus : "queued";
  const runId = textValue(row.run_id);
  const owner = runOwners.get(runId);
  const progressTotal = numberValue(row.progress_total);
  const kind = textValue(row.kind);
  const stageKey = textValue(row.stage_key);
  const error = isRecord(row.error) ? row.error : {};
  return {
    id: textValue(row.id, `job-${index + 1}`),
    run_id: runId,
    project_id: owner?.project_id || "",
    title: owner?.title || `运行 ${runId || index + 1}`,
    stage: jobStageLabel(stageKey, kind),
    stage_key: stageKey,
    kind,
    status,
    progress: progressTotal > 0 ? {
      current: numberValue(row.progress_current),
      total: progressTotal,
      unit: textValue(row.progress_unit, "项"),
      label: textValue(row.detail, jobStageLabel(stageKey, kind)),
    } : null,
    elapsed_label: `更新 ${displayTimestamp(row.updated_at)}`,
    issue: textValue(row.detail, textValue(error.message)) || undefined,
  };
}

function normalizeRun(value: unknown): WorkflowRun | null {
  if (!isRecord(value)) return null;
  const id = textValue(value.id);
  const projectId = textValue(value.project_id);
  if (!id || !projectId) return null;
  const rawStatus = textValue(value.status, "ready") as JobState;
  const status = JOB_STATES.has(rawStatus) ? rawStatus : "ready";
  const stages = Array.isArray(value.stages) ? value.stages.flatMap((entry) => {
    const row = isRecord(entry) ? entry : {};
    const key = textValue(row.stage_key, textValue(row.key));
    if (!key) return [];
    const rawStageStatus = textValue(row.status, "ready") as JobState;
    return [{
      key,
      title: textValue(row.title, `阶段 ${key}`),
      status: JOB_STATES.has(rawStageStatus) ? rawStageStatus : "ready",
      detail: textValue(row.detail),
      progress_mode: textValue(row.progress_mode, "indeterminate"),
      progress_current: typeof row.progress_current === "number" ? row.progress_current : null,
      progress_total: typeof row.progress_total === "number" ? row.progress_total : null,
      progress_unit: textValue(row.progress_unit, "项"),
    }];
  }) : [];
  return {
    id,
    project_id: projectId,
    preset_id: textValue(value.preset_id, "quality-zh-v1"),
    status,
    current_stage: textValue(value.current_stage, "01"),
    created_at: textValue(value.created_at),
    updated_at: textValue(value.updated_at),
    provider_lock: isRecord(value.provider_lock) ? value.provider_lock : {},
    prompt_lock: isRecord(value.prompt_lock) ? value.prompt_lock : {},
    stages,
  };
}

function parseJsonObject(value: string, label: string): Record<string, unknown> {
  let payload: unknown;
  try {
    payload = JSON.parse(value);
  } catch {
    throw new Error(`${label}不是有效 JSON。`);
  }
  if (!isRecord(payload)) throw new Error(`${label}必须是 JSON 对象。`);
  return payload;
}

function parseJsonArray(value: string, label: string): Record<string, unknown>[] {
  let payload: unknown;
  try {
    payload = JSON.parse(value);
  } catch {
    throw new Error(`${label}不是有效 JSON。`);
  }
  if (!Array.isArray(payload) || payload.some((item) => !isRecord(item))) {
    throw new Error(`${label}必须是 JSON 对象数组。`);
  }
  return payload as Record<string, unknown>[];
}

function parseVoiceAssignments(value: string): Record<string, string> {
  const result: Record<string, string> = {};
  for (const rawLine of value.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;
    const separator = line.indexOf("=");
    if (separator <= 0) throw new Error("角色与 voice_id 请按“role_id=voice_id”逐行填写。");
    const role = line.slice(0, separator).trim();
    const voice = line.slice(separator + 1).trim();
    if (!role || !voice) throw new Error(`角色 ${role || "（空）"} 尚未填写 voice_id。`);
    if (result[role]) throw new Error(`角色 ${role} 重复。`);
    result[role] = voice;
  }
  if (!Object.keys(result).length) throw new Error("至少填写一个角色与 voice_id。");
  return result;
}

function unpackList(payload: unknown, key: string): unknown[] {
  if (Array.isArray(payload)) return payload;
  if (isRecord(payload) && Array.isArray(payload[key])) return payload[key];
  return [];
}

function percent(progress: Progress): number {
  return Math.min(100, Math.max(0, Math.round(progress.current / progress.total * 100)));
}

function displayCount(value: number): string {
  return value > 99 ? "99+" : String(value);
}

function tabForStage(index: number): InspectorTab {
  if (index <= 2) return "overview";
  if (index <= 4) return "translation";
  if (index <= 6) return "voices";
  return "qa";
}

function ProgressLine({ progress, compact = false }: { progress: Progress | null; compact?: boolean }) {
  if (!progress) {
    return (
      <div className={`wb-progress wb-progress-indeterminate ${compact ? "compact" : ""}`}>
        <div aria-hidden="true"><i /></div>
        <span>当前阶段没有可量化进度 · 不估算虚假百分比</span>
      </div>
    );
  }
  const value = percent(progress);
  return (
    <div className={`wb-progress ${compact ? "compact" : ""}`}>
      <div
        role="progressbar"
        aria-label={progress.label}
        aria-valuemin={0}
        aria-valuemax={progress.total}
        aria-valuenow={progress.current}
      >
        <i style={{ width: `${value}%` }} />
      </div>
      <span>{progress.current.toLocaleString("zh-CN")}/{progress.total.toLocaleString("zh-CN")} {progress.unit} · {progress.label}</span>
    </div>
  );
}

function StatusBadge({ status }: { status: ProjectState }) {
  const meta = STATUS_META[status];
  return <span className={`wb-status ${meta.tone}`}><i aria-hidden="true" />{meta.label}</span>;
}

function extractProfiles(payload: unknown): ProviderProfile[] {
  return unpackList(payload, "profiles").flatMap((value, index) => {
    const row = isRecord(value) ? value : {};
    const providerId = textValue(row.provider_id) as ProviderId;
    const serviceKind = textValue(row.service_kind) as ServiceKind;
    if (!(providerId in PROVIDER_LABELS) || !["llm", "speech"].includes(serviceKind)) return [];
    const credential = isRecord(row.credential) ? row.credential : {};
    return [{
      id: textValue(row.id, `provider-${index + 1}`),
      display_name: textValue(row.display_name, "未命名服务"),
      service_kind: serviceKind,
      provider_id: providerId,
      model: textValue(row.model, "未指定模型"),
      base_url: textValue(row.base_url) || PROVIDER_DEFAULT_URLS[providerId],
      credential: {
        configured: credential.configured === true,
        persistent: credential.persistent === true,
        backend: textValue(credential.backend) || undefined,
      },
      capability: isRecord(row.capability) ? row.capability : {},
      enabled: row.enabled !== false,
    }];
  });
}

function extractPresets(payload: unknown): WorkflowPreset[] {
  return unpackList(payload, "presets").map((value, index) => {
    const row = isRecord(value) ? value : {};
    return {
      id: textValue(row.id, `preset-${index + 1}`),
      name: textValue(row.name, "未命名预设"),
      version: typeof row.version === "number" ? `v${row.version}` : textValue(row.version, "未版本化"),
      locked: row.locked !== false,
      description: textValue(row.description, "工作流提示词与门禁预设。"),
    };
  });
}

export default function WorkbenchShell({ studio }: { studio: ReactNode }) {
  const [surface, setSurface] = useState<Surface>("library");
  const [filter, setFilter] = useState<LibraryFilter>("all");
  const [search, setSearch] = useState("");
  const [projects, setProjects] = useState<ProjectRecord[]>([]);
  const [jobs, setJobs] = useState<JobRecord[]>([]);
  const [providers, setProviders] = useState<ProviderProfile[]>([]);
  const [providerCatalog, setProviderCatalog] = useState<ProviderCatalogEntry[]>([]);
  const [choiceCatalogs, setChoiceCatalogs] = useState<Record<string, ProviderChoiceCatalog>>({});
  const [presets, setPresets] = useState<WorkflowPreset[]>(FALLBACK_PRESETS);
  const [roleBindings, setRoleBindings] = useState<Record<TranslationRole, string>>({ T: "", A: "", B: "", C: "" });
  const [speechProfileId, setSpeechProfileId] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [runs, setRuns] = useState<WorkflowRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState("");
  const [runsState, setRunsState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("overview");
  const [settingSection, setSettingSection] = useState<SettingSection>("translation");
  const [connection, setConnection] = useState<"checking" | "online" | "offline">("checking");
  const [eventState, setEventState] = useState<"idle" | "live" | "offline">("idle");
  const [libraryMode, setLibraryMode] = useState<"loading" | "live" | "preview" | "empty">("loading");
  const [importOpen, setImportOpen] = useState(false);
  const [importKind, setImportKind] = useState<"video_url" | "local_file">("video_url");
  const [importSource, setImportSource] = useState("");
  const [importBusy, setImportBusy] = useState(false);
  const [importError, setImportError] = useState("");
  const [dockOpen, setDockOpen] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [providerFeedback, setProviderFeedback] = useState("");
  const [providerBusy, setProviderBusy] = useState(false);
  const [runFeedback, setRunFeedback] = useState("");
  const [workflowDraft, setWorkflowDraft] = useState<WorkflowDraft>(() => createWorkflowDraft());
  const [workflowBusy, setWorkflowBusy] = useState("");
  const [workflowFeedback, setWorkflowFeedback] = useState("");
  const [workflowResult, setWorkflowResult] = useState<Record<string, unknown>>({});
  const [publicationText, setPublicationText] = useState("");
  const [catalogBusy, setCatalogBusy] = useState("");
  const [catalogFeedback, setCatalogFeedback] = useState("");
  const [jobActionBusy, setJobActionBusy] = useState("");
  const [jobFeedback, setJobFeedback] = useState("");
  const importInputRef = useRef<HTMLInputElement | null>(null);
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const lastEventIdRef = useRef(0);
  const runOwnersRef = useRef(new Map<string, RunOwner>());
  const selectedProjectRef = useRef<ProjectRecord | null>(null);
  const selectedIdRef = useRef<string | null>(null);
  const [providerDraft, setProviderDraft] = useState<ProviderDraft>({
    display_name: "",
    service_kind: "llm",
    provider_id: "openai-compatible",
    base_url: "https://api.openai.com/v1",
    model: "",
    api_key: "",
    config: {},
  });

  const loadProjectsAndJobs = useCallback(async (signal?: AbortSignal) => {
    const [projectResult, jobResult] = await Promise.allSettled([
      fetch(`${API}/api/library/projects`, { signal }),
      fetch(`${API}/api/jobs`, { signal }),
    ]);
    if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
    if (projectResult.status !== "fulfilled" || !projectResult.value.ok) {
      setProjects(PREVIEW_PROJECTS);
      setJobs(PREVIEW_JOBS);
      selectedIdRef.current = PREVIEW_PROJECTS[0].id;
      setSelectedId(PREVIEW_PROJECTS[0].id);
      setRuns([]);
      setSelectedRunId("");
      setRunsState("idle");
      setLibraryMode("preview");
      return false;
    }
    const projectPayload: unknown = await projectResult.value.json();
    const nextProjects = unpackList(projectPayload, "projects").map(normalizeProject);
    setProjects(nextProjects);
    const nextSelected = nextProjects.find((project) => project.id === selectedIdRef.current) || nextProjects[0] || null;
    if (nextSelected?.id !== selectedIdRef.current) {
      selectedIdRef.current = nextSelected?.id || null;
      setWorkflowDraft(nextSelected ? createWorkflowDraftForProject(nextSelected) : createWorkflowDraft());
      setWorkflowFeedback("");
      setWorkflowResult({});
      setPublicationText("");
    }
    setSelectedId(nextSelected?.id || null);
    setLibraryMode(nextProjects.length ? "live" : "empty");
    if (jobResult.status === "fulfilled" && jobResult.value.ok) {
      const jobPayload: unknown = await jobResult.value.json();
      const rawJobs = unpackList(jobPayload, "jobs");
      const runOwners = new Map(runOwnersRef.current);
      const unresolvedRunIds = new Set(
        rawJobs
          .map((value) => isRecord(value) ? textValue(value.run_id) : "")
          .filter((runId) => runId && !runOwners.has(runId)),
      );
      if (unresolvedRunIds.size > 0) {
        const runResults = await Promise.allSettled(
          nextProjects.map(async (project) => {
            const response = await fetch(`${API}/api/library/projects/${encodeURIComponent(project.id)}/runs`, { signal });
            if (!response.ok) return [];
            const payload: unknown = await response.json();
            return unpackList(payload, "runs").map((run) => ({ run, project }));
          }),
        );
        for (const result of runResults) {
          if (result.status !== "fulfilled") continue;
          for (const { run, project } of result.value) {
            if (!isRecord(run)) continue;
            const runId = textValue(run.id);
            if (runId) runOwners.set(runId, { project_id: project.id, title: project.title });
          }
        }
        runOwnersRef.current = runOwners;
      }
      setJobs(rawJobs.map((value, index) => normalizeJob(value, index, runOwners)));
    } else {
      setJobs([]);
    }
    return true;
  }, []);

  const loadProviderProfiles = useCallback(async (signal?: AbortSignal) => {
    const response = await fetch(`${API}/api/providers/profiles`, { signal });
    if (!response.ok) throw new Error("Provider 配置读取失败");
    const payload: unknown = await response.json();
    setProviders(extractProfiles(payload));
  }, []);

  const loadProjectRuns = useCallback(async (projectId: string, projectTitle: string, signal?: AbortSignal) => {
    setRunsState("loading");
    try {
      const response = await fetch(`${API}/api/library/projects/${encodeURIComponent(projectId)}/runs`, { signal });
      const payload: unknown = await response.json();
      if (!response.ok) throw new Error(errorMessage(payload, "运行记录读取失败。"));
      const rawRuns = unpackList(payload, "runs");
      const detailResults = await Promise.allSettled(rawRuns.map(async (run) => {
        const runId = isRecord(run) ? textValue(run.id) : "";
        if (!runId) return null;
        const detailResponse = await fetch(`${API}/api/workflows/runs/${encodeURIComponent(runId)}`, { signal });
        if (!detailResponse.ok) return normalizeRun(run);
        return normalizeRun(await detailResponse.json());
      }));
      if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
      const nextRuns = detailResults.flatMap((result) => result.status === "fulfilled" && result.value ? [result.value] : []);
      setRuns(nextRuns);
      setSelectedRunId((current) => nextRuns.some((run) => run.id === current) ? current : nextRuns[0]?.id || "");
      const owners = new Map(runOwnersRef.current);
      for (const run of nextRuns) owners.set(run.id, { project_id: projectId, title: projectTitle });
      runOwnersRef.current = owners;
      setRunsState("ready");
      return nextRuns;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      setRuns([]);
      setSelectedRunId("");
      setRunsState("error");
      throw error;
    }
  }, []);

  const loadChoiceCatalog = useCallback(async (profileId: string) => {
    if (!profileId) return;
    setCatalogBusy(profileId);
    setCatalogFeedback("正在读取非生成目录…");
    try {
      const response = await fetch(`${API}/api/providers/profiles/${encodeURIComponent(profileId)}/catalog`);
      const payload: unknown = await response.json();
      if (!response.ok) throw new Error(errorMessage(payload, "目录读取失败。"));
      if (!isRecord(payload) || payload.generation_performed !== false) {
        throw new Error("目录响应没有明确声明 generation_performed=false，已拒绝使用。");
      }
      const serviceKind = textValue(payload.service_kind) as ServiceKind;
      const catalog: ProviderChoiceCatalog = {
        profile_id: profileId,
        service_kind: serviceKind,
        models: unpackList(payload, "models").flatMap((item) => {
          const row = isRecord(item) ? item : {};
          const modelId = textValue(row.model_id);
          return modelId ? [{ model_id: modelId, label: textValue(row.label) || undefined }] : [];
        }),
        voices: unpackList(payload, "voices").flatMap((item) => {
          const row = isRecord(item) ? item : {};
          const voiceId = textValue(row.voice_id);
          return voiceId ? [{
            voice_id: voiceId,
            name: textValue(row.name) || undefined,
            description: textValue(row.description) || undefined,
            category: textValue(row.category) || undefined,
            language: textValue(row.language) || undefined,
          }] : [];
        }),
        generation_performed: false,
      };
      setChoiceCatalogs((current) => ({ ...current, [profileId]: catalog }));
      setCatalogFeedback(`目录已读取：${catalog.models.length} 个模型，${catalog.voices.length} 个音色；未生成内容。`);
    } catch (error) {
      setCatalogFeedback(error instanceof Error ? error.message : "目录读取失败；没有生成内容。 ");
    } finally {
      setCatalogBusy("");
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    async function bootstrap() {
      try {
        const health = await fetch(`${API}/api/workbench/health`, { signal: controller.signal });
        if (!health.ok) throw new Error("offline");
        setConnection("online");
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setConnection("offline");
      }
      await loadProjectsAndJobs(controller.signal).catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setProjects(PREVIEW_PROJECTS);
        setJobs(PREVIEW_JOBS);
        selectedIdRef.current = PREVIEW_PROJECTS[0].id;
        setSelectedId(PREVIEW_PROJECTS[0].id);
        setRuns([]);
        setSelectedRunId("");
        setRunsState("idle");
        setLibraryMode("preview");
      });
      const [profileResult, presetResult, catalogResult] = await Promise.allSettled([
        loadProviderProfiles(controller.signal),
        fetch(`${API}/api/workflow-presets`, { signal: controller.signal }),
        fetch(`${API}/api/providers/catalog`, { signal: controller.signal }),
      ]);
      if (profileResult.status === "rejected" && !controller.signal.aborted) setProviders([]);
      if (presetResult.status === "fulfilled" && presetResult.value.ok) {
        const payload: unknown = await presetResult.value.json();
        const next = extractPresets(payload);
        if (next.length) setPresets(next);
      }
      if (catalogResult.status === "fulfilled" && catalogResult.value.ok) {
        const payload: unknown = await catalogResult.value.json();
        const next = unpackList(payload, "providers").flatMap((entry) => {
          const row = isRecord(entry) ? entry : {};
          const providerId = textValue(row.provider_id) as ProviderId;
          const serviceKind = textValue(row.service_kind) as ServiceKind;
          if (!(providerId in PROVIDER_LABELS) || !["llm", "speech"].includes(serviceKind)) return [];
          return [{
            provider_id: providerId,
            service_kind: serviceKind,
            label: textValue(row.label, PROVIDER_LABELS[providerId]),
            default_base_url: textValue(row.default_base_url) || undefined,
            base_url_required: row.base_url_required === true,
            recommended_config: isRecord(row.recommended_config) ? row.recommended_config : {},
          }];
        });
        setProviderCatalog(next);
      }
    }
    void bootstrap();
    return () => controller.abort();
  }, [loadProjectsAndJobs, loadProviderProfiles]);

  useEffect(() => {
    if (libraryMode !== "live") return;
    const refreshController = { current: null as AbortController | null };
    const providerRefreshController = { current: null as AbortController | null };
    const after = lastEventIdRef.current > 0 ? `?after=${lastEventIdRef.current}` : "";
    const events = new EventSource(`${API}/api/events${after}`);
    const dataEvents = [
      "project.created",
      "project.updated",
      "run.created",
      "task.created",
      "task.updated",
      "provider_request.updated",
    ] as const;
    function refreshLibrary(event: MessageEvent) {
      const eventId = Number.parseInt(event.lastEventId, 10);
      if (Number.isFinite(eventId)) lastEventIdRef.current = Math.max(lastEventIdRef.current, eventId);
      refreshController.current?.abort();
      refreshController.current = new AbortController();
      void loadProjectsAndJobs(refreshController.current.signal).catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === "AbortError")) setEventState("offline");
      });
      const currentProject = selectedProjectRef.current;
      if (currentProject) {
        void loadProjectRuns(currentProject.id, currentProject.title, refreshController.current.signal).catch(() => undefined);
      }
    }
    function refreshProviders(event: MessageEvent) {
      const eventId = Number.parseInt(event.lastEventId, 10);
      if (Number.isFinite(eventId)) lastEventIdRef.current = Math.max(lastEventIdRef.current, eventId);
      providerRefreshController.current?.abort();
      providerRefreshController.current = new AbortController();
      void loadProviderProfiles(providerRefreshController.current.signal).catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === "AbortError")) setEventState("offline");
      });
    }
    events.onopen = () => setEventState("live");
    for (const eventName of dataEvents) events.addEventListener(eventName, refreshLibrary);
    events.addEventListener("provider.saved", refreshProviders);
    events.onerror = () => setEventState("offline");
    return () => {
      refreshController.current?.abort();
      providerRefreshController.current?.abort();
      for (const eventName of dataEvents) events.removeEventListener(eventName, refreshLibrary);
      events.removeEventListener("provider.saved", refreshProviders);
      events.close();
    };
  }, [libraryMode, loadProjectRuns, loadProjectsAndJobs, loadProviderProfiles]);

  useEffect(() => {
    if (!importOpen) return;
    const focusFrame = window.requestAnimationFrame(() => importInputRef.current?.focus());
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") setImportOpen(false);
    }
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, [importOpen]);

  useEffect(() => {
    function focusLibrarySearch(event: KeyboardEvent) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLocaleLowerCase("en-US") === "k") {
        event.preventDefault();
        searchInputRef.current?.focus();
      }
    }
    window.addEventListener("keydown", focusLibrarySearch);
    return () => window.removeEventListener("keydown", focusLibrarySearch);
  }, []);

  const selected = useMemo(
    () => projects.find((project) => project.id === selectedId) || null,
    [projects, selectedId],
  );
  const selectedRun = useMemo(
    () => runs.find((run) => run.id === selectedRunId) || null,
    [runs, selectedRunId],
  );
  const selectedProjectId = selected?.id || "";
  const selectedProjectTitle = selected?.title || "";

  useEffect(() => {
    selectedProjectRef.current = selected;
  }, [selected]);

  useEffect(() => {
    if (!selectedProjectId || libraryMode !== "live") return;
    const controller = new AbortController();
    const requestFrame = window.requestAnimationFrame(() => {
      void loadProjectRuns(selectedProjectId, selectedProjectTitle, controller.signal).catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          setWorkflowFeedback(error instanceof Error ? error.message : "运行记录读取失败。 ");
        }
      });
    });
    return () => {
      window.cancelAnimationFrame(requestFrame);
      controller.abort();
    };
  }, [libraryMode, loadProjectRuns, selectedProjectId, selectedProjectTitle]);

  const readyLlmProviders = useMemo(
    () => providers.filter((profile) => profile.service_kind === "llm" && profile.enabled && profile.credential.configured),
    [providers],
  );
  const readySpeechProviders = useMemo(
    () => providers.filter((profile) => profile.service_kind === "speech" && profile.enabled && profile.credential.configured),
    [providers],
  );
  const effectiveRoleBindings = useMemo(() => ({
    T: readyLlmProviders.some((profile) => profile.id === roleBindings.T) ? roleBindings.T : readyLlmProviders[0]?.id || "",
    A: readyLlmProviders.some((profile) => profile.id === roleBindings.A) ? roleBindings.A : readyLlmProviders[0]?.id || "",
    B: readyLlmProviders.some((profile) => profile.id === roleBindings.B) ? roleBindings.B : readyLlmProviders[0]?.id || "",
    C: readyLlmProviders.some((profile) => profile.id === roleBindings.C) ? roleBindings.C : readyLlmProviders[0]?.id || "",
  }), [readyLlmProviders, roleBindings]);
  const effectiveSpeechProfileId = readySpeechProviders.some((profile) => profile.id === speechProfileId)
    ? speechProfileId
    : readySpeechProviders[0]?.id || "";

  const counts = useMemo(() => ({
    all: projects.length,
    running: projects.filter((project) => ["draft", "queued", "running", "waiting_provider", "paused"].includes(project.status)).length,
    waiting_user: projects.filter((project) => project.status === "waiting_user").length,
    error: projects.filter((project) => ["repair_required", "blocked_uncertain"].includes(project.status)).length,
    complete: projects.filter((project) => ["machine_passed", "completed"].includes(project.status)).length,
  }), [projects]);

  const visibleProjects = useMemo(() => {
    const query = search.trim().toLocaleLowerCase("zh-CN");
    return projects.filter((project) => {
      const filterMatch = filter === "all"
        || (filter === "running" && ["draft", "queued", "running", "waiting_provider", "paused"].includes(project.status))
        || (filter === "error" && ["repair_required", "blocked_uncertain"].includes(project.status))
        || (filter === "complete" && ["machine_passed", "completed"].includes(project.status))
        || project.status === filter;
      return filterMatch && (!query || `${project.title} ${project.source} ${project.language}`.toLocaleLowerCase("zh-CN").includes(query));
    });
  }, [filter, projects, search]);

  const runningJobs = jobs.filter((job) => [
    "queued",
    "running",
    "pausing",
    "cancel_requested",
    "waiting_provider",
    "waiting_worker",
    "repair_required",
    "blocked_uncertain",
    "retry_wait",
  ].includes(job.status));

  const selectedRunJobs = selectedRun ? jobs.filter((job) => job.run_id === selectedRun.id) : [];
  const selectedStageMap = new Map(selectedRun?.stages.map((stage) => [stage.key, stage]) || []);
  const currentRunStage = selectedRun ? Number.parseInt(selectedRun.current_stage, 10) || 1 : 0;
  const activeRunTask = selectedRunJobs.some((job) => [
    "queued", "running", "pausing", "cancel_requested", "waiting_provider", "waiting_worker", "retry_wait",
  ].includes(job.status));
  const runHasUncertainRequest = selectedRunJobs.some((job) => job.status === "blocked_uncertain");
  const completedKinds = new Set(selectedRunJobs.filter((job) => job.status === "completed").map((job) => job.kind));
  const probeComplete = completedKinds.has("media.probe") || currentRunStage > 2;
  const adGatePassed = selectedStageMap.get("03")?.status === "completed" || currentRunStage > 3;
  const translationComplete = completedKinds.has("provider.llm.translation") || currentRunStage > 5;
  const translationGatePassed = selectedStageMap.get("05")?.status === "completed" && currentRunStage >= 6;
  const voiceLockPassed = selectedStageMap.get("06")?.status === "completed" || currentRunStage > 6;
  const auditionComplete = completedKinds.has("provider.speech.audition");
  const speechComplete = completedKinds.has("provider.speech.synthesize") || selected?.status === "machine_passed" || currentRunStage > 7;
  const machineQaPassed = selected?.status === "machine_passed" || (selected?.status === "waiting_user" && currentRunStage === 8);
  const workflowWritesDisabled = libraryMode !== "live" || !selectedRun || Boolean(workflowBusy) || activeRunTask || runHasUncertainRequest;
  const frozenSpeechProfileId = selectedRun && isRecord(selectedRun.provider_lock)
    ? textValue(selectedRun.provider_lock.speech_profile_id)
    : "";
  const frozenSpeechCatalog = frozenSpeechProfileId ? choiceCatalogs[frozenSpeechProfileId] : undefined;

  function patchWorkflowDraft<K extends keyof WorkflowDraft>(key: K, value: WorkflowDraft[K]) {
    setWorkflowDraft((current) => ({ ...current, [key]: value }));
  }

  function addVoiceMapping() {
    const role = workflowDraft.voice_role_id.trim();
    const voice = workflowDraft.voice_id.trim();
    if (!role || !voice) {
      setWorkflowFeedback("请先填写角色 ID 并从非生成目录选择或手动输入 voice_id。");
      return;
    }
    const lines = workflowDraft.voice_assignments.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    const nextLine = `${role}=${voice}`;
    const existing = lines.findIndex((line) => line.split("=", 1)[0]?.trim() === role);
    if (existing >= 0) lines[existing] = nextLine;
    else lines.push(nextLine);
    let names: Record<string, unknown> = {};
    try {
      names = parseJsonObject(workflowDraft.role_names_json, "角色显示名");
    } catch {
      names = {};
    }
    names[role] = workflowDraft.voice_role_name.trim() || role;
    setWorkflowDraft((current) => ({
      ...current,
      voice_assignments: lines.join("\n"),
      role_names_json: JSON.stringify(names, null, 2),
    }));
    setWorkflowFeedback(`已把 ${role} → ${voice} 加入待锁定映射；尚未提交到后端。`);
  }

  async function runWorkflowAction(
    action: string,
    endpoint: string,
    payload: Record<string, unknown>,
    successMessage: string,
  ): Promise<Record<string, unknown> | null> {
    if (!selected || !selectedRun || libraryMode !== "live" || workflowBusy) return null;
    if (runHasUncertainRequest) {
      setWorkflowFeedback("当前运行存在计费状态待核对请求。为避免重复计费，所有下游提交已锁定；请先在任务坞核对请求账本。");
      return null;
    }
    setWorkflowBusy(action);
    setWorkflowFeedback("正在提交到本地运行器…");
    try {
      const response = await fetch(`${API}/api/workflows/runs/${encodeURIComponent(selectedRun.id)}/${endpoint}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const value: unknown = await response.json();
      if (!response.ok) throw new Error(errorMessage(value, "本地运行器拒绝了这次提交。"));
      const result = isRecord(value) ? value : {};
      setWorkflowResult((current) => ({ ...current, [action]: result }));
      setWorkflowFeedback(successMessage);
      if (action === "transcript" && isRecord(result.artifact)) {
        const path = textValue(result.artifact.path);
        if (path) setWorkflowDraft((current) => ({ ...current, frozen_source_path: path, slots_path: path }));
      }
      if (action === "chapter") {
        const readingPath = textValue(result.reading_path);
        const validationPath = textValue(result.validation_path);
        setWorkflowDraft((current) => ({
          ...current,
          reading_path: readingPath || current.reading_path,
          validation_path: validationPath || current.validation_path,
        }));
      }
      if (action === "approval") {
        const gatePath = textValue(result.translation_gate_path);
        if (gatePath) setWorkflowDraft((current) => ({ ...current, translation_gate_path: gatePath }));
      }
      if (action === "voice-lock") {
        const lockPath = textValue(result.voice_lock_path);
        const segmentsPath = textValue(result.segments_path);
        if (lockPath || segmentsPath) {
          setWorkflowDraft((current) => ({
            ...current,
            voice_lock_path: lockPath || current.voice_lock_path,
            segments_path: segmentsPath || current.segments_path,
          }));
        }
      }
      if (action === "publication") setPublicationText(textValue(result.copyable_text));
      await Promise.allSettled([
        loadProjectsAndJobs(),
        loadProjectRuns(selected.id, selected.title),
      ]);
      return result;
    } catch (error) {
      const message = error instanceof Error ? error.message : "提交失败；已填写内容仍保留。";
      setWorkflowFeedback(`${message} 已填写内容仍保留，请修正后再次提交。`);
      return null;
    } finally {
      setWorkflowBusy("");
    }
  }

  async function submitProbe() {
    await runWorkflowAction("probe", "probe", {
      expected_language: workflowDraft.expected_language.trim() || null,
      cookie_file: workflowDraft.cookie_file.trim() || null,
    }, "媒体探测已加入任务坞；自动格式选择结果会写入 QA。 ");
  }

  async function submitDownload() {
    await runWorkflowAction("download", "download", {
      expected_language: workflowDraft.expected_language.trim() || null,
      cookie_file: workflowDraft.cookie_file.trim() || null,
    }, "下载已加入任务坞；源母版不会被后续步骤覆盖。 ");
  }

  async function submitTranscript() {
    const version = Number.parseInt(workflowDraft.version, 10);
    if (!Number.isInteger(version) || version < 1) {
      setWorkflowFeedback("源字幕版本必须是大于 0 的整数。");
      return;
    }
    await runWorkflowAction("transcript", "ingest/source-transcript", {
      source_path: workflowDraft.transcript_path.trim(),
      language: workflowDraft.source_language.trim() || null,
      version,
    }, "源字幕已导入并冻结稳定 ID。 ");
  }

  async function submitEmbeddedSubtitle() {
    const version = Number.parseInt(workflowDraft.version, 10);
    const streamIndex = Number.parseInt(workflowDraft.embedded_stream_index, 10);
    if (!Number.isInteger(streamIndex) || streamIndex < 0) {
      setWorkflowFeedback("内嵌字幕流索引必须是大于或等于 0 的整数；请使用媒体探测报告中的 stream_index。");
      return;
    }
    await runWorkflowAction("embedded-subtitle", "ingest/embedded-subtitle", {
      source_path: workflowDraft.source_master_path.trim(),
      output_path: workflowDraft.embedded_output_path.trim() || null,
      stream_index: streamIndex,
      language: workflowDraft.source_language.trim() || null,
      version,
    }, "内嵌字幕提取已加入任务坞；FFmpeg 成功后会同时冻结字幕、稳定 ID 与文本投影。 ");
  }

  async function submitAdGate() {
    if (workflowDraft.ad_input_mode === "no_ads" && !workflowDraft.no_ads_scan_confirmed) {
      setWorkflowFeedback("“未检测到广告”只有在内容、画面和语义扫描均已完整完成后才能提交。请先完成扫描并勾选确认。");
      return;
    }
    const duration = Number(workflowDraft.source_duration);
    if (!Number.isFinite(duration) || duration <= 0) {
      setWorkflowFeedback("广告证据需要填写真实源视频时长（秒）。");
      return;
    }
    let analysis: Record<string, unknown>;
    try {
      analysis = workflowDraft.ad_input_mode === "no_ads" ? {
        status: "pass",
        decision: "no_ads_detected",
        content_scan_complete: true,
        visual_scan_complete: true,
        semantic_analysis_required: true,
        semantic_analysis_complete: true,
        analysis_method: "operator_confirmed_full_content_visual_semantic_scan",
        decision_rule_version: "ad-evidence-v1",
        candidates: [],
      } : parseJsonObject(workflowDraft.ad_analysis_json, "广告证据/计划");
    } catch (error) {
      setWorkflowFeedback(error instanceof Error ? error.message : "广告证据/计划无效。 ");
      return;
    }
    await runWorkflowAction("ad-edit", "ad-edit", {
      source_master_path: workflowDraft.source_master_path.trim(),
      working_master_path: workflowDraft.working_master_path.trim(),
      analysis,
      source_duration: duration,
      frame_width: Number(workflowDraft.frame_width) || null,
      frame_height: Number(workflowDraft.frame_height) || null,
      frozen_source_path: workflowDraft.frozen_source_path.trim() || null,
      version: Number.parseInt(workflowDraft.version, 10),
    }, "广告工作母版任务已加入任务坞；完成剪除或安全 remux 并校验后才会生成 ad_edit_gate。 ");
  }

  async function submitCoverSource() {
    const atSeconds = Number(workflowDraft.cover_source_at_seconds);
    if (!Number.isFinite(atSeconds) || atSeconds < 0) {
      setWorkflowFeedback("封面源帧时间必须是大于或等于 0 的秒数。");
      return;
    }
    try {
      const publication = parseJsonObject(workflowDraft.publication_json, "发布包证据/计划");
      await runWorkflowAction("cover-source", "cover-source", {
        working_master_path: workflowDraft.working_master_path.trim(),
        at_seconds: atSeconds,
        output_path: textValue(publication.cover_source_path) || null,
        version: Number.parseInt(workflowDraft.version, 10),
      }, "封面源帧抽取已加入任务坞；结果只作为发布封面输入，不会自动宣称已获授权或已核对人物。 ");
    } catch (error) {
      setWorkflowFeedback(error instanceof Error ? error.message : "发布包证据/计划无效。 ");
    }
  }

  async function submitTranslation() {
    const version = Number.parseInt(workflowDraft.version, 10);
    const batchSize = Number.parseInt(workflowDraft.batch_size, 10);
    await runWorkflowAction("translate", "translate", {
      slots_path: workflowDraft.slots_path.trim(),
      glossary_path: workflowDraft.glossary_path.trim() || null,
      project_context: workflowDraft.project_context,
      version,
      batch_size: batchSize,
    }, "T/A/B/C 翻译任务已加入任务坞；A/B 将分别覆盖全文。 ");
  }

  async function submitChapterReading() {
    try {
      const chapters = parseJsonArray(workflowDraft.chapters_json, "章节证据");
      const version = Number.parseInt(workflowDraft.version, 10);
      await runWorkflowAction("chapter", "chapter-reading", {
        translation_path: workflowDraft.translation_path.trim(),
        chapters: chapters.length ? chapters : null,
        timeline_path: workflowDraft.timeline_path.trim() || null,
        version,
      }, "章节阅读稿已按正式翻译逐字生成并进入人工审阅。 ");
    } catch (error) {
      setWorkflowFeedback(error instanceof Error ? error.message : "章节证据无效。 ");
    }
  }

  async function submitTranslationApproval() {
    if (!workflowDraft.approval_command.trim()) {
      setWorkflowFeedback("请输入明确下游指令，例如“开始选音色”。这条指令会绑定当前文件哈希。");
      return;
    }
    await runWorkflowAction("approval", "approve-translation", {
      command: workflowDraft.approval_command,
      translation_path: workflowDraft.translation_path.trim(),
      reading_path: workflowDraft.reading_path.trim(),
      validation_path: workflowDraft.validation_path.trim(),
      version: Number.parseInt(workflowDraft.version, 10),
    }, "翻译批准已绑定当前翻译、阅读稿和验证报告的实际哈希。 ");
  }

  async function submitVoiceLock() {
    try {
      const assignments = parseVoiceAssignments(workflowDraft.voice_assignments);
      const names = parseJsonObject(workflowDraft.role_names_json, "角色显示名");
      await runWorkflowAction("voice-lock", "voice-lock", {
        assignments,
        role_names: Object.fromEntries(Object.entries(names).map(([key, value]) => [key, String(value)])),
        translation_gate_path: workflowDraft.translation_gate_path.trim(),
        version: Number.parseInt(workflowDraft.version, 10),
      }, "角色与 voice_id 已锁定；正式语音仍固定为原生 1.0×。 ");
    } catch (error) {
      setWorkflowFeedback(error instanceof Error ? error.message : "音色映射无效。 ");
    }
  }

  async function submitGenerateFull() {
    if (!/生成全片|生成全文/.test(workflowDraft.full_command.replace(/\s+/g, ""))) {
      setWorkflowFeedback("全文付费 TTS 需要你明确输入“生成全片”或“生成全文”。自动 dry-run 通过后会直接继续。 ");
      return;
    }
    await runWorkflowAction("generate-full", "generate-full", {
      command: workflowDraft.full_command,
      segments_path: workflowDraft.segments_path.trim(),
      voice_lock_path: workflowDraft.voice_lock_path.trim(),
      translation_gate_path: workflowDraft.translation_gate_path.trim(),
      profile_id: frozenSpeechProfileId || null,
      output_dir: workflowDraft.speech_output_dir.trim(),
      version: Number.parseInt(workflowDraft.version, 10),
    }, "明确全文指令已提交；本地 dry-run 通过后将直接按原生 1.0× 生成，不再二次确认。 ");
  }

  async function submitAudition() {
    await runWorkflowAction("audition", "audition", {
      version: Number.parseInt(workflowDraft.version, 10),
    }, "一分钟原速试听已加入任务坞；音色锁即为本次试听授权，不会请求全文 TTS 授权。 ");
  }

  async function submitRender() {
    try {
      const plan = parseJsonObject(workflowDraft.render_json, "渲染证据/计划");
      await runWorkflowAction("render", "render", plan, "渲染计划已提交；production_gate 通过后才会开始 FFmpeg 成片。 ");
    } catch (error) {
      setWorkflowFeedback(error instanceof Error ? error.message : "渲染证据/计划无效。 ");
    }
  }

  async function submitPublication() {
    try {
      const plan = parseJsonObject(workflowDraft.publication_json, "发布包证据/计划");
      await runWorkflowAction("publication", "publication-package", plan, "发布包机器门禁已执行；通过后仍等待人工抽看。 ");
    } catch (error) {
      setWorkflowFeedback(error instanceof Error ? error.message : "发布包证据/计划无效。 ");
    }
  }

  async function controlJob(job: JobRecord, action: "pause" | "resume" | "cancel") {
    if (jobActionBusy) return;
    if (job.status === "blocked_uncertain") {
      setJobFeedback("计费状态待核对时禁止直接重试或用取消掩盖结果。请先核对 Provider 请求账本。 ");
      return;
    }
    setJobActionBusy(`${job.id}:${action}`);
    setJobFeedback("");
    try {
      const response = await fetch(`${API}/api/jobs/${encodeURIComponent(job.id)}/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const payload: unknown = await response.json();
      if (!response.ok) throw new Error(errorMessage(payload, "任务状态变更失败。"));
      setJobFeedback(action === "pause" ? "已请求在安全检查点暂停。" : action === "resume" ? "任务已加入恢复队列。" : "已请求在安全检查点取消。 ");
      await loadProjectsAndJobs();
    } catch (error) {
      setJobFeedback(error instanceof Error ? error.message : "任务状态变更失败。 ");
    } finally {
      setJobActionBusy("");
    }
  }

  function selectProject(project: ProjectRecord | null, openInspector = true) {
    selectedIdRef.current = project?.id || null;
    selectedProjectRef.current = project;
    setSelectedId(project?.id || null);
    setRuns([]);
    setSelectedRunId("");
    setRunsState(project && libraryMode === "live" ? "loading" : "idle");
    setWorkflowDraft(project ? createWorkflowDraftForProject(project) : createWorkflowDraft());
    setWorkflowFeedback("");
    setWorkflowResult({});
    setPublicationText("");
    if (openInspector) setInspectorOpen(Boolean(project));
  }

  async function submitImport(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const source = importSource.trim();
    if (!source) {
      setImportError(importKind === "video_url" ? "请输入视频链接。" : "请输入本机视频的完整路径。");
      return;
    }
    setImportBusy(true);
    setImportError("");
    try {
      const response = await fetch(`${API}/api/library/projects`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_kind: importKind, source }),
      });
      const payload: unknown = await response.json();
      if (!response.ok) {
        throw new Error(errorMessage(payload, "导入失败，请检查来源后重试。"));
      }
      const created = normalizeProject(payload, 0);
      setProjects((current) => [created, ...current.filter((project) => project.id !== created.id)]);
      selectProject(created);
      setLibraryMode("live");
      setImportOpen(false);
      setImportSource("");
      setFilter("all");
    } catch (error) {
      setImportError(error instanceof Error ? error.message : "本地服务暂时没有响应，导入内容尚未保存。 ");
    } finally {
      setImportBusy(false);
    }
  }

  async function startRun() {
    if (!selected || libraryMode === "preview") return;
    const readyLlmIds = new Set(
      providers
        .filter((profile) => profile.service_kind === "llm" && profile.enabled && profile.credential.configured)
        .map((profile) => profile.id),
    );
    const missingRoles = (Object.keys(effectiveRoleBindings) as TranslationRole[]).filter((role) => !readyLlmIds.has(effectiveRoleBindings[role]));
    if (missingRoles.length > 0) {
      setRunFeedback(`尚未为 ${missingRoles.join(" / ")} 绑定已配置的翻译 Provider。请前往“设置 → 翻译服务”完成绑定。`);
      return;
    }
    const readySpeech = providers.some((profile) => (
      profile.id === effectiveSpeechProfileId
      && profile.service_kind === "speech"
      && profile.enabled
      && profile.credential.configured
    ));
    if (!readySpeech) {
      setRunFeedback("尚未绑定已配置的语音 Provider。请前往“设置 → 语音服务”完成绑定。");
      return;
    }
    const presetId = presets[0]?.id || "quality-zh-v1";
    setRunFeedback("正在创建并冻结本次运行配置…");
    try {
      const response = await fetch(`${API}/api/library/projects/${encodeURIComponent(selected.id)}/runs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          preset_id: presetId,
          role_bindings: effectiveRoleBindings,
          speech_profile_id: effectiveSpeechProfileId,
        }),
      });
      const payload: unknown = await response.json();
      if (!response.ok) throw new Error(errorMessage(payload, "无法创建冻结运行。"));
      setRunFeedback("运行已创建；T/A/B/C、语音 Provider 与提示词版本已冻结。");
      const createdRun = normalizeRun(payload);
      if (createdRun) setSelectedRunId(createdRun.id);
      await Promise.allSettled([
        loadProjectsAndJobs(),
        loadProjectRuns(selected.id, selected.title),
      ]);
    } catch (error) {
      setRunFeedback(error instanceof Error ? error.message : "无法创建冻结运行，请检查本地服务。 ");
      setInspectorTab("overview");
    }
  }

  async function saveProvider(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!providerDraft.display_name.trim() || !providerDraft.model.trim() || !providerDraft.api_key.trim()) {
      setProviderFeedback("请填写名称、模型和 API Key。Key 只会发送给本机凭证服务。 ");
      return;
    }
    setProviderBusy(true);
    setProviderFeedback("");
    let profileWasSaved = false;
    try {
      const response = await fetch(`${API}/api/providers/profiles`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          service_kind: providerDraft.service_kind,
          provider_id: providerDraft.provider_id,
          display_name: providerDraft.display_name.trim(),
          base_url: providerDraft.base_url.trim() || null,
          model: providerDraft.model.trim(),
          api_key: providerDraft.api_key,
          config: providerDraft.config,
        }),
      });
      const payload: unknown = await response.json();
      if (!response.ok) throw new Error(errorMessage(payload, "保存失败。"));
      profileWasSaved = true;
      const next = extractProfiles({ profiles: [payload] })[0];
      if (next) setProviders((current) => [next, ...current.filter((profile) => profile.id !== next.id)]);
      setProviderDraft((current) => ({ ...current, api_key: "" }));
      setProviderFeedback("配置已保存到本机凭证库；正在进行不生成内容的连接探测…");
      if (!next) return;
      const probeResponse = await fetch(`${API}/api/providers/profiles/${encodeURIComponent(next.id)}/probe`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      const probePayload: unknown = await probeResponse.json();
      if (!probeResponse.ok) {
        setProviderFeedback(`配置已保存，但连接探测失败：${errorMessage(probePayload, "Provider 没有返回可用能力。")} 请检查接口地址、模型和 Key。`);
        return;
      }
      const probed = isRecord(probePayload)
        ? extractProfiles({ profiles: [probePayload.profile] })[0]
        : undefined;
      if (probed) setProviders((current) => [probed, ...current.filter((profile) => profile.id !== probed.id)]);
      setProviderFeedback("配置已保存，连接探测通过；未生成翻译或语音内容。");
    } catch (error) {
      const message = error instanceof Error ? error.message : "本地服务不可用。";
      setProviderFeedback(profileWasSaved
        ? `配置已保存，但连接探测未完成：${message} 你可以稍后再次探测；不会自动生成内容。`
        : `配置未保存：${message}`);
    } finally {
      setProviderBusy(false);
    }
  }

  function chooseFilter(next: LibraryFilter) {
    setFilter(next);
    setSurface("library");
    setNavOpen(false);
  }

  if (surface === "studio") {
    return (
      <div className="wb-studio-mode">
        <div className="wb-studio-return">
          <button type="button" onClick={() => setSurface("library")}><span aria-hidden="true">←</span> 返回视频库</button>
          <p>兼容制作台 · 当前项目的选音与一分钟试听</p>
        </div>
        {studio}
      </div>
    );
  }

  return (
    <main className={`wb-app ${dockOpen ? "dock-open" : ""}`}>
      <header className="wb-topbar">
        <button className="wb-mobile-menu" type="button" aria-label="打开资料库导航" aria-expanded={navOpen} onClick={() => setNavOpen((value) => !value)}>☰</button>
        <button className="wb-brand" type="button" onClick={() => { setSurface("library"); setFilter("all"); }}>
          <span aria-hidden="true">译</span>
          <strong>声轨工坊<small>VIDEO DUBBING LIBRARY</small></strong>
        </button>
        <label className="wb-search">
          <span aria-hidden="true">⌕</span>
          <span className="visuallyHidden">搜索视频库</span>
          <input ref={searchInputRef} value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索标题、来源或语言" />
          {search && <button type="button" aria-label="清空搜索" onClick={() => { setSearch(""); searchInputRef.current?.focus(); }}>×</button>}
          <kbd>Ctrl K</kbd>
        </label>
        <div className="wb-top-actions">
          <span className={`wb-connection ${connection}`}><i aria-hidden="true" />{connection === "online" ? "本地运行器在线" : connection === "offline" ? "本地运行器离线" : "正在连接"}</span>
          <button className="wb-import-button" type="button" onClick={() => { setImportOpen(true); setImportError(""); }}><span aria-hidden="true">＋</span> 导入视频</button>
        </div>
      </header>

      <div className="wb-layout">
        <aside className={`wb-sidebar ${navOpen ? "open" : ""}`} aria-label="资料库导航">
          <nav>
            <p>视频库</p>
            {(Object.keys(FILTER_LABELS) as LibraryFilter[]).map((item) => (
              <button key={item} type="button" className={surface === "library" && filter === item ? "active" : ""} onClick={() => chooseFilter(item)}>
                <span className={`wb-nav-icon ${item}`} aria-hidden="true" />
                {FILTER_LABELS[item]}
                <em>{displayCount(counts[item])}</em>
              </button>
            ))}
          </nav>
          <nav>
            <p>制作资源</p>
            <button type="button" onClick={() => setSurface("studio")}><span className="wb-nav-icon voices" aria-hidden="true" />声音库</button>
            <button type="button" onClick={() => setDockOpen(true)}><span className="wb-nav-icon queue" aria-hidden="true" />任务队列<em>{displayCount(runningJobs.length)}</em></button>
            <button type="button" onClick={() => { setFilter("all"); setSurface("library"); }}><span className="wb-nav-icon history" aria-hidden="true" />运行记录</button>
          </nav>
          <nav className="wb-sidebar-bottom">
            <button type="button" className={surface === "settings" ? "active" : ""} onClick={() => { setSurface("settings"); setNavOpen(false); }}><span className="wb-nav-icon settings" aria-hidden="true" />设置</button>
            <div className="wb-library-root"><span aria-hidden="true">LOCAL</span><p><strong>本地资料库</strong><small>{libraryMode === "live" ? "已连接并监听事件" : libraryMode === "preview" ? "接口待接入" : "准备就绪"}</small></p></div>
          </nav>
        </aside>

        {navOpen && <button className="wb-sidebar-scrim" type="button" aria-label="关闭资料库导航" onClick={() => setNavOpen(false)} />}

        {surface === "library" ? (
          <>
            <section className="wb-library" aria-labelledby="library-heading">
              <div className="wb-library-head">
                <div>
                  <p className="eyebrow">LIBRARY / {filter.toUpperCase()}</p>
                  <h1 id="library-heading">{FILTER_LABELS[filter]}</h1>
                  <p>{visibleProjects.length} 个项目 · 自动保存进度与可恢复检查点</p>
                </div>
                <div className="wb-view-actions" aria-label="列表操作">
                  <button type="button" onClick={() => { void loadProjectsAndJobs(); }} aria-label="刷新资料库">↻</button>
                  <button type="button" className="active" aria-pressed="true" aria-label="密集列表视图" disabled title="当前版本仅提供密集列表视图">≡</button>
                </div>
              </div>

              {libraryMode === "preview" && (
                <div className="wb-preview-banner" role="status">
                  <span>PREVIEW</span>
                  <p><strong>正在展示界面预览数据</strong>视频库接口尚未响应；下列项目不会被当作本机已保存内容。</p>
                  <button type="button" onClick={() => { setProjects([]); setJobs([]); selectProject(null, false); setLibraryMode("empty"); }}>关闭预览</button>
                </div>
              )}

              <div className="wb-table-head" aria-hidden="true">
                <span>视频项目</span><span>当前阶段</span><span>真实进度</span><span>更新时间</span>
              </div>
              <div className="wb-project-list" aria-label={`${FILTER_LABELS[filter]}项目列表`}>
                {libraryMode === "loading" && (
                  <div className="wb-loading-state"><i aria-hidden="true" /><strong>正在读取本地资料库</strong><span>只读取项目索引，不会自动启动任何付费任务。</span></div>
                )}
                {libraryMode !== "loading" && visibleProjects.map((project) => {
                  const currentStage = STAGES[project.stage_index] || STAGES[0];
                  return (
                    <button
                      type="button"
                      className={`wb-project-row ${selectedId === project.id ? "selected" : ""}`}
                      key={project.id}
                      aria-pressed={selectedId === project.id}
                      onClick={() => selectProject(project)}
                    >
                      <span className="wb-project-main">
                        <i className={`wb-thumbnail stage-${project.stage_index}`} aria-hidden="true"><b>{String(project.stage_index + 1).padStart(2, "0")}</b></i>
                        <span><strong>{project.title}</strong><small>{project.source_kind === "video_url" ? "在线视频" : "本机视频"} · {project.duration_label} · {project.resolution}</small></span>
                      </span>
                      <span className="wb-stage-cell"><StatusBadge status={project.status} /><small>{String(project.stage_index + 1).padStart(2, "0")} · {currentStage.short}</small></span>
                      <span className="wb-progress-cell"><strong>{project.stages_passed}/8 阶段通过</strong><ProgressLine progress={project.progress} compact /></span>
                      <span className="wb-updated">{project.updated_at}<small>{project.language}</small></span>
                    </button>
                  );
                })}
                {libraryMode !== "loading" && visibleProjects.length === 0 && (
                  <div className="wb-empty-state">
                    <span aria-hidden="true">＋</span>
                    <strong>{search ? "没有匹配的视频" : "资料库还是空的"}</strong>
                    <p>{search ? "换个关键词，或清空搜索查看全部项目。" : "从链接或本机路径导入第一个视频；探测通过后会自动进入生产轨。"}</p>
                    {!search && <button type="button" onClick={() => setImportOpen(true)}>导入第一个视频</button>}
                  </div>
                )}
              </div>
            </section>

            <aside className={`wb-inspector ${inspectorOpen ? "open" : ""}`} aria-label="项目检查器">
              {selected ? (
                <>
                  <div className="wb-inspector-head">
                    <div><p className="eyebrow">PROJECT INSPECTOR</p><h2>{selected.title}</h2></div>
                    <button type="button" className="wb-inspector-close" aria-label="关闭项目检查器" onClick={() => setInspectorOpen(false)}>×</button>
                    <StatusBadge status={selected.status} />
                    <dl><div><dt>片长</dt><dd>{selected.duration_label}</dd></div><div><dt>画面</dt><dd>{selected.resolution}</dd></div><div><dt>语言</dt><dd>{selected.language}</dd></div></dl>
                    <label className="wb-run-switcher" htmlFor="wb-current-run">
                      <span>当前运行</span>
                      <select
                        id="wb-current-run"
                        value={selectedRunId}
                        disabled={runsState === "loading" || runs.length === 0}
                        onChange={(event) => { setSelectedRunId(event.target.value); setWorkflowFeedback(""); }}
                      >
                        {runs.length === 0 && <option value="">{runsState === "loading" ? "正在读取运行…" : "尚未创建运行"}</option>}
                        {runs.map((run, index) => <option key={run.id} value={run.id}>#{runs.length - index} · 阶段 {run.current_stage} · {displayTimestamp(run.updated_at)}</option>)}
                      </select>
                    </label>
                  </div>
                  <div className="wb-inspector-tabs" role="tablist" aria-label="项目详情">
                    {([
                      ["overview", "概览"], ["translation", "翻译"], ["voices", "音色"], ["qa", "QA"], ["history", "记录"],
                    ] as [InspectorTab, string][]).map(([id, label]) => <button key={id} type="button" role="tab" aria-selected={inspectorTab === id} className={inspectorTab === id ? "active" : ""} onClick={() => setInspectorTab(id)}>{label}</button>)}
                  </div>
                  <div className="wb-inspector-body">
                    {inspectorTab === "overview" && (
                      <>
                        <section className="wb-current-step">
                          <div><p>当前交接点</p><strong>{selectedRun ? STAGES[Math.max(0, currentRunStage - 1)]?.label : "先创建冻结运行"}</strong><span>{selectedRun ? `运行 ${selectedRun.id} · 阶段 ${selectedRun.current_stage}` : "Provider 与提示词尚未冻结"}</span></div>
                          <ProgressLine progress={selected.progress} />
                          {selected.issue && <div className={`wb-issue ${selected.status === "waiting_user" ? "warning" : "danger"}`}><span aria-hidden="true">!</span><p>{selected.issue}</p></div>}
                          {runHasUncertainRequest && <div className="wb-issue danger"><span aria-hidden="true">!</span><p>外部请求可能已经受理，但本机没有确定结果。为避免重复计费，下游阶段已锁定；请在任务坞核对请求账本。</p></div>}
                        </section>
                        <section className="wb-production-rail" aria-labelledby="production-rail-title">
                          <div className="wb-section-title"><h3 id="production-rail-title">八阶段生产轨</h3><span>按钮随门禁放行</span></div>
                          <ol>
                            {STAGES.map((stage, index) => {
                              const record = selectedRun?.stages.find((item) => item.key === String(index + 1).padStart(2, "0"));
                              const state = record?.status === "completed" || index + 1 < currentRunStage
                                ? "passed"
                                : index + 1 === currentRunStage ? "current" : "future";
                              const enabled = Boolean(selectedRun) && index + 1 <= Math.max(1, currentRunStage);
                              return <li key={stage.id} className={state}><button type="button" disabled={!enabled} aria-label={`${stage.short}：${enabled ? "打开阶段操作" : "等待前置门禁"}`} onClick={() => setInspectorTab(tabForStage(index))}><i aria-hidden="true">{state === "passed" ? "✓" : String(index + 1)}</i><span><strong>{stage.short}</strong><small>{record?.detail || stage.label}</small></span>{state === "current" && <em>当前</em>}</button></li>;
                            })}
                          </ol>
                        </section>
                        {!selectedRun && <InspectorNote code="01 / LOCK" title="先冻结一次运行" text="创建运行只冻结 T/A/B/C、语音 Provider 与提示词版本，不会自动下载、翻译或产生付费语音。" />}
                        <WorkflowCard code="02 / MEDIA" title="探测与下载" description="探测会自动选择格式和源语言字幕；链接下载会一并登记可用字幕，缺少字幕时明确要求导入或设备 ASR。Cookie 路径留空时不会读取浏览器登录态。">
                          <form noValidate onSubmit={(event) => { event.preventDefault(); void submitProbe(); }}>
                            <div className="wb-workflow-grid">
                              <label>源语言代码<input value={workflowDraft.expected_language} onChange={(event) => patchWorkflowDraft("expected_language", event.target.value)} placeholder="en" /></label>
                              <label>Cookie 文件路径<input value={workflowDraft.cookie_file} onChange={(event) => patchWorkflowDraft("cookie_file", event.target.value)} placeholder="可选，仅本机读取" /></label>
                            </div>
                            <div className="wb-workflow-actions">
                              <button type="submit" disabled={workflowWritesDisabled || currentRunStage > 2} aria-busy={workflowBusy === "probe"}>{workflowBusy === "probe" ? "正在提交…" : "探测并自动选格式"}</button>
                              <button type="button" className="secondary" disabled={workflowWritesDisabled || selected.source_kind !== "video_url" || !probeComplete || currentRunStage > 3 || adGatePassed} onClick={() => { void submitDownload(); }}>{workflowBusy === "download" ? "正在提交…" : "下载源母版"}</button>
                            </div>
                            {!selectedRun && <small className="wb-disabled-reason">先创建并选择一个冻结运行。</small>}
                            {selected.source_kind !== "video_url" && <small className="wb-disabled-reason">本机视频不需要下载，只执行媒体探测。</small>}
                          </form>
                        </WorkflowCard>

                        <WorkflowCard code="03 / SOURCE" title="提取或导入并冻结源字幕" description="可从母版内嵌字幕流实际提取，或导入项目目录内的 SRT、VTT、JSON；两条路径都会冻结稳定 ID。">
                          <form noValidate onSubmit={(event) => { event.preventDefault(); void submitTranscript(); }}>
                            <div className="wb-workflow-grid">
                              <label className="wide">源字幕相对路径<input value={workflowDraft.transcript_path} onChange={(event) => patchWorkflowDraft("transcript_path", event.target.value)} /></label>
                              <label>语言<input value={workflowDraft.source_language} onChange={(event) => patchWorkflowDraft("source_language", event.target.value)} /></label>
                              <label>版本<input inputMode="numeric" value={workflowDraft.version} onChange={(event) => patchWorkflowDraft("version", event.target.value)} /></label>
                              <label>内嵌字幕流索引<input inputMode="numeric" value={workflowDraft.embedded_stream_index} onChange={(event) => patchWorkflowDraft("embedded_stream_index", event.target.value)} /></label>
                              <label className="wide">内嵌字幕输出相对路径<input value={workflowDraft.embedded_output_path} onChange={(event) => patchWorkflowDraft("embedded_output_path", event.target.value)} /></label>
                            </div>
                            <div className="wb-workflow-actions"><button type="submit" disabled={workflowWritesDisabled || currentRunStage > 3 || adGatePassed}>{workflowBusy === "transcript" ? "正在冻结…" : "导入源字幕"}</button><button type="button" className="secondary" disabled={workflowWritesDisabled || currentRunStage > 3 || adGatePassed} onClick={() => { void submitEmbeddedSubtitle(); }}>{workflowBusy === "embedded-subtitle" ? "正在入队…" : "提取内嵌字幕并冻结"}</button></div>
                          </form>
                        </WorkflowCard>

                        <WorkflowCard code="03 / AD GATE" title="广告证据与工作母版" description="这里不冒充自动检测。只有确实完成整片内容、画面和语义扫描，才能选择“未检测到广告”。">
                          <div className="wb-evidence-mode" role="group" aria-label="广告证据输入方式">
                            <button type="button" aria-pressed={workflowDraft.ad_input_mode === "no_ads"} className={workflowDraft.ad_input_mode === "no_ads" ? "active" : ""} onClick={() => patchWorkflowDraft("ad_input_mode", "no_ads")}>完整扫描：无广告</button>
                            <button type="button" aria-pressed={workflowDraft.ad_input_mode === "advanced"} className={workflowDraft.ad_input_mode === "advanced" ? "active" : ""} onClick={() => patchWorkflowDraft("ad_input_mode", "advanced")}>高级证据 JSON</button>
                          </div>
                          <form noValidate onSubmit={(event) => { event.preventDefault(); void submitAdGate(); }}>
                            <div className="wb-workflow-grid">
                              <label className="wide">原始母版相对路径<input value={workflowDraft.source_master_path} onChange={(event) => patchWorkflowDraft("source_master_path", event.target.value)} /></label>
                              <label className="wide">正式工作母版相对路径<input value={workflowDraft.working_master_path} onChange={(event) => patchWorkflowDraft("working_master_path", event.target.value)} /><small>必须是与原始母版不同的实体文件，不能覆盖或硬链接。</small></label>
                              <label>源时长（秒）<input inputMode="decimal" value={workflowDraft.source_duration} onChange={(event) => patchWorkflowDraft("source_duration", event.target.value)} placeholder="例如 3780.5" /></label>
                              <label>画面尺寸<input value={`${workflowDraft.frame_width}×${workflowDraft.frame_height}`} onChange={(event) => { const [width = "", height = ""] = event.target.value.split(/[×x]/); setWorkflowDraft((current) => ({ ...current, frame_width: width.trim(), frame_height: height.trim() })); }} placeholder="1920×1080" /></label>
                              <label className="wide">冻结源文相对路径<input value={workflowDraft.frozen_source_path} onChange={(event) => patchWorkflowDraft("frozen_source_path", event.target.value)} /></label>
                            </div>
                            {workflowDraft.ad_input_mode === "no_ads" ? (
                              <label className="wb-confirm-check"><input type="checkbox" checked={workflowDraft.no_ads_scan_confirmed} onChange={(event) => patchWorkflowDraft("no_ads_scan_confirmed", event.target.checked)} /><span><strong>我已完整扫描整片内容、画面与语义，未发现广告候选。</strong>这不是自动检测结果；未完成扫描时不要勾选。</span></label>
                            ) : (
                              <label className="wb-json-field">广告检测证据 / 处置计划 JSON<textarea className="resize-none" value={workflowDraft.ad_analysis_json} onChange={(event) => patchWorkflowDraft("ad_analysis_json", event.target.value)} spellCheck={false} /><small>每个 remove/mask 候选都必须包含时间、置信度、理由和证据；系统不会静默猜测坐标或区间。</small></label>
                            )}
                            <div className="wb-workflow-actions"><button type="submit" disabled={workflowWritesDisabled || currentRunStage < 3 || currentRunStage > 3 || adGatePassed}>{workflowBusy === "ad-edit" ? "正在入队…" : "生成工作母版并通过门禁"}</button></div>
                          </form>
                        </WorkflowCard>
                      </>
                    )}
                    {inspectorTab === "translation" && (
                      <>
                        <InspectorNote code="T · A · B · C" title="翻译版本受控" text="候选稿、A/B 独立全文审核和 C 裁决分别留痕。切换 Provider 或提示词必须创建新运行。" />
                        <WorkflowCard code="04–05 / TRANSLATE" title="启动全文翻译与双审" description="输入必须是广告门禁绑定的冻结源文。Provider 请求可能计费；状态不确定时系统不会自动重发。">
                          <form noValidate onSubmit={(event) => { event.preventDefault(); void submitTranslation(); }}>
                            <div className="wb-workflow-grid">
                              <label className="wide">冻结源文相对路径<input value={workflowDraft.slots_path} onChange={(event) => patchWorkflowDraft("slots_path", event.target.value)} /></label>
                              <label className="wide">术语表相对路径<input value={workflowDraft.glossary_path} onChange={(event) => patchWorkflowDraft("glossary_path", event.target.value)} placeholder="可选 JSON" /></label>
                              <label>版本<input inputMode="numeric" value={workflowDraft.version} onChange={(event) => patchWorkflowDraft("version", event.target.value)} /></label>
                              <label>每批稳定 ID<input inputMode="numeric" value={workflowDraft.batch_size} onChange={(event) => patchWorkflowDraft("batch_size", event.target.value)} /></label>
                            </div>
                            <label className="wb-json-field">项目上下文<textarea className="resize-none" value={workflowDraft.project_context} onChange={(event) => patchWorkflowDraft("project_context", event.target.value)} placeholder="嘉宾、主题、术语与风格约束。核心提示词仍由冻结预设提供。" /></label>
                            <div className="wb-workflow-actions"><button type="submit" disabled={workflowWritesDisabled || !adGatePassed || translationComplete}>{workflowBusy === "translate" ? "正在入队…" : "启动 T/A/B/C"}</button></div>
                            {!adGatePassed && <small className="wb-disabled-reason">先通过 ad_edit_gate；仅上传源文不足以开始翻译。</small>}
                          </form>
                        </WorkflowCard>

                        <WorkflowCard code="05 / READING" title="生成章节中文阅读稿" description="阅读稿只对正式译文按句对齐排版，不改写字幕。没有章节时可保留空数组，由系统生成单章。">
                          <form noValidate onSubmit={(event) => { event.preventDefault(); void submitChapterReading(); }}>
                            <div className="wb-workflow-grid"><label className="wide">正式翻译相对路径<input value={workflowDraft.translation_path} onChange={(event) => patchWorkflowDraft("translation_path", event.target.value)} /></label><label className="wide">原→工作母版时间线<input value={workflowDraft.timeline_path} onChange={(event) => patchWorkflowDraft("timeline_path", event.target.value)} /></label></div>
                            <label className="wb-json-field">章节证据 JSON<textarea className="resize-none" value={workflowDraft.chapters_json} onChange={(event) => patchWorkflowDraft("chapters_json", event.target.value)} spellCheck={false} /><small>结构化章节数组；路径、时间和版本不明确时不要静默猜测。</small></label>
                            <div className="wb-workflow-actions"><button type="submit" disabled={workflowWritesDisabled || !translationComplete}>{workflowBusy === "chapter" ? "正在生成…" : "生成并验证阅读稿"}</button></div>
                          </form>
                        </WorkflowCard>

                        <WorkflowCard code="05 / APPROVAL" title="明确批准当前翻译" description="该动作会读取实际文件并绑定 SHA-256；任何字节变化都会使旧批准失效。">
                          <form noValidate onSubmit={(event) => { event.preventDefault(); void submitTranslationApproval(); }}>
                            <div className="wb-workflow-grid"><label className="wide">章节阅读稿<input value={workflowDraft.reading_path} onChange={(event) => patchWorkflowDraft("reading_path", event.target.value)} placeholder="先生成阅读稿" /></label><label className="wide">阅读稿验证报告<input value={workflowDraft.validation_path} onChange={(event) => patchWorkflowDraft("validation_path", event.target.value)} placeholder="先生成阅读稿" /></label></div>
                            <label className="wb-explicit-command">下游执行指令<input value={workflowDraft.approval_command} onChange={(event) => patchWorkflowDraft("approval_command", event.target.value)} placeholder="完整审阅后输入：开始选音色" /><small>泛泛的“继续”不会放行。可用指令包括“开始选音色”“进入选音”“启动工作台”。</small></label>
                            <div className="wb-workflow-actions"><button type="submit" disabled={workflowWritesDisabled || !translationComplete || translationGatePassed || !workflowDraft.reading_path || !workflowDraft.validation_path}>{workflowBusy === "approval" ? "正在绑定哈希…" : "批准并进入选音"}</button></div>
                          </form>
                        </WorkflowCard>
                      </>
                    )}
                    {inspectorTab === "voices" && (
                      <>
                        <InspectorNote code="VOICE LOCK" title="音色与供应商绑定" text="voice_id、模型与 Provider 会绑定到当前运行；正式中文语音固定原生 1.0×，界面不提供速度调节。" />
                        <WorkflowCard code="06 / CATALOG" title="读取非生成音色目录" description="目录请求只验证凭证并列出可见音色，后端必须明确返回 generation_performed=false；不会生成试听或语音。">
                          <div className="wb-catalog-strip"><div><strong>{frozenSpeechProfileId || "尚未冻结语音 Provider"}</strong><span>{frozenSpeechCatalog ? `${frozenSpeechCatalog.voices.length} 个音色可选` : "目录尚未读取"}</span></div><button type="button" disabled={!frozenSpeechProfileId || catalogBusy === frozenSpeechProfileId} onClick={() => { void loadChoiceCatalog(frozenSpeechProfileId); }}>{catalogBusy === frozenSpeechProfileId ? "正在读取…" : "读取非生成目录"}</button></div>
                          {catalogFeedback && <p className="wb-inline-feedback" role="status">{catalogFeedback}</p>}
                          <div className="wb-workflow-grid">
                            <label>角色 ID<input value={workflowDraft.voice_role_id} onChange={(event) => patchWorkflowDraft("voice_role_id", event.target.value)} placeholder="narrator" /></label>
                            <label>角色显示名<input value={workflowDraft.voice_role_name} onChange={(event) => patchWorkflowDraft("voice_role_name", event.target.value)} placeholder="旁白" /></label>
                            <label className="wide">voice_id
                              {frozenSpeechCatalog?.voices.length ? (
                                <select value={workflowDraft.voice_id} onChange={(event) => patchWorkflowDraft("voice_id", event.target.value)}><option value="">选择供应商返回的音色</option>{frozenSpeechCatalog.voices.map((voice) => <option key={voice.voice_id} value={voice.voice_id}>{voice.name || voice.voice_id} · {voice.voice_id}</option>)}</select>
                              ) : (
                                <input value={workflowDraft.voice_id} onChange={(event) => patchWorkflowDraft("voice_id", event.target.value)} placeholder="目录不可用时，按供应商控制台填写精确 ID" />
                              )}
                            </label>
                          </div>
                          <div className="wb-workflow-actions"><button type="button" className="secondary" onClick={addVoiceMapping}>加入或更新角色映射</button></div>
                        </WorkflowCard>

                        <WorkflowCard code="06 / LOCK" title="锁定角色与音色" description="逐行映射会作为结构化 assignments 提交；音色锁定同时授权当前范围的一分钟试听，但不授权全文 TTS。">
                          <form noValidate onSubmit={(event) => { event.preventDefault(); void submitVoiceLock(); }}>
                            <label className="wb-json-field">角色 → voice_id（每行一组）<textarea className="resize-none" value={workflowDraft.voice_assignments} onChange={(event) => patchWorkflowDraft("voice_assignments", event.target.value)} spellCheck={false} placeholder="narrator=male-qn-qingse" /></label>
                            <details className="wb-workflow-advanced"><summary>角色显示名 JSON</summary><label className="wb-json-field"><span className="visuallyHidden">角色显示名 JSON</span><textarea className="resize-none" value={workflowDraft.role_names_json} onChange={(event) => patchWorkflowDraft("role_names_json", event.target.value)} spellCheck={false} /></label></details>
                            <div className="wb-workflow-actions"><button type="submit" disabled={workflowWritesDisabled || !translationGatePassed || voiceLockPassed}>{workflowBusy === "voice-lock" ? "正在锁定…" : "保存并锁定音色"}</button></div>
                            {!translationGatePassed && <small className="wb-disabled-reason">翻译门禁通过后才允许选音。</small>}
                          </form>
                        </WorkflowCard>

                        <WorkflowCard code="06 / AUDITION" title="生成一分钟试听" description="按当前音色锁自动选择覆盖角色的片段并生成音频试听；不会渲染视频，也不会扩大为全文 TTS。">
                          <p className="wb-card-note">试听固定使用供应商原生 1.0× 语速。相同版本的活跃任务会由后端去重，进度与失败原因统一显示在任务坞。</p>
                          <div className="wb-workflow-actions"><button type="button" disabled={workflowWritesDisabled || !voiceLockPassed || auditionComplete} onClick={() => { void submitAudition(); }}>{workflowBusy === "audition" ? "正在准备试听…" : auditionComplete ? "一分钟试听已生成" : "生成一分钟试听"}</button></div>
                          {!voiceLockPassed && <small className="wb-disabled-reason">先锁定全部角色与 voice_id；音色锁会自动授权本次试听。</small>}
                        </WorkflowCard>

                        <WorkflowCard code="07 / FULL TTS" title="生成全文语音" description="你的明确指令即授权当前冻结输入的付费全文 TTS；自动 dry-run 通过后直接继续，不再弹出二次确认。">
                          <form noValidate onSubmit={(event) => { event.preventDefault(); void submitGenerateFull(); }}>
                            <div className="wb-workflow-grid"><label className="wide">TTS 片段计划<input value={workflowDraft.segments_path} onChange={(event) => patchWorkflowDraft("segments_path", event.target.value)} /></label><label className="wide">音色锁<input value={workflowDraft.voice_lock_path} onChange={(event) => patchWorkflowDraft("voice_lock_path", event.target.value)} /></label><label className="wide">输出目录<input value={workflowDraft.speech_output_dir} onChange={(event) => patchWorkflowDraft("speech_output_dir", event.target.value)} /></label></div>
                            <label className="wb-explicit-command">全文授权指令<input value={workflowDraft.full_command} onChange={(event) => patchWorkflowDraft("full_command", event.target.value)} placeholder="确认后输入：生成全文" /><small>正式中文语音固定原生 1.0×；不会对生成音频做拉伸、加速或截断。</small></label>
                            <div className="wb-workflow-actions"><button type="submit" disabled={workflowWritesDisabled || !voiceLockPassed || speechComplete}>{workflowBusy === "generate-full" ? "正在执行 dry-run…" : "按明确指令生成全文"}</button></div>
                          </form>
                        </WorkflowCard>

                        <WorkflowCard code="07 / RENDER" title="画面重定时与成片" description="渲染只接收已冻结门禁与产物路径；后端会从片段缓存自动组装中文 WAV、字幕、时间线、重定时计划和媒体参数，不要求手填派生数据。">
                          <form noValidate onSubmit={(event) => { event.preventDefault(); void submitRender(); }}>
                            <label className="wb-json-field">渲染证据 / 计划 JSON<textarea className="large resize-none" value={workflowDraft.render_json} onChange={(event) => patchWorkflowDraft("render_json", event.target.value)} spellCheck={false} /></label>
                            <div className="wb-workflow-actions"><button type="submit" disabled={workflowWritesDisabled || !speechComplete}>{workflowBusy === "render" ? "正在入队…" : "验证门禁并开始渲染"}</button></div>
                            {!speechComplete && <small className="wb-disabled-reason">先完成全文语音任务；后端随后自动组装音频、字幕和时间线。</small>}
                          </form>
                        </WorkflowCard>
                      </>
                    )}
                    {inspectorTab === "qa" && (
                      <>
                        <InspectorNote code={`${selected.stages_passed}/8 PASS`} title="机器门禁，不是最终批准" text="只有输入哈希、字幕覆盖、音频时序、全片解码与发布包全部通过，才会显示“机器验收通过”；之后仍需人工抽看。" />
                        <section className="wb-gate-matrix" aria-label="当前运行门禁">
                          {[{ label: "广告处理", pass: adGatePassed, path: "qa/ad_edit_gate.json" }, { label: "翻译批准", pass: translationGatePassed, path: "qa/translation_gate.json" }, { label: "音色锁", pass: voiceLockPassed, path: workflowDraft.voice_lock_path }, { label: "机器 QA", pass: machineQaPassed, path: "qa/final_machine_qa_v1.json" }].map((gate) => <div key={gate.label}><span className={gate.pass ? "pass" : "waiting"}>{gate.pass ? "通过" : "待生成"}</span><strong>{gate.label}</strong><small>{gate.path}</small></div>)}
                        </section>
                        <WorkflowCard code="08 / COVER SOURCE" title="从工作母版抽取封面源帧" description="只从 ad_edit_gate 绑定的正式工作母版按时间点抽取 PNG；发布授权、人物身份、文字与广告残留仍需单独核对。">
                          <form noValidate onSubmit={(event) => { event.preventDefault(); void submitCoverSource(); }}>
                            <div className="wb-workflow-grid"><label>时间点（秒）<input inputMode="decimal" value={workflowDraft.cover_source_at_seconds} onChange={(event) => patchWorkflowDraft("cover_source_at_seconds", event.target.value)} /></label><label className="wide">正式工作母版<input value={workflowDraft.working_master_path} onChange={(event) => patchWorkflowDraft("working_master_path", event.target.value)} /></label></div>
                            <div className="wb-workflow-actions"><button type="submit" disabled={workflowWritesDisabled || !adGatePassed}>{workflowBusy === "cover-source" ? "正在入队…" : "抽取 PNG 源帧"}</button></div>
                            {!adGatePassed && <small className="wb-disabled-reason">先生成并通过 ad_edit_gate，防止从错误母版取帧。</small>}
                          </form>
                        </WorkflowCard>
                        <WorkflowCard code="08 / PUBLICATION" title="生成正式发布包" description="仅在最终机器 QA 通过后执行。标题、简介、章节、书单与封面核验都由当前证据 JSON 明确提供。">
                          <form noValidate onSubmit={(event) => { event.preventDefault(); void submitPublication(); }}>
                            <label className="wb-json-field">发布包证据 / 计划 JSON<textarea className="large resize-none" value={workflowDraft.publication_json} onChange={(event) => patchWorkflowDraft("publication_json", event.target.value)} spellCheck={false} /><small>必须核对原视频链接、外文人名、去推广说明、封面授权与人物身份；不能把未核对项自动设为 true。</small></label>
                            <div className="wb-workflow-actions"><button type="submit" disabled={workflowWritesDisabled || !machineQaPassed}>{workflowBusy === "publication" ? "正在生成并校验…" : "生成发布包"}</button></div>
                            {!machineQaPassed && <small className="wb-disabled-reason">最终机器 QA 通过后才开放发布包。</small>}
                          </form>
                        </WorkflowCard>
                        {publicationText && <section className="wb-copyable-publication"><div><strong>UTF-8 发布文字</strong><button type="button" onClick={() => { void navigator.clipboard.writeText(publicationText).then(() => setWorkflowFeedback("发布文字已复制。"), () => setWorkflowFeedback("浏览器拒绝剪贴板访问；请在文本框中手动复制。")); }}>复制全部</button></div><textarea className="resize-none" readOnly value={publicationText} aria-label="可复制的正式发布文字" /></section>}
                      </>
                    )}
                    {inspectorTab === "history" && (
                      <>
                        <InspectorNote code={selected.updated_at} title="可审计运行记录" text="阶段、恢复、人工批准、Provider 请求与制品哈希保存在本机。下方只显示脱敏状态，不显示 Key、Cookie 或临时 URL。" />
                        <section className="wb-run-history"><h3>运行阶段</h3>{selectedRun?.stages.map((stage) => <article key={stage.key}><span>{stage.key}</span><div><strong>{stage.title}</strong><small>{stage.detail || "尚无说明"}</small></div><em>{stage.status}</em></article>) || <p>尚未选择运行。</p>}</section>
                        <section className="wb-run-history"><h3>后台任务</h3>{selectedRunJobs.length ? selectedRunJobs.map((job) => <article key={job.id}><span>{job.stage_key || "—"}</span><div><strong>{job.stage}</strong><small>{job.issue || job.elapsed_label}</small></div><em>{job.status}</em></article>) : <p>当前运行还没有后台任务。</p>}</section>
                        {Object.keys(workflowResult).length > 0 && <section className="wb-run-history"><h3>本次页面已确认的响应</h3>{Object.keys(workflowResult).map((action) => <article key={action}><span>✓</span><div><strong>{action}</strong><small>响应已脱敏保存在当前页面；真实制品以项目目录与 QA 哈希为准。</small></div><em>received</em></article>)}</section>}
                      </>
                    )}
                    {workflowFeedback && <div className={`wb-workflow-feedback ${runHasUncertainRequest ? "danger" : ""}`} role="status">{workflowFeedback}</div>}
                  </div>
                  <div className="wb-inspector-actions">
                    <button type="button" onClick={() => setSurface("studio")}>打开制作台</button>
                    <button type="button" className="primary" disabled={libraryMode !== "live" || Boolean(workflowBusy) || activeRunTask} onClick={() => { void startRun(); }}>{selectedRun ? "新建冻结运行" : "创建冻结运行"}</button>
                    {libraryMode === "preview" && <small>预览项目不能启动任务。</small>}
                    {activeRunTask && <small>当前运行仍有后台任务；完成或安全暂停后再创建新运行。</small>}
                    {runFeedback && <small role="status">{runFeedback}</small>}
                  </div>
                </>
              ) : (
                <div className="wb-inspector-empty"><span aria-hidden="true">↗</span><strong>选择一个视频项目</strong><p>这里会显示生产轨、当前进度、问题原因和恢复动作。</p></div>
              )}
            </aside>
            {inspectorOpen && <button className="wb-inspector-scrim" type="button" aria-label="关闭项目检查器" onClick={() => setInspectorOpen(false)} />}
          </>
        ) : (
          <section className="wb-settings" aria-labelledby="settings-title">
            <div className="wb-settings-head"><p className="eyebrow">LOCAL CONFIGURATION</p><h1 id="settings-title">工作台设置</h1><p>服务、提示词和资料库位置只需配置一次；具体项目运行时会冻结所用版本。</p></div>
            <div className="wb-settings-layout">
              <nav aria-label="设置分类">
                {([
                  ["translation", "翻译服务", "T / A / B / C"],
                  ["speech", "语音服务", "MiniMax 与本地模型"],
                  ["presets", "工作流预设", "提示词与固定规则"],
                  ["storage", "存储与迁移", "资料库、导出与接管"],
                  ["advanced", "高级与诊断", "事件、日志与能力探测"],
                ] as [SettingSection, string, string][]).map(([id, label, hint]) => <button key={id} type="button" className={settingSection === id ? "active" : ""} onClick={() => { setSettingSection(id); setProviderFeedback(""); }}><strong>{label}</strong><small>{hint}</small></button>)}
              </nav>
              <div className="wb-settings-content">
                {(settingSection === "translation" || settingSection === "speech") && (
                  <ProviderSettings
                    kind={settingSection}
                    providers={providers.filter((profile) => profile.service_kind === (settingSection === "translation" ? "llm" : "speech"))}
                    providerCatalog={providerCatalog}
                    choiceCatalogs={choiceCatalogs}
                    catalogBusy={catalogBusy}
                    catalogFeedback={catalogFeedback}
                    draft={providerDraft}
                    busy={providerBusy}
                    feedback={providerFeedback}
                    roleBindings={effectiveRoleBindings}
                    speechProfileId={effectiveSpeechProfileId}
                    onDraft={setProviderDraft}
                    onRoleBinding={(role, profileId) => setRoleBindings((current) => ({ ...current, [role]: profileId }))}
                    onSpeechProfile={setSpeechProfileId}
                    onLoadCatalog={loadChoiceCatalog}
                    onSubmit={saveProvider}
                  />
                )}
                {settingSection === "presets" && <PresetSettings presets={presets} />}
                {settingSection === "storage" && <StorageSettings live={libraryMode === "live"} />}
                {settingSection === "advanced" && <AdvancedSettings connection={connection} eventState={eventState} />}
              </div>
            </div>
          </section>
        )}
      </div>

      <section className={`wb-job-dock ${dockOpen ? "open" : ""}`} aria-label="后台任务坞">
        <button className="wb-dock-handle" type="button" aria-expanded={dockOpen} onClick={() => setDockOpen((value) => !value)}>
          <span><i aria-hidden="true" />任务坞 <em>{runningJobs.length} 项进行中或需处理</em></span>
          <b>{dockOpen ? "收起" : "展开"} {dockOpen ? "⌄" : "⌃"}</b>
        </button>
        <div className="wb-dock-jobs">
          {jobs.length ? jobs.map((job) => (
            <article key={job.id} className={`wb-dock-job ${job.status}`}>
              <div><span>{job.stage}</span><strong>{job.title}</strong><small>{job.elapsed_label}</small></div>
              <ProgressLine progress={job.progress} compact />
              <p>{job.issue || (job.status === "running" ? "本地执行器正在处理；可以离开此页面。" : job.status === "blocked_uncertain" ? "计费状态待核对，已停止自动重试。" : "任务状态已保存。")}</p>
              <div className="wb-dock-actions">
                <button type="button" disabled={!job.project_id} onClick={() => { const project = projects.find((item) => item.id === job.project_id); if (!project) return; selectProject(project); setSurface("library"); setDockOpen(false); }}>查看项目</button>
                {["queued", "running", "retry_wait", "waiting_provider"].includes(job.status) && <button type="button" disabled={Boolean(jobActionBusy)} onClick={() => { void controlJob(job, "pause"); }}>暂停</button>}
                {["paused", "waiting_provider", "waiting_worker"].includes(job.status) && <button type="button" disabled={Boolean(jobActionBusy)} onClick={() => { void controlJob(job, "resume"); }}>继续</button>}
                {!['completed', 'cancelled', 'superseded', 'blocked_uncertain'].includes(job.status) && <button type="button" disabled={Boolean(jobActionBusy)} onClick={() => { void controlJob(job, "cancel"); }}>取消</button>}
              </div>
            </article>
          )) : <div className="wb-dock-empty">当前没有后台任务。导入视频后，阶段进度会在这里持续显示。</div>}
          {jobFeedback && <p className="wb-job-feedback" role="status">{jobFeedback}</p>}
        </div>
      </section>

      {importOpen && (
        <div className="wb-dialog-layer" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setImportOpen(false); }}>
          <section className="wb-import-dialog" role="dialog" aria-modal="true" aria-labelledby="import-title">
            <button className="wb-dialog-close" type="button" aria-label="关闭导入窗口" onClick={() => setImportOpen(false)}>×</button>
            <p className="eyebrow">NEW LIBRARY ITEM</p>
            <h2 id="import-title">导入一个视频</h2>
            <p>项目会先进入资料库，再由本地运行器探测格式、语言、分辨率与可恢复下载方案。</p>
            <div className="wb-import-kind" aria-label="导入来源">
              <button type="button" aria-pressed={importKind === "video_url"} className={importKind === "video_url" ? "active" : ""} onClick={() => { setImportKind("video_url"); setImportSource(""); setImportError(""); }}><span aria-hidden="true">↗</span><strong>粘贴视频链接</strong><small>YouTube 或其他受支持来源</small></button>
              <button type="button" aria-pressed={importKind === "local_file"} className={importKind === "local_file" ? "active" : ""} onClick={() => { setImportKind("local_file"); setImportSource(""); setImportError(""); }}><span aria-hidden="true">⌁</span><strong>选择本机视频</strong><small>输入完整路径，文件不会上传到云端</small></button>
            </div>
            <form onSubmit={submitImport} noValidate>
              <label htmlFor="wb-import-source">{importKind === "video_url" ? "视频链接" : "本机完整路径"}</label>
              <div className="wb-import-input"><span aria-hidden="true">{importKind === "video_url" ? "URL" : "FILE"}</span><input ref={importInputRef} id="wb-import-source" value={importSource} onChange={(event) => setImportSource(event.target.value)} placeholder={importKind === "video_url" ? "https://www.youtube.com/watch?v=…" : "D:\\Videos\\source.mp4"} /></div>
              <ul><li>自动探测并选择兼容的画面与音频格式</li><li>导入后立即建立项目记录，不必停留等待下载</li><li>Cookie 与 API Key 只从本机凭证库读取</li></ul>
              {importError && <div className="wb-form-error" role="alert"><span aria-hidden="true">!</span>{importError}</div>}
              <div className="wb-dialog-actions"><button type="button" onClick={() => setImportOpen(false)}>取消</button><button type="submit" className="primary" disabled={importBusy}>{importBusy ? "正在建立项目…" : "加入视频库"}</button></div>
            </form>
          </section>
        </div>
      )}
    </main>
  );
}

function InspectorNote({ code, title, text }: { code: string; title: string; text: string }) {
  return <section className="wb-inspector-note"><span>{code}</span><h3>{title}</h3><p>{text}</p></section>;
}

function WorkflowCard({
  code,
  title,
  description,
  children,
}: {
  code: string;
  title: string;
  description: string;
  children: ReactNode;
}) {
  return <section className="wb-workflow-card"><header><span>{code}</span><div><h3>{title}</h3><p>{description}</p></div></header><div className="wb-workflow-card-body">{children}</div></section>;
}

type ProviderDraft = {
  display_name: string;
  service_kind: ServiceKind;
  provider_id: ProviderId;
  base_url: string;
  model: string;
  api_key: string;
  config: Record<string, unknown>;
};

function ProviderSettings({
  kind,
  providers,
  providerCatalog,
  choiceCatalogs,
  catalogBusy,
  catalogFeedback,
  draft,
  busy,
  feedback,
  roleBindings,
  speechProfileId,
  onDraft,
  onRoleBinding,
  onSpeechProfile,
  onLoadCatalog,
  onSubmit,
}: {
  kind: "translation" | "speech";
  providers: ProviderProfile[];
  providerCatalog: ProviderCatalogEntry[];
  choiceCatalogs: Record<string, ProviderChoiceCatalog>;
  catalogBusy: string;
  catalogFeedback: string;
  draft: ProviderDraft;
  busy: boolean;
  feedback: string;
  roleBindings: Record<TranslationRole, string>;
  speechProfileId: string;
  onDraft: (value: ProviderDraft) => void;
  onRoleBinding: (role: TranslationRole, profileId: string) => void;
  onSpeechProfile: (profileId: string) => void;
  onLoadCatalog: (profileId: string) => Promise<void>;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  const isTranslation = kind === "translation";
  const serviceKind: ServiceKind = isTranslation ? "llm" : "speech";
  const allowedProviders: ProviderId[] = providerCatalog
    .filter((provider) => provider.service_kind === serviceKind)
    .map((provider) => provider.provider_id);
  if (allowedProviders.length === 0) {
    allowedProviders.push(...(isTranslation
      ? ["openai-compatible", "minimax-llm"] as ProviderId[]
      : ["openai-compatible-speech", "minimax-speech"] as ProviderId[]));
  }
  const defaultProvider = allowedProviders[0];
  const readyProviders = providers.filter((provider) => provider.enabled && provider.credential.configured);
  const activeDraft: ProviderDraft = draft.service_kind === serviceKind
    ? draft
    : {
        display_name: "",
        service_kind: serviceKind,
        provider_id: defaultProvider,
        base_url: PROVIDER_DEFAULT_URLS[defaultProvider] || "",
        model: "",
        api_key: "",
        config: {},
      };
  const [showApiKey, setShowApiKey] = useState(false);
  function patch<K extends keyof ProviderDraft>(key: K, value: ProviderDraft[K]) {
    onDraft({ ...activeDraft, [key]: value, service_kind: serviceKind });
  }
  function selectProvider(providerId: ProviderId) {
    const catalog = providerCatalog.find((provider) => provider.provider_id === providerId && provider.service_kind === serviceKind);
    onDraft({
      ...activeDraft,
      service_kind: serviceKind,
      provider_id: providerId,
      base_url: catalog?.default_base_url || PROVIDER_DEFAULT_URLS[providerId] || "",
      config: catalog?.recommended_config || {},
    });
  }
  return (
    <>
      <div className="wb-setting-title"><div><p className="eyebrow">{isTranslation ? "LLM PROVIDERS" : "SPEECH PROVIDERS"}</p><h2>{isTranslation ? "翻译服务" : "语音服务"}</h2><p>{isTranslation ? "同一个凭证可以承担 T/A/B/C，也可按角色绑定不同模型。运行开始后不静默切换。" : "云端和兼容语音服务遵循同一任务接口；正式语音固定使用原生 1.0×。"}</p></div><span>{readyProviders.length}/{providers.length} 已配置</span></div>
      <div className="wb-provider-list">
        {providers.map((provider) => {
          const configured = provider.credential.configured;
          const probed = Object.keys(provider.capability).length > 0;
          const catalog = choiceCatalogs[provider.id];
          return <article key={provider.id}><i className={configured ? "ready" : ""} aria-hidden="true" /><div><strong>{provider.display_name}</strong><span>{PROVIDER_LABELS[provider.provider_id]} · {provider.model}</span><small>{catalog ? `${catalog.models.length} 个模型 · ${catalog.voices.length} 个音色 · 未生成内容` : provider.base_url || (configured ? "凭证保存在本机系统存储" : "尚未写入凭证")}</small></div><div className="wb-provider-row-actions"><em className={configured ? "ready" : ""}>{!configured ? "待配置" : probed ? "已探测" : "待探测"}</em><button type="button" disabled={!configured || catalogBusy === provider.id} onClick={() => { void onLoadCatalog(provider.id); }}>{catalogBusy === provider.id ? "读取中" : "读取目录"}</button></div></article>;
        })}
        {providers.length === 0 && <div className="wb-dock-empty">还没有{isTranslation ? "翻译" : "语音"}服务。先在下方保存一个 Provider，再绑定到运行。</div>}
      </div>
      {catalogFeedback && <p className="wb-provider-feedback" role="status">{catalogFeedback}</p>}
      <details className="wb-provider-editor" open>
        <summary>{isTranslation ? "T / A / B / C 角色绑定" : "正式语音绑定"} <span>＋</span></summary>
        <form noValidate onSubmit={(event) => event.preventDefault()}>
          <div className="wb-form-grid">
            {isTranslation ? (["T", "A", "B", "C"] as TranslationRole[]).map((role) => (
              <label key={role}>{role} 角色 Provider
                <select value={roleBindings[role]} disabled={readyProviders.length === 0} onChange={(event) => onRoleBinding(role, event.target.value)}>
                  {readyProviders.length === 0 && <option value="">请先配置翻译 Provider</option>}
                  {readyProviders.map((provider) => <option key={provider.id} value={provider.id}>{provider.display_name} · {provider.model}</option>)}
                </select>
              </label>
            )) : (
              <label className="wide">全文语音 Provider
                <select value={speechProfileId} disabled={readyProviders.length === 0} onChange={(event) => onSpeechProfile(event.target.value)}>
                  {readyProviders.length === 0 && <option value="">请先配置语音 Provider</option>}
                  {readyProviders.map((provider) => <option key={provider.id} value={provider.id}>{provider.display_name} · {provider.model}</option>)}
                </select>
              </label>
            )}
          </div>
          <div className="wb-provider-policy"><span aria-hidden="true">冻</span><p><strong>这些绑定只用于下一次创建运行。</strong>创建后会把各角色、语音服务与能力快照写入工作流锁；后续改设置不会改变已开始的运行。</p></div>
        </form>
      </details>
      <details className="wb-provider-editor" open={providers.every((provider) => !provider.credential.configured)}>
        <summary>添加{isTranslation ? "翻译" : "语音"}服务 <span>＋</span></summary>
        <form onSubmit={onSubmit} noValidate>
          <div className="wb-form-grid">
            <label>配置名称<input value={activeDraft.display_name} onChange={(event) => patch("display_name", event.target.value)} placeholder={isTranslation ? "例如：MiniMax 主翻译" : "例如：MiniMax 正式配音"} /></label>
            <label>服务类型<select value={activeDraft.provider_id} onChange={(event) => selectProvider(event.target.value as ProviderId)}>{allowedProviders.map((providerId) => <option key={providerId} value={providerId}>{PROVIDER_LABELS[providerId]}</option>)}</select></label>
            <label className="wide">接口地址<input value={activeDraft.base_url} onChange={(event) => patch("base_url", event.target.value)} placeholder={isTranslation ? "https://api.example.com/v1" : "https://api.example.com/v1"} /><small>MiniMax 已填默认地址；兼容服务需填写 HTTPS，本机 localhost 可使用 HTTP。</small></label>
            <label>模型<input value={activeDraft.model} onChange={(event) => patch("model", event.target.value)} placeholder={isTranslation ? "模型 ID" : "speech-2.8-hd"} /></label>
            <label htmlFor={`wb-provider-key-${serviceKind}`}>API Key
              <span className="wb-secret-field">
                <input id={`wb-provider-key-${serviceKind}`} type={showApiKey ? "text" : "password"} autoComplete="new-password" value={activeDraft.api_key} onChange={(event) => patch("api_key", event.target.value)} placeholder="只发送到本机凭证服务" />
                <button type="button" aria-label={showApiKey ? "隐藏 API Key" : "显示 API Key"} aria-pressed={showApiKey} onClick={() => setShowApiKey((current) => !current)}>{showApiKey ? "隐藏" : "显示"}</button>
              </span>
            </label>
            {!isTranslation && activeDraft.provider_id === "openai-compatible-speech" && <label className="wide">兼容语音 voice_id 目录<input value={Array.isArray(activeDraft.config.voices) ? activeDraft.config.voices.map((value) => typeof value === "string" ? value : "").filter(Boolean).join(", ") : ""} onChange={(event) => patch("config", { ...activeDraft.config, voices: event.target.value.split(",").map((value) => value.trim()).filter(Boolean) })} placeholder="alloy, nova, shimmer" /><small>逗号分隔，保存后可通过非生成目录接口读取；MiniMax 会直接读取供应商音色目录。</small></label>}
          </div>
          <div className="wb-provider-policy"><span aria-hidden="true">锁</span><p><strong>Key 不进入项目、日志或浏览器存储。</strong>保存后只返回脱敏配置；能力探测失败时不会自动换用其他模型。</p></div>
          {feedback && <p className="wb-provider-feedback" role="status">{feedback}</p>}
          <div className="wb-form-actions"><button type="submit" className="primary" disabled={busy} aria-busy={busy}>{busy ? "正在安全保存…" : "保存并测试连接"}</button></div>
        </form>
      </details>
    </>
  );
}

function PresetSettings({ presets }: { presets: WorkflowPreset[] }) {
  return (
    <>
      <div className="wb-setting-title"><div><p className="eyebrow">PROMPT PACKS</p><h2>工作流预设</h2><p>核心提示词默认锁定。需要调整时复制为新版本，旧项目仍绑定原始哈希。</p></div></div>
      <div className="wb-preset-list">{presets.map((preset) => <article key={preset.id}><span>{preset.locked ? "锁" : "自定义"}</span><div><strong>{preset.name}</strong><small>{preset.version}</small><p>{preset.description}</p></div><button type="button" disabled title="自定义预设复制接口尚未开放">复制为自定义版本</button></article>)}</div>
      <div className="wb-rule-grid"><section><span>T</span><strong>全文候选稿</strong><p>根据冻结源文、上下文和术语表直接翻译。</p></section><section><span>A</span><strong>中文表达审核</strong><p>独立全文覆盖，不与其他角色共用结果。</p></section><section><span>B</span><strong>语义忠实审核</strong><p>核对数字、否定、主客体和领域逻辑。</p></section><section><span>C</span><strong>主控裁决</strong><p>合并争议并生成新版本，不直接覆盖候选稿。</p></section></div>
    </>
  );
}

function StorageSettings({ live }: { live: boolean }) {
  return (
    <>
      <div className="wb-setting-title"><div><p className="eyebrow">LIBRARY & HANDOFF</p><h2>存储与迁移</h2><p>代码、项目制品和凭证分开管理；项目包可迁移，Key 需要在每台设备重新绑定。</p></div></div>
      <div className="wb-storage-card"><div><span>LIBRARY ROOT</span><strong>{live ? "由本地服务管理" : "尚未连接持久化资料库"}</strong><small>原片、字幕、语音、成片、QA 与运行清单使用相对路径组织。</small></div><button type="button" disabled>更改位置</button></div>
      <div className="wb-storage-grid"><article><span>01</span><strong>导出项目迁移包</strong><p>包含制品、状态与校验哈希，不包含 API Key 和 Cookie。</p><button type="button" disabled>选择项目后导出</button></article><article><span>02</span><strong>接管已有项目</strong><p>先验证清单与文件哈希，再从最近的安全检查点恢复。</p><button type="button" disabled>选择迁移包</button></article></div>
      <div className="wb-lease-note"><span aria-hidden="true">1×</span><p><strong>同一运行只允许一个活跃 Worker。</strong>多设备查看不会重复提交翻译或付费语音请求。</p></div>
    </>
  );
}

function AdvancedSettings({ connection, eventState }: { connection: "checking" | "online" | "offline"; eventState: "idle" | "live" | "offline" }) {
  return (
    <>
      <div className="wb-setting-title"><div><p className="eyebrow">DIAGNOSTICS</p><h2>高级与诊断</h2><p>这里只显示运行能力与事件连接；普通生产不需要修改。</p></div></div>
      <dl className="wb-diagnostics"><div><dt>本地 API</dt><dd><span className={connection === "online" ? "ready" : ""} />{API}</dd></div><div><dt>事件通道</dt><dd><span className={eventState === "live" ? "ready" : ""} />{eventState === "live" ? "SSE 实时连接" : eventState === "offline" ? "事件流暂不可用" : "等待资料库连接"}</dd></div><div><dt>进度策略</dt><dd>仅显示有真实分母的百分比</dd></div><div><dt>付费重试</dt><dd>状态不确定时自动停止</dd></div></dl>
      <details className="wb-advanced-disclosure"><summary>自定义 Provider 安全说明</summary><p>任意第三方可执行插件可能读取凭证。第一版只启用内置适配器；自定义接口必须通过 HTTPS、能力探测、结构化输出验证和服务端地址限制。</p></details>
    </>
  );
}
