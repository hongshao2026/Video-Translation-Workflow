"""Shared loader for the isolated local CosyVoice runtime."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import numpy as np

from backend.local_runtime import COSYVOICE_REPO_DIR

COSYVOICE_SOURCE_DIR = COSYVOICE_REPO_DIR
MATCHA_SOURCE_DIR = COSYVOICE_SOURCE_DIR / "third_party" / "Matcha-TTS"

RUNTIME_FRONTEND_YAML = """
# Only inference-time frontend objects are required below. The official training
# pipeline entries import PyArrow and PyWorld even though SFT inference never
# calls them, so the local workbench intentionally leaves those entries out.
get_tokenizer: !name:whisper.tokenizer.get_tokenizer
    multilingual: True
    num_languages: 100
    language: 'en'
    task: 'transcribe'
allowed_special: 'all'
feat_extractor: !name:matcha.utils.audio.mel_spectrogram
    n_fft: 1024
    num_mels: 80
    sampling_rate: !ref <sample_rate>
    hop_size: 256
    win_size: 1024
    fmin: 0
    fmax: 8000
    center: False
"""


def add_source_paths() -> None:
    for path in (COSYVOICE_SOURCE_DIR, MATCHA_SOURCE_DIR):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)


def load_cosyvoice(model_dir: Path):
    add_source_paths()
    from cosyvoice.cli.cosyvoice import CosyVoice
    from cosyvoice.cli.frontend import CosyVoiceFrontEnd
    from cosyvoice.cli.model import CosyVoiceModel
    from cosyvoice.utils.class_utils import get_model_type
    from hyperpyyaml import load_hyperpyyaml

    config_path = model_dir / "cosyvoice.yaml"
    raw_config = config_path.read_text(encoding="utf-8")
    model_config = raw_config.split("# processor functions", 1)[0]
    configs = load_hyperpyyaml(io.StringIO(model_config + RUNTIME_FRONTEND_YAML))
    if get_model_type(configs) is not CosyVoiceModel:
        raise TypeError(f"Unsupported CosyVoice model type: {model_dir}")

    instance = CosyVoice.__new__(CosyVoice)
    instance.model_dir = str(model_dir)
    instance.fp16 = False
    instance.frontend = CosyVoiceFrontEnd(
        configs["get_tokenizer"],
        configs["feat_extractor"],
        str(model_dir / "campplus.onnx"),
        str(model_dir / "speech_tokenizer_v1.onnx"),
        str(model_dir / "spk2info.pt"),
        configs["allowed_special"],
    )
    instance.sample_rate = configs["sample_rate"]
    instance.model = CosyVoiceModel(
        configs["llm"], configs["flow"], configs["hift"], fp16=False
    )
    instance.model.load(
        str(model_dir / "llm.pt"),
        str(model_dir / "flow.pt"),
        str(model_dir / "hift.pt"),
    )
    return instance


def synthesize_cosyvoice(model, text: str, speaker: str) -> np.ndarray:
    chunks = []
    for result in model.inference_sft(text, speaker, stream=False, speed=1.0):
        tensor = result["tts_speech"].detach().cpu().float().numpy()
        chunks.append(np.asarray(tensor, dtype=np.float32).reshape(-1))
    if not chunks:
        raise RuntimeError(f"CosyVoice returned no audio for {speaker}")
    return np.concatenate(chunks)
