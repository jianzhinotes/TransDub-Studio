from dataclasses import dataclass
import copy
import gc
import os
import platform
import re
import shutil
import subprocess
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
    PIPELINE_VERSION="quality-v4-preflight"
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
            f"{self.PIPELINE_VERSION}-nfe{int(settings.get('f5tts_nfe') or 32)}"
            f"-seed{int(settings.get('f5tts_seed', 42))}"
            f"-resume-anchor{anchor_sigs or self._file_sig(self.resume_chinese_anchor_ref or '')}")

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
        return text if F5TTS._punct_ok(text) else text + "."

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
            from faster_whisper import WhisperModel
            return WhisperModel(str(self._get_validator_model_path()),
                                device="cpu", compute_type="int8")
        except Exception as e:
            logger.warning(f"参考质检模型不可用，跳过回读验证: {e}")
            return None

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
        passed = []
        for cand in ranked[:max_try]:
            try:
                transcript = self._transcribe_one_for_validation(validator, cand[1])
            except Exception as e:
                logger.debug(f"参考回读失败,跳过候选: {e}")
                continue
            sim = self._text_similarity(transcript, cand[2])
            threshold = float(settings.get('f5tts_ref_similarity', 0.75) or 0.75)
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
        cluster_refs = {}
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
            cluster_refs[label] = self._choose_reference(pool, tag=f"spk{label}")
        if len(cluster_refs) < 2:
            return
        # 把归属写进条目：_run 按行取所属簇的参考；缓存键含 cluster_ref 指纹
        assigned = 0
        for pos, (i, it) in enumerate(lines):
            label = labels.get(pos)
            if label in cluster_refs:
                it['cluster_ref'], it['cluster_ref_text'] = cluster_refs[label]
                assigned += 1
        logger.debug(f'多说话人参考启用: {len(cluster_refs)} 簇, 覆盖 {assigned}/{len(lines)} 行, 时长占比={totals}')

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
        try:
            # The app no longer keeps F5 resident from launch.  Start it only
            # when an F5 dubbing task actually reaches synthesis.
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
                    raise DubbingSrtError("F5-TTS 本地服务按需启动失败，请查看 F5-TTS 日志。")
            if self._should_run_preflight():
                self._run_preflight()
                # 16 GB 机型会在预飞回读前停掉 F5，全片放行前再启动。
                if managed_local and not self._local_service_is_ready():
                    if not self._start_local_service():
                        raise DubbingSrtError("F5-TTS 预飞通过后重启本地服务失败。")
            super()._exec()
            if self.is_test or not self.language or self.language[:2].lower() != "zh":
                return
            if low_memory:
                # Never overlap the F5 Metal model with large-v3-turbo's CPU
                # buffers on a 16 GB unified-memory Mac.
                self._stop_local_service()
            self._verify_chinese_outputs()
        finally:
            if low_memory:
                self._stop_local_service()

    @staticmethod
    def _latin_words(text: str) -> List[str]:
        return re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text or "")

    def _has_unexpected_english(self, expected: str, transcript: str) -> bool:
        expected_words = {word.lower() for word in self._latin_words(expected)}
        unexpected = [
            word for word in self._latin_words(transcript)
            if word.lower() not in expected_words
        ]
        # Whisper occasionally renders one short Chinese sound as an English word.
        # A longer word or several words is a much stronger signal that F5 copied
        # speech from the English reference clip.
        latin_chars = sum(len(word.replace("'", "")) for word in unexpected)
        if latin_chars >= 8 or (len(unexpected) >= 2 and latin_chars >= 6):
            return True

        reference_words = {
            word.lower() for word in self._latin_words(self.safe_ref_text or "")
            if len(word.replace("'", "")) >= 5
        }
        # Catch a single leaked proper name such as "Hansen". This used to slip
        # through the general threshold because it is only six letters long.
        return any(
            word.lower() in reference_words and word.lower() not in expected_words
            for word in unexpected
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

    def _transcribe_batch_for_validation(self, model) -> Dict[int, str]:
        batch_audio = AudioSegment.empty()
        boundaries = []
        gap_ms = 700
        for idx, item in enumerate(self.queue_tts):
            if not item.get("text", "").strip() or not vail_file(item.get("filename")):
                continue
            clip = AudioSegment.from_file(item["filename"])
            start_ms = len(batch_audio)
            batch_audio += clip
            boundaries.append((start_ms, len(batch_audio), idx))
            batch_audio += AudioSegment.silent(duration=gap_ms)

        if not boundaries:
            return {}

        batch_file = Path(ROOT_DIR) / "tmp" / f"f5-language-check-{self.uuid}.wav"
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
        return str(item.get("cluster_ref") or "__main_speaker__")

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
                (score, item["filename"], self._ensure_punct(text))
            )

        assigned = 0
        for _, item, _ in failed:
            pool = candidates.get(self._speaker_key(item)) or []
            if not pool:
                continue
            _, wav, text = min(pool, key=lambda row: row[0])
            item["chinese_anchor_ref"] = wav
            item["chinese_anchor_text"] = text
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
        for speaker_key, pool in candidates.items():
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
                    selected[speaker_key] = (item["filename"], text)
                    logger.info(
                        "F5-TTS 恢复任务已选定同说话人中文锚点: 第 %s 段 %s",
                        idx + 1, item["filename"],
                    )
                    break

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

        # 先覆盖不同声纹参考，避免只验证到主讲人。
        seen_refs = set()
        for idx in by_short:
            item = self.queue_tts[idx]
            ref = str(item.get("cluster_ref") or self.safe_ref_wav or item.get("ref_wav") or "")
            if ref and ref not in seen_refs:
                chosen.append(idx)
                seen_refs.add(ref)
            if len(chosen) >= limit:
                return chosen

        def add(idx):
            if idx not in chosen:
                chosen.append(idx)

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
        compact = re.sub(r"[^A-Za-z\u4e00-\u9fff]+", "", transcript or "")
        return bool(re.search(r"(.{1,4})\1{4,}", compact))

    def _run_preflight(self) -> None:
        from faster_whisper import WhisperModel

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
            model = WhisperModel(
                str(self._get_validator_model_path()), device="cpu", compute_type="int8"
            )
            failures = []
            try:
                for idx, original, sample in samples:
                    transcript = self._transcribe_one_for_validation(model, sample["filename"])
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

            if failures:
                details = "；".join(
                    f"第 {idx + 1} 段：{transcript[:70]}" for idx, transcript in failures[:3]
                )
                raise DubbingSrtError(
                    f"F5-TTS 预飞质量核对未通过 {len(failures)}/{len(samples)} 段，"
                    f"已在全片生成前停止。{details}"
                )

            # 预飞参数与正式任务完全相同，通过的音频直接复用。
            for _idx, original, sample in samples:
                target = Path(original["filename"])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(sample["filename"], target)
            self.signal(text=f"F5-TTS 预飞通过 {len(samples)}/{len(samples)}，开始全片配音")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

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
                if self._has_unexpected_english(item["text"], transcript):
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

    def _write_leak_sidecar(self, failed) -> None:
        """BaseTTS 对 queue_tts 做了 deepcopy，直接改条目传不回调用方。
        把 {文件名: 转写} 写到配音目录的 lang_leak.json，由 trans_create 合并回真正的队列。"""
        try:
            import json
            marks = {}
            for _, item, transcript in failed:
                name = Path(item.get("filename") or "").name
                if name:
                    marks[name] = transcript[:120]
            if not marks:
                return
            first = next((it for it in self.queue_tts if it.get("filename")), None)
            if first:
                sidecar = Path(first["filename"]).parent / "lang_leak.json"
                sidecar.write_text(json.dumps(marks, ensure_ascii=False), encoding="utf-8")
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
        if script is None:
            logger.error("F5-TTS 本地服务启动失败：未找到 start_service.sh")
            return False

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
            )
        except Exception as error:
            logger.error("F5-TTS 本地服务启动失败: %s", error)
            return False

        for _ in range(240):
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
                logger.error(
                    "F5-TTS 启动脚本已退出，错误代码: %s，输出: %s",
                    process.returncode, output or "<无输出，子进程可能被系统终止>",
                )
                return False
            time.sleep(1)
        logger.error("F5-TTS 本地服务启动超时: %s", self._local_health_url())
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

    def _item_task(self, data_item, idx=-1):
        """Retry one local infrastructure failure immediately on a fresh model.

        Gradio hides the server traceback by default, so an MPS OOM reaches the
        client as a generic "upstream app" exception.  Waiting until the whole
        retry batch finishes leaves several deleted clips behind.  Restart and
        retry the affected clip at the point of failure instead.
        """
        error = super()._item_task(data_item, idx)
        if (
            not error
            or self._exit()
            or not self._is_managed_local_service()
            or not self._is_service_error(error)
        ):
            return error
        self.signal(text=f"F5-TTS 第 {idx + 1} 段后端内存异常，正在隔离重启并只重试该段")
        logger.warning("F5-TTS 第 %s 段服务异常，立即重启后单段重试: %s", idx + 1, error)
        filename = data_item.get("filename")
        if filename:
            Path(filename).unlink(missing_ok=True)
        if not self._recover_local_service():
            return error
        return super()._item_task(data_item, idx)

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
        from faster_whisper import WhisperModel

        model = None
        try:
            self.signal(text="正在检查 F5-TTS 配音中是否混入英文原声…")
            logger.debug("开始对 F5-TTS 中文配音执行英文原声泄漏检查")
            model = WhisperModel(
                str(self._get_validator_model_path()),
                device="cpu",
                compute_type="int8",
            )

            transcripts = self._transcribe_batch_for_validation(model)
            failed = []
            for idx, item in enumerate(self.queue_tts):
                if not item.get("text", "").strip() or not vail_file(item.get("filename")):
                    continue
                transcript = transcripts.get(idx, "")
                if self._has_unexpected_english(item["text"], transcript):
                    failed.append((idx, item, transcript))

            # 批量音频只负责高召回初筛；逐文件确认后才允许触发昂贵的 F5 重配。
            failed = self._confirm_batch_failures(model, failed, transcripts)

            if self._is_systemic_language_failure(failed):
                for idx, item, transcript in failed:
                    item["lang_leak"] = transcript[:120]
                self._write_leak_sidecar(failed)
                message = (
                    f"F5-TTS 智能熔断：逐段复核确认 {len(failed)} 段存在语言异常，"
                    "已停止大规模自动返工，避免继续浪费数小时。"
                    "请在修复参考音频后重新运行。"
                )
                logger.error(message)
                self.signal(text=message)
                raise DubbingSrtError(message)

            if failed and str(settings.get("f5tts_chinese_anchor", True)).lower() != "false":
                self._assign_chinese_anchors(failed, transcripts)

            for retry_index in range(self.MAX_LANGUAGE_RETRIES):
                if not failed:
                    break
                logger.warning(
                    "检测到 %s 段 F5-TTS 配音混入字幕之外的英文，开始第 %s 次重生成",
                    len(failed), retry_index + 1
                )
                self.signal(
                    text=f"检测到 {len(failed)} 段混入英文原声，正在自动重配 "
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
                low_memory = bool(getattr(self, "_low_memory_profile", False))
                if low_memory and not self._start_local_service():
                    raise DubbingSrtError("F5-TTS 本地服务在质量复核重配前启动失败。")
                try:
                    retry_started = time.monotonic()
                    retry_total = len(failed)
                    for retry_pos, (idx, item, old_transcript) in enumerate(failed, 1):
                        Path(item["filename"]).unlink(missing_ok=True)
                        # 标记重试轮次：_run 据此偏移种子（第 2 轮起换备选参考），
                        # 否则固定种子下重新生成的结果与上次完全相同
                        item['lang_leak_retry'] = retry_index + 1
                        error = self._item_task(item, idx)
                        item.pop('lang_leak_retry', None)
                        if error or not vail_file(item["filename"]):
                            service_failed.append((idx, item, str(error or old_transcript)))
                        else:
                            regenerated.append((idx, item, old_transcript))
                        self.signal(text=self._eta_text(
                            f"F5-TTS 质量返工 {retry_index + 1}/{self.MAX_LANGUAGE_RETRIES}",
                            retry_pos, retry_total, time.monotonic() - retry_started,
                        ))

                    if service_failed and any(
                            self._is_service_error(error) for _, _, error in service_failed):
                        service_failed = self._retry_service_failures(service_failed, retry_index)
                        regenerated.extend(
                            (idx, item, old_error)
                            for idx, item, old_error in failed
                            if vail_file(item.get("filename"))
                            and not any(idx == failed_idx for failed_idx, _, _ in regenerated)
                        )
                finally:
                    if low_memory:
                        # Reload Whisper only after Metal allocations are gone.
                        self._stop_local_service()

                if service_failed:
                    details = "；".join(
                        f"第 {idx + 1} 段：{error[:100]}"
                        for idx, _, error in service_failed[:5]
                    )
                    message = (
                        f"F5-TTS 本地服务在质量复核重配时失败，已保留其他成功片段。"
                        f"{details}"
                    )
                    logger.error(message)
                    self.signal(text=message)
                    raise DubbingSrtError(message)

                model = WhisperModel(
                    str(self._get_validator_model_path()),
                    device="cpu",
                    compute_type="int8",
                )
                retry_failed = []
                for idx, item, _ in regenerated:
                    transcript = self._transcribe_one_for_validation(model, item["filename"])
                    if self._has_unexpected_english(item["text"], transcript):
                        retry_failed.append((idx, item, transcript))
                failed = retry_failed

            if failed:
                for idx, item, transcript in failed:
                    item["lang_leak"] = transcript[:120]
                self._write_leak_sidecar(failed)
                details = "；".join(
                    f"第 {idx + 1} 段：{transcript[:80]}"
                    for idx, _, transcript in failed[:5]
                )
                message = (
                    f"F5-TTS 质量门禁未通过：重试后仍有 {len(failed)} 段疑似混入"
                    f"字幕之外的英文，已停止合成。{details}"
                )
                logger.error(message)
                self.signal(text=message)
                if str(settings.get("f5tts_strict_language_gate", True)).lower() != "false":
                    raise DubbingSrtError(message)
                logger.warning("严格语言门禁已被关闭，保留泄漏标记并继续流程")
            else:
                logger.debug("F5-TTS 英文原声泄漏检查通过")
                self.signal(text="F5-TTS 配音内容检查通过")
        finally:
            model = None
            gc.collect()

    def _run(self, data_item: Union[Dict, List, None], idx: int = -1) -> Union[str, None]:
        ref_wav,ref_text=self.get_ref_wav(data_item)
        if data_item.get("role") == "clone":
            resume_anchor = getattr(self, "resume_chinese_anchors", {}).get(
                self._speaker_key(data_item)
            )
            if data_item.get("chinese_anchor_ref"):
                # 泄漏重试专用：用已验收的同说话人中文成品约束生成语言。
                ref_wav = data_item["chinese_anchor_ref"]
                ref_text = data_item.get("chinese_anchor_text") or ref_text
            elif resume_anchor:
                # 中断恢复时，对尚未生成的片段优先使用已验收中文
                # 成品；按说话人簇匹配，避免主持人与嘉宾互换音色。
                ref_wav, ref_text = resume_anchor
            elif (not data_item.get("cluster_ref")
                  and getattr(self, "resume_chinese_anchor_ref", None)):
                # 兼容没有可靠声纹分簇的单说话人项目。
                ref_wav = self.resume_chinese_anchor_ref
                ref_text = self.resume_chinese_anchor_text or ref_text
            elif data_item.get('cluster_ref'):
                # 多说话人：该行所属说话人簇的参考（各说各的音色）
                ref_wav, ref_text = data_item['cluster_ref'], data_item.get('cluster_ref_text') or ref_text
            elif self.safe_ref_wav:
                ref_wav, ref_text = self.safe_ref_wav, self.safe_ref_text
        # 泄漏重试：第 2 轮起换备选参考——主参考自身导致大面积串音时，换参考才有救
        retry_no = int(data_item.get('lang_leak_retry') or 0)
        if (retry_no >= 2 and not data_item.get("chinese_anchor_ref")
                and data_item.get("role") == "clone"
                and getattr(self, 'ref_backups', None)):
            ref_wav, ref_text = self.ref_backups[(retry_no - 2) % len(self.ref_backups)]
        speed_slider = 0.5 if ref_text  and len(ref_text) < 10 else self.get_speed()
        gen_text = data_item['text'].strip()
        if gen_text[-1:] not in ".!?。！？":
            gen_text += "。"
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
        ref_wav_audio=AudioSegment.from_file(ref_wav)
        if len(ref_wav_audio)>self.MAX_REF_AUDIO_MS:
            raise DubbingSrtError(
                f"F5-TTS 参考音频超过 {self.MAX_REF_AUDIO_MS / 1000:.0f} 秒，"
                "已停止以避免复制英文原声。"
            )

        return self._send(kwargs,data_item)
