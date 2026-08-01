"""Local post-synthesis speaker-identity verification."""

from __future__ import annotations

import hashlib
from pathlib import Path


GATE_VERSION = "voice-identity-v1"


def _speaker_id(item: dict) -> str:
    return str(
        item.get("cluster_ref_speaker_id")
        or item.get("speaker_id")
        or item.get("spk")
        or ""
    ).strip()


def _default_model_path() -> Path | None:
    from videotrans.configure.config import ROOT_DIR

    candidates = (
        Path(ROOT_DIR) / "models/onnx/nemo_en_titanet_small.onnx",
        Path(ROOT_DIR)
        / "models/onnx/3dspeaker_speech_eres2net_large_sv_zh-cn_3dspeaker_16k.onnx",
    )
    return next((path for path in candidates if path.is_file()), None)


class _EmbeddingExtractor:
    def __init__(self, model_path: Path):
        import sherpa_onnx

        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(model_path), num_threads=2, debug=False, provider="cpu"
        )
        self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        self.memory_cache = {}

    @staticmethod
    def _signature(filename: str) -> str:
        path = Path(filename)
        stat = path.stat()
        with path.open("rb") as stream:
            head = stream.read(1 << 20)
        return f"{hashlib.sha256(head).hexdigest()}-{stat.st_size}"

    def __call__(self, filename: str):
        import numpy as np
        import soundfile as sf
        from scipy.signal import resample_poly
        import math

        signature = self._signature(filename)
        cached = self.memory_cache.get(signature)
        if cached is not None:
            return cached
        audio, sample_rate = sf.read(
            filename, dtype="float32", always_2d=True
        )
        mono = audio.mean(axis=1, dtype=np.float32)
        if sample_rate != 16000:
            divisor = math.gcd(int(sample_rate), 16000)
            mono = resample_poly(
                mono, 16000 // divisor, int(sample_rate) // divisor
            ).astype(np.float32, copy=False)
        stream = self.extractor.create_stream()
        stream.accept_waveform(16000, np.ascontiguousarray(mono))
        stream.input_finished()
        if not self.extractor.is_ready(stream):
            raise RuntimeError(f"speaker embedding input is too short: {filename}")
        vector = np.asarray(self.extractor.compute(stream), dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        if norm <= 0:
            raise RuntimeError(f"empty speaker embedding: {filename}")
        vector = vector / norm
        self.memory_cache[signature] = vector
        return vector


def verify_voice_identity(
        queue: list[dict], *, min_similarity: float = 0.30,
        cross_speaker_margin: float = 0.08, embedding_fn=None,
        model_path: str | Path | None = None) -> dict:
    """Compare every contracted output with its own and competing anchors."""
    required = [
        item for item in queue
        if item.get("speaker_identity_required")
        and str(item.get("filename") or "")
    ]
    if not required:
        return {
            "version": GATE_VERSION, "status": "not_required",
            "items": [], "failures": {},
        }
    model = Path(model_path) if model_path else _default_model_path()
    if embedding_fn is None:
        if model is None or not model.is_file():
            return {
                "version": GATE_VERSION, "status": "unavailable",
                "reason": "speaker embedding model is missing",
                "items": [], "failures": {},
            }
        embedding_fn = _EmbeddingExtractor(model)

    anchors = {}
    for item in required:
        speaker_id = _speaker_id(item)
        anchor = str(item.get("cluster_ref") or "")
        if speaker_id and anchor and Path(anchor).is_file():
            anchors.setdefault(speaker_id, anchor)
    anchor_vectors = {
        speaker_id: embedding_fn(filename)
        for speaker_id, filename in anchors.items()
    }
    items = []
    failures = {}
    for item in required:
        filename = str(item.get("filename") or "")
        speaker_id = _speaker_id(item)
        result = {
            "filename": filename,
            "speaker_id": speaker_id,
            "passed": True,
            "hard_failures": [],
        }
        if speaker_id not in anchor_vectors or not Path(filename).is_file():
            result["passed"] = False
            result["hard_failures"].append("speaker_identity_reference_missing")
        else:
            try:
                generated = embedding_fn(filename)
                own_similarity = float(generated @ anchor_vectors[speaker_id])
                competing = {
                    other: float(generated @ vector)
                    for other, vector in anchor_vectors.items()
                    if other != speaker_id
                }
                strongest_other = max(competing.values()) if competing else None
                result.update({
                    "similarity": round(own_similarity, 4),
                    "strongest_other_similarity": (
                        round(strongest_other, 4)
                        if strongest_other is not None else None
                    ),
                })
                if own_similarity < float(min_similarity):
                    result["hard_failures"].append(
                        "speaker_identity_similarity_low"
                    )
                if (
                    strongest_other is not None
                    and own_similarity < strongest_other + float(cross_speaker_margin)
                ):
                    result["hard_failures"].append("cross_speaker_voice_match")
                result["passed"] = not result["hard_failures"]
            except Exception as error:
                result["passed"] = False
                result["hard_failures"].append("speaker_embedding_error")
                result["error"] = str(error)
        items.append(result)
        if not result["passed"]:
            failures[Path(filename).name] = result
    return {
        "version": GATE_VERSION,
        "status": "passed" if not failures else "needs_review",
        "model": str(model or "injected"),
        "min_similarity": float(min_similarity),
        "cross_speaker_margin": float(cross_speaker_margin),
        "items": items,
        "failures": failures,
    }
