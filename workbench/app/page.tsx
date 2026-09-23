"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import MiniMaxPanel from "./MiniMaxPanel";
import WorkbenchShell from "./WorkbenchShell";
import WorkflowPanel from "./WorkflowPanel";
import {
  defaultMiniMaxAssignments,
  type MiniMaxCatalog,
  type MiniMaxConfig,
  type MiniMaxVoice,
} from "./minimax";

const API = process.env.NEXT_PUBLIC_DUB_API || "http://127.0.0.1:8765";
const DEFAULT_URL = process.env.NEXT_PUBLIC_DUB_VIDEO_URL || "https://www.youtube.com/watch?v=USHR-lJ25Qo&list=PLOKAuKOgwd_dVEbKiMEw4LqHWJc97VvnB&index=39";

type Voice = {
  id: string;
  speaker: string;
  name: string;
  label: string;
  gender: string;
  language: string;
  tone: string;
  recommended: boolean;
  engine: "qwen" | "kokoro" | "cosyvoice";
  model: string;
  preview_ready: boolean;
  preview_url: string;
};

type Role = {
  id: string;
  name: string;
  line_count: number | null;
  sample: string;
  default_voice: string;
  default_minimax_voice?: string;
  minimax_candidates?: string[];
};

type Project = {
  id: string;
  url: string;
  title: string;
  duration_label: string;
  source_resolution: string;
  available_resolution?: string | null;
  ready: boolean;
  notice: string;
  paid_audition_authorized: boolean;
  voice_selection_saved?: boolean;
  voice_selection_version?: number | null;
  voice_selection_sha256?: string | null;
  roles: Role[];
  audition: { start: number; end: number; duration: number; segments: number };
};

