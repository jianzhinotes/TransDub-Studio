"""用统一协议包装现有 videotrans.tts.run。"""

import copy
import json
from pathlib import Path

from .base import AudioArtifact, BackendCapabilities, DubbingBackend


class LegacyTTSBackend(DubbingBackend):
    def __init__(self, *, tts_type: int, language: str, uuid=None,
                 is_cuda=False, use_cache=True, source_audio=None,
                 reference_dir=None):
        self.tts_type = int(tts_type)
        self.language = language
        self.uuid = uuid
        self.is_cuda = is_cuda
        self.use_cache = use_cache
        self.source_audio = str(source_audio or '')
        self.reference_dir = Path(reference_dir) if reference_dir else None
        self.name = f"legacy-tts-{self.tts_type}"

    def _repair_clone_reference(self, item, index):
        """Rebuild expired temporary clone references from the saved source audio."""
        if item.get("role") != "clone":
            return
        ref_wav = Path(str(item.get("ref_wav") or ''))
        if ref_wav.is_file() or not self.source_audio:
            return
        source = Path(self.source_audio)
        if not source.is_file():
            return
        start_ms = item.get("start_time_source", item.get("start_time", 0))
        end_ms = item.get("end_time_source", item.get("end_time", 0))
        try:
            start_ms = max(0, int(start_ms))
            end_ms = int(end_ms)
        except (TypeError, ValueError):
            return
        if end_ms <= start_ms:
            return

        target_dir = self.reference_dir or Path(item["filename"]).parent / "_references"
        target_dir.mkdir(parents=True, exist_ok=True)
        line = item.get("line", index + 1)
        target = target_dir / f"clone-{line}-{start_ms}-{end_ms}.wav"
        if not target.is_file() or target.stat().st_size == 0:
            from videotrans.util.help_ffmpeg import runffmpeg
            runffmpeg([
                "-ss", f"{start_ms / 1000:.3f}",
                "-t", f"{(end_ms - start_ms) / 1000:.3f}",
                "-i", str(source), "-vn", "-ac", "1", "-ar", "24000",
                "-c:a", "pcm_s16le", str(target),
            ])
        item["ref_wav"] = str(target)

    def capabilities(self):
        from videotrans.tts import SUPPORT_CLONE
        return BackendCapabilities(
            supports_voice_clone=self.tts_type in SUPPORT_CLONE,
            supports_speed_control=True,
            supports_batch=True,
            recommended_concurrency=1,
        )

    def should_isolate_failure(self, requests, error):
        message = str(error).lower()
        global_markers = (
            "large-v3-turbo", "no this tts channel", "configure the sk",
            "connection refused", "failed to establish", "model is missing",
        )
        if any(marker in message for marker in global_markers):
            return False
        # F5 严格门禁在生成完候选后抛错；存在输出说明值得二分定位坏段。
        return any(Path(request.output_path).is_file() for request in requests)

    def synthesize_batch(self, requests):
        if not requests:
            return []
        queue = []
        for index, request in enumerate(requests):
            item = copy.deepcopy(request.legacy_payload)
            item["text"] = request.text
            item["filename"] = request.output_path
            item["line"] = item.get("line", index + 1)
            item["tts_type"] = self.tts_type
            Path(request.output_path).parent.mkdir(parents=True, exist_ok=True)
            self._repair_clone_reference(item, index)
            queue.append(item)

        sidecar = Path(requests[0].output_path).parent / "lang_leak.json"
        sidecar.unlink(missing_ok=True)

        from videotrans import tts
        tts.run(
            queue_tts=queue,
            language=self.language,
            uuid=self.uuid,
            tts_type=self.tts_type,
            is_cuda=self.is_cuda,
            use_cache=self.use_cache,
        )

        leak_marks = {}
        if sidecar.is_file():
            try:
                leak_marks = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                leak_marks = {}

        from pydub import AudioSegment
        artifacts = []
        for request in requests:
            path = Path(request.output_path)
            if not path.is_file() or path.stat().st_size == 0:
                raise RuntimeError(f"TTS did not create candidate audio: {path}")
            artifacts.append(AudioArtifact(
                request_id=request.id,
                path=str(path),
                duration_ms=len(AudioSegment.from_file(path)),
                backend=self.name,
                metadata={"language_leak": leak_marks.get(path.name)},
            ))
        return artifacts
