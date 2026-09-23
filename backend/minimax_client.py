"""Small, testable MiniMax speech adapter used by the local workbench.

The adapter deliberately owns authentication, validation, billing estimates and
response decoding so the UI and timeline pipeline never handle API keys or hex
audio directly.
"""

from __future__ import annotations

import os
import threading
from dataclasses import asdict, dataclass
from typing import Any

import requests

API_BASE_URL = "https://api.minimax.cn"
MODELS = (
    "speech-2.8-hd",
    "speech-2.8-turbo",
    "speech-2.6-hd",
    "speech-2.6-turbo",
    "speech-02-hd",
    "speech-02-turbo",
    "speech-01-hd",
    "speech-01-turbo",
)
SAMPLE_RATES = (8000, 16000, 22050, 24000, 32000, 44100)
BITRATES = (32000, 64000, 128000, 256000)
AUDIO_FORMATS = ("mp3", "wav", "flac")
EMOTIONS = (
    "happy",
    "sad",
    "angry",
    "fearful",
    "disgusted",
    "surprised",
    "calm",
    "fluent",
    "whisper",
)
LANGUAGE_BOOSTS = ("Chinese", "auto", "Chinese,Yue")
SOUND_EFFECTS = ("", "spacious_echo", "auditorium_echo", "lofi_telephone", "robotic")

MODEL_LABELS = {
    "speech-2.8-hd": "2.8 HD · 细节优先",
    "speech-2.8-turbo": "2.8 Turbo · 速度优先",
    "speech-2.6-hd": "2.6 HD · 支持低语",
    "speech-2.6-turbo": "2.6 Turbo · 支持低语",
    "speech-02-hd": "02 HD · 历史模型",
    "speech-02-turbo": "02 Turbo · 历史模型",
    "speech-01-hd": "01 HD · 历史模型",
    "speech-01-turbo": "01 Turbo · 历史模型",
}

# These IDs are explicitly present in the current official T2A / get_voice docs.
# The live account catalogue replaces this seed after a successful connection.
STARTER_VOICES = (
    {
        "voice_id": "Chinese (Mandarin)_Reliable_Executive",
        "voice_name": "沉稳高管",
        "description": "沉稳可靠的中年男性声音，标准普通话。",
        "category": "system",
    },
    {
        "voice_id": "Chinese (Mandarin)_News_Anchor",
        "voice_name": "新闻女声",
        "description": "专业、清楚的中年女性新闻主播声音。",
        "category": "system",
    },
    {
        "voice_id": "Chinese (Mandarin)_Lyrical_Voice",
        "voice_name": "抒情声音",
        "description": "官方文档列出的普通话系统音色。",
        "category": "system",
    },
    {
        "voice_id": "Chinese (Mandarin)_HK_Flight_Attendant",
        "voice_name": "港风空乘",
        "description": "官方文档列出的普通话系统音色。",
        "category": "system",
    },
    {
        "voice_id": "moss_audio_ce44fc67-7ce3-11f0-8de5-96e35d26fb85",
        "voice_name": "中文声音 CE44",
        "description": "MiniMax 最新文档列出的中文系统音色。",
        "category": "system",
    },
    {
        "voice_id": "moss_audio_aaa1346a-7ce7-11f0-8e61-2e6e3c7ee85d",
        "voice_name": "中文声音 AAA1",
        "description": "MiniMax 最新文档列出的中文系统音色。",
        "category": "system",
    },
)


