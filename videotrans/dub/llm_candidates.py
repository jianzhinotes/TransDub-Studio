"""面向整轮语境的 DeepSeek 中文配音候选生成器。

LLM 一次看到同一说话轮次的全部分段方案、原文、现有译文和时间窗。模型只
提出文本候选，最终选择仍由本地时长模型和质量约束完成；任何 API、解析或
校验失败都会回退到确定性规则生成器。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter
from pathlib import Path

from videotrans.configure.config import TEMP_ROOT, params

from .duration import spoken_units
from .schema import TextCandidate
from .translation import ChineseCandidateGenerator


PROMPT_VERSION = "deepseek-turn-candidates-v3"
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?%?")
_SOURCE_NAME_RE = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9'’.-]+(?:\s+[A-Z][A-Za-z0-9'’.-]+)+|[A-Z]{2,})\b"
)
_MODEL_TOKEN_RE = re.compile(
    r"\b(?=[A-Za-z0-9.-]*[A-Za-z])(?=[A-Za-z0-9.-]*\d)[A-Za-z0-9.-]+\b"
)
_CAMEL_NAME_RE = re.compile(r"\b[A-Z][a-z]+[A-Z][A-Za-z0-9]*\b")
_MIXED_CASE_BRAND_RE = re.compile(
    r"\b(?=[A-Za-z0-9]*[a-z][A-Za-z0-9]*[A-Z])[A-Za-z][A-Za-z0-9]*\b"
)
_ACRONYM_RE = re.compile(r"\b[A-Z]{2,}[A-Z0-9.-]*\b")
_OBVIOUS_ENGLISH_WORDS = {
    "a", "an", "and", "are", "because", "but", "earth", "for", "from",
    "how", "in", "is", "it", "of", "on", "orbit", "putting", "solar",
    "that", "the", "this", "to", "was", "we", "we're", "what", "what's",
    "with", "yeah", "yes", "you", "your",
}


def _candidate_id(segment_id, kind, text):
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    return f"{segment_id}:{kind}:{digest}"


def _clean(text):
    text = re.sub(r"[ \t]+", " ", str(text or "").strip())
    text = re.sub(r"\s*([，。！？；：])\s*", r"\1", text)
    return text


def _is_chinese_target(target_language):
    normalized = str(target_language or "").strip().lower()
    return normalized.startswith("zh") or "chinese" in normalized or "中文" in normalized


def has_obvious_english_leak(text):
    """Conservative checkpoint guard for ordinary English left in Chinese copy."""
    return any(
        word.lower() in _OBVIOUS_ENGLISH_WORDS
        for word in re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", str(text or ""))
    )


def _sanitize_obvious_english(text):
    """Last-resort local repair used only when a targeted LLM repair fails.

    It is intentionally conservative and mainly handles ASR/translation boundary
    fragments.  The LLM repair remains the preferred path for natural phrasing.
    """
    value = str(text or "")
    phrases = (
        (r"\bWhat's\s+Starship's\b", "星舰的"),
        (r"\bMaster\s+Orbit\b", "质量入轨"),
        (r"\bPutting\s+Solar\b", "部署太阳能"),
    )
    for pattern, replacement in phrases:
        value = re.sub(pattern, replacement, value, flags=re.I)
    words = {
        "a": "", "an": "", "and": "和", "are": "是", "because": "因为",
        "but": "但", "earth": "地球", "for": "为了", "from": "从",
        "how": "如何", "in": "在", "is": "是", "it": "它", "of": "的",
        "on": "在", "orbit": "轨道", "putting": "部署", "solar": "太阳能",
        "that": "那", "the": "", "this": "这", "to": "到", "was": "是",
        "we": "我们", "we're": "我们", "what": "什么", "what's": "什么是",
        "with": "和", "yeah": "对", "yes": "对", "you": "你", "your": "你的",
    }
    value = re.sub(
        r"[A-Za-z]+(?:'[A-Za-z]+)?",
        lambda match: words.get(match.group(0).lower(), match.group(0)),
        value,
    )
    value = re.sub(r"(?:对[，, ]*){2,}", "对，", value)
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"^[，,、；;：:]+|[，,、；;：:]+$", "", value)
    return _clean(value)


def _protected_terms(group):
    terms = set(_NUMBER_RE.findall(f"{group.source_text} {group.baseline_text}"))
    # 只锁定明确的专名、缩写和型号；普通英文不锁定，避免把旧译文里的英文
    # 泄漏延续到新候选。
    terms.update(match.group(0).strip() for match in _MODEL_TOKEN_RE.finditer(group.baseline_text))
    terms.update(match.group(0).strip() for match in _CAMEL_NAME_RE.finditer(group.baseline_text))
    terms.update(match.group(0).strip() for match in _MIXED_CASE_BRAND_RE.finditer(group.baseline_text))
    terms.update(match.group(0).strip() for match in _ACRONYM_RE.finditer(group.baseline_text))
    # 原文只锁定多词专名和全大写缩写，允许 Tesla -> 特斯拉这类合理本地化。
    # ASR 句首常形成伪专名，如 "Yeah The"、"What's Starship's"、
    # "An AI"。含普通英文词的 Title Case 片段绝不能锁定，否则模型会被
    # 要求逐字保留污染文本。真正的 Elon Musk 等多词专名仍会保留。
    terms.update(
        match.group(0).strip()
        for match in _SOURCE_NAME_RE.finditer(group.source_text)
        if not has_obvious_english_leak(match.group(0))
    )
    return sorted((term for term in terms if term), key=lambda value: (-len(value), value))


def _extract_json(raw):
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("DeepSeek response contains no JSON object")
    parsed = json.loads(text[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("DeepSeek response root must be an object")
    return parsed


class DeepSeekCandidateGenerator:
    """生成整轮中文口语候选，并用本地规则严格验收模型输出。"""

    name = "deepseek"

    def __init__(self, *, api_key=None, model=None, cache_dir=None,
                 request_fn=None, fallback=None):
        self.api_key = params.get("deepseek_key", "") if api_key is None else api_key
        self.model = (model or params.get("deepseek_model") or "deepseek-chat").strip()
        self.cache_dir = Path(cache_dir or f"{TEMP_ROOT}/joint_dub_llm_cache")
        self.request_fn = request_fn or self._request
        self.fallback = fallback or ChineseCandidateGenerator()
        self.last_diagnostics = {}

    @classmethod
    def is_configured(cls):
        key = str(params.get("deepseek_key") or "").strip()
        model = str(params.get("deepseek_model") or "").strip()
        return bool(key and model and model != "-")

    def _payload(self, turn, options, target_language):
        protected = {
            group.id: _protected_terms(group)
            for option in options for group in option.groups
        }
        data = {
            "prompt_version": PROMPT_VERSION,
            "target_language": target_language,
            "turn": {
                "id": turn.id,
                "speaker_id": turn.speaker_id,
                "start_ms": turn.start_ms,
                "end_ms": turn.end_ms,
                "source_text": turn.source_text,
            },
            "segmentation_options": [
                {
                    "option_id": option.id,
                    "kind": option.kind,
                    "segments": [
                        {
                            "segment_id": group.id,
                            "source_text": group.source_text,
                            "baseline_text": group.baseline_text,
                            "target_duration_ms": max(group.end_ms - group.start_ms, 1),
                            "protected_terms": protected[group.id],
                        }
                        for group in option.groups
                    ],
                }
                for option in options
            ],
        }
        system = """你是中文长视频智能配音编排器中的候选改写模块。请在完整说话轮次语境下，为每个分段方案的每个 segment 生成两个中文口语候选：natural（语义完整、自然）和 compact（仅在不丢失事实与逻辑的前提下更短）。
