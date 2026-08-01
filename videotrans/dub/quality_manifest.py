"""Persistent, content-addressed quality results for generated dubbing clips.

The TTS cache only proves that an audio file was generated.  This module records
the stronger statement that a particular audio payload was checked against a
particular target text by a particular validator/ruleset.  Results are stored
both beside the active project and in a small global content-addressed cache so
an interrupted task can resume even before ``save_project`` has run.
"""

from __future__ import annotations

import hashlib
import json
import time
import unicodedata
from pathlib import Path
from typing import Dict, Iterable, Optional

from videotrans.configure import config
from videotrans.dub.store import atomic_write_json


MANIFEST_FILE = "quality_manifest.json"
MANIFEST_SCHEMA_VERSION = 1
DEFAULT_RULES_VERSION = "zh-content-v1"
GLOBAL_QUALITY_DIR = Path(config.TEMP_ROOT) / "dubb_quality"
GLOBAL_REFERENCE_QUALITY_DIR = Path(config.TEMP_ROOT) / "dubb_reference_quality"
_GLOBAL_MAX_AGE_S = 90 * 86400
_global_pruned = False


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def text_hash(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "")
    normalized = " ".join(normalized.split())
    return _sha256_bytes(normalized.encode("utf-8"))


def expected_spoken_text(item: dict) -> str:
    """Return the exact text that was supplied to the speech model.

    Viewer subtitles may preserve compact Arabic notation while the TTS input
    spells it out in Chinese.  A quality pass is only reusable when it was
    checked against that actual spoken payload.
    """
    return str(item.get("spoken_text") or item.get("text") or "")


def file_hash(path) -> str:
    """Return a full content hash; dubbing clips are small enough for this.

    A partial hash is suitable for a performance cache but not for a quality
    decision: a false hit here could allow an unchecked clip into the output.
    """
    source = Path(path or "")
    if not source.is_file():
        return ""
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def unit_key(item: dict, index: Optional[int] = None) -> str:
    stable = str(item.get("dub_unit_id") or "").strip()
    if stable:
        return stable
    filename = Path(item.get("filename") or "").name
    if filename:
        return filename
    return f"line-{item.get('line', index if index is not None else 'unknown')}"


def is_unresolved_quality(item: dict) -> bool:
    return bool(
        item.get("lang_leak")
        or str(item.get("quality_status") or "").startswith("needs_")
    )


def unresolved_queue_indices(queue: Iterable[dict]) -> list:
    return [index for index, item in enumerate(queue) if is_unresolved_quality(item)]


def queue_quality_coverage(
        queue: Iterable[dict], root, *, rules_version: str,
        validator_model: str = "large-v3-turbo", verify_audio_hashes: bool = True) -> dict:
    """Match a persisted manifest against the queue's exact current content."""
    path = Path(root) / MANIFEST_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries = payload.get("entries") if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        entries = {}
    entries = entries if isinstance(entries, dict) else {}
    total = 0
    matched = {}
    for index, item in enumerate(queue):
        filename = str(item.get("filename") or "")
        if not expected_spoken_text(item).strip() or not Path(filename).is_file():
            continue
        total += 1
        entry = entries.get(unit_key(item, index))
        if not isinstance(entry, dict):
            continue
        if (
                entry.get("rules_version") != rules_version
                or entry.get("validator_model") != validator_model
                or (verify_audio_hashes and entry.get("audio_hash") != file_hash(filename))
                or entry.get("expected_text_hash") != text_hash(expected_spoken_text(item))
        ):
            continue
        matched[index] = entry
    return {
        "total": total,
        "covered": len(matched),
        "missing": max(total - len(matched), 0),
        "failed": sum(1 for entry in matched.values() if not entry.get("passed")),
        "entries": matched,
    }


