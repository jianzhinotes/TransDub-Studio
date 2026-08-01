"""Strong-ASR audit of existing dubbing clips for legacy/editable projects."""

from __future__ import annotations

import json
import re
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from videotrans.configure.config import settings


class QualityAuditWorker(QThread):
    progress = Signal(int, int)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, queue_snapshot, manifest_root, parent=None):
        super().__init__(parent=parent)
        self._queue = list(queue_snapshot)
        self._manifest_root = str(manifest_root)

    @staticmethod
    def _validator_spec(backend=None):
        from videotrans.tts._f5tts import F5TTS

        probe = F5TTS.__new__(F5TTS)
        backend = backend or probe._validator_identity()[0]
        model_path = (
            probe._mlx_validator_model_path()
            if backend == "mlx-whisper-mps" else probe._get_validator_model_path()
        )
        return backend, F5TTS.VALIDATOR_MODEL, str(model_path)

    def _run_validator(self, files, backend, logs_file):
        from videotrans.process.quality_validator import (
            validate_faster_whisper_files, validate_mlx_whisper_files,
        )
        from videotrans.process.signelobj import GlobalProcessManager
        from videotrans.util.resource_governor import runtime_limits

        backend, _model, model_path = self._validator_spec(backend)
        callback = (
            validate_mlx_whisper_files
            if backend == "mlx-whisper-mps" else validate_faster_whisper_files
        )
        limits = runtime_limits(mode=settings.get("resource_mode", "auto"))
        future = GlobalProcessManager.submit_task_cpu(
            callback,
            files=files,
            model_path=model_path,
            cpu_threads=limits.validator_cpu_threads,
            logs_file=str(logs_file),
        )
        last_progress = None
        while not future.done():
            try:
                payload = json.loads(Path(logs_file).read_text(encoding="utf-8"))
                match = re.search(r"(\d+)/(\d+)", str(payload.get("text") or ""))
                if match:
                    current = (int(match.group(1)), int(match.group(2)))
                    if current != last_progress:
                        last_progress = current
                        self.progress.emit(*current)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                pass
            self.msleep(300)
        data, error = future.result(timeout=30)
        if error or data is False:
            raise RuntimeError(str(error or "强模型没有返回核验结果"))
        return dict(data or {}), backend

    def run(self):
        try:
            from videotrans.dub.chinese_quality import (
                hard_quality_failures, quality_metrics,
            )
            from videotrans.dub.quality_manifest import QualityManifest
            from videotrans.tts._f5tts import F5TTS

            manifest = QualityManifest(self._manifest_root)
            backend, model, _path = self._validator_spec()
            rules_version = F5TTS.QUALITY_RULES_VERSION
            validator_args = {
                "validator_backend": backend,
                "validator_model": model,
                "rules_version": rules_version,
            }
            results = {}
            pending = []
            for idx, item in enumerate(self._queue):
                filename = str(item.get("filename") or "")
                if not str(item.get("text") or "").strip() or not Path(filename).is_file():
                    continue
                cached = manifest.lookup(item, index=idx, **validator_args)
                if cached:
                    results[idx] = cached
                else:
                    pending.append((idx, filename))

            if pending:
                logs_file = Path(self._manifest_root) / ".quality_audit_progress.json"
                logs_file.parent.mkdir(parents=True, exist_ok=True)
                logs_file.unlink(missing_ok=True)
                try:
                    transcripts, used_backend = self._run_validator(
                        pending, backend, logs_file)
                except Exception:
                    if backend != "mlx-whisper-mps":
                        raise
                    backend, model, _path = self._validator_spec("faster-whisper-cpu")
                    validator_args.update({
                        "validator_backend": backend,
                        "validator_model": model,
                    })
                    transcripts, used_backend = self._run_validator(
                        pending, backend, logs_file)
                validator_args["validator_backend"] = used_backend
                for pos, (idx, _filename) in enumerate(pending, 1):
                    item = self._queue[idx]
                    transcript = str(transcripts.get(idx) or "")
                    failures = hard_quality_failures(
                        str(item.get("text") or ""),
                        transcript,
                        zero_unexpected_latin=str(settings.get(
                            "f5tts_zero_unexpected_latin", True)).lower() != "false",
                    )
                    entry = manifest.record(
                        item,
                        index=idx,
                        passed=not failures,
                        transcript=transcript,
                        hard_failures=failures,
                        metrics=quality_metrics(str(item.get("text") or ""), transcript),
                        save=False,
                        **validator_args,
                    )
                    results[idx] = entry
                    self.progress.emit(pos, len(pending))
                logs_file.unlink(missing_ok=True)
            # ``lookup`` may materialize global content-cache hits into this
            # project. Persist them even when no validator process was needed.
            if results:
                manifest.save()
            self.done.emit(results)
        except BaseException as error:
            self.failed.emit(str(error))
