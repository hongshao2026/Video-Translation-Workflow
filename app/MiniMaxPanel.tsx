"use client";

import { useMemo, useState } from "react";
import {
  estimatePreviewCost,
  MINIMAX_EFFECT_LABELS,
  MINIMAX_EMOTION_LABELS,
  type MiniMaxCatalog,
  type MiniMaxConfig,
  type MiniMaxPreview,
  type MiniMaxVoice,
} from "./minimax";


type MiniMaxPanelProps = {
  apiBase: string;
  catalog: MiniMaxCatalog;
  voices: MiniMaxVoice[];
  connected: boolean;
  config: MiniMaxConfig;
  requestsPerMinute: 10 | 20;
  onCatalogVoices: (voices: MiniMaxVoice[], connected: boolean) => void;
  onCredentialStatus: (configured: boolean, source: MiniMaxCatalog["source"]) => void;
  onConfig: (config: MiniMaxConfig) => void;
  onRequestsPerMinute: (value: 10 | 20) => void;
  activeVoiceKey: string | null;
  onPlayCachedVoice: (voiceId: string) => void;
  segmentCount: number;
  paidAuthorized: boolean;
};

function responseError(value: unknown): string {
  if (value && typeof value === "object" && "detail" in value) return String(value.detail);
  return "MiniMax 服务暂时没有响应";
}

function categoryLabel(category: MiniMaxVoice["category"]): string {
  if (category === "voice_cloning") return "账号复刻音色";
  if (category === "voice_generation") return "账号设计音色";
  return "系统音色";
}

function RangeField({
  id,
  label,
  hint,
  value,
  min,
  max,
  step,
  unit,
  disabled = false,
  onChange,
}: {
  id: string;
  label: string;
  hint: string;
  value: number;
  min: number;
  max: number;
  step: number;
  unit?: string;
  disabled?: boolean;
  onChange: (value: number) => void;
}) {
  function updateNumber(rawValue: string) {
    const parsed = Number(rawValue);
    if (!Number.isFinite(parsed)) return;
    onChange(Math.min(max, Math.max(min, parsed)));
  }

  return (
    <div className="rangeField">
      <div className="rangeLabel"><label htmlFor={id}>{label}<small>{hint}</small></label><output htmlFor={id}>{value}{unit || ""}</output></div>
      <div className="rangeControls">
        <input id={id} type="range" min={min} max={max} step={step} value={value} disabled={disabled} onChange={(event) => updateNumber(event.target.value)} />
        <input className="rangeNumber" aria-label={`${label}精确数值`} type="number" min={min} max={max} step={step} value={value} disabled={disabled} onChange={(event) => updateNumber(event.target.value)} />
      </div>
    </div>
  );
}