def validation_signature(
    item: dict,
    *,
    validator_backend: str,
    validator_model: str,
    rules_version: str = DEFAULT_RULES_VERSION,
) -> Dict[str, str]:
    audio_digest = file_hash(item.get("filename"))
    expected_digest = text_hash(expected_spoken_text(item))
    payload = {
        "audio_hash": audio_digest,
        "expected_text_hash": expected_digest,
        "validator_backend": validator_backend,
        "validator_model": validator_model,
        "rules_version": rules_version,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload["signature"] = _sha256_bytes(encoded)
    return payload


class QualityManifest:
    def __init__(self, root) -> None:
        global _global_pruned
        self.root = Path(root)
        self.path = self.root / MANIFEST_FILE
        self.entries: Dict[str, dict] = {}
        if not _global_pruned:
            _global_pruned = True
            cutoff = time.time() - _GLOBAL_MAX_AGE_S
            try:
                for cached in GLOBAL_QUALITY_DIR.glob("*/*.json"):
                    if cached.stat().st_mtime < cutoff:
                        cached.unlink(missing_ok=True)
            except OSError:
                pass
        self._load()

    @classmethod
    def for_queue(cls, queue: Iterable[dict]) -> "QualityManifest":
        first = next((item for item in queue if item.get("filename")), None)
        root = Path(first["filename"]).parent if first else Path(config.TEMP_ROOT)
        return cls(root)

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            entries = payload.get("entries") if isinstance(payload, dict) else None
            if isinstance(entries, dict):
                self.entries = entries
        except (OSError, json.JSONDecodeError, TypeError):
            self.entries = {}

    def save(self) -> None:
        atomic_write_json(self.path, {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "updated_at": int(time.time()),
            "entries": self.entries,
        })

    @staticmethod
    def _global_path(signature: str) -> Path:
        return GLOBAL_QUALITY_DIR / signature[:2] / f"{signature}.json"

    def lookup(
        self,
        item: dict,
        *,
        validator_backend: str,
        validator_model: str,
        rules_version: str = DEFAULT_RULES_VERSION,
        index: Optional[int] = None,
    ) -> Optional[dict]:
        sig = validation_signature(
            item,
            validator_backend=validator_backend,
            validator_model=validator_model,
            rules_version=rules_version,
        )
        if not sig["audio_hash"]:
            return None
        key = unit_key(item, index)
        local = self.entries.get(key)
        if isinstance(local, dict) and local.get("signature") == sig["signature"]:
            return local
        try:
            cached = json.loads(self._global_path(sig["signature"]).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        if not isinstance(cached, dict) or cached.get("signature") != sig["signature"]:
            return None
        # Materialize a global hit into the project manifest on the next save.
        self.entries[key] = {**cached, "unit_id": key}
        return self.entries[key]

    def record(
        self,
        item: dict,
        *,
        validator_backend: str,
        validator_model: str,
        passed: bool,
        transcript: str,
        hard_failures=None,
        warnings=None,
        metrics=None,
        disposition=None,
        attempts: int = 0,
        rules_version: str = DEFAULT_RULES_VERSION,
        index: Optional[int] = None,
        save: bool = True,
    ) -> dict:
        sig = validation_signature(
            item,
            validator_backend=validator_backend,
            validator_model=validator_model,
            rules_version=rules_version,
        )
        key = unit_key(item, index)
        entry = {
            "id": f"quality:{sig['signature']}",
            "unit_id": key,
            **sig,
            "passed": bool(passed),
            "disposition": str(disposition or ("passed" if passed else "retryable")),
            "attempts": max(int(attempts or 0), 0),
            "transcript": transcript or "",
            "hard_failures": list(hard_failures or []),
            "warnings": list(warnings or []),
            "metrics": dict(metrics or {}),
            "created_at": int(time.time()),
        }
        self.entries[key] = entry
        global_path = self._global_path(sig["signature"])
        if not global_path.is_file():
            try:
                atomic_write_json(global_path, entry)
            except FileNotFoundError:
                # Two projects may validate identical content concurrently.
                # The winner wrote the same signature; reuse it safely.
                if not global_path.is_file():
                    raise
        if save:
            self.save()
        return entry

    def set_disposition(
        self,
        item: dict,
        disposition: str,
        *,
        index: Optional[int] = None,
        attempts: Optional[int] = None,
        reason: str = "",
        save: bool = True,
    ) -> Optional[dict]:
        """Update workflow state without changing the validated content signature."""
        key = unit_key(item, index)
        entry = self.entries.get(key)
        if not isinstance(entry, dict):
            return None
        entry["disposition"] = str(disposition)
        if attempts is not None:
            entry["attempts"] = max(int(attempts), 0)
        if reason:
            entry["resolution_reason"] = str(reason)[:500]
        entry["updated_at"] = int(time.time())
        if save:
            self.save()
        return entry

    def summary(self) -> dict:
        result = {
            "total": len(self.entries),
            "passed": 0,
            "failed": 0,
            "dispositions": {},
        }
        for entry in self.entries.values():
            if not isinstance(entry, dict):
                continue
            if entry.get("passed"):
                result["passed"] += 1
            else:
                result["failed"] += 1
            disposition = str(entry.get("disposition") or "")
            if disposition:
                result["dispositions"][disposition] = (
                    result["dispositions"].get(disposition, 0) + 1)
        return result

    def reports_for_unit(self, key: str) -> list:
        entry = self.entries.get(str(key))
        return [entry] if isinstance(entry, dict) else []


class ReferenceValidationCache:
    """Content cache for source-reference ASR/text similarity checks."""

    RULES_VERSION = "reference-similarity-v1"

    @classmethod
    def _signature(cls, filename, expected_text, validator_model):
        payload = {
            "audio_hash": file_hash(filename),
            "expected_text_hash": text_hash(expected_text),
            "validator_model": str(validator_model),
            "rules_version": cls.RULES_VERSION,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        payload["signature"] = _sha256_bytes(encoded)
        return payload

    @classmethod
    def lookup(cls, filename, expected_text, validator_model) -> Optional[dict]:
        sig = cls._signature(filename, expected_text, validator_model)
        if not sig["audio_hash"]:
            return None
        path = GLOBAL_REFERENCE_QUALITY_DIR / sig["signature"][:2] / f"{sig['signature']}.json"
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        return entry if entry.get("signature") == sig["signature"] else None

    @classmethod
    def record(
        cls, filename, expected_text, validator_model, *, transcript, similarity, passed
    ) -> dict:
        sig = cls._signature(filename, expected_text, validator_model)
        entry = {
            **sig,
            "transcript": transcript or "",
            "similarity": float(similarity or 0),
            "passed": bool(passed),
            "created_at": int(time.time()),
        }
        path = GLOBAL_REFERENCE_QUALITY_DIR / sig["signature"][:2] / f"{sig['signature']}.json"
        if not path.is_file():
            atomic_write_json(path, entry)
        return entry