严格要求：
1. 不增删事实、数字、否定、条件、因果关系或说话人态度；protected_terms 必须逐字保留。
2. 不按英文逐词翻译；结合整轮上下文处理指代、承接和断句。
3. 文本用于口播，不写注释、括号说明或舞台提示，不夹杂非 protected_terms 的英文。
4. target_duration_ms 是软约束；不能为了时长牺牲语义。
5. 只返回 JSON，不要 Markdown。结构必须是：
{"options":[{"option_id":"...","segments":[{"segment_id":"...","candidates":[{"kind":"natural","text":"..."},{"kind":"compact","text":"..."}]}]}]}"""
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(data, ensure_ascii=False, separators=(",", ":"))},
            ],
            "temperature": 0.25,
            "max_completion_tokens": min(
                max(int(float(params.get("deepseek_max_token") or 8192)), 4096), 16384),
        }, data

    def _repair_payload(self, turn, dirty_groups, target_language):
        """Build a small second-pass request only for contaminated fallback rows."""
        options = {}
        for option, group in dirty_groups:
            options.setdefault(option.id, []).append({
                "segment_id": group.id,
                "source_text": group.source_text,
                "baseline_text": group.baseline_text,
                "target_duration_ms": max(group.end_ms - group.start_ms, 1),
                "protected_terms": _protected_terms(group),
            })
        data = {
            "prompt_version": f"{PROMPT_VERSION}-repair",
            "target_language": target_language,
            "turn_context": turn.source_text,
            "options": [
                {"option_id": option_id, "segments": segments}
                for option_id, segments in options.items()
            ],
        }
        system = """你是中文配音译文修复器。输入只包含首轮候选验收失败且夹杂普通英文的片段。