export default function MiniMaxPanel({
  apiBase,
  catalog,
  voices,
  connected,
  config,
  requestsPerMinute,
  onCatalogVoices,
  onCredentialStatus,
  onConfig,
  onRequestsPerMinute,
  activeVoiceKey,
  onPlayCachedVoice,
  segmentCount,
  paidAuthorized,
}: MiniMaxPanelProps) {
  const [apiKey, setApiKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [connectionBusy, setConnectionBusy] = useState(false);
  const [connectionMessage, setConnectionMessage] = useState("");
  const [connectionError, setConnectionError] = useState("");
  const [previewText, setPreviewText] = useState("你好，这是一段 MiniMax 中文配音试听。我们先听自然度，再决定是否批量生成。");
  const [previewVoice, setPreviewVoice] = useState(voices[0]?.voice_id || "");
  const [previewBusy, setPreviewBusy] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [preview, setPreview] = useState<MiniMaxPreview | null>(null);
  const [cacheSearch, setCacheSearch] = useState("");

  const resolvedPreviewVoice = voices.some((voice) => voice.voice_id === previewVoice)
    ? previewVoice
    : voices[0]?.voice_id || "";
  const previewEstimate = useMemo(
    () => estimatePreviewCost(previewText, config.model),
    [previewText, config.model],
  );
  const batchEstimate = catalog.audition_estimates[config.model];
  const allowedEmotions = config.model.startsWith("speech-2.8")
    ? catalog.emotions.filter((emotion) => !["fluent", "whisper"].includes(emotion))
    : catalog.emotions;
  const cachedMandarinVoices = useMemo(
    () => voices.filter((voice) => voice.language === "zh-CN" && voice.preview_ready && voice.preview_url),
    [voices],
  );
  const visibleCachedVoices = useMemo(() => {
    const query = cacheSearch.trim().toLowerCase();
    if (!query) return cachedMandarinVoices;
    return cachedMandarinVoices.filter((voice) => (
      `${voice.voice_name} ${voice.description} ${voice.voice_id}`.toLowerCase().includes(query)
    ));
  }, [cacheSearch, cachedMandarinVoices]);

  function patchConfig<K extends keyof MiniMaxConfig>(key: K, value: MiniMaxConfig[K]) {
    onConfig({ ...config, [key]: value });
  }

  async function saveAndConnect() {
    if (!paidAuthorized) {
      setConnectionError("请先用本地缓存完成全部角色的选音，并保存锁定音色。 ");
      return;
    }
    if (!apiKey.trim() && !catalog.configured) {
      setConnectionError("请先填写 API Key；它只会保存在本机后端内存。 ");
      return;
    }
    setConnectionBusy(true);
    setConnectionError("");
    setConnectionMessage("");
    try {
      if (apiKey.trim()) {
        const saveResponse = await fetch(`${apiBase}/api/minimax/credential`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ api_key: apiKey.trim() }),
        });
        const savePayload = await saveResponse.json();
        if (!saveResponse.ok) throw savePayload;
        onCredentialStatus(true, savePayload.source);
        setApiKey("");
        setShowKey(false);
      }
      const response = await fetch(`${apiBase}/api/minimax/connect`, { method: "POST" });
      const payload = await response.json();
      if (!response.ok) throw payload;
      onCatalogVoices(payload.voices, true);
      onCredentialStatus(true, payload.source);
      setConnectionMessage(payload.detail);
    } catch (error) {
      onCatalogVoices(voices, false);
      setConnectionError(responseError(error));
    } finally {
      setConnectionBusy(false);
    }
  }

  async function clearCredential() {
    setConnectionBusy(true);
    setConnectionError("");
    try {
      const response = await fetch(`${apiBase}/api/minimax/credential`, { method: "DELETE" });
      const payload = await response.json();
      if (!response.ok) throw payload;
      onCredentialStatus(false, "none");
      onCatalogVoices(payload.voices || catalog.starter_voices, false);
      setConnectionMessage(payload.detail);
      setPreview(null);
    } catch (error) {
      setConnectionError(responseError(error));
    } finally {
      setConnectionBusy(false);
    }
  }

  async function createPreview() {
    if (!paidAuthorized || !connected || !previewText.trim() || !resolvedPreviewVoice || previewBusy) return;
    setPreviewBusy(true);
    setPreviewError("");
    setPreview(null);
    try {
      const response = await fetch(`${apiBase}/api/minimax/preview`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text: previewText.trim(),
          voice_id: resolvedPreviewVoice,
          config,
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw payload;
      setPreview(payload);
    } catch (error) {
      setPreviewError(responseError(error));
    } finally {
      setPreviewBusy(false);
    }
  }

  return (
    <section className="minimaxPanel" aria-labelledby="minimax-panel-title">
      <div className="minimaxPanelHead">
        <div><p className="eyebrow">MINIMAX API MODULE</p><h3 id="minimax-panel-title">{paidAuthorized ? "连接账号并生成试听" : "先听本地缓存，再锁定角色音色"}</h3></div>
        <span className={`providerStatus ${connected ? "connected" : catalog.configured ? "configured" : "waiting"}`}>
          {connected ? `已连接 · ${voices.length} 音色` : catalog.configured ? "密钥已保存 · 待测试" : "等待 API Key"}
        </span>
      </div>

      <div className="credentialPanel">
        <div className="credentialCopy"><strong>{paidAuthorized ? "本机临时凭证" : "等待音色锁定"}</strong><p>{paidAuthorized ? "只发给 127.0.0.1 后端，不写入网页源码、日志或浏览器存储；重启服务后自动清除。" : "先播放本机缓存完成选音；保存并锁定后即可连接账号生成试听。"}</p></div>
        <div className="secretField">
          <label htmlFor="minimax-api-key">MiniMax API Key</label>
          <div><input id="minimax-api-key" type={showKey ? "text" : "password"} value={apiKey} autoComplete="off" disabled={!paidAuthorized} placeholder={paidAuthorized ? catalog.configured ? "已配置；留空可直接测试" : "输入 sk-…" : "当前关卡不需要凭证"} onChange={(event) => setApiKey(event.target.value)} />
            <button type="button" aria-label={showKey ? "隐藏 API Key" : "显示 API Key"} aria-pressed={showKey} disabled={!paidAuthorized} onClick={() => setShowKey((current) => !current)}>{showKey ? "隐藏" : "显示"}</button>
          </div>
        </div>
        <div className="credentialActions">
          <button type="button" className="primarySmall" onClick={saveAndConnect} disabled={connectionBusy || !paidAuthorized}>{connectionBusy ? "正在连接…" : paidAuthorized ? apiKey.trim() ? "保存并测试连接" : "测试连接并载入音色" : "先锁定角色音色"}</button>
          {catalog.source === "memory" && <button type="button" className="secondaryAction" onClick={clearCredential} disabled={connectionBusy}>清除内存密钥</button>}
        </div>
      </div>
      <div className="providerFeedback" aria-live="polite">
        {connectionMessage && <p className="connectionSuccess">✓ {connectionMessage}</p>}
        {connectionError && <p className="connectionFailure" role="alert">! {connectionError}</p>}
        {!connectionMessage && !connectionError && <p>当前显示{catalog.catalog_source === "account" ? "账号实时" : catalog.catalog_source === "local_cache" ? "本地缓存" : "官方文档示例"}音色；连接后会读取这个账号的系统、复刻和设计音色。</p>}
      </div>

      <section className="voiceLibrary minimaxVoiceLibrary" aria-labelledby="minimax-cache-title">
        <div className="libraryHead">
          <div><p className="eyebrow">本地普通话试听库</p><h3 id="minimax-cache-title">点击即听，不再扣额度</h3></div>
          <p><strong>{cachedMandarinVoices.length} / {catalog.preview_cache.mandarin_total}</strong> 个已缓存</p>
        </div>
        <div className="cachePromise"><span aria-hidden="true">✓</span><p><strong>这些按钮不会调用 MiniMax API</strong><small>统一使用 speech-2.8-hd、中性、1.0× 生成一次，之后只播放本机 MP3。</small></p></div>
        <div className="voiceFilters minimaxCacheFilters">
          <p>显示 {visibleCachedVoices.length} 个匹配音色</p>
          <div className="voiceSearch"><span aria-hidden="true">⌕</span><input value={cacheSearch} onChange={(event) => setCacheSearch(event.target.value)} placeholder="搜索音色名称或特点" aria-label="搜索 MiniMax 普通话音色" />{cacheSearch && <button type="button" aria-label="清空 MiniMax 音色搜索" onClick={() => setCacheSearch("")}>×</button>}</div>
        </div>
        <div className="voiceCatalog minimaxVoiceCatalog">
          {visibleCachedVoices.map((voice) => {
            const playing = activeVoiceKey === `minimax:${voice.voice_id}`;
            return (
              <button type="button" className={`voiceChip minimaxVoiceChip ${playing ? "active" : ""}`} key={voice.voice_id} onClick={() => onPlayCachedVoice(voice.voice_id)} aria-pressed={playing} aria-label={`${playing ? "暂停" : "试听"}${voice.voice_name}，本地缓存`}>
                <span>{playing ? "Ⅱ" : "▶"}</span><p><strong>{voice.voice_name}</strong><small>{voice.description}</small></p><em>MiniMax 2.8 HD</em><i>本地</i>
              </button>
            );
          })}
        </div>
        {visibleCachedVoices.length === 0 && <div className="emptyVoices">{cachedMandarinVoices.length ? "没有匹配的音色，换个关键词试试。" : "本地试听正在准备，完成后会在这里显示。"}</div>}
      </section>

      <div className="minimaxConfigGrid">
        <div className="selectField wideField"><label htmlFor="minimax-model">语音模型<small>HD 更细腻，Turbo 更省费用</small></label><select id="minimax-model" value={config.model} onChange={(event) => {
          const model = event.target.value;
          const emotion = model.startsWith("speech-2.8") && ["fluent", "whisper"].includes(config.emotion) ? "" : config.emotion;
          onConfig({ ...config, model, emotion });
        }}>{catalog.models.map((model) => <option key={model.id} value={model.id}>{model.label}</option>)}</select></div>
        <div className="selectField"><label htmlFor="minimax-emotion">情绪<small>自动通常最自然</small></label><select id="minimax-emotion" value={config.emotion} onChange={(event) => patchConfig("emotion", event.target.value)}><option value="">自动判断</option>{allowedEmotions.map((emotion) => <option key={emotion} value={emotion}>{MINIMAX_EMOTION_LABELS[emotion] || emotion}</option>)}</select></div>
        <div className="selectField"><label htmlFor="minimax-language">语言增强<small>中文配音固定优先普通话</small></label><select id="minimax-language" value={config.language_boost} onChange={(event) => patchConfig("language_boost", event.target.value)}>{catalog.language_boosts.map((value) => <option key={value} value={value}>{value === "Chinese" ? "普通话" : value === "Chinese,Yue" ? "粤语" : "自动识别"}</option>)}</select></div>

        <RangeField id="minimax-speed" label="语速" hint="本项目固定原生 1.0×" value={1} min={1} max={1} step={0.05} disabled onChange={() => patchConfig("speed", 1)} />
        <RangeField id="minimax-volume" label="音量" hint="建议保持 1.0" value={config.volume} min={0.1} max={10} step={0.1} onChange={(value) => patchConfig("volume", value)} />
        <RangeField id="minimax-pitch" label="基础语调" hint="半音调整" value={config.pitch} min={-12} max={12} step={1} onChange={(value) => patchConfig("pitch", value)} />

        <div className="selectField"><label htmlFor="minimax-rate">采样率<small>视频配音建议 32 kHz</small></label><select id="minimax-rate" value={config.sample_rate} onChange={(event) => patchConfig("sample_rate", Number(event.target.value))}>{catalog.sample_rates.map((value) => <option key={value} value={value}>{(value / 1000).toFixed(value % 1000 ? 2 : 0)} kHz</option>)}</select></div>
        <div className="selectField"><label htmlFor="minimax-format">返回格式<small>工作台会自动转为 24 kHz WAV</small></label><select id="minimax-format" value={config.format} onChange={(event) => patchConfig("format", event.target.value)}>{catalog.formats.map((value) => <option key={value} value={value}>{value.toUpperCase()}</option>)}</select></div>
        <div className="selectField"><label htmlFor="minimax-bitrate">MP3 比特率<small>仅 MP3 时生效</small></label><select id="minimax-bitrate" value={config.bitrate} disabled={config.format !== "mp3"} onChange={(event) => patchConfig("bitrate", Number(event.target.value))}>{catalog.bitrates.map((value) => <option key={value} value={value}>{value / 1000} kbps</option>)}</select></div>
      </div>

      <details className="advancedVoiceSettings">
        <summary>声音细调与调用频率</summary>
        <div className="advancedGrid">
          <RangeField id="minimax-modifier-pitch" label="明暗" hint="低沉 → 明亮" value={config.modifier_pitch} min={-100} max={100} step={5} onChange={(value) => patchConfig("modifier_pitch", value)} />
          <RangeField id="minimax-intensity" label="力量质感" hint="刚劲 → 柔和" value={config.modifier_intensity} min={-100} max={100} step={5} onChange={(value) => patchConfig("modifier_intensity", value)} />
          <RangeField id="minimax-timbre" label="音色质感" hint="浑厚 → 清脆" value={config.modifier_timbre} min={-100} max={100} step={5} onChange={(value) => patchConfig("modifier_timbre", value)} />
          <div className="selectField"><label htmlFor="minimax-effect">声音效果<small>对白通常保持关闭</small></label><select id="minimax-effect" value={config.sound_effect} onChange={(event) => patchConfig("sound_effect", event.target.value)}>{catalog.sound_effects.map((value) => <option key={value || "none"} value={value}>{MINIMAX_EFFECT_LABELS[value] || value}</option>)}</select></div>
          <fieldset className="rpmField"><legend>请求频率<small>按账号等级选择，默认免费档更稳妥</small></legend><div><button type="button" className={requestsPerMinute === 10 ? "active" : ""} aria-pressed={requestsPerMinute === 10} onClick={() => onRequestsPerMinute(10)}>免费档 10 RPM</button><button type="button" className={requestsPerMinute === 20 ? "active" : ""} aria-pressed={requestsPerMinute === 20} onClick={() => onRequestsPerMinute(20)}>充值档 20 RPM</button></div></fieldset>
          <label className="normalizationToggle" htmlFor="minimax-normalization" aria-label="启用数字文本规范化"><input id="minimax-normalization" type="checkbox" checked={config.text_normalization} onChange={(event) => patchConfig("text_normalization", event.target.checked)} /><span><strong>数字文本规范化</strong><small>数字、日期较多时更稳，但会略增延迟</small></span></label>
        </div>
      </details>

      <section className="minimaxPreview" aria-labelledby="minimax-preview-title">
        <div className="previewCopy"><p className="eyebrow">自定义单句 · 付费</p><h4 id="minimax-preview-title">需要换台词或参数时，再生成新试听</h4><p>上方 58 个固定试听可永久免费本地播放；只有点击这里的生成按钮才会调用接口。</p></div>
        <div className="previewEditor">
          <label htmlFor="minimax-preview-text">试听台词</label>
          <textarea id="minimax-preview-text" className="resize-none" value={previewText} maxLength={500} rows={3} onChange={(event) => setPreviewText(event.target.value)} />
          <div className="previewMeta"><span>{previewText.length}/500 字符</span><span>计费字符 {previewEstimate.billable_characters}</span><strong>预估 ¥{previewEstimate.estimated_cny.toFixed(4)}</strong></div>
        </div>
        <div className="previewVoiceField selectField"><label htmlFor="minimax-preview-voice">试听音色<small>{voices.find((voice) => voice.voice_id === resolvedPreviewVoice)?.description || "先连接账号读取音色"}</small></label><select id="minimax-preview-voice" value={resolvedPreviewVoice} onChange={(event) => setPreviewVoice(event.target.value)}>{(["system", "voice_cloning", "voice_generation"] as const).map((category) => {
          const categoryVoices = voices.filter((voice) => voice.category === category);
          return categoryVoices.length ? <optgroup key={category} label={categoryLabel(category)}>{categoryVoices.map((voice) => <option key={voice.voice_id} value={voice.voice_id}>{voice.voice_name}</option>)}</optgroup> : null;
        })}</select></div>
        <div className="previewAction">
          <button type="button" className="primarySmall paidAction" onClick={createPreview} disabled={!paidAuthorized || !connected || !previewText.trim() || !resolvedPreviewVoice || previewBusy}>{previewBusy ? "正在生成…" : paidAuthorized ? `生成试听 · 约 ¥${previewEstimate.estimated_cny.toFixed(4)}` : "先锁定角色音色"}</button>
          {!paidAuthorized ? <small>保存并锁定角色音色后即可生成，无需另行批准。</small> : !connected && <small>先完成“测试连接并载入音色”</small>}
        </div>
        {previewError && <div className="inlineError" role="alert"><span>!</span><p>{previewError}</p></div>}
        {preview && (
          <div className="previewResult">
            {/* This result is speech-only audio generated from the visible text above. */}
            {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
            <audio controls preload="metadata" src={`${apiBase}${preview.audio_url}`} />
            <p><strong>试听已返回</strong><span>实际计费字符 {preview.usage_characters} · 约 ¥{preview.estimated_cny.toFixed(4)}{preview.audio_length_ms ? ` · ${(preview.audio_length_ms / 1000).toFixed(2)}s` : ""}</span></p>
          </div>
        )}
      </section>

      <div className="batchCostNotice"><span>{paidAuthorized ? "¥" : "锁"}</span><p><strong>{paidAuthorized ? `这一分钟批量生成预估 ${batchEstimate ? `¥${batchEstimate.estimated_cny.toFixed(4)}` : "—"}` : "锁定角色音色后即可生成"}</strong><br />{paidAuthorized ? `共 ${segmentCount} 句，按句调用并遵守 ${requestsPerMinute} RPM；网络超时或限流不会自动重试，避免重复计费。` : "先听 58 个本地缓存音色并完成角色选择；保存后直接解锁试听，同时显示预计字符数和费用。"}</p></div>
    </section>
  );
}