function formatTimecode(seconds: number): string {
  let millis = Math.max(0, Math.round(seconds * 1000));
  const hours = Math.floor(millis / 3_600_000);
  millis -= hours * 3_600_000;
  const minutes = Math.floor(millis / 60_000);
  millis -= minutes * 60_000;
  const secs = Math.floor(millis / 1000);
  millis -= secs * 1000;
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}.${String(millis).padStart(3, "0")}`;
}

type Job = {
  id: string;
  status: "queued" | "running" | "complete" | "error";
  stage: string;
  progress: number;
  detail: string;
  elapsed_seconds?: number;
  output_url?: string;
  warning?: string;
  metrics?: {
    synthesis_seconds?: number;
    max_stretch_rate?: number;
    timing_policy?: "natural_no_stretch";
    timing_overflow_count?: number;
    timing_overlap_count?: number;
    max_overflow_seconds?: number;
    minimax?: { estimated_cny?: number; usage_characters?: number };
  };
};

type DubbingMode = "local" | "minimax" | "external";
type WorkbenchSurface = "studio" | "workflow";

type ExternalSegment = {
  segment_id: string;
  filename: string;
  role_id: string;
  role_name: string;
  start_time: string;
  end_time: string;
  slot_seconds: number;
  text: string;
  status: "ready" | "tight" | "long" | "missing" | "invalid";
  audio_seconds: number | null;
  stretch_rate: number | null;
  duration_ratio?: number | null;
  overflow_seconds?: number | null;
  message: string;
};

type ExternalPack = {
  pack_id: string;
  ready: boolean;
  required_count: number;
  ready_count: number;
  missing: string[];
  invalid: string[];
  extras: string[];
  warning_count: number;
  segments: ExternalSegment[];
};

type UploadStatus = "idle" | "uploading" | "success" | "error" | "cancelled";

const AUDIO_EXTENSIONS = ["wav", "mp3", "m4a", "aac", "flac", "ogg"];
const MAX_UPLOAD_BYTES = 512 * 1024 * 1024;
const MAX_AUDIO_BYTES = 128 * 1024 * 1024;
const DEFAULT_MINIMAX_CONFIG: MiniMaxConfig = {
  model: "speech-2.8-hd",
  speed: 1,
  volume: 1,
  pitch: 0,
  emotion: "",
  sample_rate: 32000,
  bitrate: 128000,
  format: "mp3",
  channel: 1,
  language_boost: "Chinese",
  text_normalization: true,
  modifier_pitch: 0,
  modifier_intensity: 0,
  modifier_timbre: 0,
  sound_effect: "",
};

function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function apiError(value: unknown): string {
  if (value && typeof value === "object" && "detail" in value) return String(value.detail);
  return "本地服务暂时没有响应";
}

function StudioWorkspace() {
  const [activeSurface, setActiveSurface] = useState<WorkbenchSurface>("studio");
  const [url, setUrl] = useState(DEFAULT_URL);
  const [voices, setVoices] = useState<Voice[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [assignments, setAssignments] = useState<Record<string, string>>({});
  const [connection, setConnection] = useState<"checking" | "online" | "offline">("checking");
  const [analyzing, setAnalyzing] = useState(false);
  const [message, setMessage] = useState("");
  const [activeVoice, setActiveVoice] = useState<string | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [engineFilter, setEngineFilter] = useState<"all" | "qwen" | "kokoro" | "cosyvoice">("all");
  const [genderFilter, setGenderFilter] = useState<"all" | "男声" | "女声">("all");
  const [voiceSearch, setVoiceSearch] = useState("");
  const [voiceLimit, setVoiceLimit] = useState(24);
  const [dubbingMode, setDubbingMode] = useState<DubbingMode>("minimax");
  const [templateBusy, setTemplateBusy] = useState(false);
  const [externalFiles, setExternalFiles] = useState<File[]>([]);
  const [externalPack, setExternalPack] = useState<ExternalPack | null>(null);
  const [uploadStatus, setUploadStatus] = useState<UploadStatus>("idle");
  const [uploadProgress, setUploadProgress] = useState(0);
  const [uploadError, setUploadError] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const [minimaxCatalog, setMinimaxCatalog] = useState<MiniMaxCatalog | null>(null);
  const [minimaxVoices, setMinimaxVoices] = useState<MiniMaxVoice[]>([]);
  const [minimaxAssignments, setMinimaxAssignments] = useState<Record<string, string>>({});
  const [minimaxConfig, setMinimaxConfig] = useState<MiniMaxConfig>(DEFAULT_MINIMAX_CONFIG);
  const [minimaxConnected, setMinimaxConnected] = useState(false);
  const [minimaxRequestsPerMinute, setMinimaxRequestsPerMinute] = useState<10 | 20>(10);
  const [selectionSaveStatus, setSelectionSaveStatus] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [selectionFeedback, setSelectionFeedback] = useState("");
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const uploadRequestRef = useRef<XMLHttpRequest | null>(null);
  const pollTimerRef = useRef<number | null>(null);

  const voiceMap = useMemo(() => Object.fromEntries(voices.map((voice) => [voice.id, voice])), [voices]);
  const minimaxVoiceMap = useMemo(
    () => Object.fromEntries(minimaxVoices.map((voice) => [voice.voice_id, voice])),
    [minimaxVoices],
  );
  const voicesByEngine = useMemo(() => ({
    qwen: voices.filter((voice) => voice.engine === "qwen"),
    kokoro: voices.filter((voice) => voice.engine === "kokoro"),
    cosyvoice: voices.filter((voice) => voice.engine === "cosyvoice"),
  }), [voices]);
  const filteredVoices = useMemo(() => {
    const query = voiceSearch.trim().toLowerCase();
    return voices.filter((voice) => {
      const matchesEngine = engineFilter === "all" || voice.engine === engineFilter;
      const matchesGender = genderFilter === "all" || voice.gender === genderFilter;
      const haystack = `${voice.name} ${voice.label} ${voice.id} ${voice.model}`.toLowerCase();
      return matchesEngine && matchesGender && (!query || haystack.includes(query));
    });
  }, [voices, engineFilter, genderFilter, voiceSearch]);
  const visibleVoices = filteredVoices.slice(0, voiceLimit);

  function engineName(engine: Voice["engine"]): string {
    if (engine === "kokoro") return "Kokoro 82M";
    if (engine === "cosyvoice") return "CosyVoice 300M";
    return "Qwen3-TTS";
  }

  async function loadWorkbench(signal?: AbortSignal) {
    try {
      const [healthResponse, voiceResponse, minimaxResponse] = await Promise.all([
        fetch(`${API}/api/health`, { signal }),
        fetch(`${API}/api/voices`, { signal }),
        fetch(`${API}/api/minimax/catalog`, { signal }),
      ]);
      if (!healthResponse.ok || !voiceResponse.ok || !minimaxResponse.ok) throw new Error("offline");
      const voicePayload = await voiceResponse.json();
      const minimaxPayload: MiniMaxCatalog = await minimaxResponse.json();
      setVoices(voicePayload.voices);
      setMinimaxCatalog(minimaxPayload);
      setMinimaxVoices(minimaxPayload.voices);
      setMinimaxConfig({ ...minimaxPayload.defaults, emotion: minimaxPayload.defaults.emotion || "" });
      setConnection("online");
      await analyze(DEFAULT_URL, true, signal, minimaxPayload.voices);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setConnection("offline");
      setMessage("本地处理服务尚未启动。运行“启动声轨工坊”后刷新页面即可。 ");
    }
  }

  useEffect(() => {
    const controller = new AbortController();
    // Initial network synchronization intentionally hydrates the client workbench.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    loadWorkbench(controller.signal);
    return () => {
      controller.abort();
      audioRef.current?.pause();
      uploadRequestRef.current?.abort();
      if (pollTimerRef.current !== null) window.clearTimeout(pollTimerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function analyze(
    targetUrl = url,
    silent = false,
    signal?: AbortSignal,
    minimaxVoiceRows: MiniMaxVoice[] = minimaxVoices,
  ) {
    if (!targetUrl.trim()) return;
    setAnalyzing(true);
    if (!silent) setMessage("");
    try {
      const response = await fetch(`${API}/api/analyze`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: targetUrl.trim(), expected_roles: 4 }),
        signal,
      });
      const payload = await response.json();
      if (!response.ok) throw payload;
      setProject(payload);
      setSelectionSaveStatus(payload.voice_selection_saved ? "saved" : "idle");
      setSelectionFeedback(payload.voice_selection_saved ? `已载入第 ${payload.voice_selection_version} 版锁定选择。` : "");
      setAssignments(Object.fromEntries(payload.roles.map((role: Role) => [role.id, role.default_voice])));
      const generatedMiniMaxDefaults = defaultMiniMaxAssignments(
        payload.roles.map((role: Role) => role.id),
        minimaxVoiceRows,
      );
      const availableMiniMaxVoices = new Set(minimaxVoiceRows.map((voice) => voice.voice_id));
      setMinimaxAssignments(Object.fromEntries(payload.roles.map((role: Role) => [
        role.id,
        role.default_minimax_voice && availableMiniMaxVoices.has(role.default_minimax_voice)
          ? role.default_minimax_voice
          : generatedMiniMaxDefaults[role.id] || "",
      ])));
      setMessage(payload.notice);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setMessage(apiError(error));
    } finally {
      setAnalyzing(false);
    }
  }

  function renameRole(roleId: string, name: string) {
    setProject((current) => current ? {
      ...current,
      roles: current.roles.map((role) => role.id === roleId ? { ...role, name } : role),
    } : current);
    setSelectionSaveStatus("idle");
    setSelectionFeedback("");
  }

  function chooseVoice(roleId: string, voiceId: string) {
    setAssignments((current) => ({ ...current, [roleId]: voiceId }));
  }

  function updateMiniMaxVoices(nextVoices: MiniMaxVoice[], connected: boolean) {
    setMinimaxVoices(nextVoices);
    setMinimaxConnected(connected);
    setMinimaxCatalog((current) => current ? {
      ...current,
      voices: nextVoices,
      catalog_source: connected ? "account" : nextVoices.some((voice) => voice.preview_ready) ? "local_cache" : "official_seed",
    } : current);
    setMinimaxAssignments((current) => {
      const available = new Set(nextVoices.map((voice) => voice.voice_id));
      const defaults = defaultMiniMaxAssignments(
        (project?.roles || []).map((role) => role.id),
        nextVoices,
      );
      return Object.fromEntries((project?.roles || []).map((role) => [
        role.id,
        available.has(current[role.id]) ? current[role.id] : defaults[role.id] || "",
      ]));
    });
    setSelectionSaveStatus("idle");
    setSelectionFeedback("");
  }

  function chooseMiniMaxVoice(roleId: string, voiceId: string) {
    setMinimaxAssignments((current) => ({ ...current, [roleId]: voiceId }));
    setSelectionSaveStatus("idle");
    setSelectionFeedback("");
  }

  async function saveVoiceSelection() {
    if (!project?.ready) return;
    setSelectionSaveStatus("saving");
    setSelectionFeedback("");
    try {
      const response = await fetch(`${API}/api/voice-selection`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: project.url,
          assignments: minimaxAssignments,
          role_names: Object.fromEntries(project.roles.map((role) => [role.id, role.name])),
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw payload;
      setProject((current) => current ? {
        ...current,
        voice_selection_saved: true,
        voice_selection_version: payload.version,
        voice_selection_sha256: payload.selection_sha256,
        paid_audition_authorized: payload.paid_audition_authorized,
      } : current);
      setSelectionSaveStatus("saved");
      setSelectionFeedback(payload.detail);
    } catch (error) {
      setSelectionSaveStatus("error");
      setSelectionFeedback(apiError(error));
    }
  }

  function updateMiniMaxCredential(configured: boolean, source: MiniMaxCatalog["source"]) {
    setMinimaxCatalog((current) => current ? { ...current, configured, source } : current);
    if (!configured) setMinimaxConnected(false);
  }

  function playVoice(voiceId: string) {
    const voice = voiceMap[voiceId];
    if (!voice?.preview_ready) return;
    audioRef.current?.pause();
    if (activeVoice === voiceId) {
      setActiveVoice(null);
      return;
    }
    const audio = new Audio(`${API}${voice.preview_url}`);
    audioRef.current = audio;
    setActiveVoice(voiceId);
    audio.onended = () => setActiveVoice(null);
    audio.onerror = () => {
      setActiveVoice(null);
      setMessage("这个试听音频尚未生成，请先运行语音预览生成步骤。 ");
    };
    audio.play();
  }

  function playMiniMaxVoice(voiceId: string) {
    const voice = minimaxVoiceMap[voiceId];
    if (!voice?.preview_ready || !voice.preview_url) return;
    const playbackKey = `minimax:${voiceId}`;
    audioRef.current?.pause();
    if (activeVoice === playbackKey) {
      setActiveVoice(null);
      return;
    }
    const audio = new Audio(`${API}${voice.preview_url}`);
    audioRef.current = audio;
    setActiveVoice(playbackKey);
    audio.onended = () => setActiveVoice(null);
    audio.onerror = () => {
      setActiveVoice(null);
      setMessage("本地 MiniMax 试听文件无法播放，请检查缓存文件是否完整。 ");
    };
    audio.play().catch(() => {
      setActiveVoice(null);
      setMessage("浏览器没有开始播放，请再点击一次试听按钮。 ");
    });
  }

  function updateVoiceFilters(next: () => void) {
    setVoiceLimit(24);
    next();
  }

  async function pollJob(jobId: string) {
    const response = await fetch(`${API}/api/jobs/${jobId}`);
    const payload = await response.json();
    if (!response.ok) throw payload;
    setJob(payload);
    if (payload.status === "queued" || payload.status === "running") {
      pollTimerRef.current = window.setTimeout(
        () => pollJob(jobId).catch((error) => setMessage(apiError(error))),
        1800,
      );
    }
  }

  function roleNames(): Record<string, string> {
    return Object.fromEntries((project?.roles || []).map((role) => [role.id, role.name]));
  }

  async function downloadExternalTemplate() {
    if (!project) return;
    setTemplateBusy(true);
    setUploadError("");
    try {
      const response = await fetch(`${API}/api/external-packs/template`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: project.url, role_names: roleNames() }),
      });
      if (!response.ok) throw await response.json();
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = `配音任务_${project.id}.zip`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(objectUrl);
    } catch (error) {
      setUploadError(apiError(error));
    } finally {
      setTemplateBusy(false);
    }
  }

  function selectExternalFiles(fileList: FileList | File[]) {
    const nextFiles = Array.from(fileList);
    setUploadError("");
    setUploadStatus("idle");
    if (!nextFiles.length) return;
    if (nextFiles.length > 200) {
      setUploadError("一次最多选择 200 个音频文件。");
      return;
    }
    const zipFiles = nextFiles.filter((file) => file.name.toLowerCase().endsWith(".zip"));
    if (zipFiles.length && (zipFiles.length !== 1 || nextFiles.length !== 1)) {
      setUploadError("ZIP 请单独选择；不要与音频文件混在一起上传。");
      return;
    }
    const unsupported = nextFiles.filter((file) => {
      const extension = file.name.split(".").pop()?.toLowerCase() || "";
      return extension !== "zip" && !AUDIO_EXTENSIONS.includes(extension);
    });
    if (unsupported.length) {
      setUploadError(`不支持的格式：${unsupported.slice(0, 3).map((file) => file.name).join("、")}`);
      return;
    }
    if (nextFiles.some((file) => file.size === 0 || file.size > (file.name.toLowerCase().endsWith(".zip") ? MAX_UPLOAD_BYTES : MAX_AUDIO_BYTES))) {
      setUploadError("存在空文件或超大文件：单个音频不超过 128 MB，ZIP 不超过 512 MB。");
      return;
    }
    if (nextFiles.reduce((total, file) => total + file.size, 0) > MAX_UPLOAD_BYTES) {
      setUploadError("本次文件总大小不能超过 512 MB。");
      return;
    }
    setExternalFiles(nextFiles);
  }

  function removeExternalFile(index: number) {
    setExternalFiles((current) => current.filter((_, fileIndex) => fileIndex !== index));
    setUploadError("");
  }

  function uploadExternalFiles() {
    if (!project || externalFiles.length === 0 || uploadStatus === "uploading") return;
    const formData = new FormData();
    for (const file of externalFiles) formData.append("files", file);
    formData.append("url", project.url);
    formData.append("role_names", JSON.stringify(roleNames()));
    if (externalPack?.pack_id) formData.append("pack_id", externalPack.pack_id);

    const request = new XMLHttpRequest();
    uploadRequestRef.current = request;
    setUploadStatus("uploading");
    setUploadProgress(0);
    setUploadError("");
    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) setUploadProgress(Math.round((event.loaded / event.total) * 100));
    });
    request.addEventListener("load", () => {
      uploadRequestRef.current = null;
      let payload: ExternalPack | { detail?: string } = {};
      try { payload = JSON.parse(request.responseText); } catch { payload = {}; }
      if (request.status >= 200 && request.status < 300 && "pack_id" in payload) {
        setExternalPack(payload as ExternalPack);
        setExternalFiles([]);
        setUploadStatus("success");
        setUploadProgress(100);
        if (fileInputRef.current) fileInputRef.current.value = "";
      } else {
        setUploadStatus("error");
        setUploadError("detail" in payload && payload.detail ? String(payload.detail) : `上传失败（${request.status}）`);
      }
    });
    request.addEventListener("error", () => {
      uploadRequestRef.current = null;
      setUploadStatus("error");
      setUploadError("无法连接本地处理服务，文件仍保留在选择列表中。");
    });
    request.addEventListener("abort", () => {
      uploadRequestRef.current = null;
      setUploadStatus("cancelled");
      setUploadError("上传已取消，文件仍保留在选择列表中。");
    });
    request.open("POST", `${API}/api/external-packs`);
    request.send(formData);
  }

  function cancelExternalUpload() {
    uploadRequestRef.current?.abort();
  }

  async function createAudition() {
    if (!project) return;
    setMessage("");
    setJob({ id: "new", status: "queued", stage: "创建任务", progress: 0, detail: "正在提交声线配置" });
    try {
      const endpoint = dubbingMode === "external"
        ? "/api/jobs/external-audition"
        : dubbingMode === "minimax"
          ? "/api/jobs/minimax-audition"
          : "/api/jobs/audition";
      const body = dubbingMode === "external"
        ? {
            url: project.url,
            pack_id: externalPack?.pack_id,
            subtitle_mode: "hard",
            preserve_source_resolution: true,
          }
        : dubbingMode === "minimax"
          ? {
              url: project.url,
              assignments: minimaxAssignments,
              role_names: roleNames(),
              config: minimaxConfig,
              requests_per_minute: minimaxRequestsPerMinute,
              subtitle_mode: "hard",
              preserve_source_resolution: true,
            }
          : {
            url: project.url,
            assignments,
            subtitle_mode: "hard",
            preserve_source_resolution: true,
          };
      const response = await fetch(`${API}${endpoint}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const payload = await response.json();
      if (!response.ok) throw payload;
      setJob(payload);
      await pollJob(payload.id);
    } catch (error) {
      setJob(null);
      setMessage(apiError(error));
    }
  }

  const running = job?.status === "queued" || job?.status === "running";
  const canGenerate = dubbingMode === "local"
    ? project?.ready && voices.length > 0
    : dubbingMode === "minimax"
      ? project?.ready && project.paid_audition_authorized && minimaxConnected && project.roles.every((role) => Boolean(minimaxAssignments[role.id]))
      : project?.ready && Boolean(externalPack?.ready);

  return (
    <main className="shell">
      <header className="topbar">
        <div className="brand">
          <span className="brandMark">译</span>
          <div><strong>声轨工坊</strong><span>AI VIDEO DUBBING DESK</span></div>
        </div>
        <nav className="deskNav" aria-label="工作台主视图">
          <button type="button" className={activeSurface === "studio" ? "active" : ""} aria-pressed={activeSurface === "studio"} onClick={() => setActiveSurface("studio")}>制作台</button>
          <button type="button" className={activeSurface === "workflow" ? "active" : ""} aria-pressed={activeSurface === "workflow"} onClick={() => setActiveSurface("workflow")}>全流程地图</button>
        </nav>
        <div className={`localPill ${connection}`}><i />
          {connection === "online" ? "本地服务已连接" : connection === "offline" ? "本地服务未连接" : "正在连接本地服务"}
        </div>
      </header>

      {activeSurface === "workflow" ? (
        <WorkflowPanel apiBase={API} />
      ) : (
        <>
      <section className="hero compactHero">
        <p className="eyebrow">01 / 导入视频</p>
        <h1>让每个角色，<br /><em>说自己的中文。</em></h1>
        <p className="heroCopy">粘贴公开视频链接，为每位说话人逐一挑选声音。翻译、校对、配音、对轴与字幕都在同一张工作台完成。</p>
        <form className="urlBar" noValidate onSubmit={(event) => { event.preventDefault(); analyze(); }}>
          <span className="linkIcon">↗</span>
          <input aria-label="视频链接" value={url} onChange={(event) => setUrl(event.target.value)} placeholder="粘贴 YouTube 或其他公开视频链接" />
          <button type="submit" disabled={analyzing || connection !== "online"}>{analyzing ? "分析中…" : "分析链接"}<span>→</span></button>
        </form>
        {message && <div className={`notice ${project?.ready ? "success" : ""}`}><span>{project?.ready ? "✓" : "!"}</span>{message}</div>}
      </section>

      {project && (
        <section className="workspace">
          <div className="sourceStrip">
            <div className="sourceIndex">SOURCE<br /><strong>{project.id.slice(0, 8)}</strong></div>
            <div className="sourceTitle"><small>已导入视频</small><strong>{project.title}</strong></div>
            <dl><div><dt>时长</dt><dd>{project.duration_label}</dd></div><div><dt>本地画质</dt><dd>{project.source_resolution}</dd></div></dl>
            <span className={`readyBadge ${project.ready ? "ready" : "waiting"}`}>{project.ready ? project.paid_audition_authorized ? "可生成试听" : "可选择音色" : "等待预处理"}</span>
          </div>

          <section className="modeSwitch" aria-labelledby="dubbing-mode-title">
            <div className="modeIntro"><p className="eyebrow">配音来源</p><h2 id="dubbing-mode-title">选择这次由谁来发声</h2></div>
            <div className="modeOptions">
              <button type="button" className={dubbingMode === "local" ? "active" : ""} aria-pressed={dubbingMode === "local"} onClick={() => setDubbingMode("local")}>
                <span>本地模型</span><small>Qwen · Kokoro · CosyVoice</small>
              </button>
              <button type="button" className={dubbingMode === "minimax" ? "active" : ""} aria-pressed={dubbingMode === "minimax"} onClick={() => setDubbingMode("minimax")}>
                <span>MiniMax API</span><small>自然音色 · 参数化付费调用</small>
              </button>
              <button type="button" className={dubbingMode === "external" ? "active" : ""} aria-pressed={dubbingMode === "external"} onClick={() => setDubbingMode("external")}>
                <span>外部配音包</span><small>剪映或任意付费语音工具</small>
              </button>
            </div>
          </section>

          <div className="sectionHead roleSectionHead">
            <div><p className="eyebrow">02 / 角色与声音</p><h2>{dubbingMode === "local" ? "逐个挑选你喜欢的声线" : dubbingMode === "minimax" ? "给每个角色分配 MiniMax 音色" : "确认角色，再交给外部工具配音"}</h2></div>
            {dubbingMode === "local" ? (
              <div className="voiceCount"><strong>{voices.length}</strong><span>个本地语音包<br />Qwen 9 · Kokoro 100 · CosyVoice 7</span></div>
            ) : dubbingMode === "minimax" ? (
              <div className="voiceCount minimaxCount"><strong>{minimaxVoices.length}</strong><span>个 MiniMax 音色<br />{minimaxConnected ? "当前账号实时目录" : "连接后载入账号目录"}</span></div>
            ) : (
              <div className="voiceCount externalCount"><strong>{project.audition.segments}</strong><span>句独立音频<br />固定文件名自动归位</span></div>
            )}
          </div>

          <div className="roleGrid">
            {project.roles.map((role, index) => {
              const selected = voiceMap[assignments[role.id]];
              const selectedMiniMax = minimaxVoiceMap[minimaxAssignments[role.id]];
              const candidateIds = role.minimax_candidates || [];
              const candidateIdSet = new Set(candidateIds);
              const candidateVoices = candidateIds
                .map((voiceId) => minimaxVoiceMap[voiceId])
                .filter((voice): voice is MiniMaxVoice => Boolean(voice));
              const otherCachedVoices = minimaxVoices.filter((voice) => (
                voice.language === "zh-CN"
                && voice.preview_ready
                && !candidateIdSet.has(voice.voice_id)
              ));
              return (
                <article className="roleCard interactiveCard" key={role.id}>
                  <div className="roleTop">
                    <span className={`avatar color${index % 4}`}>{String(index + 1).padStart(2, "0")}</span>
                    <span className={`lineCount ${role.line_count === 0 ? "muted" : ""}`}>{role.line_count == null ? "待切分" : `${role.line_count} 句台词`}</span>
                  </div>
                  <p className="roleLabel">角色名称 · 可修改</p>
                  <input className="roleNameInput" aria-label={`${role.name}名称`} value={role.name} onChange={(event) => renameRole(role.id, event.target.value)} />
                  <p className="sampleLine">“{role.sample}”</p>
                  {dubbingMode === "local" ? (
                    <>
                      <div className="voiceChoice liveChoice">
                        <button className={`play ${activeVoice === selected?.id ? "playing" : ""}`} type="button" disabled={!selected?.preview_ready} onClick={() => selected && playVoice(selected.id)} aria-label={`试听${selected?.name || "语音"}`}>{activeVoice === selected?.id ? "Ⅱ" : "▶"}</button>
                        <label><small>选择语音包</small>
                          <select aria-label={`${role.name}的本地语音包`} value={assignments[role.id] || ""} onChange={(event) => chooseVoice(role.id, event.target.value)}>
                            <optgroup label="Qwen3-TTS · 9 个声音">
                              {voicesByEngine.qwen.map((voice) => <option value={voice.id} key={voice.id}>{voice.name} · {voice.label}</option>)}
                            </optgroup>
                            <optgroup label="Kokoro 82M · 100 个中文声音">
                              {voicesByEngine.kokoro.map((voice) => <option value={voice.id} key={voice.id}>{voice.name} · {voice.label}</option>)}
                            </optgroup>
                            <optgroup label="CosyVoice 300M SFT · 7 个官方声音">
                              {voicesByEngine.cosyvoice.map((voice) => <option value={voice.id} key={voice.id}>{voice.name} · {voice.label}</option>)}
                            </optgroup>
                          </select>
                        </label>
                      </div>
                      {selected && <div className="voiceMeta"><span className={`engineTag ${selected.engine}`}>{engineName(selected.engine)}</span><span>{selected.gender}</span><span>{selected.language}</span><p>{selected.tone}</p></div>}
                    </>
                  ) : dubbingMode === "minimax" ? (
                    <>
                      <div className="voiceChoice liveChoice minimaxRoleChoice">
                        <button className={`cloudVoiceMark ${activeVoice === `minimax:${selectedMiniMax?.voice_id}` ? "playing" : ""}`} type="button" disabled={!selectedMiniMax?.preview_ready} onClick={() => selectedMiniMax && playMiniMaxVoice(selectedMiniMax.voice_id)} aria-label={selectedMiniMax?.preview_ready ? `试听${selectedMiniMax.voice_name}，播放本地缓存` : `${selectedMiniMax?.voice_name || "该音色"}暂无本地试听`}>{activeVoice === `minimax:${selectedMiniMax?.voice_id}` ? "Ⅱ" : "▶"}</button>
                        <label><small>选择 MiniMax 音色</small>
                          <select aria-label={`${role.name}的 MiniMax 音色`} value={minimaxAssignments[role.id] || ""} onChange={(event) => chooseMiniMaxVoice(role.id, event.target.value)}>
                            <optgroup label={`本项目候选 · ${candidateVoices.length} 个`}>
                              {candidateVoices.map((voice, candidateIndex) => <option value={voice.voice_id} key={voice.voice_id}>{candidateIndex === 0 ? "推荐 · " : ""}{voice.voice_name}</option>)}
                            </optgroup>
                            <optgroup label={`其他本地缓存音色 · ${otherCachedVoices.length} 个`}>
                              {otherCachedVoices.map((voice) => <option value={voice.voice_id} key={voice.voice_id}>{voice.voice_name}</option>)}
                            </optgroup>
                          </select>
                        </label>
                      </div>
                      {selectedMiniMax && <div className="voiceMeta minimaxMeta"><span className="engineTag minimax">MiniMax</span><span>{selectedMiniMax.category === "system" ? "系统" : selectedMiniMax.category === "voice_cloning" ? "复刻" : "设计"}</span><p>{selectedMiniMax.description}</p></div>}
                    </>
                  ) : (
                    <div className="voiceChoice externalRoleChoice"><span>⇣</span><div><small>外部配音标记</small><strong>{role.line_count === 0 ? "本段没有台词，无需提供音频" : `${role.line_count} 句将按角色名写入任务包`}</strong></div></div>
                  )}
                </article>
              );
            })}
          </div>

          {dubbingMode === "minimax" && (
            <section className={`selectionCheckpoint ${selectionSaveStatus}`} aria-labelledby="voice-selection-title">
              <div className="selectionCheckpointCopy">
                <p className="eyebrow">音色锁定</p>
                <h3 id="voice-selection-title">保存这次角色音色</h3>
                <p>保存后直接解锁一分钟试听；显示费用估算，但不再另行批准。</p>
              </div>
              <div className="selectionSummary" aria-label="当前角色音色分配">
                {project.roles.map((role) => (
                  <span key={role.id}><strong>{role.name}</strong><i>→</i>{minimaxVoiceMap[minimaxAssignments[role.id]]?.voice_name || "待选择"}</span>
                ))}
              </div>
              <div className="selectionCheckpointAction">
                <button
                  type="button"
                  className="primaryAction"
                  onClick={saveVoiceSelection}
                  disabled={selectionSaveStatus === "saving" || !project.ready || !project.roles.every((role) => {
                    const voice = minimaxVoiceMap[minimaxAssignments[role.id]];
                    return voice?.language === "zh-CN" && voice.preview_ready;
                  })}
                >
                  {selectionSaveStatus === "saving" ? "正在保存…" : selectionSaveStatus === "saved" ? "已保存音色" : "保存并锁定音色"}<span>→</span>
                </button>
                <p className={`selectionFeedback ${selectionSaveStatus}`} aria-live="polite">{selectionFeedback || "选定全部角色后，在这里写入版本化本地记录。"}</p>
              </div>
            </section>
          )}

          {dubbingMode === "local" && <section className="voiceLibrary">
            <div className="libraryHead">
              <div><p className="eyebrow">声音库</p><h3>先听，再选</h3></div>
              <p>显示 {Math.min(visibleVoices.length, filteredVoices.length)} / {filteredVoices.length} 个匹配声音</p>
            </div>
            <div className="voiceFilters">
              <div className="filterGroup" aria-label="模型筛选">
                {(["all", "qwen", "kokoro", "cosyvoice"] as const).map((value) => (
                  <button type="button" key={value} className={engineFilter === value ? "active" : ""} onClick={() => updateVoiceFilters(() => setEngineFilter(value))}>{value === "all" ? "全部模型" : value === "qwen" ? "Qwen3-TTS" : value === "kokoro" ? "Kokoro 82M" : "CosyVoice 300M"}</button>
                ))}
              </div>
              <div className="filterGroup" aria-label="性别筛选">
                {(["all", "男声", "女声"] as const).map((value) => (
                  <button type="button" key={value} className={genderFilter === value ? "active" : ""} onClick={() => updateVoiceFilters(() => setGenderFilter(value))}>{value === "all" ? "全部性别" : value}</button>
                ))}
              </div>
              <div className="voiceSearch"><span aria-hidden="true">⌕</span><input value={voiceSearch} onChange={(event) => updateVoiceFilters(() => setVoiceSearch(event.target.value))} placeholder="搜索编号或名称" aria-label="搜索声音" />{voiceSearch && <button type="button" aria-label="清空声音搜索" onClick={() => updateVoiceFilters(() => setVoiceSearch(""))}>×</button>}</div>
            </div>
            <div className="voiceCatalog">
              {visibleVoices.map((voice) => (
                <button type="button" className={`voiceChip ${activeVoice === voice.id ? "active" : ""}`} key={voice.id} onClick={() => playVoice(voice.id)} disabled={!voice.preview_ready}>
                  <span>{activeVoice === voice.id ? "Ⅱ" : "▶"}</span><p><strong>{voice.name}</strong><small>{voice.label}</small></p><em className={`modelDot ${voice.engine}`}>{engineName(voice.engine)}</em>{voice.recommended && <i>推荐</i>}
                </button>
              ))}
            </div>
            {visibleVoices.length < filteredVoices.length && <button className="loadMore" type="button" onClick={() => setVoiceLimit((current) => current + 24)}>再显示 24 个</button>}
            {filteredVoices.length === 0 && <div className="emptyVoices">没有匹配的声音，换个筛选条件试试。</div>}
          </section>}

          {dubbingMode === "minimax" && minimaxCatalog && (
            <MiniMaxPanel
              apiBase={API}
              catalog={minimaxCatalog}
              voices={minimaxVoices}
              connected={minimaxConnected}
              config={minimaxConfig}
              requestsPerMinute={minimaxRequestsPerMinute}
              onCatalogVoices={updateMiniMaxVoices}
              onCredentialStatus={updateMiniMaxCredential}
              onConfig={setMinimaxConfig}
              onRequestsPerMinute={setMinimaxRequestsPerMinute}
              activeVoiceKey={activeVoice}
              onPlayCachedVoice={playMiniMaxVoice}
              segmentCount={project.audition.segments}
              paidAuthorized={project.paid_audition_authorized}
            />
          )}

          {dubbingMode === "external" && (
            <section className="externalPackPanel" aria-labelledby="external-pack-title">
              <div className="externalPackHead">
                <div><p className="eyebrow">外部配音交接</p><h3 id="external-pack-title">一份清单出去，一包音频回来</h3></div>
                <p>工作台负责文件编号、时长检查和绝对时间轴；外部工具只负责声音。</p>
              </div>

              <div className="handoffGrid">
                <article className="handoffStep">
                  <span className="stepNumber">A</span>
                  <div><strong>下载配音任务包</strong><p>包含 CSV、JSON、中文台词和 {project.audition.segments} 个固定文件名。</p></div>
                  <button type="button" className="secondaryAction" onClick={downloadExternalTemplate} disabled={templateBusy || !project.ready}>{templateBusy ? "正在打包…" : "下载 ZIP"}</button>
                </article>

                <article className="handoffStep uploadStep">
                  <span className="stepNumber">B</span>
                  <div><strong>{externalPack ? "补充或替换音频" : "上传配音结果"}</strong><p>可上传一个 ZIP，或多选 WAV / MP3 / M4A 等逐句音频。</p></div>
                  <input
                    ref={fileInputRef}
                    className="visuallyHidden"
                    type="file"
                    accept=".zip,.wav,.mp3,.m4a,.aac,.flac,.ogg,audio/*"
                    multiple
                    aria-label="选择外部配音文件"
                    onChange={(event) => event.target.files && selectExternalFiles(event.target.files)}
                  />
                  <button
                    type="button"
                    className={`dropZone ${dragOver ? "dragOver" : ""}`}
                    onClick={() => fileInputRef.current?.click()}
                    onDragOver={(event) => { event.preventDefault(); setDragOver(true); }}
                    onDragLeave={() => setDragOver(false)}
                    onDrop={(event) => {
                      event.preventDefault();
                      setDragOver(false);
                      selectExternalFiles(event.dataTransfer.files);
                    }}
                  >
                    <span>＋</span><strong>选择文件</strong><small>也可以拖到这里 · 总计不超过 512 MB</small>
                  </button>
                </article>
              </div>

              {externalFiles.length > 0 && (
                <div className="uploadQueue" aria-live="polite">
                  <div className="queueHead"><strong>已选择 {externalFiles.length} 个文件</strong><span>{formatBytes(externalFiles.reduce((total, file) => total + file.size, 0))}</span></div>
                  <div className="queueFiles">
                    {externalFiles.map((file, index) => (
                      <div className="queueFile" key={`${file.name}-${file.lastModified}`}><span>{file.name}</span><small>{formatBytes(file.size)}</small><button type="button" aria-label={`移除 ${file.name}`} onClick={() => removeExternalFile(index)} disabled={uploadStatus === "uploading"}>移除</button></div>
                    ))}
                  </div>
                  {uploadStatus === "uploading" && <div className="uploadProgress"><div><i style={{ width: `${uploadProgress}%` }} /></div><span>正在上传并解码 {uploadProgress}%</span></div>}
                  <div className="queueActions">
                    {uploadStatus === "uploading" ? (
                      <button type="button" className="secondaryAction" onClick={cancelExternalUpload}>取消上传</button>
                    ) : (
                      <button type="button" className="primarySmall" onClick={uploadExternalFiles}>{externalPack ? "补充并重新检查" : "上传并检查时间轴"}</button>
                    )}
                  </div>
                </div>
              )}

              {uploadError && <div className="inlineError" role="alert"><span>!</span><p>{uploadError}</p></div>}

              {externalPack && (
                <section className={`packReport ${externalPack.ready ? "packReady" : "packIncomplete"}`} aria-labelledby="pack-report-title">
                  <div className="reportSummary">
                    <div><p className="eyebrow">导入检查</p><h4 id="pack-report-title">{externalPack.ready ? "音频齐全，可以生成" : "还有音频需要补齐"}</h4></div>
                    <div className="reportMeter"><strong>{externalPack.ready_count}<span>/{externalPack.required_count}</span></strong><small>句已识别</small></div>
                    <dl><div><dt>缺失</dt><dd>{externalPack.missing.length}</dd></div><div><dt>无效</dt><dd>{externalPack.invalid.length}</dd></div><div><dt>超时句</dt><dd>{externalPack.warning_count}</dd></div><div><dt>多余文件</dt><dd>{externalPack.extras.length}</dd></div></dl>
                  </div>
                  <div className="segmentTableWrap">
                    <table className="segmentTable">
                      <caption>外部配音逐句检查结果</caption>
                      <thead><tr><th scope="col">片段</th><th scope="col">角色 / 中文台词</th><th scope="col">原时间槽</th><th scope="col">音频</th><th scope="col">检查结果</th></tr></thead>
                      <tbody>
                        {externalPack.segments.map((segment) => (
                          <tr key={segment.segment_id}>
                            <td><strong>{segment.segment_id.replace("segment_", "#")}</strong><small>{segment.start_time}</small></td>
                            <td><span>{segment.role_name}</span><p>{segment.text}</p></td>
                            <td>{segment.slot_seconds.toFixed(2)}s</td>
                            <td>{segment.audio_seconds === null ? "—" : `${segment.audio_seconds.toFixed(2)}s`}</td>
                            <td><span className={`segmentStatus ${segment.status}`}>{segment.status === "ready" ? "原速合适" : segment.status === "tight" ? "轻微超时" : segment.status === "long" ? "建议重配" : segment.status === "invalid" ? "文件无效" : "缺少音频"}</span><small>{segment.message}</small></td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </section>
              )}
            </section>
          )}

          <section className="exportPanel">
            <div className="sectionHead exportHead"><div><p className="eyebrow">03 / 生成设置</p><h2>{dubbingMode === "local" ? "先做一分钟试听" : dubbingMode === "minimax" ? "让 MiniMax 逐句生成并自动对轴" : "把外部音频放回原时间轴"}</h2></div><span className="timecode">{formatTimecode(project.audition.start)} → {formatTimecode(project.audition.end)}</span></div>
            <div className="settingsGrid">
              <label className="setting" htmlFor="setting-subtitles" aria-label="烧录中文字幕"><input id="setting-subtitles" type="checkbox" defaultChecked /><span><strong>烧录中文字幕</strong><small>兼容所有播放器</small></span></label>
              <label className="setting" htmlFor="setting-resolution" aria-label="保持源分辨率"><input id="setting-resolution" type="checkbox" defaultChecked /><span><strong>保持源分辨率</strong><small>不进行虚假插值放大</small></span></label>
              <label className="setting" htmlFor="setting-background" aria-label="保留环境声"><input id="setting-background" type="checkbox" defaultChecked /><span><strong>保留环境声</strong><small>使用人声分离后的背景轨</small></span></label>
              <label className="setting" htmlFor="setting-timeline" aria-label="固定原始起点"><input id="setting-timeline" type="checkbox" defaultChecked /><span><strong>固定原始起点</strong><small>保持自然语速，不做后期变速</small></span></label>
            </div>

            {job && (
              <div className={`jobPanel ${job.status}`}>
                <div className="jobTop"><div><small>{job.stage}</small><strong>{job.detail}</strong></div><b>{job.progress}%</b></div>
                <div className="progressTrack"><i style={{ width: `${job.progress}%` }} /></div>
                {job.warning && <p className="jobWarning">{job.warning}</p>}
                {job.status === "complete" && job.output_url && (
                  /* Captions are permanently burned into this generated video. */
                  // eslint-disable-next-line jsx-a11y/media-has-caption
                  <div className="resultPlayer"><video controls preload="metadata" src={`${API}${job.output_url}`} /><div><strong>试听片已完成</strong><span>耗时 {job.elapsed_seconds}s · 自然语速 · 超时 {job.metrics?.timing_overflow_count || 0} 句{job.metrics?.minimax?.estimated_cny !== undefined ? ` · MiniMax 约 ¥${job.metrics.minimax.estimated_cny.toFixed(4)}` : ""}</span><a href={`${API}${job.output_url}`} download>下载 MP4</a></div></div>
                )}
              </div>
            )}

            <div className="actionRow finalAction">
              <div className="timelineNote"><span>{project.audition.duration.toFixed(2)}s</span><p><strong>{canGenerate ? "配置已就绪" : dubbingMode === "external" ? "请先上传完整配音包" : dubbingMode === "minimax" ? project.paid_audition_authorized ? "请先测试 MiniMax 连接" : "请先保存并锁定全部角色音色" : "这个项目还需要预处理"}</strong><br />{dubbingMode === "external" ? "自动校正采样率、裁句尾静音并按原始起点落位" : dubbingMode === "minimax" ? project.paid_audition_authorized ? "点击后会产生付费调用；逐句生成、保持自然语速并烧录字幕" : "全部角色选定并保存后直接解锁一分钟试听；费用仍会预估显示，但无需再次确认。" : project.ready ? "换声线只重做配音和导出，不重做翻译" : "完成下载、转写和角色切分后即可生成"}</p></div>
              <button className="primaryAction" type="button" onClick={createAudition} disabled={!canGenerate || running}>{running ? `${job?.stage} ${job?.progress}%` : dubbingMode === "external" ? "用外部配音生成试听" : dubbingMode === "minimax" ? project.paid_audition_authorized ? `生成一分钟试听 · 约 ¥${minimaxCatalog?.audition_estimates[minimaxConfig.model]?.estimated_cny.toFixed(4) || "—"}` : "先保存并锁定音色" : "用所选声音生成试听"}<span>↗</span></button>
            </div>
          </section>
        </section>
      )}
        </>
      )}

      <footer><span>声轨工坊 / LOCAL-FIRST</span><p>项目文件留在本机；仅在你点击生成时把当前中文台词发送给所选语音服务</p></footer>
    </main>
  );
}

export default function Home() {
  return <WorkbenchShell studio={<StudioWorkspace />} />;
}