class MiniMaxError(RuntimeError):
    """User-safe MiniMax failure with optional trace metadata."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        trace_id: str | None = None,
        uncertain_completion: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.trace_id = trace_id
        self.uncertain_completion = uncertain_completion


@dataclass(frozen=True)
class SpeechConfig:
    model: str = "speech-2.8-hd"
    speed: float = 1.0
    volume: float = 1.0
    pitch: int = 0
    emotion: str | None = None
    sample_rate: int = 32000
    bitrate: int = 128000
    format: str = "mp3"
    channel: int = 1
    language_boost: str = "Chinese"
    text_normalization: bool = True
    modifier_pitch: int = 0
    modifier_intensity: int = 0
    modifier_timbre: int = 0
    sound_effect: str = ""

    @classmethod
    def from_mapping(cls, value: dict[str, Any] | None) -> SpeechConfig:
        raw = value or {}
        config = cls(
            model=str(raw.get("model", cls.model)),
            speed=float(raw.get("speed", cls.speed)),
            volume=float(raw.get("volume", cls.volume)),
            pitch=int(raw.get("pitch", cls.pitch)),
            emotion=(str(raw["emotion"]) if raw.get("emotion") else None),
            sample_rate=int(raw.get("sample_rate", cls.sample_rate)),
            bitrate=int(raw.get("bitrate", cls.bitrate)),
            format=str(raw.get("format", cls.format)),
            channel=int(raw.get("channel", cls.channel)),
            language_boost=str(raw.get("language_boost", cls.language_boost)),
            text_normalization=bool(raw.get("text_normalization", cls.text_normalization)),
            modifier_pitch=int(raw.get("modifier_pitch", cls.modifier_pitch)),
            modifier_intensity=int(raw.get("modifier_intensity", cls.modifier_intensity)),
            modifier_timbre=int(raw.get("modifier_timbre", cls.modifier_timbre)),
            sound_effect=str(raw.get("sound_effect", cls.sound_effect)),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.model not in MODELS:
            raise ValueError("不支持的 MiniMax 语音模型")
        if not 0.5 <= self.speed <= 2.0:
            raise ValueError("语速必须在 0.5–2.0 之间")
        if not 0 < self.volume <= 10:
            raise ValueError("音量必须大于 0 且不超过 10")
        if not -12 <= self.pitch <= 12:
            raise ValueError("基础语调必须在 -12–12 之间")
        if self.emotion and self.emotion not in EMOTIONS:
            raise ValueError("不支持的情绪参数")
        if self.model.startswith("speech-2.8") and self.emotion in {"fluent", "whisper"}:
            raise ValueError("2.8 模型不支持生动或低语模式，请选择自动情绪或 2.6 模型")
        if self.sample_rate not in SAMPLE_RATES:
            raise ValueError("不支持的采样率")
        if self.bitrate not in BITRATES:
            raise ValueError("不支持的 MP3 比特率")
        if self.format not in AUDIO_FORMATS:
            raise ValueError("工作台试听仅支持 MP3、WAV 或 FLAC")
        if self.channel not in (1, 2):
            raise ValueError("声道数只能是 1 或 2")
        if self.language_boost not in LANGUAGE_BOOSTS:
            raise ValueError("不支持的语言增强选项")
        for label, number in (
            ("明暗", self.modifier_pitch),
            ("力量", self.modifier_intensity),
            ("质感", self.modifier_timbre),
        ):
            if not -100 <= number <= 100:
                raise ValueError(f"{label}细调必须在 -100–100 之间")
        if self.sound_effect not in SOUND_EFFECTS:
            raise ValueError("不支持的声音效果")

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)

    def request_payload(
        self,
        text: str,
        voice_id: str,
        *,
        subtitle_type: str | None = None,
    ) -> dict[str, Any]:
        voice_setting: dict[str, Any] = {
            "voice_id": voice_id,
            "speed": self.speed,
            "vol": self.volume,
            "pitch": self.pitch,
            "text_normalization": self.text_normalization,
        }
        if self.emotion:
            voice_setting["emotion"] = self.emotion
        voice_modify: dict[str, Any] = {
            "pitch": self.modifier_pitch,
            "intensity": self.modifier_intensity,
            "timbre": self.modifier_timbre,
        }
        if self.sound_effect:
            voice_modify["sound_effects"] = self.sound_effect
        if subtitle_type not in {None, "sentence", "word", "word_streaming"}:
            raise ValueError("不支持的 MiniMax 字幕时间戳类型")
        return {
            "model": self.model,
            "text": text,
            "stream": False,
            "voice_setting": voice_setting,
            "audio_setting": {
                "sample_rate": self.sample_rate,
                "bitrate": self.bitrate,
                "format": self.format,
                "channel": self.channel,
            },
            "language_boost": self.language_boost,
            "voice_modify": voice_modify,
            "subtitle_enable": subtitle_type is not None,
            **({"subtitle_type": subtitle_type} if subtitle_type else {}),
            "output_format": "hex",
            "aigc_watermark": False,
        }


@dataclass(frozen=True)
class SpeechResult:
    audio: bytes
    audio_format: str
    trace_id: str | None
    extra_info: dict[str, Any]
    subtitle_file: str | None = None


_credential_lock = threading.Lock()
_runtime_api_key = ""


def set_runtime_api_key(api_key: str) -> None:
    value = api_key.strip()
    if len(value) < 8 or len(value) > 512 or any(char.isspace() for char in value):
        raise ValueError("API Key 格式不正确")
    global _runtime_api_key
    with _credential_lock:
        _runtime_api_key = value


def clear_runtime_api_key() -> None:
    global _runtime_api_key
    with _credential_lock:
        _runtime_api_key = ""


def credential_status() -> dict[str, str | bool]:
    with _credential_lock:
        if _runtime_api_key:
            return {"configured": True, "source": "memory"}
    if os.getenv("MINIMAX_API_KEY", "").strip():
        return {"configured": True, "source": "environment"}
    return {"configured": False, "source": "none"}


def resolve_api_key() -> str:
    with _credential_lock:
        if _runtime_api_key:
            return _runtime_api_key
    value = os.getenv("MINIMAX_API_KEY", "").strip()
    if not value:
        raise MiniMaxError("请先在工作台中填写 MiniMax API Key，再测试连接")
    return value


def billable_characters(text: str) -> int:
    def is_han(character: str) -> bool:
        code = ord(character)
        return (
            0x3400 <= code <= 0x4DBF
            or 0x4E00 <= code <= 0x9FFF
            or 0xF900 <= code <= 0xFAFF
            or 0x20000 <= code <= 0x323AF
        )

    return sum(2 if is_han(character) else 1 for character in text)


def model_price_per_10k(model: str) -> float:
    return 3.5 if model.endswith("-hd") else 2.0


def estimate_cost(text: str, model: str) -> dict[str, int | float]:
    characters = billable_characters(text)
    price = model_price_per_10k(model)
    return {
        "billable_characters": characters,
        "price_per_10k_cny": price,
        "estimated_cny": round(characters / 10_000 * price, 4),
    }


def normalize_voice_catalog(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    groups = (
        ("system_voice", "system"),
        ("voice_cloning", "voice_cloning"),
        ("voice_generation", "voice_generation"),
    )
    for response_key, category in groups:
        values = payload.get(response_key) or []
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict) or not item.get("voice_id"):
                continue
            description = item.get("description") or []
            if isinstance(description, list):
                description_text = " ".join(str(value) for value in description if value)
            else:
                description_text = str(description)
            voice_id = str(item["voice_id"])
            rows.append(
                {
                    "voice_id": voice_id,
                    "voice_name": str(item.get("voice_name") or voice_id),
                    "description": description_text or "此音色没有附加说明。",
                    "created_time": item.get("created_time"),
                    "category": category,
                }
            )
    return rows


class MiniMaxClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = API_BASE_URL,
        session: requests.Session | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._session = session or requests.Session()

    def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout: tuple[float, float],
        billable: bool,
    ) -> dict[str, Any]:
        try:
            response = self._session.post(
                f"{self._base_url}{path}",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                timeout=timeout,
            )
        except requests.Timeout as exc:
            message = (
                "MiniMax 请求超时，服务端是否已完成和计费目前未知；请先到控制台核对后再重试。"
                if billable
                else "连接 MiniMax 超时，请稍后重试。"
            )
            raise MiniMaxError(message, uncertain_completion=billable) from exc
        except requests.RequestException as exc:
            raise MiniMaxError("无法连接 MiniMax 服务，请检查网络后重试。") from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise MiniMaxError(f"MiniMax 返回了无法解析的响应（HTTP {response.status_code}）") from exc
        if not isinstance(body, dict):
            raise MiniMaxError("MiniMax 返回的数据结构不正确")
        trace_id = str(body.get("trace_id") or response.headers.get("Trace-Id") or "") or None
        base_resp = body.get("base_resp") or {}
        service_code = int(base_resp.get("status_code") or 0)
        if response.status_code >= 400 or service_code != 0:
            service_message = str(base_resp.get("status_msg") or "").strip()
            friendly = {
                1001: "MiniMax 合成超时，完成和计费状态未知，请先到控制台核对。",
                1002: "MiniMax 请求过于频繁，已触发限流；本任务不会自动重试。",
                1004: "MiniMax API Key 鉴权失败，请检查密钥。",
                1039: "MiniMax 输入吞吐量达到限制，请稍后再试。",
                1042: "文本中的非法字符比例超过 10%，请先清理台词。",
                2013: "MiniMax 拒绝了当前参数，请检查模型、音色和格式组合。",
            }.get(service_code)
            message = friendly or service_message or f"MiniMax 请求失败（HTTP {response.status_code}）"
            raise MiniMaxError(
                message,
                status_code=service_code or response.status_code,
                trace_id=trace_id,
                uncertain_completion=billable and service_code in {1001},
            )
        return body

    def list_voices(self) -> list[dict[str, Any]]:
        body = self._post(
            "/v1/get_voice",
            {"voice_type": "all"},
            timeout=(8, 30),
            billable=False,
        )
        return normalize_voice_catalog(body)

    def synthesize(
        self,
        text: str,
        voice_id: str,
        config: SpeechConfig,
        *,
        subtitle_type: str | None = None,
    ) -> SpeechResult:
        normalized_text = text.strip()
        if not normalized_text:
            raise ValueError("试听文本不能为空")
        if len(normalized_text) >= 10_000:
            raise ValueError("同步合成文本必须少于 10000 个字符")
        if not voice_id.strip() or len(voice_id) > 256:
            raise ValueError("音色 ID 不正确")
        config.validate()
        body = self._post(
            "/v1/t2a_v2",
            config.request_payload(
                normalized_text,
                voice_id.strip(),
                subtitle_type=subtitle_type,
            ),
            timeout=(10, 120),
            billable=True,
        )
        data = body.get("data") or {}
        audio_hex = data.get("audio")
        if not isinstance(audio_hex, str) or not audio_hex:
            raise MiniMaxError("MiniMax 没有返回可用音频", trace_id=body.get("trace_id"))
        try:
            audio = bytes.fromhex(audio_hex)
        except ValueError as exc:
            raise MiniMaxError("MiniMax 返回的音频编码不正确", trace_id=body.get("trace_id")) from exc
        if not audio:
            raise MiniMaxError("MiniMax 返回了空音频", trace_id=body.get("trace_id"))
        extra_info = body.get("extra_info") or {}
        audio_format = str(extra_info.get("audio_format") or config.format).lower()
        return SpeechResult(
            audio=audio,
            audio_format=audio_format,
            trace_id=str(body.get("trace_id") or "") or None,
            extra_info=extra_info if isinstance(extra_info, dict) else {},
            subtitle_file=(
                str(data.get("subtitle_file"))
                if data.get("subtitle_file")
                else None
            ),
        )


def public_catalog() -> dict[str, Any]:
    return {
        **credential_status(),
        "api_base_url": API_BASE_URL,
        "models": [{"id": model, "label": MODEL_LABELS[model]} for model in MODELS],
        "sample_rates": list(SAMPLE_RATES),
        "bitrates": list(BITRATES),
        "formats": list(AUDIO_FORMATS),
        "emotions": list(EMOTIONS),
        "language_boosts": list(LANGUAGE_BOOSTS),
        "sound_effects": list(SOUND_EFFECTS),
        "defaults": SpeechConfig().public_dict(),
        "starter_voices": list(STARTER_VOICES),
        "pricing": {"hd_cny_per_10k": 3.5, "turbo_cny_per_10k": 2.0},
        "rate_limits": {"free_rpm": 10, "paid_rpm": 20},
    }
