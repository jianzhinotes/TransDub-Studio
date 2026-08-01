from dataclasses import dataclass
import copy
import gc
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import List, Dict, Union
from urllib.parse import urlparse
from urllib.request import urlopen

from gradio_client import  handle_file
from videotrans.configure.config import ROOT_DIR, logger, settings
from videotrans.configure.excepts import DubbingSrtError
from videotrans.tts._gradio import GradioBase
from videotrans.util.help_misc import vail_file
from pydub import AudioSegment


@dataclass
class F5TTS(GradioBase):

    # F5-TTS 参考音频越接近 6-10s 干净人声，克隆音色越像；过短(3s 级)音色明显失真。
    # 上限 12s（F5 官方建议 <15s），选择时以 8s 为最优目标。
    MAX_REF_AUDIO_MS=12000
    BEST_REF_AUDIO_MS=8000
    MIN_IDEAL_REF_AUDIO_MS=5000
    MAX_IDEAL_REF_AUDIO_MS=8500
    MAX_LANGUAGE_RETRIES=2
    MASS_GATE_FAILURE_RATIO=0.10
    MASS_GATE_MIN_FAILURES=10
    PIPELINE_VERSION="quality-v6-chinese-anchor-bank"
    QUALITY_RULES_VERSION="zh-content-v3-coverage"
    VALIDATOR_BACKEND="faster-whisper-cpu"
    VALIDATOR_MODEL="large-v3-turbo"
    SERVICE_ERROR_MARKERS = (
        "connection refused", "failed to connect", "could not connect",
        "cancelledError", "mps backend out of memory", "out of memory",
        "could not fetch config", "could not get gradio config",
        "upstream gradio app has raised an exception",
    )

    def __post_init__(self):
        self.ainame = "f5tts"
        super().__post_init__()
        self._low_memory_profile = (
            str(settings.get("f5tts_low_memory_mode", True)).lower() != "false"
            and self._is_managed_local_service()
            and self._is_low_memory_apple_silicon()
        )
        if self._low_memory_profile:
            # Local F5 is already deliberately serial.  The generic one-second
            # cloud API throttle only wastes time between clips here.
            self.dub_nums = 1
            self.wait_sec = float(settings.get("f5tts_dubbing_wait", 0.15) or 0.15)
            logger.info("F5-TTS 已启用 Apple Silicon 低内存模式（服务/门禁错峰加载）")
        self._synthesis_supervisor_obj = self._new_synthesis_supervisor()
        # 参考质检与最终泄漏门禁共用。质量优先：优先 large-v3-turbo，
        # 只有显式允许时才退回 tiny，避免弱模型漏掉短促英文。
        validator = self._load_validator()
        try:
            self.safe_ref_wav, self.safe_ref_text = self._select_safe_reference(validator)
            self._build_cluster_refs(validator)
            self.resume_chinese_anchors = {}
            (
                self.resume_chinese_anchor_ref,
                self.resume_chinese_anchor_text,
            ) = self._select_existing_chinese_anchor(validator)
        finally:
            del validator
            gc.collect()
        # nfe/seed 影响输出音质，纳入配音缓存键，防止调参后命中旧缓存
        anchor_sigs = ",".join(
            self._file_sig(value[0])
            for _key, value in sorted(self.resume_chinese_anchors.items())
        )
        self.dubb_cache_extra = (
            f"{self.PIPELINE_VERSION}-adaptive-slot-v1"
            f"-nfe{int(settings.get('f5tts_nfe') or 32)}"
            f"-seed{int(settings.get('f5tts_seed', 42))}"
            f"-resume-anchor{anchor_sigs or self._file_sig(self.resume_chinese_anchor_ref or '')}")

    @staticmethod
    def _new_synthesis_supervisor():
        from videotrans.dub.synthesis_supervisor import SynthesisSupervisor
        return SynthesisSupervisor(
            stall_floor_s=float(settings.get("f5tts_item_timeout_s", 180) or 180),
            stall_multiplier=float(
                settings.get("f5tts_item_timeout_multiplier", 4.0) or 4.0),
            min_available_mb=int(
                settings.get("f5tts_min_available_mb", 1536) or 1536),
            max_slot_ratio=float(settings.get("f5tts_max_slot_ratio", 1.15) or 1.15),
            max_backend_speed=float(
                settings.get("f5tts_max_backend_speed", 1.3) or 1.3),
        )

    def _synthesis_supervisor(self):
        supervisor = getattr(self, "_synthesis_supervisor_obj", None)
        if supervisor is None:
            supervisor = self._synthesis_supervisor_obj = self._new_synthesis_supervisor()
        return supervisor

    @staticmethod
    def _slot_aware_speed(*, requested_speed: float, ref_text: str,
                          gen_text: str, ref_duration_ms: int,
                          target_duration_ms: int, fit_to_slot: bool) -> float:
        """Keep F5 generation near the orchestration slot before alignment.

        F5 estimates duration from UTF-8 byte counts.  A global -50% rate can
        turn a five-second Chinese line into 10-15 seconds, only for the
        alignment stage to compress it again.  That wastes Metal memory and is
        less natural than synthesizing near the target in the first place.
        """
        from videotrans.dub.synthesis_supervisor import SynthesisSupervisor
        from videotrans.util.resource_governor import ResourceSnapshot
        supervisor = SynthesisSupervisor(snapshot_fn=lambda: ResourceSnapshot())
        return supervisor.admit(
            requested_speed=requested_speed,
            ref_text=ref_text,
            gen_text=gen_text,
            ref_duration_ms=ref_duration_ms,
            target_duration_ms=target_duration_ms,
            fit_to_slot=fit_to_slot,
        ).effective_speed

    @staticmethod
    def _reference_text_penalty(text: str) -> int:
        words = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text or "")
        lowered = (text or "").lower()
        penalty = 0

        # Introductions, names, brands and calls to action are particularly
        # noticeable when F5 copies one word from the English reference.
        blocked_phrases = (
            "my name is", "welcome", "subscribe", "former cia", "officer",
            "today show", "fox news", "shark tank", "amazon", "youtube",
            # 访谈对话口癖：这类句子多半是主持人/嘉宾互相对话，容易选中
            # 非主讲人的声音当克隆模板，导致成品音色不像
            "as you mentioned", "as you said", "thank you", "thanks for",
            "great question", "welcome back", "joining us",
            # 短口头语会让跨语言生成更像“继续说英文”，不适合作为克隆条件。
            "yeah", "yes", "all right", "you know", "i mean",
        )
        penalty += sum(12000 for phrase in blocked_phrases if phrase in lowered)

        # Ignore the first word because normal English sentences capitalize it.
        for word in words[1:]:
            plain = word.replace("'", "")
            if plain.isupper() and len(plain) >= 2:
                penalty += 6000
            elif plain[:1].isupper() and plain[1:].islower() and len(plain) >= 4:
                penalty += 9000
        return penalty

    # ---- 参考音频自动质检与构建（全自动，无需人工指定） ----
    @staticmethod
    def _punct_ok(text: str) -> bool:
        return (text or "")[-1:] in ".!?。！？"

    @staticmethod
    def _ensure_punct(text: str) -> str:
        text = (text or "").strip()
        suffix = "。" if re.search(r"[\u4e00-\u9fff]", text) else "."
        return text if F5TTS._punct_ok(text) else text + suffix

    @staticmethod
    def _text_similarity(a: str, b: str) -> float:
        """转写与字幕文本的相似度（跨语言鲁棒：拉丁词 + CJK 单字为 token）。"""
        import difflib

        def norm(s):
            tokens = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?|[一-鿿]", (s or "").lower())
            return " ".join(tokens)

        na, nb = norm(a), norm(b)
        if not na or not nb:
            return 0.0
        return difflib.SequenceMatcher(None, na, nb).ratio()

    def _load_validator(self):
        try:
            return self._new_validator()
        except Exception as e:
            logger.warning(f"参考质检模型不可用，跳过回读验证: {e}")
            return None

    def _new_validator(self):
        from faster_whisper import WhisperModel
        from videotrans.util.resource_governor import runtime_limits

        limits = runtime_limits(mode=settings.get("resource_mode", "auto"))
        return WhisperModel(
            str(self._get_validator_model_path()),
            device="cpu",
            compute_type="int8",
            cpu_threads=limits.validator_cpu_threads,
            num_workers=1,
        )

    def _mlx_validator_model_path(self):
        candidates = (
            Path(ROOT_DIR) / "models/mlx--mlx-community--whisper-large-v3-turbo",
            Path(ROOT_DIR) / "models/models--mlx-community--whisper-large-v3-turbo",
        )
        return next(
            (path for path in candidates if (path / "weights.safetensors").is_file()),
            None,
        )

    def _should_use_mlx_validator(self) -> bool:
        if str(settings.get("use_mlx_whisper", False)).lower() != "true":
            return False
        if platform.system() != "Darwin" or platform.machine().lower() not in {"arm64", "aarch64"}:
            return False
        try:
            import importlib.util
            return (
                importlib.util.find_spec("mlx_whisper") is not None
                and self._mlx_validator_model_path() is not None
            )
        except Exception:
            return False

    def _validator_identity(self):
        if self._should_use_mlx_validator():
            return "mlx-whisper-mps", self.VALIDATOR_MODEL
        return self.VALIDATOR_BACKEND, self.VALIDATOR_MODEL

    def _collect_candidates(self, allowed=None):
        """收集克隆参考候选并打分。allowed 为 None 时考虑全部行，否则只看指定下标。

        半句文本重罚：ref_text 掐在半句上时 F5 会把参考文本"续写"进生成结果
        （曾导致 49 段串音 "First you've got..."）。
        """
        candidates = []
        queue_len = max(len(self.queue_tts), 1)
        for index, item in enumerate(self.queue_tts):
            if allowed is not None and index not in allowed:
                continue
            if item.get("role") != "clone":
                continue
            ref_wav = item.get("ref_wav", "")
            ref_text = (item.get("ref_text") or "").strip()
            if not ref_wav or not Path(ref_wav).is_file() or len(ref_text) < 15:
                continue
            try:
                duration_ms = len(AudioSegment.from_file(ref_wav))
            except Exception:
                continue
            if 2500 <= duration_ms <= self.MAX_REF_AUDIO_MS:
                position = index / queue_len
                # Prefer ordinary narration near the middle. Avoid intros,
                # outros, proper names and branded montage clips.
                edge_penalty = 0
                if position < 0.18 or position > 0.88:
                    edge_penalty = 8000
                position_penalty = int(abs(position - 0.5) * 2500)
                # 以 BEST_REF_AUDIO_MS(8s) 为最优：长参考音色更像，且长句
                # 多为主讲人连续陈述，降低选中访谈另一方声音的概率
                duration_penalty = abs(duration_ms - self.BEST_REF_AUDIO_MS)
                if not (self.MIN_IDEAL_REF_AUDIO_MS <= duration_ms <= self.MAX_IDEAL_REF_AUDIO_MS):
                    duration_penalty += 5000
                text_penalty = self._reference_text_penalty(ref_text)
                punct_penalty = 0 if self._punct_ok(ref_text) else 7000
                score = (text_penalty + edge_penalty + position_penalty
                         + duration_penalty + punct_penalty)
                candidates.append((score, ref_wav, ref_text, index, duration_ms))
        return candidates

    def _validate_candidates(self, ranked, validator, need=4, max_try=24):
        """回读验证：把候选转写一遍，与字幕文本对不上的淘汰。

        这一步专杀"文本与音频错位"的毒参考。validator 不可用时返回空列表，
        调用方退回按分数排序的旧行为。
        """
        if not validator:
            return []
        from videotrans.dub.quality_manifest import ReferenceValidationCache

        passed = []
        for cand in ranked[:max_try]:
            threshold = float(settings.get('f5tts_ref_similarity', 0.75) or 0.75)
            cached = ReferenceValidationCache.lookup(
                cand[1], cand[2], self.VALIDATOR_MODEL
            )
            if cached is not None:
                transcript = str(cached.get("transcript") or "")
                sim = float(cached.get("similarity") or 0)
            else:
                try:
                    transcript = self._transcribe_one_for_validation(validator, cand[1])
                except Exception as e:
                    logger.debug(f"参考回读失败,跳过候选: {e}")
                    continue
                sim = self._text_similarity(transcript, cand[2])
                ReferenceValidationCache.record(
                    cand[1], cand[2], self.VALIDATOR_MODEL,
                    transcript=transcript,
                    similarity=sim,
                    passed=sim >= threshold,
                )
            if sim >= threshold:
                passed.append(cand)
            else:
                logger.debug(f"参考回读不匹配(sim={sim:.2f}),淘汰: {cand[2][:50]!r} vs {transcript[:50]!r}")
            if len(passed) >= need:
                break
        return passed

    def _compose_reference(self, pool, tag="main"):
        """聚合式参考（ElevenLabs 思路）：主片段不足 7s 时拼接次优片段到 ~8-12s。

        单一坏片段不再决定全片音色。pool 元素: (score, wav, text, index, duration_ms)。
        返回 (wav_path, ref_text)。
        """
        _s, wav, text, _i, duration_ms = pool[0]
        text = self._ensure_punct(text)
        if duration_ms >= 7000 or len(pool) < 2:
            return wav, text
        try:
            combined = AudioSegment.from_file(wav)
            parts_text = [text]
            for _s2, w2, t2, _i2, d2 in pool[1:]:
                if len(combined) + d2 > self.MAX_REF_AUDIO_MS:
                    continue
                combined += AudioSegment.silent(duration=200) + AudioSegment.from_file(w2)
                parts_text.append(self._ensure_punct(t2))
                if len(combined) >= 7000:
                    break
            if len(parts_text) == 1:
                return wav, text
            out = Path(wav).parent / f"f5-composite-ref-{tag}.wav"
            combined.export(out, format="wav")
            logger.debug(f"F5-TTS 复合参考[{tag}]: {len(parts_text)} 段, {len(combined)}ms")
            return out.as_posix(), " ".join(parts_text)
        except Exception as e:
            logger.warning(f"复合参考构建失败,退回单片段: {e}")
            return wav, text

    def _choose_reference(self, pool, tag="main"):
        """质量优先选择单个5-8.5秒完整句；确实没有合格单句时才复合。

        非连续片段拼接虽然能增加音色信息，但也会增加英语条件文本和人物串音
        风险，因此不再作为默认路径。
        """
        for cand in pool:
            _score, wav, text, _idx, duration_ms = cand
            if (self.MIN_IDEAL_REF_AUDIO_MS <= duration_ms <= self.MAX_IDEAL_REF_AUDIO_MS
                    and self._punct_ok(text)):
                return wav, self._ensure_punct(text)
        return self._compose_reference(pool, tag=tag)

    def _select_safe_reference(self, validator=None):
        candidates = self._collect_candidates()
        if not candidates:
            return None, None
        candidates = self._keep_dominant_speaker(candidates)
        ranked = sorted(candidates, key=lambda item: item[0])
        validated = self._validate_candidates(ranked, validator)
        if validator is not None and not validated:
            raise DubbingSrtError(
                "F5-TTS 参考音频回读全部与参考文本不匹配，已在生成前停止。"
                "这通常表示参考音频裁剪时间轴错位。"
            )
        pool = validated if validator is not None else ranked[:4]
        ref_wav, ref_text = self._choose_reference(pool, tag="main")
        # 备选参考（同簇次优）：主参考仍导致大面积串音时，泄漏重试第 2 轮起换用
        self.ref_backups = []
        for _s, w, t, _i, _d in pool[1:]:
            if w != ref_wav:
                self.ref_backups.append((w, self._ensure_punct(t)))
            if len(self.ref_backups) >= 3:
                break
        logger.debug(
            "F5-TTS 参考选择: 候选=%s 回读通过=%s ref_wav=%s ref_text=%s 备选=%s",
            len(ranked), len(validated), ref_wav, ref_text, len(self.ref_backups)
        )
        return ref_wav, ref_text

    def _build_cluster_refs(self, validator=None):
        """多说话人模式：逐句归属说话人簇，各簇构建独立参考（各说各的音色）。

        置信门槛：聚类可靠且次要说话人时长占比 ≥12% 才启用；否则维持
        单一主讲人参考（旧行为）。可用 settings['f5tts_multi_speaker']=false 关闭。
        """
        if str(settings.get('f5tts_multi_speaker', True)).lower() == 'false':
            return
        lines = [(i, it) for i, it in enumerate(self.queue_tts)
                 if it.get('role') == 'clone' and it.get('ref_wav')
                 and Path(it.get('ref_wav', '')).is_file()]
        if len(lines) < 12:
            return
        try:
            from videotrans.util.speaker_cluster import label_speakers
            labels = label_speakers([it['ref_wav'] for _, it in lines])
        except Exception as e:
            logger.warning(f'逐句声纹归属失败,维持单参考: {e}')
            return
        if not labels:
            return
        # 各簇时长占比
        totals = {}
        for pos, (i, it) in enumerate(lines):
            if pos not in labels:
                continue
            d = max(int(it.get('end_time', 0) or 0) - int(it.get('start_time', 0) or 0), 0)
            totals[labels[pos]] = totals.get(labels[pos], 0) + d
        if len(totals) < 2 or sum(totals.values()) <= 0:
            return
        if min(totals.values()) / sum(totals.values()) < 0.12:
            logger.debug('次要说话人占比过低,视为单说话人,维持单参考')
            return
        # 每簇独立选参考（同样走打分+回读验证+复合）
        cluster_banks = {}
        for label in totals:
            allowed = {i for pos, (i, _it) in enumerate(lines) if labels.get(pos) == label}
            cands = self._collect_candidates(allowed=allowed)
            if not cands:
                continue
            ranked = sorted(cands, key=lambda item: item[0])
            validated = self._validate_candidates(ranked, validator, need=3, max_try=6)
            # 验证器存在却没有任何候选通过时，该声纹簇不安全。
            # 主参考仍可用，因此只跳过该簇，不使用错配的次优候选。
            pool = validated if validator is not None else ranked[:3]
            if not pool:
                logger.warning("F5-TTS 声纹簇 %s 无安全参考，跳过该簇", label)
                continue
            bank = [
                self._anchor_entry(wav, text, duration_ms)
                for _score, wav, text, _index, duration_ms in pool[:3]
            ]
            if bank:
                cluster_banks[label] = bank
        if len(cluster_banks) < 2:
            return
        # 把稳定声纹簇 ID 与候选库写进条目。每行可按句式选择不同参考，
        # speaker_cluster_id 保证换参考不会被误判成换说话人。
        assigned = 0
        for pos, (i, it) in enumerate(lines):
            label = labels.get(pos)
            bank = cluster_banks.get(label)
            if bank:
                anchor = self._choose_chinese_anchor(bank, it)
                it['speaker_cluster_id'] = f'cluster:{label}'
                it['cluster_ref_bank'] = bank
                it['cluster_ref'] = anchor['wav']
                it['cluster_ref_text'] = anchor['text']
                assigned += 1
        logger.debug(f'多说话人参考库启用: {len(cluster_banks)} 簇, 覆盖 {assigned}/{len(lines)} 行, 时长占比={totals}')

    @staticmethod
    def _keep_dominant_speaker(candidates):
        """多说话人视频（访谈等）里只保留主讲人的片段做克隆参考。

        对候选片段做声纹聚类，按说话总时长判定主讲人簇（访谈里说得最多的
        通常就是被采访者），其余簇的片段剔除。聚类不可靠（单说话人/样本少/
        依赖异常）时原样返回，不影响原有选择逻辑。
        candidates 元素: (score, ref_wav, ref_text, index, duration_ms)
        """
        try:
            from videotrans.util.speaker_cluster import cluster_speakers
            labels = cluster_speakers([c[1] for c in candidates])
            if not labels:
                return candidates
            totals = {}
            for pos, label in labels.items():
                totals[label] = totals.get(label, 0) + candidates[pos][4]
            dominant = max(totals, key=totals.get)
            kept = [c for pos, c in enumerate(candidates)
                    if labels.get(pos, dominant) == dominant]
            logger.debug(
                "声纹聚类保留主讲人片段 %s/%s (各簇时长 %s)",
                len(kept), len(candidates), totals,
            )
            return kept or candidates
        except Exception as e:
            logger.warning(f"声纹聚类失败,退回原候选: {e}")
            return candidates

    def _exec(self) -> None:
        managed_local = self._is_managed_local_service()
        low_memory = bool(getattr(self, "_low_memory_profile", False))
        needs_content_gate = bool(
            not self.is_test and self.language and self.language[:2].lower() == "zh"
        )
        serialize_validator = bool(
            low_memory or (needs_content_gate and self._should_use_mlx_validator())
        )
        self._service_circuit_error = ""
        try:
            # The app no longer keeps F5 resident from launch.  Start it only
            # when an F5 dubbing task actually reaches synthesis.
            if managed_local and not self._wait_for_synthesis_resources(0):
                raise DubbingSrtError(
                    str(getattr(self, "_service_circuit_error", ""))
                    or "F5-TTS 启动前资源保护未放行"
                )
            if managed_local and not self._start_local_service():
                # A long reference-analysis pass can leave reclaimable native
                # pages behind.  Give macOS one pressure-relief cycle and retry
                # once instead of aborting the fully planned long-video task.
                self.signal(text="F5-TTS 首次启动未成功，正在释放内存并自动重试…")
                logger.warning("F5-TTS 首次按需启动失败，释放内存后重试一次")
                self._stop_local_service()
                self._release_memory_pressure()
                time.sleep(2)
                if not self._start_local_service(recovery=True):
                    raise self._service_start_error("F5-TTS 本地服务按需启动失败")
            if self._should_run_preflight():
                self._run_preflight()
                # 16 GB 机型会在预飞回读前停掉 F5，全片放行前再启动。
                if managed_local and not self._local_service_is_ready():
                    if not self._start_local_service():
                        raise self._service_start_error("F5-TTS 预飞通过后重启本地服务失败")
            super()._exec()
            missing = [
                idx for idx, item in enumerate(self.queue_tts)
                if item.get("text", "").strip() and not vail_file(item.get("filename"))
            ]
            if missing:
                detail = str(
                    getattr(self, "_service_circuit_error", "")
                    or getattr(self, "error", "") or "本地服务未生成音频"
                )
                raise DubbingSrtError(
                    f"F5-TTS 配音未完成：已保留成功片段，仍缺少 {len(missing)} 段。"
                    f"下次重试将复用成功片段。原因：{detail[:300]}"
                )
            if self.is_test or not self.language or self.language[:2].lower() != "zh":
                return
            if serialize_validator:
                # Never overlap the F5 Metal model with large-v3-turbo's CPU
                # buffers or MLX Metal allocations.
                self._stop_local_service()
            self._verify_chinese_outputs()
        finally:
            if serialize_validator:
                self._stop_local_service()

    @staticmethod
    def _latin_words(text: str) -> List[str]:
        from videotrans.dub.chinese_quality import latin_words
        return latin_words(text)

    def _has_unexpected_english(self, expected: str, transcript: str) -> bool:
        from videotrans.dub.chinese_quality import has_unexpected_english
        return has_unexpected_english(
            expected,
            transcript,
            safe_reference_text=getattr(self, "safe_ref_text", "") or "",
            zero_unexpected_latin=str(settings.get(
                "f5tts_zero_unexpected_latin", True)).lower() != "false",
        )

    def _get_validator_model_path(self) -> Path:
        candidates = [
            Path(ROOT_DIR) / "models/models--mobiuslabsgmbh--faster-whisper-large-v3-turbo",
            Path(ROOT_DIR) / "models/large-v3-turbo",
        ]
        # tiny 只可作为用户显式开启的应急降级，不能承担质量门禁。
        if str(settings.get("f5tts_allow_weak_validator", False)).lower() == "true":
            candidates.append(Path(ROOT_DIR) / "models/faster-whisper-tiny")
        for path in candidates:
            if (path / "model.bin").is_file():
                return path
        raise DubbingSrtError(
            "F5-TTS 中文配音验收需要已下载的 large-v3-turbo 识别模型，但没有找到该模型。"
        )

    def _transcribe_batch_for_validation(self, model, indices=None) -> Dict[int, str]:
        """Screen one bounded micro-batch and map transcripts back to queue rows.

        The previous implementation concatenated the entire long video plus
        700 ms after every line.  Keeping batches bounded makes checkpoints and
        cancellation cheap and avoids transcribing minutes of synthetic silence.
        Suspicious rows are still confirmed individually before regeneration.
        """
        batch_audio = AudioSegment.empty()
        boundaries = []
        gap_ms = max(150, min(int(settings.get("f5tts_validation_gap_ms", 250) or 250), 500))
        allowed = set(indices) if indices is not None else None
        for idx, item in enumerate(self.queue_tts):
            if allowed is not None and idx not in allowed:
                continue
            if not item.get("text", "").strip() or not vail_file(item.get("filename")):
                continue
            clip = AudioSegment.from_file(item["filename"])
            start_ms = len(batch_audio)
            batch_audio += clip
            boundaries.append((start_ms, len(batch_audio), idx))
            batch_audio += AudioSegment.silent(duration=gap_ms)

        if not boundaries:
            return {}

        marker = f"{boundaries[0][2]}-{boundaries[-1][2]}-{time.time_ns()}"
        batch_file = Path(ROOT_DIR) / "tmp" / f"f5-language-check-{self.uuid}-{marker}.wav"
        batch_file.parent.mkdir(parents=True, exist_ok=True)
        batch_audio.export(batch_file, format="wav")
        transcripts = {idx: [] for _, _, idx in boundaries}
        try:
            segments, _ = model.transcribe(
                str(batch_file),
                beam_size=1,
                vad_filter=False,
                condition_on_previous_text=False,
                temperature=0,
            )
            boundary_pos = 0
            for segment in segments:
                segment_start = int(segment.start * 1000)
                segment_end = int(segment.end * 1000)
                while (
                    boundary_pos < len(boundaries) - 1
                    and segment_start >= boundaries[boundary_pos][1]
                ):
                    boundary_pos += 1

                best_idx = None
                best_overlap = 0
                for pos in range(boundary_pos, min(boundary_pos + 2, len(boundaries))):
                    start_ms, end_ms, idx = boundaries[pos]
                    overlap = max(0, min(segment_end, end_ms) - max(segment_start, start_ms))
                    if overlap > best_overlap:
                        best_overlap = overlap
                        best_idx = idx
                if best_idx is not None:
                    transcripts[best_idx].append(segment.text)
            return {idx: "".join(parts).strip() for idx, parts in transcripts.items()}
        finally:
            batch_file.unlink(missing_ok=True)

    @staticmethod
    def _speaker_key(item) -> str:
        """把中文锚点限制在同一个自动声纹簇，避免重试时发生音色串人。"""
        return str(
            item.get("speaker_cluster_id")
            or item.get("cluster_ref")
            or "__main_speaker__"
        )

    @staticmethod
    def _anchor_style(text: str) -> str:
        value = (text or "").strip()
        if value.endswith(("?", "？")):
            return "question"
        if value.endswith(("!", "！")):
            return "exclamation"
        return "statement"

    def _anchor_entry(self, filename, text, duration_ms) -> dict:
        text = self._ensure_punct(text)
        return {
            "wav": str(filename),
            "text": text,
            "duration_ms": int(duration_ms),
            "style": self._anchor_style(text),
            "cjk_chars": len(re.findall(r"[\u4e00-\u9fff]", text)),
        }

    def _choose_chinese_anchor(self, bank, item, retry_no=0):
        if not bank:
            return None
        target_text = str(item.get("text") or "")
        target_style = self._anchor_style(target_text)
        target_chars = len(re.findall(r"[\u4e00-\u9fff]", target_text))
        ranked = sorted(bank, key=lambda anchor: (
            0 if anchor.get("style") == target_style else 1,
            abs(int(anchor.get("cjk_chars") or 0) - target_chars),
            abs(int(anchor.get("duration_ms") or 0) - 6500),
        ))
        return ranked[max(int(retry_no or 0), 0) % len(ranked)]

    def _assign_chinese_anchors(self, failed, transcripts) -> int:
        """从已通过验收的成品中挑同说话人 5-8.5s 中文片段作为重试参考。"""
        failed_indices = {idx for idx, _, _ in failed}
        candidates = {}
        for idx, item in enumerate(self.queue_tts):
            if idx in failed_indices or not vail_file(item.get("filename")):
                continue
            text = (item.get("text") or "").strip()
            if len(re.findall(r"[\u4e00-\u9fff]", text)) < 6:
                continue
            if self._has_unexpected_english(text, transcripts.get(idx, "")):
                continue
            try:
                duration_ms = len(AudioSegment.from_file(item["filename"]))
            except Exception:
                continue
            if not self.MIN_IDEAL_REF_AUDIO_MS <= duration_ms <= self.MAX_IDEAL_REF_AUDIO_MS:
                continue
            score = abs(duration_ms - 6500)
            candidates.setdefault(self._speaker_key(item), []).append(
                (score, self._anchor_entry(item["filename"], text, duration_ms))
            )

        assigned = 0
        for _, item, _ in failed:
            pool = candidates.get(self._speaker_key(item)) or []
            if not pool:
                continue
            bank = [entry for _score, entry in sorted(pool, key=lambda row: row[0])[:3]]
            anchor = self._choose_chinese_anchor(bank, item)
            item["chinese_anchor_bank"] = bank
            item["chinese_anchor_ref"] = anchor["wav"]
            item["chinese_anchor_text"] = anchor["text"]
            assigned += 1
        if assigned:
            logger.debug("F5-TTS 中文锚点已分配给 %s/%s 个泄漏重试段", assigned, len(failed))
        return assigned

    def _select_existing_chinese_anchor(self, validator):
        """Validate one completed Chinese clip for a resumed/partial task.

        Cross-language F5 generation can copy the tail of an English reference.
        A resumed task already has completed clips, so use one that Whisper
        confirms as clean Chinese to constrain only the remaining synthesis.
        """
        if validator is None:
            return None, None
        candidates = {}
        for idx, item in enumerate(self.queue_tts):
            filename = item.get("filename")
            text = (item.get("text") or "").strip()
            if not filename or not vail_file(filename):
                continue
            if len(re.findall(r"[\u4e00-\u9fff]", text)) < 8:
                continue
            try:
                duration_ms = len(AudioSegment.from_file(filename))
            except Exception:
                continue
            if not self.MIN_IDEAL_REF_AUDIO_MS <= duration_ms <= self.MAX_IDEAL_REF_AUDIO_MS:
                continue
            speaker_key = self._speaker_key(item)
            candidates.setdefault(speaker_key, []).append(
                (abs(duration_ms - 6500), idx, item)
            )

        selected = {}
        banks = {}
        for speaker_key, pool in candidates.items():
            bank = []
            for _score, idx, item in sorted(pool)[:16]:
                try:
                    transcript = self._transcribe_one_for_validation(validator, item["filename"])
                except Exception as error:
                    logger.debug("F5-TTS 恢复锚点回读失败，跳过第 %s 段: %s", idx + 1, error)
                    continue
                if (
                    len(re.findall(r"[\u4e00-\u9fff]", transcript)) >= 6
                    and not self._has_unexpected_english(item["text"], transcript)
                    and not self._has_pathological_repetition(transcript)
                ):
                    text = self._ensure_punct(item["text"])
                    bank.append(self._anchor_entry(
                        item["filename"], text,
                        len(AudioSegment.from_file(item["filename"]))))
                    logger.info(
                        "F5-TTS 恢复任务已选定同说话人中文锚点: 第 %s 段 %s",
                        idx + 1, item["filename"],
                    )
                    if len(bank) >= 3:
                        break
            if bank:
                banks[speaker_key] = bank
                anchor = self._choose_chinese_anchor(bank, pool[0][2])
                selected[speaker_key] = (anchor["wav"], anchor["text"])

        self.resume_chinese_anchor_banks = banks
        self.resume_chinese_anchors = selected
        if selected:
            self.signal(text=f"F5-TTS 已从现有成品选定 {len(selected)} 个同说话人中文音色锚点")
            # 单说话人任务直接返回主锚点；多说话人任务的 _run 会按
            # cluster_ref 精确取对应锚点，不会拿主持人的声音给嘉宾补句。
            main_key = "__main_speaker__"
            if main_key not in selected:
                main_key = max(selected, key=lambda key: len(candidates.get(key, ())))
            return selected[main_key]
        if candidates:
            logger.warning("F5-TTS 现有成品中未找到可验收的中文恢复锚点")
        return None, None

    def _transcribe_one_for_validation(self, model, filename: str) -> str:
        segments, _ = model.transcribe(
            filename,
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
            temperature=0,
        )
        return "".join(segment.text for segment in segments).strip()

    def _hard_quality_failures(self, expected: str, transcript: str) -> List[str]:
        from videotrans.dub.chinese_quality import hard_quality_failures
        return hard_quality_failures(
            expected,
            transcript,
            safe_reference_text=getattr(self, "safe_ref_text", "") or "",
            zero_unexpected_latin=str(settings.get(
                "f5tts_zero_unexpected_latin", True)).lower() != "false",
        )

    @staticmethod
    def _chinese_similarity(expected: str, transcript: str) -> float:
        from videotrans.dub.chinese_quality import chinese_similarity
        return chinese_similarity(expected, transcript)

    def _quality_metrics(self, expected: str, transcript: str) -> dict:
        from videotrans.dub.chinese_quality import quality_metrics
        return quality_metrics(expected, transcript)

    def _transcribe_isolated_for_validation(self, indices, backend=None) -> Dict[int, str]:
        from videotrans.process.quality_validator import (
            validate_faster_whisper_files,
            validate_mlx_whisper_files,
        )
        from videotrans.util.resource_governor import runtime_limits

        files = [
            (idx, self.queue_tts[idx]["filename"])
            for idx in indices
            if vail_file(self.queue_tts[idx].get("filename"))
        ]
        if not files:
            return {}
        logs_file = str(
            Path(ROOT_DIR) / "tmp"
            / f"f5-validator-{self.uuid}-{indices[0]}-{indices[-1]}.log"
        )
        backend = backend or self._validator_identity()[0]
        use_mlx = backend == "mlx-whisper-mps"
        callback = validate_mlx_whisper_files if use_mlx else validate_faster_whisper_files
        model_path = self._mlx_validator_model_path() if use_mlx else self._get_validator_model_path()
        limits = runtime_limits(mode=settings.get("resource_mode", "auto"))
        return self._new_process(
            callback=callback,
            title=f"F5-TTS quality {indices[0] + 1}-{indices[-1] + 1}",
            is_cuda=False,
            kwargs={
                "files": files,
                "model_path": str(model_path),
                "cpu_threads": limits.validator_cpu_threads,
                "logs_file": logs_file,
            },
        )

    def _transcribe_isolated_with_fallback(self, indices, backend):
        try:
            return self._transcribe_isolated_for_validation(indices, backend=backend), backend
        except Exception as error:
            if backend != "mlx-whisper-mps":
                raise
            logger.warning("MLX 配音核验不可用，自动回退隔离 CPU 核验: %s", error)
            self.signal(text="MLX 核验当前不可用，已自动切换 CPU 强模型，不降低核验等级")
            fallback = self.VALIDATOR_BACKEND
            return self._transcribe_isolated_for_validation(indices, backend=fallback), fallback

    @staticmethod
    def _human_duration(seconds: float) -> str:
        seconds = max(int(seconds or 0), 0)
        hours, remain = divmod(seconds, 3600)
        minutes, secs = divmod(remain, 60)
        if hours:
            return f"{hours}小时{minutes}分"
        if minutes:
            return f"{minutes}分{secs}秒"
        return f"{secs}秒"

    def _eta_text(self, label: str, completed: int, total: int, elapsed: float) -> str:
        if completed <= 0 or total <= 0:
            return f"{label} 0/{total}"
        average = elapsed / completed
        eta = average * max(total - completed, 0)
        return (
            f"{label} {completed}/{total}｜平均 {average:.1f}秒/段｜"
            f"预计剩余 {self._human_duration(eta)}"
        )

    def _format_tts_progress(self, completed: int, total: int, elapsed: float) -> str:
        return self._eta_text("F5-TTS 配音", completed, total, elapsed)

    def _should_run_preflight(self) -> bool:
        if self.is_test or not self.language or self.language[:2].lower() != "zh":
            return False
        sample_count = int(settings.get("f5tts_preflight_samples", 5) or 0)
        return sample_count > 0 and any(
            item.get("text", "").strip() and not vail_file(item.get("filename"))
            for item in self.queue_tts
        )

    @staticmethod
    def _preflight_risk(item) -> tuple:
        text = (item.get("text") or "").strip()
        return len(re.findall(r"[\u4e00-\u9fff]", text)), len(text)

    @staticmethod
    def _preflight_compute_risk(item) -> float:
        """Estimate F5 work, including a slow requested rate and a tight slot."""
        text = str(item.get("text") or "").strip()
        ref_text = str(item.get("ref_text") or "").strip()
        ref_ms = max(
            int(item.get("end_time_source") or item.get("end_time") or 0)
            - int(item.get("start_time_source") or item.get("start_time") or 0),
            1000,
        )
        slot_ms = max(
            int(item.get("target_duration_ms") or 0)
            or (int(item.get("end_time") or 0) - int(item.get("start_time") or 0)),
            500,
        )
        rate = str(item.get("rate") or "+0%").strip()
        try:
            speed = max(1 + float(rate.replace("%", "")) / 100, 0.3)
        except (TypeError, ValueError):
            speed = 1.0
        byte_ratio = len(text.encode("utf-8")) / max(len(ref_text.encode("utf-8")), 1)
        predicted_ratio = ref_ms * byte_ratio / speed / slot_ms
        return round(predicted_ratio * max(len(text), 1), 3)

    def _preflight_indices(self, limit: int) -> List[int]:
        pending = [
            idx for idx, item in enumerate(self.queue_tts)
            if item.get("text", "").strip() and not vail_file(item.get("filename"))
        ]
        if len(pending) <= limit:
            return pending

        chosen = []
        by_short = sorted(pending, key=lambda idx: self._preflight_risk(self.queue_tts[idx]))
        by_long = sorted(
            pending, key=lambda idx: self._preflight_risk(self.queue_tts[idx]), reverse=True
        )
        by_compute = sorted(
            pending,
            key=lambda idx: self._preflight_compute_risk(self.queue_tts[idx]),
            reverse=True,
        )

        def add(idx):
            if idx not in chosen:
                chosen.append(idx)

        # The row most likely to cause a Metal peak is mandatory.  Previously,
        # many speaker references could consume the whole sample budget before
        # this pathological row was considered.
        add(by_compute[0])

        # 先覆盖不同声纹参考，并优先为每位说话人选择较长句，以便预飞
        # 通过后立刻把它升级成中文锚点供后续全片使用。
        by_reference = {}
        for idx in pending:
            item = self.queue_tts[idx]
            ref = str(item.get("cluster_ref") or self.safe_ref_wav or item.get("ref_wav") or "")
            if ref:
                old = by_reference.get(ref)
                if old is None or self._preflight_risk(item) > self._preflight_risk(
                        self.queue_tts[old]):
                    by_reference[ref] = idx
        speaker_budget = max(0, limit - 2)
        for idx in sorted(
                by_reference.values(),
                key=lambda value: self._preflight_compute_risk(self.queue_tts[value]),
                reverse=True)[:speaker_budget]:
            add(idx)

        # 短文本最容易续写英文参考，长文本最容易触发 MPS 峰值。
        add(by_short[0])
        add(by_long[0])
        for fraction in (0.25, 0.5, 0.75):
            add(pending[int((len(pending) - 1) * fraction)])
        for idx in pending:
            add(idx)
            if len(chosen) >= limit:
                break
        return chosen[:limit]

    @staticmethod
    def _has_pathological_repetition(transcript: str) -> bool:
        from videotrans.dub.chinese_quality import has_pathological_repetition
        return has_pathological_repetition(transcript)

    def _run_preflight(self) -> None:
        limit = max(1, min(int(settings.get("f5tts_preflight_samples", 5) or 5), 8))
        indices = self._preflight_indices(limit)
        if not indices:
            return
        temp_dir = Path(ROOT_DIR) / "tmp" / f"f5-preflight-{self.uuid}"
        shutil.rmtree(temp_dir, ignore_errors=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        samples = []
        self.signal(text=f"F5-TTS 长视频预飞：先验证 {len(indices)} 段，通过后才跑全片")
        started = time.monotonic()
        try:
            for pos, idx in enumerate(indices, 1):
                original = self.queue_tts[idx]
                sample = copy.deepcopy(original)
                sample["filename"] = str(temp_dir / f"sample-{idx}.wav")
                error = self._item_task(sample, idx)
                if error or not vail_file(sample["filename"]):
                    raise DubbingSrtError(
                        f"F5-TTS 预飞第 {idx + 1} 段合成失败，已在全片生成前停止："
                        f"{str(error)[:180]}"
                    )
                samples.append((idx, original, sample))
                self.signal(text=self._eta_text(
                    "F5-TTS 预飞合成", pos, len(indices), time.monotonic() - started
                ))

            if getattr(self, "_low_memory_profile", False):
                self._stop_local_service()
            self.signal(text="F5-TTS 预飞合成完成，正在回读内容与重复度")
            model = self._new_validator()
            failures = []
            sample_transcripts = {}
            try:
                for idx, original, sample in samples:
                    transcript = self._transcribe_one_for_validation(model, sample["filename"])
                    sample_transcripts[idx] = transcript
                    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", transcript))
                    if (
                        cjk_count < 2
                        or self._has_unexpected_english(original["text"], transcript)
                        or self._has_pathological_repetition(transcript)
                    ):
                        failures.append((idx, transcript))
            finally:
                model = None
                gc.collect()

            # 预飞参数与正式任务完全相同，通过的音频直接复用。
            for _idx, original, sample in samples:
                target = Path(original["filename"])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(sample["filename"], target)
            if any(original.get("dub_unit_id") for _, original, _ in samples):
                from videotrans.dub.quality_manifest import QualityManifest
                manifest = QualityManifest.for_queue(self.queue_tts)
                failed_indices = {idx for idx, _transcript in failures}
                for idx, original, _sample in samples:
                    transcript = sample_transcripts.get(idx, "")
                    passed = idx not in failed_indices
                    manifest.record(
                        original,
                        index=idx,
                        validator_backend=self.VALIDATOR_BACKEND,
                        validator_model=self.VALIDATOR_MODEL,
                        rules_version=self.QUALITY_RULES_VERSION,
                        passed=passed,
                        transcript=transcript,
                        hard_failures=(
                            [] if passed else
                            self._hard_quality_failures(original["text"], transcript)
                        ),
                        disposition="passed" if passed else "retryable",
                        metrics=self._quality_metrics(original["text"], transcript),
                        save=False,
                    )
                manifest.save()
            anchor_count = self._bootstrap_chinese_anchors(
                samples, sample_transcripts, failures)
            if failures:
                details = "；".join(
                    f"第 {idx + 1} 段：{transcript[:70]}"
                    for idx, transcript in failures[:3]
                )
                self.signal(text=(
                    f"F5-TTS 预飞发现 {len(failures)}/{len(samples)} 段内容异常，"
                    f"已标记并继续；稍后只返工这些片段。{details}"
                ))
            else:
                self.signal(text=f"F5-TTS 预飞通过 {len(samples)}/{len(samples)}，开始全片配音")
            if anchor_count:
                self.signal(text=(
                    f"F5-TTS 已建立 {anchor_count} 个中文音色锚点；"
                    "后续片段不再持续使用英文参考条件"
                ))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _bootstrap_chinese_anchors(self, samples, transcripts, failures) -> int:
        """Promote clean preflight outputs to per-speaker Chinese references."""
        failed_indices = {idx for idx, _transcript in failures}
        candidates = {}
        for idx, original, _sample in samples:
            if idx in failed_indices or not vail_file(original.get("filename")):
                continue
            transcript = str(transcripts.get(idx) or "")
            if self._hard_quality_failures(original.get("text", ""), transcript):
                continue
            try:
                duration_ms = len(AudioSegment.from_file(original["filename"]))
            except Exception:
                continue
            if not 2500 <= duration_ms <= self.MAX_REF_AUDIO_MS:
                continue
            key = self._speaker_key(original)
            score = abs(duration_ms - 6500)
            candidates.setdefault(key, []).append((
                score,
                self._anchor_entry(
                    original["filename"], original.get("text", ""), duration_ms),
            ))
        if not candidates:
            return 0
        for item in self.queue_tts:
            pool = candidates.get(self._speaker_key(item)) or []
            bank = [entry for _score, entry in sorted(pool, key=lambda row: row[0])[:3]]
            anchor = self._choose_chinese_anchor(bank, item)
            if anchor and item.get("filename") != anchor["wav"]:
                item["chinese_anchor_bank"] = bank
                item["chinese_anchor_ref"] = anchor["wav"]
                item["chinese_anchor_text"] = anchor["text"]
        self.preflight_chinese_anchors = {
            key: [entry for _score, entry in sorted(value, key=lambda row: row[0])[:3]]
            for key, value in candidates.items()
        }
        return len(candidates)

    def _confirm_batch_failures(self, model, failed, transcripts):
        """Individually re-read batch-gate candidates before expensive redubbing.

        A long concatenated validation file is efficient but Whisper timestamps can
        drift across silence boundaries.  It is therefore only an initial screen;
        no clip is regenerated until a standalone transcription confirms leakage.
        """
        if not failed:
            return []
        eligible = sum(
            1 for item in self.queue_tts
            if item.get("text", "").strip() and vail_file(item.get("filename"))
        )
        ratio = len(failed) / max(eligible, 1)
        if ratio >= self.MASS_GATE_FAILURE_RATIO:
            message = (
                f"批量门禁初筛标记 {len(failed)}/{eligible} 段，比例异常偏高，"
                "正在逐段复核，确认前不会重配"
            )
            logger.warning(message)
        else:
            message = f"批量门禁发现 {len(failed)} 段可疑，正在逐段复核"
        self.signal(text=message)

        confirmed = []
        started = time.monotonic()
        for pos, (idx, item, batch_transcript) in enumerate(failed, 1):
            try:
                transcript = self._transcribe_one_for_validation(model, item["filename"])
            except Exception as error:
                # Quality-first: inability to verify must not silently clear a
                # genuinely leaked clip.
                logger.warning("第 %s 段逐段语言复核失败，保留为可疑: %s", idx + 1, error)
                transcript = batch_transcript
                confirmed.append((idx, item, transcript))
            else:
                transcripts[idx] = transcript
                if self._hard_quality_failures(item["text"], transcript):
                    confirmed.append((idx, item, transcript))
            if pos == 1 or pos == len(failed) or pos % 5 == 0:
                self.signal(text=self._eta_text(
                    "F5-TTS 门禁复核", pos, len(failed), time.monotonic() - started
                ))

        logger.info(
            "F5-TTS 门禁逐段复核完成：批量可疑=%s，确认泄漏=%s",
            len(failed), len(confirmed),
        )
        self.signal(text=f"门禁逐段复核完成：确认 {len(confirmed)}/{len(failed)} 段需要重配")
        return confirmed

    def _is_systemic_language_failure(self, failed) -> bool:
        eligible = sum(
            1 for item in self.queue_tts
            if item.get("text", "").strip() and vail_file(item.get("filename"))
        )
        return (
            len(failed) >= self.MASS_GATE_MIN_FAILURES
            and len(failed) / max(eligible, 1) >= self.MASS_GATE_FAILURE_RATIO
        )

    @staticmethod
    def _defer_clip_failures() -> bool:
        return str(settings.get("f5tts_defer_clip_failures", True)).lower() != "false"

    def _write_leak_sidecar(self, failed) -> None:
        """BaseTTS 对 queue_tts 做了 deepcopy，直接改条目传不回调用方。
        把 {文件名: 转写} 写到配音目录的 lang_leak.json，由 trans_create 合并回真正的队列。"""
        try:
            import json
            first = next((it for it in self.queue_tts if it.get("filename")), None)
            if not first:
                return
            sidecar = Path(first["filename"]).parent / "lang_leak.json"
            marks = {}
            if sidecar.is_file():
                try:
                    loaded = json.loads(sidecar.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        marks.update(loaded)
                except (OSError, json.JSONDecodeError):
                    pass
            # This queue is authoritative only for its own filenames. Preserve
            # marks belonging to other concurrently repairable clips.
            for item in self.queue_tts:
                name = Path(item.get("filename") or "").name
                if name:
                    marks.pop(name, None)
            for _, item, transcript in failed:
                name = Path(item.get("filename") or "").name
                if name:
                    marks[name] = transcript[:120]
            from videotrans.dub.store import atomic_write_json
            if marks:
                atomic_write_json(sidecar, marks)
            else:
                sidecar.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"写配音泄漏标记文件失败,忽略: {e}")

    @classmethod
    def _is_service_error(cls, error) -> bool:
        value = str(error or "").lower()
        return any(marker.lower() in value for marker in cls.SERVICE_ERROR_MARKERS)

    @staticmethod
    def _is_low_memory_apple_silicon() -> bool:
        """Return true for unified-memory Macs where model overlap causes swap."""
        if platform.system() != "Darwin" or platform.machine().lower() not in {"arm64", "aarch64"}:
            return False
        try:
            total_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (AttributeError, OSError, TypeError, ValueError):
            return False
        # 18 GiB includes nominal 16 GB machines while leaving 24 GB+ Macs on
        # the normal path.
        return total_bytes <= 18 * 1024 ** 3

    @staticmethod
    def _release_memory_pressure() -> None:
        """Return unused Python, Metal and native heap pages before F5 launch."""
        gc.collect()
        try:
            import torch
            if hasattr(torch, "mps") and torch.backends.mps.is_available():
                torch.mps.empty_cache()
        except Exception:
            pass
        if platform.system() == "Darwin":
            try:
                import ctypes
                libc = ctypes.CDLL(None)
                relief = libc.malloc_zone_pressure_relief
                relief.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
                relief.restype = ctypes.c_size_t
                relieved = int(relief(None, 0))
                logger.info("F5-TTS 启动前已向 macOS 归还 %.1f MB 原生堆内存", relieved / 1024 ** 2)
            except Exception as error:
                logger.debug("macOS 原生堆内存释放不可用: %s", error)

    def _is_managed_local_service(self) -> bool:
        parsed = urlparse(getattr(self, "api_url", ""))
        return (
            parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and (parsed.port or 7860) == 7860
        )

    @staticmethod
    def _local_service_script(filename: str):
        scripts = (
            Path(ROOT_DIR).parent / "f5-tts-service" / filename,
            Path.home() / "Library/Application Support/pyVideoTrans/f5-tts-service" / filename,
        )
        return next((path for path in scripts if path.is_file()), None)

    def _local_health_url(self) -> str:
        parsed = urlparse(self.api_url)
        return f"{parsed.scheme or 'http'}://{parsed.hostname}:{parsed.port or 7860}/gradio_api/info"

    @staticmethod
    def _local_service_environment(script: Path) -> dict:
        """Isolate the service cache from HF variables inherited from the app.

        The main application deliberately points ``HF_HUB_CACHE`` at its ASR
        model directory. Merely setting ``HF_HOME`` in the shell launcher does
        not override that more specific variable, which made a complete local
        Vocos cache appear missing while offline mode was enabled.
        """
        env = os.environ.copy()
        service_cache = script.parent / "cache"
        hf_home = service_cache / "huggingface"
        hub = hf_home / "hub"
        env.update({
            "HF_HOME": str(hf_home),
            "HF_HUB_CACHE": str(hub),
            "HUGGINGFACE_HUB_CACHE": str(hub),
            "XDG_CACHE_HOME": str(service_cache),
            "CACHED_PATH_CACHE_ROOT": str(hub),
        })
        return env

    @staticmethod
    def _summarize_local_service_error(text: str) -> str:
        text = str(text or "")
        lowered = text.lower()
        if "vocos-mel-24khz" in lowered and (
                "localentrynotfounderror" in lowered
                or "offlinemodeisenabled" in lowered):
            return "Vocos 声码器缓存不可用或启动环境指向了错误的模型目录"
        if "mps backend out of memory" in lowered or "out of memory" in lowered:
            return "Metal 统一内存不足"
        if "address already in use" in lowered:
            return "本地端口 7860 已被其他程序占用"
        missing = re.findall(r"(?:No module named|ModuleNotFoundError:)\s*['\"]?([^'\"\n]+)", text)
        if missing:
            return f"F5-TTS 环境缺少依赖：{missing[-1].strip()}"
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for line in reversed(lines):
            if not line.startswith(("File ", "Traceback", "warnings.warn")):
                return line[:300]
        return "未知启动错误"

    @staticmethod
    def _new_log_text(log_file: Path, start_size: int) -> str:
        try:
            with log_file.open("rb") as stream:
                stream.seek(min(max(int(start_size), 0), log_file.stat().st_size))
                return stream.read()[-12000:].decode("utf-8", errors="replace")
        except OSError:
            return ""

    def _service_start_error(self, context: str) -> DubbingSrtError:
        detail = str(getattr(self, "_local_service_error", "") or "").strip()
        return DubbingSrtError(f"{context}：{detail or '请查看 F5-TTS 日志'}。")

    def _wait_for_f5_headroom(self, timeout_s: int = 45) -> bool:
        """Wait briefly for a disposable validator process to release memory."""
        from videotrans.util.resource_governor import resource_snapshot

        deadline = time.monotonic() + max(int(timeout_s), 0)
        announced = False
        while True:
            snapshot = resource_snapshot()
            if (
                    (not snapshot.available_mb or snapshot.available_mb >= 3072)
                    and snapshot.memory_percent < 86
            ):
                return True
            if time.monotonic() >= deadline:
                logger.warning(
                    "F5-TTS 返工前内存未恢复: available=%sMB memory=%s%% swap=%sMB",
                    snapshot.available_mb, snapshot.memory_percent, snapshot.swap_used_mb,
                )
                return False
            if not announced:
                announced = True
                self.signal(text=(
                    f"强模型已退出，正在等待系统回收内存后再局部返工…"
                    f"（当前可用 {snapshot.available_mb} MB）"
                ))
            self._release_memory_pressure()
            time.sleep(1)

    @staticmethod
    def _quality_retry_path(filename: str, retry_index: int) -> Path:
        path = Path(filename)
        return path.with_name(
            f".{path.stem}.quality-retry-{int(retry_index) + 1}{path.suffix or '.wav'}")

    def _local_service_is_ready(self) -> bool:
        if not self._is_managed_local_service():
            return False
        try:
            with urlopen(self._local_health_url(), timeout=2) as response:
                return response.status < 500
        except Exception:
            return False

    def _start_local_service(self, recovery: bool = False) -> bool:
        if not self._is_managed_local_service():
            return False
        if self._local_service_is_ready():
            self.reset_thread_client()
            return True
        script = self._local_service_script("start_service.sh")
        self._local_service_error = ""
        if script is None:
            logger.error("F5-TTS 本地服务启动失败：未找到 start_service.sh")
            self._local_service_error = "未找到本地服务启动脚本"
            return False

        log_file = script.parent / "logs" / "f5-tts.log"
        try:
            log_start_size = log_file.stat().st_size
        except OSError:
            log_start_size = 0

        self.reset_thread_client()
        if recovery:
            self.signal(text="F5-TTS 本地服务内存不足后已退出，正在自动恢复…")
            logger.warning("F5-TTS 本地服务已断开，启动自动恢复: %s", script)
        else:
            self.signal(text="正在按需启动 F5-TTS 本地服务…")
            logger.info("按需启动 F5-TTS 本地服务: %s", script)
        try:
            process = subprocess.Popen(
                [str(script)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, start_new_session=True,
                env=self._local_service_environment(script),
            )
        except Exception as error:
            logger.error("F5-TTS 本地服务启动失败: %s", error)
            self._local_service_error = str(error)[:300]
            return False

        for waited_s in range(240):
            if self._exit():
                return False
            if self._local_service_is_ready():
                self.reset_thread_client()
                if recovery:
                    self.signal(text="F5-TTS 本地服务已恢复，仅继续重配失败片段")
                logger.info("F5-TTS 本地服务已就绪")
                return True
            if process.poll() is not None:
                output = ""
                try:
                    output = (process.communicate(timeout=1)[0] or "").strip()
                except Exception:
                    pass
                log_text = self._new_log_text(log_file, log_start_size)
                self._local_service_error = self._summarize_local_service_error(
                    f"{output}\n{log_text}")
                logger.error(
                    "F5-TTS 启动脚本已退出，错误代码: %s，原因: %s，输出: %s",
                    process.returncode, self._local_service_error,
                    output or "<无输出，子进程可能被系统终止>",
                )
                self.signal(text=f"F5-TTS 启动失败：{self._local_service_error}")
                return False
            if waited_s and waited_s % 15 == 0:
                self.signal(text=(
                    f"F5-TTS 正在加载模型｜已用 {waited_s} 秒｜"
                    "首次冷启动通常需要 1–2 分钟"
                ))
            time.sleep(1)
        logger.error("F5-TTS 本地服务启动超时: %s", self._local_health_url())
        self._local_service_error = "模型加载超过 240 秒"
        return False

    def _stop_local_service(self) -> bool:
        """Release the bundled F5 process and its Metal allocations."""
        if not self._is_managed_local_service():
            return False
        script = self._local_service_script("停止F5-TTS.command")
        if script is None:
            logger.warning("F5-TTS 本地服务停止失败：未找到停止脚本")
            return False
        self.reset_thread_client()
        try:
            subprocess.run(
                [str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=15, check=False,
            )
        except Exception as error:
            logger.warning("F5-TTS 本地服务停止失败: %s", error)
            return False
        for _ in range(20):
            if not self._local_service_is_ready():
                gc.collect()
                logger.info("F5-TTS 本地服务已停止并释放模型")
                return True
            time.sleep(0.25)
        logger.warning("F5-TTS 停止后端口仍在响应")
        return False

    def _recover_local_service(self) -> bool:
        """Restart the bundled localhost F5 service after a crash.

        Remote/custom endpoints are never started or modified here.  The launcher
        script owns model/environment setup, so recovery follows the same path as
        an ordinary app launch.
        """
        if not self._is_managed_local_service():
            return False
        # An OOM'd MPS process may keep several GB allocated while its HTTP
        # endpoint still exists.  A real restart is required before retrying.
        self._stop_local_service()
        self._release_memory_pressure()
        return self._start_local_service(recovery=True)

    def _wait_for_synthesis_resources(self, idx: int) -> bool:
        """Do not submit another Metal job while this run is actively swapping."""
        if not self._is_managed_local_service():
            return True
        supervisor = self._synthesis_supervisor()
        decision = supervisor.resource_decision()
        if decision.allow:
            if getattr(self, "_resource_recycle_pending", False):
                if not self._start_local_service(recovery=True):
                    return False
                self._resource_recycle_pending = False
            return True

        if self._local_service_is_ready():
            self.signal(text=(
                f"F5-TTS 第 {idx + 1} 段提交前检测到资源压力：{decision.reason}；"
                "正在释放模型，已生成片段不会丢失"
            ))
            self._stop_local_service()
            supervisor.mark_recycle()
        self._resource_recycle_pending = True
        deadline = time.monotonic() + max(
            int(settings.get("f5tts_resource_wait_s", 60) or 60), 1)
        last_signal = 0.0
        while time.monotonic() < deadline and not self._exit():
            self._release_memory_pressure()
            decision = supervisor.resource_decision()
            if decision.allow:
                if self._start_local_service(recovery=True):
                    self._resource_recycle_pending = False
                    return True
                return False
            now = time.monotonic()
            if now - last_signal >= 10:
                last_signal = now
                self.signal(text=(
                    f"F5-TTS 正在等待系统回收内存｜{decision.reason}｜"
                    "可安全停止，已完成片段已保存"
                ))
            time.sleep(1)
        self._service_circuit_error = (
            f"F5-TTS 资源保护已暂停新片段：{decision.reason}。"
            "请关闭高内存应用后继续，已完成片段会自动复用。"
        )
        return False

    def _post_clip_resource_guard(self, idx: int) -> None:
        if not self._is_managed_local_service():
            return
        decision = self._synthesis_supervisor().resource_decision()
        if decision.allow or not self._local_service_is_ready():
            return
        self.signal(text=(
            f"F5-TTS 第 {idx + 1} 段完成后触发资源保护：{decision.reason}；"
            "先释放模型，下段自动恢复"
        ))
        self._stop_local_service()
        self._synthesis_supervisor().mark_recycle()
        self._resource_recycle_pending = True

    def _persist_synthesis_supervisor(self, data_item) -> None:
        try:
            from videotrans.dub.store import atomic_write_json
            filename = data_item.get("filename") if isinstance(data_item, dict) else None
            if filename:
                atomic_write_json(
                    Path(filename).parent / "synthesis_supervisor.json",
                    self._synthesis_supervisor().diagnostics(),
                )
        except Exception as error:
            logger.debug("保存合成监督器诊断失败，忽略: %s", error)

    def _item_task(self, data_item, idx=-1):
        """Retry one local infrastructure failure immediately on a fresh model.

        Gradio hides the server traceback by default, so an MPS OOM reaches the
        client as a generic "upstream app" exception.  Waiting until the whole
        retry batch finishes leaves several deleted clips behind.  Restart and
        retry the affected clip at the point of failure instead.
        """
        if getattr(self, "_service_circuit_error", ""):
            return self._service_circuit_error
        if not self._wait_for_synthesis_resources(idx):
            return str(
                getattr(self, "_service_circuit_error", "")
                or getattr(self, "_local_service_error", "")
                or "F5-TTS 资源保护未放行"
            )
        error = super()._item_task(data_item, idx)
        if (
            not error
            or self._exit()
            or not self._is_managed_local_service()
            or not self._is_service_error(error)
        ):
            if not error:
                self._post_clip_resource_guard(idx)
            return error
        self.signal(text=f"F5-TTS 第 {idx + 1} 段后端内存异常，正在隔离重启并只重试该段")
        logger.warning("F5-TTS 第 %s 段服务异常，立即重启后单段重试: %s", idx + 1, error)
        filename = data_item.get("filename")
        if filename:
            Path(filename).unlink(missing_ok=True)
        if not self._recover_local_service():
            self._service_circuit_error = str(error)
            return error
        error = super()._item_task(data_item, idx)
        if not error:
            self._post_clip_resource_guard(idx)
        return error

    def _retry_service_failures(self, failures, retry_index):
        """Retry only infrastructure-failed items after one service recovery."""
        if not failures or not self._recover_local_service():
            return failures
        remaining = []
        for idx, item, old_error in failures:
            item['lang_leak_retry'] = retry_index + 1
            error = self._item_task(item, idx)
            item.pop('lang_leak_retry', None)
            if error or not vail_file(item["filename"]):
                remaining.append((idx, item, str(error or old_error)))
        return remaining

    def _verify_chinese_outputs(self) -> None:
        from videotrans.dub.quality_manifest import QualityManifest

        persist_quality = any(item.get("dub_unit_id") for item in self.queue_tts)
        manifest = QualityManifest.for_queue(self.queue_tts) if persist_quality else None
        isolated_validation = (
            persist_quality
            and str(settings.get("f5tts_isolate_validator", True)).lower() != "false"
        )
        model = None
        validator_backend, validator_model = (
            self._validator_identity()
            if isolated_validation else
            (self.VALIDATOR_BACKEND, self.VALIDATOR_MODEL)
        )
        validator_args = {
            "validator_backend": validator_backend,
            "validator_model": validator_model,
            "rules_version": self.QUALITY_RULES_VERSION,
        }

        def record(idx, item, transcript, passed, *, save=False):
            if manifest is None:
                return
            failures = [] if passed else self._hard_quality_failures(item["text"], transcript)
            manifest.record(
                item,
                index=idx,
                passed=passed,
                transcript=transcript,
                hard_failures=failures,
                metrics=self._quality_metrics(item["text"], transcript),
                save=save,
                **validator_args,
            )

        def defer_failed(failures, reason: str, *, attempts=0):
            for idx, item, transcript in failures:
                item["lang_leak"] = str(transcript or "")[:120]
                item["quality_status"] = "needs_review"
                if manifest is not None:
                    manifest.set_disposition(
                        item, "needs_review", index=idx, attempts=attempts,
                        reason=reason, save=False)
            if manifest is not None:
                manifest.save()
            self._write_leak_sidecar(failures)

        try:
            self.signal(text="正在增量核对 F5-TTS 中文配音内容…")
            transcripts = {}
            failed = []
            pending = []
            cache_hits = 0
            for idx, item in enumerate(self.queue_tts):
                if not item.get("text", "").strip() or not vail_file(item.get("filename")):
                    continue
                cached = None
                if manifest is not None and getattr(self, "use_cache", True):
                    cached = manifest.lookup(item, index=idx, **validator_args)
                if cached:
                    cache_hits += 1
                    transcript = str(cached.get("transcript") or "")
                    transcripts[idx] = transcript
                    if not cached.get("passed"):
                        failed.append((idx, item, transcript))
                    continue
                pending.append(idx)

            if cache_hits:
                self.signal(text=f"质量记录复用 {cache_hits} 段，仅核对新增或变化片段")

            if pending:
                backend_label = (
                    "MLX/Metal large-v3-turbo"
                    if validator_backend == "mlx-whisper-mps" else
                    "CPU large-v3-turbo"
                )
                self.signal(text=f"质量核验后端：{backend_label}，待核对 {len(pending)} 段")
                if not isolated_validation:
                    model = self._new_validator()
                from videotrans.util.resource_governor import runtime_limits
                configured_batch = max(
                    4, min(int(settings.get("f5tts_validation_batch_size", 24) or 24), 40)
                )
                limits = runtime_limits(
                    mode=settings.get("resource_mode", "auto"),
                    validation_batch_size=configured_batch,
                )
                # The isolated worker already transcribes files one by one and
                # never concatenates audio. Loading large-v3-turbo once for the
                # whole pending set is both cooler and lower-memory than spawning
                # a fresh model process for every micro-batch.
                batch_size = len(pending) if isolated_validation else limits.validation_batch_size
                if limits.pressure != "normal":
                    pressure_labels = {
                        "elevated": "偏高", "high": "较高", "critical": "严重"
                    }
                    self.signal(text=(
                        f"系统资源压力{pressure_labels.get(limits.pressure, limits.pressure)}，"
                        f"已自动降低核验线程；逐文件串行检查 {len(pending)} 段，"
                        "强模型只加载一次，配音质量不变"
                    ))
                started = time.monotonic()
                for offset in range(0, len(pending), batch_size):
                    indices = pending[offset:offset + batch_size]
                    if isolated_validation:
                        screened, used_backend = self._transcribe_isolated_with_fallback(
                            indices, backend=validator_backend
                        )
                        if used_backend != validator_backend:
                            validator_backend = used_backend
                            validator_args["validator_backend"] = used_backend
                    else:
                        try:
                            screened = self._transcribe_batch_for_validation(model, indices)
                        except TypeError:
                            # Compatibility for subclasses implementing the former
                            # one-argument hook.
                            screened = self._transcribe_batch_for_validation(model)
                    transcripts.update(screened)
                    suspicious = []
                    for idx in indices:
                        item = self.queue_tts[idx]
                        transcript = transcripts.get(idx, "")
                        if self._hard_quality_failures(item["text"], transcript):
                            suspicious.append((idx, item, transcript))

                    # Micro-batches are only a high-recall screen. Standalone
                    # confirmation prevents timestamp drift causing rework.
                    confirmed = (
                        suspicious if isolated_validation else
                        self._confirm_batch_failures(model, suspicious, transcripts)
                    )
                    confirmed_indices = {idx for idx, _, _ in confirmed}
                    failed.extend(confirmed)
                    for idx in indices:
                        item = self.queue_tts[idx]
                        transcript = transcripts.get(idx, "")
                        record(idx, item, transcript, idx not in confirmed_indices)
                    if manifest is not None:
                        manifest.save()
                    completed = min(offset + len(indices), len(pending))
                    self.signal(text=self._eta_text(
                        "F5-TTS 增量质量核对", completed, len(pending),
                        time.monotonic() - started,
                    ))

            if not pending and not failed:
                logger.info("F5-TTS 全部片段命中已通过的质量记录: %s", cache_hits)
                self.signal(text=f"F5-TTS 配音内容检查通过（复用 {cache_hits} 段质量记录）")
                return

            if self._is_systemic_language_failure(failed):
                for idx, item, transcript in failed:
                    item["lang_leak"] = transcript[:120]
                    item["quality_status"] = "needs_reference"
                    if manifest is not None:
                        manifest.set_disposition(
                            item, "needs_reference", index=idx,
                            reason="systemic content failure", save=False)
                if manifest is not None:
                    manifest.save()
                self._write_leak_sidecar(failed)
                message = (
                    f"F5-TTS 智能熔断：逐段复核确认 {len(failed)} 段存在内容异常，"
                    "已停止大规模自动返工，避免继续浪费数小时。"
                    "已保留全部成功片段，并把异常片段送入工作台。"
                )
                logger.error(message)
                self.signal(text=message)
                if self._defer_clip_failures():
                    return
                raise DubbingSrtError(message)

            if failed and str(settings.get("f5tts_chinese_anchor", True)).lower() != "false":
                self._assign_chinese_anchors(failed, transcripts)

            for retry_index in range(self.MAX_LANGUAGE_RETRIES):
                if not failed:
                    break
                logger.warning(
                    "检测到 %s 段 F5-TTS 配音内容异常，开始第 %s 次重生成",
                    len(failed), retry_index + 1
                )
                self.signal(
                    text=f"检测到 {len(failed)} 段内容异常，正在自动重配 "
                         f"({retry_index + 1}/{self.MAX_LANGUAGE_RETRIES})…"
                )
                # CTranslate2/Whisper and F5 both use unified memory on Apple
                # Silicon.  Keeping large-v3-turbo resident while invoking F5
                # pushed the service over the MPS high-water mark.  Release the
                # validator during synthesis, then reload it for verification.
                model = None
                gc.collect()
                regenerated = []
                service_failed = []
                service_errors = []
                low_memory = bool(getattr(self, "_low_memory_profile", False))
                serialize_models = bool(low_memory or validator_backend == "mlx-whisper-mps")
                if serialize_models and (
                        not self._wait_for_f5_headroom()
                        or not self._start_local_service()
                ):
                    detail = str(getattr(self, "_local_service_error", "") or "")
                    message = (
                        f"系统资源尚未恢复，已跳过本轮自动返工并保留 {len(failed)} 个"
                        f"异常片段，稍后可在工作台只重配这些片段。{detail}"
                    )
                    logger.warning(message)
                    self.signal(text=message)
                    defer_failed(
                        failed, "automatic repair deferred: insufficient resources",
                        attempts=retry_index)
                    if self._defer_clip_failures():
                        return
                    raise self._service_start_error("F5-TTS 本地服务在质量复核重配前启动失败")
                try:
                    retry_started = time.monotonic()
                    retry_total = len(failed)
                    for retry_pos, (idx, item, old_transcript) in enumerate(failed, 1):
                        candidate_path = self._quality_retry_path(
                            item["filename"], retry_index)
                        candidate_path.unlink(missing_ok=True)
                        candidate_item = copy.deepcopy(item)
                        candidate_item["filename"] = str(candidate_path)
                        # 标记重试轮次：_run 据此偏移种子（第 2 轮起换备选参考），
                        # 否则固定种子下重新生成的结果与上次完全相同
                        candidate_item['lang_leak_retry'] = retry_index + 1
                        error = self._item_task(candidate_item, idx)
                        if error or not vail_file(candidate_path):
                            candidate_path.unlink(missing_ok=True)
                            service_failed.append((idx, item, old_transcript))
                            service_errors.append((idx, str(error or "未生成候选音频")))
                        else:
                            regenerated.append((idx, item, old_transcript, candidate_path))
                        self.signal(text=self._eta_text(
                            f"F5-TTS 质量返工 {retry_index + 1}/{self.MAX_LANGUAGE_RETRIES}",
                            retry_pos, retry_total, time.monotonic() - retry_started,
                        ))
                finally:
                    if serialize_models:
                        # Reload Whisper only after Metal allocations are gone.
                        self._stop_local_service()

                if service_errors:
                    details = "；".join(
                        f"第 {idx + 1} 段：{error[:100]}"
                        for idx, error in service_errors[:5]
                    )
                    message = (
                        f"F5-TTS 有 {len(service_errors)} 个返工候选生成失败，"
                        "原音频未删除；"
                        f"{details}"
                    )
                    logger.warning(message)
                    self.signal(text=message)

                retry_failed = []
                regenerated_indices = [idx for idx, _, _, _ in regenerated]
                if isolated_validation and regenerated:
                    original_paths = {
                        idx: item["filename"] for idx, item, _, _ in regenerated
                    }
                    try:
                        for idx, item, _, candidate_path in regenerated:
                            item["filename"] = str(candidate_path)
                        isolated_transcripts, used_backend = (
                            self._transcribe_isolated_with_fallback(
                                regenerated_indices, backend=validator_backend
                            )
                        )
                    finally:
                        for idx, item, _, _ in regenerated:
                            item["filename"] = original_paths[idx]
                    if used_backend != validator_backend:
                        validator_backend = used_backend
                        validator_args["validator_backend"] = used_backend
                else:
                    isolated_transcripts = {}
                if not isolated_validation and regenerated:
                    model = self._new_validator()
                for idx, item, old_transcript, candidate_path in regenerated:
                    transcript = (
                        isolated_transcripts.get(idx, "") if isolated_validation else
                        self._transcribe_one_for_validation(model, str(candidate_path))
                    )
                    failures = self._hard_quality_failures(item["text"], transcript)
                    if failures:
                        candidate_path.unlink(missing_ok=True)
                        retry_failed.append((idx, item, old_transcript))
                    else:
                        os.replace(candidate_path, item["filename"])
                        record(idx, item, transcript, True)
                        item.pop("lang_leak", None)
                if manifest is not None:
                    manifest.save()
                failed = retry_failed + service_failed

            if failed:
                defer_failed(
                    failed, "automatic retries exhausted",
                    attempts=self.MAX_LANGUAGE_RETRIES)
                details = "；".join(
                    f"第 {idx + 1} 段：{transcript[:80]}"
                    for idx, _, transcript in failed[:5]
                )
                message = (
                    f"F5-TTS 质量门禁未通过：重试后仍有 {len(failed)} 段内容异常，"
                    f"已保留成功结果，异常片段等待局部返工。{details}"
                )
                logger.error(message)
                self.signal(text=message)
                if self._defer_clip_failures():
                    return
                if str(settings.get("f5tts_strict_language_gate", True)).lower() != "false":
                    raise DubbingSrtError(message)
                logger.warning("严格语言门禁已被关闭，保留泄漏标记并继续流程")
            else:
                logger.debug("F5-TTS 增量内容检查通过")
                self.signal(text="F5-TTS 配音内容检查通过")
        finally:
            if manifest is not None:
                manifest.save()
            model = None
            gc.collect()

    def _run(self, data_item: Union[Dict, List, None], idx: int = -1) -> Union[str, None]:
        ref_wav,ref_text=self.get_ref_wav(data_item)
        used_chinese_anchor = False
        if data_item.get("role") == "clone":
            retry_no = int(data_item.get('lang_leak_retry') or 0)
            resume_anchor = getattr(self, "resume_chinese_anchors", {}).get(
                self._speaker_key(data_item)
            )
            item_bank = data_item.get("chinese_anchor_bank") or []
            resume_bank = getattr(self, "resume_chinese_anchor_banks", {}).get(
                self._speaker_key(data_item), [])
            selected_bank_anchor = self._choose_chinese_anchor(
                item_bank or resume_bank, data_item, retry_no=retry_no)
            if selected_bank_anchor:
                ref_wav = selected_bank_anchor["wav"]
                ref_text = selected_bank_anchor["text"]
                used_chinese_anchor = True
            elif data_item.get("chinese_anchor_ref"):
                # 泄漏重试专用：用已验收的同说话人中文成品约束生成语言。
                ref_wav = data_item["chinese_anchor_ref"]
                ref_text = data_item.get("chinese_anchor_text") or ref_text
                used_chinese_anchor = True
            elif resume_anchor:
                # 中断恢复时，对尚未生成的片段优先使用已验收中文
                # 成品；按说话人簇匹配，避免主持人与嘉宾互换音色。
                ref_wav, ref_text = resume_anchor
                used_chinese_anchor = True
            elif (not data_item.get("cluster_ref")
                  and getattr(self, "resume_chinese_anchor_ref", None)):
                # 兼容没有可靠声纹分簇的单说话人项目。
                ref_wav = self.resume_chinese_anchor_ref
                ref_text = self.resume_chinese_anchor_text or ref_text
                used_chinese_anchor = True
            elif data_item.get('cluster_ref'):
                # 多说话人：该行所属说话人簇的参考（各说各的音色）
                ref_wav, ref_text = data_item['cluster_ref'], data_item.get('cluster_ref_text') or ref_text
            elif self.safe_ref_wav:
                ref_wav, ref_text = self.safe_ref_wav, self.safe_ref_text
        # 泄漏重试：第 2 轮起换备选参考——主参考自身导致大面积串音时，换参考才有救
        retry_no = int(data_item.get('lang_leak_retry') or 0)
        if (retry_no >= 2 and not used_chinese_anchor
                and data_item.get("role") == "clone"
                and getattr(self, 'ref_backups', None)):
            ref_wav, ref_text = self.ref_backups[(retry_no - 2) % len(self.ref_backups)]
        gen_text = data_item['text'].strip()
        if gen_text[-1:] not in ".!?。！？":
            gen_text += "。"
        ref_wav_audio = AudioSegment.from_file(ref_wav)
        requested_speed = 0.5 if ref_text and len(ref_text) < 10 else self.get_speed()
        target_duration_ms = max(
            int(data_item.get("target_duration_ms") or 0)
            or (
                int(data_item.get("end_time") or 0)
                - int(data_item.get("start_time") or 0)
            ),
            0,
        )
        supervisor = self._synthesis_supervisor()
        admission = supervisor.admit(
            requested_speed=requested_speed,
            ref_text=ref_text,
            gen_text=gen_text,
            ref_duration_ms=len(ref_wav_audio),
            target_duration_ms=target_duration_ms,
            fit_to_slot=bool(data_item.get("fit_to_slot")),
        )
        speed_slider = admission.effective_speed
        # nfe: F5 默认 32 步；16 是曾经的 Apple Silicon 轻量模式（省一半时间但损失音质细节）。
        # seed: 固定种子保证全片音色一致，逐句随机会导致音色漂移；设为负数恢复随机。
        nfe = int(settings.get('f5tts_nfe') or 32)
        seed = int(settings.get('f5tts_seed', 42))
        if seed >= 0 and retry_no:
            # 固定种子下重试必须偏移种子，否则重新生成的结果与上次完全相同，重试形同虚设
            seed += 9973 * retry_no
        kwargs={
            "ref_audio_input":handle_file(ref_wav),
            "ref_text_input":ref_text,
            "gen_text_input":gen_text,
            "remove_silence":True,
            "randomize_seed":seed < 0,
            "seed_input":max(seed, 0),
            "cross_fade_duration_slider":0.0, # 默认交叉淡入淡出时长
            "nfe_slider":nfe,
            "speed_slider":speed_slider,
            "api_name":'/basic_tts'
        }
        if len(ref_wav_audio)>self.MAX_REF_AUDIO_MS:
            raise DubbingSrtError(
                f"F5-TTS 参考音频超过 {self.MAX_REF_AUDIO_MS / 1000:.0f} 秒，"
                "已停止以避免复制英文原声。"
            )

        started = time.monotonic()
        heartbeat_stop = threading.Event()
        watchdog_fired = threading.Event()
        total_items = int(getattr(self, "len", 0) or len(getattr(self, "queue_tts", [])) or 1)

        def heartbeat():
            while not heartbeat_stop.wait(15):
                elapsed = int(time.monotonic() - started)
                if supervisor.is_stalled(started) and not watchdog_fired.is_set():
                    watchdog_fired.set()
                    self._resource_recycle_pending = True
                    self.signal(text=(
                        f"F5-TTS 第 {idx + 1}/{total_items} 段超过动态看门狗 "
                        f"{int(supervisor.timeout_seconds())} 秒，正在终止后端并只重试该段"
                    ))
                    logger.error(
                        "F5-TTS 第 %s 段触发合成看门狗: elapsed=%ss timeout=%ss",
                        idx + 1, elapsed, supervisor.timeout_seconds(),
                    )
                    if self._is_managed_local_service():
                        self._stop_local_service()
                    return
                self.signal(text=(
                    f"F5-TTS 第 {idx + 1}/{total_items} 段正在生成｜"
                    f"已用 {elapsed} 秒｜推理速度 {speed_slider:.2f}x｜"
                    f"看门狗 {int(supervisor.timeout_seconds())} 秒"
                ))

        heartbeat_thread = threading.Thread(
            target=heartbeat,
            name=f"f5-progress-{idx}",
            daemon=True,
        )
        heartbeat_thread.start()
        admission_note = ""
        if admission.action != "preserve_rate":
            admission_note = (
                f"｜智能时长准入 {admission.requested_speed:.2f}→"
                f"{admission.effective_speed:.2f}x"
            )
        self.signal(text=(
            f"F5-TTS 第 {idx + 1}/{total_items} 段开始｜"
            f"目标 {target_duration_ms / 1000:.1f} 秒｜推理速度 {speed_slider:.2f}x"
            f"{admission_note}"
        ))
        result = None
        send_error = None
        try:
            try:
                result = self._send(kwargs,data_item)
            except Exception as error:
                send_error = error
        finally:
            heartbeat_stop.set()
            elapsed = time.monotonic() - started
            supervisor.finish(
                elapsed,
                success=vail_file(data_item.get("filename")),
                timed_out=watchdog_fired.is_set(),
            )
            self._persist_synthesis_supervisor(data_item)
        if watchdog_fired.is_set():
            return "F5-TTS watchdog timeout: connection refused after service recycle"
        if send_error is not None:
            raise send_error
        return result
