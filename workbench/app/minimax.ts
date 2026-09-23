export type MiniMaxVoice = {
  voice_id: string;
  voice_name: string;
  description: string;
  category: "system" | "voice_cloning" | "voice_generation";
  created_time?: string | null;
  language?: "zh-CN" | null;
  preview_ready?: boolean;
  preview_url?: string | null;
  preview_model?: string | null;
  preview_sample_text?: string | null;
  preview_duration_ms?: number | null;
};

export type MiniMaxConfig = {
  model: string;
  speed: number;
  volume: number;
  pitch: number;
  emotion: string;
  sample_rate: number;
  bitrate: number;
  format: string;
  channel: number;
  language_boost: string;
  text_normalization: boolean;
  modifier_pitch: number;
  modifier_intensity: number;
  modifier_timbre: number;
  sound_effect: string;
};

export type BillingEstimate = {
  billable_characters: number;
  price_per_10k_cny: number;
  estimated_cny: number;
};

export type MiniMaxCatalog = {
  configured: boolean;
  source: "none" | "memory" | "environment";
  catalog_source: "official_seed" | "local_cache" | "account";
  api_base_url: string;
  models: { id: string; label: string }[];
  sample_rates: number[];
  bitrates: number[];
  formats: string[];
  emotions: string[];
  language_boosts: string[];
  sound_effects: string[];
  defaults: MiniMaxConfig;
  voices: MiniMaxVoice[];
  starter_voices: MiniMaxVoice[];
  pricing: { hd_cny_per_10k: number; turbo_cny_per_10k: number };
  rate_limits: { free_rpm: 10; paid_rpm: 20 };
  preview_cache: {
    ready: number;
    mandarin_total: number;
    sample_model: string;
    local_playback_billable: boolean;
  };
  audition_estimates: Record<string, BillingEstimate>;
  paid_audition_authorized: boolean;
};

export type MiniMaxPreview = {
  id: string;
  audio_url: string;
  trace_id?: string | null;
  audio_format: string;
  audio_length_ms?: number | null;
  usage_characters: number;
  estimated_cny: number;
};

export function billableCharacters(text: string): number {
  return Array.from(text).reduce((total, character) => {
    const code = character.codePointAt(0) || 0;
    const isHan = (
      (code >= 0x3400 && code <= 0x4dbf)
      || (code >= 0x4e00 && code <= 0x9fff)
      || (code >= 0xf900 && code <= 0xfaff)
      || (code >= 0x20000 && code <= 0x323af)
    );
    return total + (isHan ? 2 : 1);
  }, 0);
}

export function estimatePreviewCost(text: string, model: string): BillingEstimate {
  const characters = billableCharacters(text);
  const price = model.endsWith("-hd") ? 3.5 : 2;
  return {
    billable_characters: characters,
    price_per_10k_cny: price,
    estimated_cny: Math.round((characters / 10_000 * price) * 10_000) / 10_000,
  };
}

export function defaultMiniMaxAssignments(
  roleIds: string[],
  voices: MiniMaxVoice[],
): Record<string, string> {
  if (!voices.length) return {};
  return Object.fromEntries(roleIds.map((roleId, index) => [
    roleId,
    voices[index % voices.length].voice_id,
  ]));
}

export const MINIMAX_EMOTION_LABELS: Record<string, string> = {
  "": "自动判断",
  happy: "高兴",
  sad: "悲伤",
  angry: "愤怒",
  fearful: "害怕",
  disgusted: "厌恶",
  surprised: "惊讶",
  calm: "中性",
  fluent: "生动（仅 2.6）",
  whisper: "低语（仅 2.6）",
};

export const MINIMAX_EFFECT_LABELS: Record<string, string> = {
  "": "不加效果",
  spacious_echo: "空旷回音",
  auditorium_echo: "礼堂广播",
  lofi_telephone: "电话失真",
  robotic: "电音",
};