结合 turn_context 和 source_text 重新翻译，每段给出 natural 与 compact 两个自然中文口语候选。
严格要求：
1. 普通英文必须全部译成中文；仅 protected_terms 可原样保留。
2. 保留事实、数字、否定、因果和语气；不要解释，不要逐词硬译。
3. 即使 source_text 是半句，也要给出可与相邻片段衔接的自然中文半句。
4. 只返回 JSON：
{"options":[{"option_id":"...","segments":[{"segment_id":"...","candidates":[{"kind":"natural","text":"..."},{"kind":"compact","text":"..."}]}]}]}"""
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(data, ensure_ascii=False, separators=(",", ":"))},
            ],
            "temperature": 0.15,
            "max_completion_tokens": min(
                max(2048, len(dirty_groups) * 320),
                max(int(float(params.get("deepseek_max_token") or 8192)), 4096),
            ),
        }, data

    def _cache_key(self, request_data):
        raw = json.dumps(
            {"model": self.model, "request": request_data},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _cache_path(self, key):
        return self.cache_dir / f"{key}.json"

    def _read_cache(self, key):
        path = self._cache_path(key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _write_cache(self, key, value):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._cache_path(key)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _request(self, payload):
        if not str(self.api_key or "").strip():
            raise ValueError("DeepSeek API key is not configured")
        from openai import OpenAI

        kwargs = dict(payload)
        model = kwargs.pop("model")
        client = OpenAI(api_key=self.api_key, base_url="https://api.deepseek.com/v1/")
        extra_body = {
            "thinking": {
                "type": "enabled" if params.get("deepseek_thinking") else "disabled"
            }
        }
        response = client.chat.completions.create(
            model=model, timeout=300, extra_body=extra_body, **kwargs)
        if not getattr(response, "choices", None):
            raise ValueError("DeepSeek returned no choices")
        message = response.choices[0].message
        if not getattr(message, "content", None):
            raise ValueError("DeepSeek returned empty content")
        return message.content

    @staticmethod
    def _indexed_response(parsed):
        result = {}
        options = parsed.get("options")
        if not isinstance(options, list):
            raise ValueError("DeepSeek response is missing options")
        for option in options:
            if not isinstance(option, dict) or not isinstance(option.get("segments"), list):
                continue
            option_id = str(option.get("option_id") or "")
            if not option_id or option_id in result:
                raise ValueError("DeepSeek response has a missing or duplicate option_id")
            segments = {}
            for segment in option["segments"]:
                if not isinstance(segment, dict):
                    continue
                segment_id = str(segment.get("segment_id") or "")
                if not segment_id or segment_id in segments:
                    raise ValueError("DeepSeek response has a missing or duplicate segment_id")
                candidates = segment.get("candidates")
                if not isinstance(candidates, list) or not candidates:
                    raise ValueError("DeepSeek response has an empty candidates list")
                segments[segment_id] = candidates
            result[option_id] = segments
        return result

    @staticmethod
    def _validate_shape(indexed, options):
        expected_options = {option.id for option in options}
        if set(indexed) != expected_options:
            raise ValueError("DeepSeek response option IDs do not match the request")
        for option in options:
            expected_segments = {group.id for group in option.groups}
            if set(indexed[option.id]) != expected_segments:
                raise ValueError(
                    f"DeepSeek response segment IDs do not match option {option.id}")

    @staticmethod
    def _valid_text(text, group, protected_terms, kind, target_language):
        text = _clean(text)
        if not text:
            return None
        # 原文和旧译文通常重复出现同一数字，只要求候选保留较高的一侧计数，
        # 而不是把原文+译文的重复计数相加。
        source_numbers = Counter(_NUMBER_RE.findall(group.source_text))
        baseline_numbers = Counter(_NUMBER_RE.findall(group.baseline_text))
        required_numbers = source_numbers | baseline_numbers
        if Counter(_NUMBER_RE.findall(text)) != required_numbers:
            return None
        if any(term not in text for term in protected_terms):
            return None
        if _is_chinese_target(target_language):
            remainder = text
            for term in protected_terms:
                remainder = remainder.replace(term, "")
            cjk = len(re.findall(r"[\u3400-\u9fff]", remainder))
            latin = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", remainder)
            # All Latin tokens in a Chinese dubbing candidate must be traceable
            # to an explicitly protected name/model/acronym.  The previous ratio
            # threshold admitted short leaks such as "Yes The".
            if cjk == 0 or latin:
                return None
        baseline_units = spoken_units(group.baseline_text)
        ratio = spoken_units(text) / max(baseline_units, 1.0)
        minimum_ratio = 0.70 if kind == "natural" else 0.55
        if ratio < minimum_ratio or ratio > 1.65:
            return None
        return text

    def _merge_candidates(self, *, parsed, fallback, options, duration_model,
                          cache_hit, target_language):
        indexed = self._indexed_response(parsed)
        self._validate_shape(indexed, options)
        accepted = 0
        accepted_groups = 0
        total_groups = sum(len(option.groups) for option in options)
        output = {}
        for option in options:
            output[option.id] = {}
            for group in option.groups:
                candidates = list(fallback[option.id][group.id])
                seen = {candidate.text for candidate in candidates}
                protected = _protected_terms(group)
                raw_candidates = indexed.get(option.id, {}).get(group.id, [])
                if isinstance(raw_candidates, list):
                    group_accepted = 0
                    for raw in raw_candidates[:4]:
                        if not isinstance(raw, dict):
                            continue
                        kind = str(raw.get("kind") or "").strip().lower()
                        if kind not in {"natural", "compact"}:
                            continue
                        text = self._valid_text(
                            raw.get("text"), group, protected, kind,
                            target_language)
                        if not text or text in seen:
                            continue
                        seen.add(text)
                        accepted += 1
                        group_accepted += 1
                        candidates.append(TextCandidate(
                            id=_candidate_id(group.id, f"deepseek-{kind}", text),
                            text=text,
                            kind=f"deepseek_{kind}",
                            semantic_score=0.98 if kind == "natural" else 0.93,
                            naturalness_score=0.96 if kind == "natural" else 0.90,
                            estimated_duration_ms=duration_model.estimate(
                                text, group.speaker_id),
                            metadata={
                                "provider": "deepseek",
                                "model": self.model,
                                "prompt_version": PROMPT_VERSION,
                                "cache_hit": cache_hit,
                                "protected_terms": protected,
                            },
                        ))
                    if group_accepted:
                        accepted_groups += 1
                output[option.id][group.id] = candidates
        return output, accepted, accepted_groups, total_groups

    def _repair_dirty_fallbacks(self, *, output, turn, options, duration_model,
                                target_language):
        dirty = []
        for option in options:
            for group in option.groups:
                candidates = output[option.id][group.id]
                for candidate in candidates:
                    if has_obvious_english_leak(candidate.text):
                        candidate.semantic_score = 0.0
                        candidate.naturalness_score = 0.0
                        candidate.metadata["english_leak_fallback"] = True
                has_clean_model_candidate = any(
                    candidate.kind.startswith("deepseek_")
                    and not has_obvious_english_leak(candidate.text)
                    for candidate in candidates)
                if (not has_clean_model_candidate
                        and has_obvious_english_leak(group.baseline_text)):
                    dirty.append((option, group))
        if not dirty:
            return 0, 0, False, None

        repaired_candidates = 0
        repaired_groups = 0
        repair_cache_hit = False
        repair_error = None
        try:
            payload, request_data = self._repair_payload(turn, dirty, target_language)
            key = self._cache_key({"repair": request_data})
            cached = self._read_cache(key)
            repair_cache_hit = cached is not None
            parsed = cached if repair_cache_hit else _extract_json(self.request_fn(payload))
            indexed = self._indexed_response(parsed)
            for option, group in dirty:
                protected = _protected_terms(group)
                raw_candidates = indexed.get(option.id, {}).get(group.id, [])
                accepted_here = 0
                seen = {candidate.text for candidate in output[option.id][group.id]}
                for raw in raw_candidates[:4]:
                    if not isinstance(raw, dict):
                        continue
                    kind = str(raw.get("kind") or "").strip().lower()
                    if kind not in {"natural", "compact"}:
                        continue
                    text = self._valid_text(
                        raw.get("text"), group, protected, kind, target_language)
                    if not text or text in seen:
                        continue
                    seen.add(text)
                    accepted_here += 1
                    repaired_candidates += 1
                    output[option.id][group.id].append(TextCandidate(
                        id=_candidate_id(group.id, f"deepseek-repair-{kind}", text),
                        text=text,
                        kind=f"deepseek_repair_{kind}",
                        semantic_score=0.97 if kind == "natural" else 0.92,
                        naturalness_score=0.97 if kind == "natural" else 0.91,
                        estimated_duration_ms=duration_model.estimate(
                            text, group.speaker_id),
                        metadata={
                            "provider": "deepseek",
                            "model": self.model,
                            "prompt_version": f"{PROMPT_VERSION}-repair",
                            "cache_hit": repair_cache_hit,
                            "protected_terms": protected,
                        },
                    ))
                if accepted_here:
                    repaired_groups += 1
            if repaired_candidates and not repair_cache_hit:
                self._write_cache(key, parsed)
        except Exception as error:
            repair_error = f"{type(error).__name__}: {error}"

        # Network/JSON/model failures must not restore a known-bad baseline.
        # Add a conservative local candidate only for still-unrepaired rows.
        for option, group in dirty:
            candidates = output[option.id][group.id]
            if any(candidate.kind.startswith("deepseek_repair_") for candidate in candidates):
                continue
            text = _sanitize_obvious_english(group.baseline_text)
            if (not text or text == group.baseline_text
                    or has_obvious_english_leak(text)):
                continue
            candidates.append(TextCandidate(
                id=_candidate_id(group.id, "local-english-repair", text),
                text=text,
                kind="local_english_repair",
                semantic_score=0.86,
                naturalness_score=0.80,
                estimated_duration_ms=duration_model.estimate(text, group.speaker_id),
                metadata={"provider": "local", "reason": "english_leak_fallback"},
            ))
            repaired_candidates += 1
            repaired_groups += 1
        return repaired_candidates, repaired_groups, repair_cache_hit, repair_error

    def generate_turn(self, *, turn, options, target_language, duration_model):
        fallback = self.fallback.generate_turn(
            turn=turn, options=options, target_language=target_language,
            duration_model=duration_model)
        payload, request_data = self._payload(turn, options, target_language)
        key = self._cache_key(request_data)
        cached = self._read_cache(key)
        try:
            cache_hit = cached is not None
            parsed = cached if cache_hit else _extract_json(self.request_fn(payload))
            output, accepted, accepted_groups, total_groups = self._merge_candidates(
                parsed=parsed, fallback=fallback, options=options,
                duration_model=duration_model, cache_hit=cache_hit,
                target_language=target_language)
            if accepted <= 0:
                raise ValueError("DeepSeek response contained no valid candidates")
            repaired, repaired_groups, repair_cache_hit, repair_error = (
                self._repair_dirty_fallbacks(
                    output=output, turn=turn, options=options,
                    duration_model=duration_model,
                    target_language=target_language,
                )
            )
            complete = accepted_groups == total_groups
            if not cache_hit and complete:
                self._write_cache(key, parsed)
            self.last_diagnostics = {
                "status": "ok" if complete else "partial",
                "cache_hit": cache_hit,
                "accepted_candidates": accepted,
                "accepted_segments": accepted_groups,
                "total_segments": total_groups,
                "repaired_candidates": repaired,
                "repaired_segments": repaired_groups,
                "repair_cache_hit": repair_cache_hit,
                "repair_error": repair_error,
                "model": self.model,
                "prompt_version": PROMPT_VERSION,
            }
            return output
        except Exception as error:
            # A malformed/empty primary response used to bypass the repair pass
            # entirely and restore the contaminated baseline.  Run the same
            # targeted/local repair against the fallback map before giving up.
            repaired, repaired_groups, repair_cache_hit, repair_error = (
                self._repair_dirty_fallbacks(
                    output=fallback, turn=turn, options=options,
                    duration_model=duration_model,
                    target_language=target_language,
                )
            )
            self.last_diagnostics = {
                "status": "repaired_fallback" if repaired else "fallback",
                "cache_hit": cached is not None,
                "error": f"{type(error).__name__}: {error}",
                "repaired_candidates": repaired,
                "repaired_segments": repaired_groups,
                "repair_cache_hit": repair_cache_hit,
                "repair_error": repair_error,
                "model": self.model,
                "prompt_version": PROMPT_VERSION,
            }
            return fallback


def build_candidate_generator(kind="auto", *, cache_dir=None, target_language=None):
    """为预览入口选择候选后端；正式任务流水线不受影响。"""
    normalized = str(kind or "auto").strip().lower()
    if normalized in {"rules", "local", "offline"}:
        return ChineseCandidateGenerator()
    if normalized == "auto":
        if (not _is_chinese_target(target_language)
                or not DeepSeekCandidateGenerator.is_configured()):
            return ChineseCandidateGenerator()
        return DeepSeekCandidateGenerator(cache_dir=cache_dir)
    if normalized == "deepseek":
        if not _is_chinese_target(target_language):
            raise ValueError("DeepSeek dubbing candidates currently support Chinese targets only")
        if not DeepSeekCandidateGenerator.is_configured():
            raise ValueError("DeepSeek candidate backend requires deepseek_key and deepseek_model")
        return DeepSeekCandidateGenerator(cache_dir=cache_dir)
    raise ValueError(f"Unknown candidate backend: {kind}")
