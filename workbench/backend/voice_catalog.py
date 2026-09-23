"""Local voice catalog shared by the API and synthesis workers."""

from __future__ import annotations

from backend.local_runtime import KOKORO_MODEL_DIR

QWEN_VOICES = [
    {
        "id": "dylan",
        "speaker": "Dylan",
        "name": "Dylan",
        "label": "北京青年男声",
        "gender": "男声",
        "language": "中文",
        "tone": "自然、清爽、有好奇心",
        "recommended": True,
    },
    {
        "id": "eric",
        "speaker": "Eric",
        "name": "Eric",
        "label": "成都青年男声",
        "gender": "男声",
        "language": "中文",
        "tone": "亲切、松弛、适合访谈",
        "recommended": True,
    },
    {
        "id": "serena",
        "speaker": "Serena",
        "name": "Serena",
        "label": "温柔青年女声",
        "gender": "女声",
        "language": "中文",
        "tone": "温暖、耐听、表达细腻",
        "recommended": True,
    },
    {
        "id": "vivian",
        "speaker": "Vivian",
        "name": "Vivian",
        "label": "明亮青年女声",
        "gender": "女声",
        "language": "中文",
        "tone": "清晰、灵动、节奏明快",
        "recommended": True,
    },
    {
        "id": "uncle_fu",
        "speaker": "Uncle_Fu",
        "name": "Uncle Fu",
        "label": "成熟长者男声",
        "gender": "男声",
        "language": "中文",
        "tone": "低沉、从容、有阅历感",
        "recommended": True,
    },
    {
        "id": "ryan",
        "speaker": "Ryan",
        "name": "Ryan",
        "label": "沉稳国际男声",
        "gender": "男声",
        "language": "多语",
        "tone": "稳重、清晰、偏正式",
        "recommended": False,
    },
    {
        "id": "aiden",
        "speaker": "Aiden",
        "name": "Aiden",
        "label": "阳光国际男声",
        "gender": "男声",
        "language": "多语",
        "tone": "年轻、积极、富有活力",
        "recommended": False,
    },
    {
        "id": "ono_anna",
        "speaker": "Ono_Anna",
        "name": "Ono Anna",
        "label": "轻快日系女声",
        "gender": "女声",
        "language": "多语",
        "tone": "活泼、轻盈、角色感强",
        "recommended": False,
    },
    {
        "id": "sohee",
        "speaker": "Sohee",
        "name": "Sohee",
        "label": "温暖韩系女声",
        "gender": "女声",
        "language": "多语",
        "tone": "柔和、亲近、情绪自然",
        "recommended": False,
    },
]

for voice in QWEN_VOICES:
    voice.update(
        engine="qwen",
        model="Qwen3-TTS 0.6B",
        preview_filename=f"{voice['id']}.wav",
    )


def discover_kokoro_voices() -> list[dict]:
    """Build the catalog from the official local voice packs without inventing IDs."""

    voices_dir = KOKORO_MODEL_DIR / "voices"
    rows = []
    for path in sorted(voices_dir.glob("z[fm]_*.pt")):
        speaker = path.stem
        gender = "女声" if speaker.startswith("zf_") else "男声"
        number = speaker.split("_", 1)[1]
        voice_id = f"kokoro_{speaker}"
        rows.append(
            {
                "id": voice_id,
                "speaker": speaker,
                "name": f"Kokoro {speaker.upper()}",
                "label": f"中文{gender} #{number}",
                "gender": gender,
                "language": "中文",
                "tone": "Kokoro v1.1 官方中文预置音色",
                "recommended": speaker in {"zf_001", "zf_002", "zm_009", "zm_010"},
                "engine": "kokoro",
                "model": "Kokoro 82M v1.1 中文版",
                "voice_path": str(path),
                "preview_filename": f"{voice_id}.wav",
            }
        )
    return rows


KOKORO_VOICES = discover_kokoro_voices()

COSYVOICE_VOICES = [
    {
        "id": "cosyvoice_zh_female",
        "speaker": "中文女",
        "name": "CosyVoice 中文女",
        "label": "官方普通话女声",
        "gender": "女声",
        "language": "中文",
        "tone": "清晰、自然、适合叙述与访谈",
        "recommended": True,
    },
    {
        "id": "cosyvoice_zh_male",
        "speaker": "中文男",
        "name": "CosyVoice 中文男",
        "label": "官方普通话男声",
        "gender": "男声",
        "language": "中文",
        "tone": "沉稳、自然、适合中长篇对话",
        "recommended": True,
    },
    {
        "id": "cosyvoice_ja_male",
        "speaker": "日语男",
        "name": "CosyVoice 日语男",
        "label": "官方日语男声",
        "gender": "男声",
        "language": "日语",
        "tone": "清晰、克制、日语韵律",
        "recommended": False,
    },
    {
        "id": "cosyvoice_yue_female",
        "speaker": "粤语女",
        "name": "CosyVoice 粤语女",
        "label": "官方粤语女声",
        "gender": "女声",
        "language": "粤语",
        "tone": "灵动、明亮、粤语表达",
        "recommended": True,
    },
    {
        "id": "cosyvoice_en_female",
        "speaker": "英文女",
        "name": "CosyVoice 英文女",
        "label": "官方英语女声",
        "gender": "女声",
        "language": "英语",
        "tone": "自然、清晰、英语表达",
        "recommended": False,
    },
    {
        "id": "cosyvoice_en_male",
        "speaker": "英文男",
        "name": "CosyVoice 英文男",
        "label": "官方英语男声",
        "gender": "男声",
        "language": "英语",
        "tone": "稳重、清晰、英语表达",
        "recommended": False,
    },
    {
        "id": "cosyvoice_ko_female",
        "speaker": "韩语女",
        "name": "CosyVoice 韩语女",
        "label": "官方韩语女声",
        "gender": "女声",
        "language": "韩语",
        "tone": "柔和、清晰、韩语韵律",
        "recommended": False,
    },
]

for voice in COSYVOICE_VOICES:
    voice.update(
        engine="cosyvoice",
        model="CosyVoice 300M SFT",
        preview_filename=f"{voice['id']}.wav",
    )

VOICES = QWEN_VOICES + KOKORO_VOICES + COSYVOICE_VOICES
VOICE_MAP = {voice["id"]: voice for voice in VOICES}
VOICE_IDS = set(VOICE_MAP)
