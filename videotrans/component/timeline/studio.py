"""Dubbing Studio：配音后精修工作台（替代旧的表格校对弹窗）。

上部：视频预览 + 逐句说话人卡片（原文/译文/音色/试听/重配）；
下部：可编辑时间轴（拖块移动、拉端点改时长）+ 原声/配音波形。
无倒计时——用户点「继续合成」才放行流水线，「终止任务」停止。
"""
import json
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QRadioButton,
    QSizePolicy, QSplitter, QVBoxLayout, QWidget,
)

from videotrans.configure.config import ROOT_DIR, logger, tr
from videotrans.component.timeline.cards import SpeakerCardList
from videotrans.component.timeline.dialog import PrepWorker
from videotrans.component.timeline.dub_preview import (
    build_dub_preview_wav, cleanup_previews, preview_cache_name,
    preview_loading_policy,
)
from videotrans.component.timeline.edit_logic import serializable
from videotrans.component.timeline.edit_track import EditableSubtitleTrack
from videotrans.component.timeline.joint_plan import (
    JointPlanPreviewDialog, JointPlanningWorker, JointSynthesisWorker,
)
from videotrans.component.timeline.peaks import extract_peaks
from videotrans.component.timeline.player import AUDIO_DUBBED, AUDIO_ORIGINAL, PreviewPlayer
from videotrans.component.timeline.redub import RedubQueue
from videotrans.component.timeline.quality_audit import QualityAuditWorker
from videotrans.component.timeline.studio_state import StudioState
from videotrans.component.timeline.timeline_view import TimelineView
from videotrans.util.tools import vail_file

_REBUILD_DEBOUNCE_MS = 800


class _PreviewRebuildWorker(QThread):
    done = Signal(object, str)   # peaks, wav_path
    failed = Signal(str)
    progress = Signal(int, int)

    def __init__(self, queue_snapshot, duration_ms, cache_folder, out_name,
                 prepare_peaks=True, parent=None):
        super().__init__(parent=parent)
        self._queue = queue_snapshot
        self._duration_ms = duration_ms
        self._cache_folder = cache_folder
        self._out_name = out_name
        self._prepare_peaks = bool(prepare_peaks)

    def run(self):
        try:
            wav = build_dub_preview_wav(
                self._queue, self._duration_ms, self._cache_folder,
                progress_cb=lambda current, total: self.progress.emit(current, total),
                out_name=self._out_name)
            peaks = None
            if self._prepare_peaks:
                peaks, _ = extract_peaks(wav, self._cache_folder)
            self.done.emit(peaks, wav)
        except Exception as e:
            logger.exception(f'配音预览重建失败: {e}', exc_info=True)
            self.failed.emit(str(e))


class DubbingStudioDialog(QDialog):
    # 工程模式点"导出成品"时发出，携带工程目录，交由调用方跑 RealignWorker
    regenerate_requested = Signal(str)
    # 内嵌模式点"返回"时发出，交由外层工作区切回上一态
    back_requested = Signal()
    # 内嵌中途配音校对点"下一步"时发出，交由外层继续流水线
    proof_done = Signal()

    def __init__(self, parent=None, language=None, cache_folder=None,
                 video_path=None, source_wav=None, project_dir=None, embedded=False,
                 source_language=None, auto_plan=False):
        super().__init__(parent)
        self.project_dir = project_dir
        self._project_mode = bool(project_dir)
        self._embedded = embedded   # 内嵌进工作区（非弹窗），主按钮发信号而非 accept
        self.setWindowTitle(tr("Dubbing Studio"))
        self.setWindowIcon(QIcon(f"{ROOT_DIR}/videotrans/styles/icon.ico"))
        if not embedded:
            self.setMinimumSize(1280, 800)
        self.setWindowFlags(Qt.WindowTitleHint | Qt.WindowSystemMenuHint
                            | Qt.WindowMaximizeButtonHint | Qt.WindowCloseButtonHint)

        # 数据：工程模式从 .tdproj 加载（filename 绝对化），否则读流水线 cache
        queue_tts = []
        project_manifest = None
        if self._project_mode:
            from videotrans.task.project import load_project
            project, queue_tts = load_project(project_dir)
            project_manifest = project
            language = project.get('target_language_code') or language
            source_language = ((project.get('cfg') or {}).get('source_language_code')
                               or source_language)
            cache_folder = project_dir
            video_path = project.get('source_video') or video_path
            source_wav = str(Path(project_dir) / 'source.wav')
        else:
            qfile = Path(f'{cache_folder}/queue_tts.json')
            if qfile.exists():
                try:
                    queue_tts = json.loads(qfile.read_text(encoding='utf-8'))
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(f'加载 queue_tts.json 失败: {e}')
        self.language = language
        self.source_language = source_language or 'auto'
        self.cache_folder = cache_folder
        self.video_path = video_path or ''
        self.project_manifest = project_manifest
        quality_coverage = None
        if str(language or '').lower().startswith('zh'):
            from videotrans.dub.quality_manifest import queue_quality_coverage
            from videotrans.tts._f5tts import F5TTS
            quality_root = project_dir if self._project_mode else cache_folder
            quality_coverage = queue_quality_coverage(
                queue_tts,
                quality_root,
                rules_version=F5TTS.QUALITY_RULES_VERSION,
                validator_model=F5TTS.VALIDATOR_MODEL,
                verify_audio_hashes=False,
            )
            # Rehydrate both saved projects and an interrupted live pipeline.
            # The queue JSON may still contain five pre-repair failures while
            # the manifest proves that four newer files already passed.
            for idx, entry in quality_coverage['entries'].items():
                item = queue_tts[idx]
                if entry.get('passed'):
                    item.pop('lang_leak', None)
                    item.pop('quality_status', None)
                    item.pop('quality_failures', None)
                else:
                    item['lang_leak'] = str(entry.get('transcript') or '')[:200]
                    item['quality_status'] = 'needs_review'
                    item['quality_failures'] = list(entry.get('hard_failures') or [])
        estimated_duration = max(
            [int(item.get('end_time', 0) or 0) for item in queue_tts] or [1])
        self._duration_ms = max(estimated_duration, 1)
        self.state = StudioState(
            queue_tts, duration_ms=self._duration_ms, parent=self)
        self._auto_plan = bool(auto_plan)
        self._advanced_mode = False
        self._continuous_preview_ready = False
        self._preview_requested = False
        self._single_preview_end_ms = 0
        self._preview_rev = 0
        self._rebuild_worker = None
        self._rebuild_pending = False
        self._prev_preview_wav = None
        self._accepting = False
        self._joint_worker = None
        self._joint_synth_worker = None
        self._joint_dialog = None
        self._joint_project = None
        self._joint_plan = None
        self._quality_audit_worker = None
        self._quality_last_summary = ''
        self._quality_repair_message = ''
        self._repair_batch_total = 0
        self._repair_batch_done = 0
        self._repair_batch_passed = 0
        self._quality_audit_required = bool(
            self._project_mode
            and str(language or '').lower().startswith('zh')
            and (quality_coverage or {}).get('missing', 1) > 0
        )

        roles = self._compute_roles(queue_tts)
        self.redub_queue = RedubQueue(self.state, language, parent=self)

        # ---- 布局 ----
        layout = QVBoxLayout(self)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter = splitter
        # 左：视频 + 控制
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.player = PreviewPlayer(self)
        from videotrans.component.timeline.video_overlay import VideoOverlay
        self.video_area = VideoOverlay(self.player)
        self.video_area.setMinimumSize(480, 270)
        self.video_area.setSizePolicy(QSizePolicy.Policy.Expanding,
                                      QSizePolicy.Policy.Expanding)
        left_layout.addWidget(self.video_area, stretch=1)

        self.subtitle_label = QLabel('')
        self.subtitle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setStyleSheet('font-size:14px;color:#E6E9EC;padding:2px 8px;')
        self.subtitle_label.setMinimumHeight(36)
        left_layout.addWidget(self.subtitle_label)

        # 默认只暴露用户真正需要的“原声 / 中文配音”和同步预览。
        ctrl = QHBoxLayout()
        self.preview_status = QLabel('')
        self.preview_status.setStyleSheet('color:#9AA7B4;font-size:12px;')
        ctrl.addWidget(self.preview_status)
        ctrl.addStretch(1)
        self.original_radio = QRadioButton(tr("Original audio"))
        self.original_radio.setChecked(True)
        self.dubbed_radio = QRadioButton(tr("Dubbed audio"))
        self.dubbed_radio.setEnabled(False)
        self.original_radio.toggled.connect(
            lambda orig: self.player.set_audio_mode(AUDIO_ORIGINAL if orig else AUDIO_DUBBED))
        ctrl.addWidget(self.original_radio)
        ctrl.addWidget(self.dubbed_radio)
        self.sync_preview_btn = QPushButton(tr('Prepare synced video preview'))
        self.sync_preview_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sync_preview_btn.clicked.connect(self._request_continuous_preview)
        ctrl.addWidget(self.sync_preview_btn)
        left_layout.addLayout(ctrl)
        splitter.addWidget(left)

        # 右：高级编辑才按需创建 500+ 张卡片。
        self.cards = SpeakerCardList(self.state, roles, defer_build=True)
        splitter.addWidget(self.cards)
        splitter.setSizes([700, 560])
        layout.addWidget(splitter, stretch=1)

        # 下：可编辑时间轴
        self.timeline_hint = QLabel(tr("Drag block to move, drag edge to resize"))
        self.timeline_hint.setStyleSheet('color:#9AA7B4;font-size:12px;')
        layout.addWidget(self.timeline_hint)
        self.timeline = TimelineView(
            self._duration_ms, subtitle_track_cls=EditableSubtitleTrack)
        self.wave_original = self.timeline.add_waveform_track(tr("Original audio"))
        self.wave_original.set_placeholder(tr("Generating waveform..."))
        self.wave_dubbed = self.timeline.add_waveform_track(tr("Dubbed audio"))
        self.wave_dubbed.set_placeholder(tr("Generating waveform..."))
        self.timeline.set_subtitles(self.state.items)
        layout.addWidget(self.timeline)

        # 质量问题独占一行，避免长视频返工控件挤压主操作按钮。
        quality_row = QHBoxLayout()
        self.quality_status = QLabel('')
        self.quality_status.setStyleSheet('color:#ff8a65;font-size:12px;font-weight:bold;')
        quality_row.addWidget(self.quality_status)
        quality_row.addStretch(1)
        self.quality_audit_btn = QPushButton(tr('Recheck existing dubbing'))
        self.quality_audit_btn.clicked.connect(self._start_quality_audit)
        quality_row.addWidget(self.quality_audit_btn)
        self.quality_filter_btn = QPushButton(tr('Show only failed clips'))
        self.quality_filter_btn.setCheckable(True)
        self.quality_filter_btn.toggled.connect(self.cards.set_quality_filter)
        quality_row.addWidget(self.quality_filter_btn)
        self.retry_failed_btn = QPushButton(tr('Retry failed clips'))
        self.retry_failed_btn.clicked.connect(self._retry_quality_failed)
        quality_row.addWidget(self.retry_failed_btn)
        layout.addLayout(quality_row)

        # 工程级操作统一收进“高级编辑”。
        self.advanced_controls = QWidget()
        bottom = QHBoxLayout(self.advanced_controls)
        bottom.setContentsMargins(0, 0, 0, 0)
        for text, fn in ((tr("Zoom out"), lambda: self.timeline.zoom_out()),
                         (tr("Zoom in"), lambda: self.timeline.zoom_in()),
                         (tr("Fit"), lambda: self.timeline.zoom_fit())):
            btn = QPushButton(text)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(fn)
            bottom.addWidget(btn)
        self.joint_status = QLabel('')
        self.joint_status.setStyleSheet('color:#9AA7B4;font-size:12px;')
        bottom.addWidget(self.joint_status)
        self.joint_btn = QPushButton(tr("Smart optimization"))
        self.joint_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.joint_btn.clicked.connect(self._on_smart_button)
        bottom.addWidget(self.joint_btn)
        bottom.addStretch(1)
        layout.addWidget(self.advanced_controls)

        # 主操作行：无论是否展开高级编辑，都只保留返回/导出两个决策。
        primary = QHBoxLayout()
        self.advanced_toggle_btn = QPushButton(tr('Advanced editing'))
        self.advanced_toggle_btn.setCheckable(True)
        self.advanced_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.advanced_toggle_btn.toggled.connect(self._set_advanced_mode)
        primary.addWidget(self.advanced_toggle_btn)
        primary.addStretch(1)
        # 内嵌工程重编辑：导出成品；内嵌中途配音校对：下一步；工程弹窗：重新生成；流水线：继续合成
        if self._embedded and self._project_mode:
            main_text = tr("flow_export")
        elif self._embedded:
            main_text = tr("flow_proof_next")
        elif self._project_mode:
            main_text = tr("flow_regenerate")
        else:
            main_text = tr("Continue synthesis")
        self.continue_btn = QPushButton(main_text)
        self.continue_btn.setObjectName('startBtn')
        self.continue_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.continue_btn.setMinimumSize(300, 36)
        self.continue_btn.clicked.connect(
            self._regenerate if self._project_mode else self._continue_synthesis)
        primary.addWidget(self.continue_btn)
        if self._embedded:
            cancel_text = tr("flow_back")
            cancel_action = self._on_back
        elif self._project_mode:
            cancel_text = tr("Close")
            cancel_action = self.close
        else:
            cancel_text = tr("Terminate this mission")
            cancel_action = self._terminate
        cancel_btn = QPushButton(cancel_text)
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setStyleSheet('background-color:transparent')
        cancel_btn.clicked.connect(cancel_action)
        primary.addWidget(cancel_btn)
        layout.addLayout(primary)

        # ---- 接线 ----
        self.timeline.seekRequested.connect(self._seek)
        self.timeline.blockClicked.connect(self._on_block_clicked)
        self.timeline.subtitle_track.timesEditRequested.connect(self._on_times_edited)
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)

        self.cards.seekRequested.connect(self._seek)
        self.cards.playRequested.connect(self._play_single_line)
        self.cards.redubRequested.connect(self._on_redub_requested)
        self.redub_queue.started.connect(self._on_redub_started)
        self.redub_queue.finished.connect(self._on_redub_finished)

        self.state.timesChanged.connect(self._on_state_times_changed)
        self._refresh_quality_summary()

        # 去抖重建定时器
        self._rebuild_timer = QTimer(self)
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.setInterval(_REBUILD_DEBOUNCE_MS)
        self._rebuild_timer.timeout.connect(self._start_rebuild)

        # ---- 启动 ----
        self.player.load(video_path)
        eager_waveform, eager_dubbed = preview_loading_policy(
            self._duration_ms, len(self.state.items))
        self._eager_dubbed_preview = eager_dubbed
        if not eager_waveform:
            self.wave_original.set_placeholder(tr('Waveform deferred for long video'))
        if not eager_dubbed:
            self.wave_dubbed.set_placeholder(tr('Use per-segment listening for long video'))
            self.preview_status.setText(tr('Long video: synced preview is prepared only when requested'))
        else:
            self.preview_status.setText(tr('Preparing synced preview...'))
            self.sync_preview_btn.setEnabled(False)
        self._prep_worker = PrepWorker(
            source_media=source_wav if source_wav and Path(source_wav).exists() else video_path,
            cache_dir=cache_folder,
            queue_tts=self.state.items,
            prepare_original=eager_waveform,
            prepare_dubbed=eager_dubbed,
        )
        self._prep_worker.originalReady.connect(self._on_original_ready)
        self._prep_worker.dubbedReady.connect(self._on_dubbed_ready)
        self._prep_worker.failed.connect(self._on_prep_failed)
        self._prep_worker.start()
        self._set_advanced_mode(False)
        QTimer.singleShot(0, self.timeline.zoom_fit)
        if self._auto_plan and self.state.items:
            QTimer.singleShot(700, self._auto_start_joint_planning)

    # ---- 角色列表 ----
    def _compute_roles(self, queue_tts) -> list:
        if not queue_tts:
            return []
        current = str(queue_tts[0].get('role') or '')
        try:
            from videotrans.util.help_role import role_menu
            langcode = self.language
            roles = role_menu(queue_tts[0].get('tts_type'), langcode) or []
            return [str(r) for r in roles]
        except Exception as e:
            logger.warning(f'获取音色列表失败，仅保留当前音色: {e}')
            return [current] if current else []

    # ---- PrepWorker 回调 ----
    def _on_original_ready(self, peaks, duration_ms):
        self._duration_ms = max(self._duration_ms, int(duration_ms))
        self.state.duration_ms = self._duration_ms
        self.timeline.scale.set_duration(self._duration_ms)
        self.wave_original.set_clips([(0, peaks)])
        self.timeline.zoom_fit()

    def _on_dubbed_ready(self, peaks, dub_wav):
        self.wave_dubbed.set_clips([(0, peaks)])
        self.player.set_dub_source(dub_wav, offset_ms=0)
        self.dubbed_radio.setEnabled(True)
        self.sync_preview_btn.setEnabled(True)
        self._continuous_preview_ready = True
        self._single_preview_end_ms = 0
        self._prev_preview_wav = dub_wav
        self.preview_status.setText(tr('Synced Chinese preview is ready'))
        self.sync_preview_btn.setText(tr('Play synced Chinese preview'))

    def _on_prep_failed(self, msg):
        short = msg.splitlines()[0] if msg else 'unknown'
        self.wave_original.set_placeholder(f'{tr("anerror")}: {short}')
        self.wave_dubbed.set_placeholder('')
        self.preview_status.setText(tr('Synced preview is not ready'))

    # ---- 简洁 / 高级界面 ----
    def _set_advanced_mode(self, enabled: bool):
        self._advanced_mode = bool(enabled)
        self.cards.setVisible(self._advanced_mode)
        self.timeline_hint.setVisible(self._advanced_mode)
        self.timeline.setVisible(self._advanced_mode)
        self.advanced_controls.setVisible(self._advanced_mode)
        if self._advanced_mode:
            self.cards.start_building()
            self.main_splitter.setSizes([700, 560])
            self.advanced_toggle_btn.setText(tr('Hide advanced editing'))
        else:
            self.main_splitter.setSizes([1, 0])
            self.advanced_toggle_btn.setText(tr('Advanced editing'))
        if self.advanced_toggle_btn.isChecked() != self._advanced_mode:
            self.advanced_toggle_btn.setChecked(self._advanced_mode)
        self._refresh_quality_summary()

    # ---- 播放联动 ----
    @staticmethod
    def _fmt(ms: int) -> str:
        s = max(int(ms), 0) // 1000
        return f'{s // 60:02d}:{s % 60:02d}.{max(int(ms), 0) % 1000 // 100}'

    def _on_duration(self, ms: int):
        if ms > 0:
            self._duration_ms = max(self._duration_ms, int(ms))
            self.state.duration_ms = self._duration_ms
            self.timeline.scale.set_duration(self._duration_ms)

    def _seek(self, ms: int):
        # 立即移动播放头 + 高亮（不等 positionChanged，暂停态该信号可能不发）
        self.player.seek(ms)
        self._on_position(ms)

    def _on_position(self, ms: int):
        if self._single_preview_end_ms and ms >= self._single_preview_end_ms:
            self._single_preview_end_ms = 0
            self.player.pause()
        self.timeline.set_position(ms)
        idx = self.timeline.subtitle_track.index_for_ms(ms)
        items = self.state.items
        if 0 <= idx < len(items) and ms <= int(items[idx]['end_time']):
            self.timeline.subtitle_track.set_active(idx)
            self.cards.set_active(idx)   # 播放中只高亮不滚动
            self.subtitle_label.setText(str(items[idx]['text']))
        else:
            self.timeline.subtitle_track.set_active(-1)
            self.subtitle_label.setText('')

    def _on_block_clicked(self, idx: int):
        self.cards.set_active(idx)
        self.cards.scroll_to(idx)

    # ---- 编辑 ----
    def _on_times_edited(self, idx: int, start_ms: int, end_ms: int):
        self.state.set_times(idx, start_ms, end_ms)
        self.timeline.subtitle_track.set_items(self.state.items)

    def _on_state_times_changed(self, idx: int):
        self._invalidate_continuous_preview()
        if self._eager_dubbed_preview:
            self._rebuild_timer.start()

    def _invalidate_continuous_preview(self):
        if not self._continuous_preview_ready:
            return
        self._continuous_preview_ready = False
        self._single_preview_end_ms = 0
        self.original_radio.setChecked(True)
        self.dubbed_radio.setEnabled(False)
        self.sync_preview_btn.setEnabled(True)
        self.sync_preview_btn.setText(tr('Prepare synced video preview'))
        self.preview_status.setText(tr('Dubbing changed; refresh synced preview'))

    # ---- 重配 ----
    def _on_redub_requested(self, idx: int):
        if self._quality_audit_worker is not None:
            return
        card = self.cards.card(idx)
        if card:
            card.set_busy(True, queued=bool(self.redub_queue.pending()))
        self.redub_queue.enqueue(idx)

    def _on_redub_started(self, idx: int):
        card = self.cards.card(idx)
        if card:
            card.set_busy(True, queued=False)
        if self._repair_batch_total:
            self._quality_repair_message = tr(
                'Repairing failed clips: {0}/{1} (line {2})').format(
                    self._repair_batch_done + 1,
                    self._repair_batch_total,
                    idx + 1,
                )
            self._refresh_quality_summary()

    def _on_redub_finished(self, idx: int, ok: bool, err: str):
        card = self.cards.card(idx)
        if card:
            card.set_busy(False)
            card.refresh()
        if ok:
            if self._repair_batch_total:
                self._repair_batch_passed += 1
            self._invalidate_continuous_preview()
            if self._eager_dubbed_preview:
                self._rebuild_timer.start()
        elif not self._repair_batch_total:
            QMessageBox.warning(self, tr('anerror'), err[:600])
        try:
            # A crash or app restart must reopen with the latest per-clip
            # repair state, not the five failures from before this batch.
            self._persist_quality_audit_state()
        except OSError as error:
            logger.warning('保存局部返工状态失败: %s', error)
        if self._repair_batch_total:
            self._repair_batch_done += 1
            if self._repair_batch_done >= self._repair_batch_total:
                failed = self._repair_batch_total - self._repair_batch_passed
                if failed:
                    self._quality_repair_message = tr(
                        'Context repair complete: {0} passed, {1} still need review. '
                        'Original audio was preserved.').format(
                            self._repair_batch_passed, failed)
                else:
                    self._quality_repair_message = tr(
                        'All failed clips passed. Click Next to continue.')
                self._repair_batch_total = 0
                self._repair_batch_done = 0
                self._repair_batch_passed = 0
        self._refresh_quality_summary()

    def _retry_quality_failed(self):
        if self._quality_audit_worker is not None or self.redub_queue.pending():
            return
        indices = self.state.quality_failed_indices()
        if not indices:
            return
        self._repair_batch_total = len(indices)
        self._repair_batch_done = 0
        self._repair_batch_passed = 0
        self._quality_repair_message = tr(
            'Preparing contextual repair for {0} failed clips...').format(len(indices))
        for idx in indices:
            self._on_redub_requested(idx)
        self._refresh_quality_summary()

    def _refresh_quality_summary(self):
        count = len(self.state.quality_failed_indices())
        pending = self.redub_queue.pending()
        if self._quality_repair_message:
            text = self._quality_repair_message
        elif count:
            text = tr('Quality issues: {0}').format(count)
        elif self._quality_audit_required:
            text = tr('This legacy project has not passed strong quality review.')
        else:
            text = self._quality_last_summary
        self.quality_status.setText(text)
        self.quality_status.setVisible(bool(text))
        is_chinese = str(self.language or '').lower().startswith('zh')
        self.quality_audit_btn.setVisible(
            is_chinese and (self._advanced_mode or self._quality_audit_required))
        self.quality_filter_btn.setVisible(bool(count) and self._advanced_mode)
        self.retry_failed_btn.setVisible(bool(count))
        self.retry_failed_btn.setEnabled(
            bool(count) and not pending and self._quality_audit_worker is None)
        if self._repair_batch_total:
            self.retry_failed_btn.setText(tr('Repairing {0}/{1}').format(
                min(self._repair_batch_done + 1, self._repair_batch_total),
                self._repair_batch_total))
        elif count:
            self.retry_failed_btn.setText(
                tr('Retry failed clips with Chinese context'))
        else:
            self.retry_failed_btn.setText(tr('Retry failed clips'))
        self.continue_btn.setEnabled(
            not count and not pending and self._quality_audit_worker is None)
        if not count and self.quality_filter_btn.isChecked():
            self.quality_filter_btn.setChecked(False)

    def _start_quality_audit(self):
        if self._quality_audit_worker is not None or self.redub_queue.pending():
            return
        answer = QMessageBox.question(
            self,
            tr('Dubbing Studio'),
            tr('Use the strong local speech model to recheck every existing clip? '
               'Passed clips will be kept and only failed clips need re-dubbing.'),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.quality_audit_btn.setEnabled(False)
        self.retry_failed_btn.setEnabled(False)
        self._quality_last_summary = tr('Starting strong quality review...')
        self._refresh_quality_summary()
        worker = QualityAuditWorker(
            serializable(self.state.items),
            self.project_dir or self.cache_folder,
            parent=self,
        )
        worker.progress.connect(self._on_quality_audit_progress)
        worker.done.connect(self._on_quality_audit_done)
        worker.failed.connect(self._on_quality_audit_failed)
        self._quality_audit_worker = worker
        worker.start()

    def _on_quality_audit_progress(self, current: int, total: int):
        self._quality_last_summary = tr('Strong quality review: {0}/{1}').format(
            current, total)
        self._refresh_quality_summary()

    def _finish_quality_audit(self):
        worker = self._quality_audit_worker
        self._quality_audit_worker = None
        self.quality_audit_btn.setEnabled(True)
        self.retry_failed_btn.setEnabled(True)
        if worker is not None:
            worker.deleteLater()

    def _persist_quality_audit_state(self):
        if self._project_mode:
            from videotrans.task.project import save_queue
            save_queue(self.project_dir, self.state.items)
        else:
            self.state.save(self.cache_folder)

    def _on_quality_audit_done(self, results):
        passed = 0
        failed = 0
        for raw_idx, entry in dict(results or {}).items():
            idx = int(raw_idx)
            if not 0 <= idx < len(self.state.items):
                continue
            if entry.get('passed'):
                passed += 1
                self.state.clear_quality_failure(idx)
            else:
                failed += 1
                self.state.items[idx]['quality_failures'] = list(
                    entry.get('hard_failures') or [])
                self.state.set_quality_failure(
                    idx, str(entry.get('transcript') or ''), status='needs_review')
        self._quality_audit_required = False
        self._quality_last_summary = tr(
            'Quality recheck complete: {0} passed, {1} need repair.').format(
                passed, failed)
        try:
            self._persist_quality_audit_state()
        except OSError as error:
            logger.exception('保存质量复核结果失败: %s', error, exc_info=True)
            QMessageBox.warning(self, tr('anerror'), str(error))
        self._finish_quality_audit()
        self._refresh_quality_summary()

    def _on_quality_audit_failed(self, message):
        self._quality_last_summary = tr('Strong quality review failed')
        self._finish_quality_audit()
        self._refresh_quality_summary()
        QMessageBox.warning(self, tr('anerror'), str(message)[:1000])

    # ---- 单句试听 ----
    def _play_single_line(self, idx: int):
        item = self.state.items[idx]
        filename = item.get('filename')
        if not filename or not vail_file(filename):
            QMessageBox.information(self, tr('Dubbing Studio'), tr('No audio'))
            return
        start_ms = int(item.get('start_time', 0) or 0)
        self.player.pause()
        if not self._continuous_preview_ready:
            # The video remains the master clock.  The selected clip starts at
            # local audio position 0 but is anchored to its absolute subtitle
            # timestamp, so this is real A/V listening rather than a WAV-only
            # audition.
            self.player.set_dub_source(filename, offset_ms=start_ms)
            duration_ms = max(
                int(float(item.get('dubbing_s', 0) or 0) * 1000),
                int(item.get('end_time', start_ms) or start_ms) - start_ms,
                200,
            )
            self._single_preview_end_ms = start_ms + duration_ms
            self.preview_status.setText(tr('Playing this line with video'))
        else:
            self._single_preview_end_ms = 0
        self.dubbed_radio.setEnabled(True)
        self.dubbed_radio.setChecked(True)
        self._seek(start_ms)
        QTimer.singleShot(120, self, self.player.play)

    def _request_continuous_preview(self):
        if self._continuous_preview_ready:
            self.dubbed_radio.setChecked(True)
            self.preview_status.setText(tr('Synced Chinese preview is ready'))
            self.player.play()
            return
        self._preview_requested = True
        self.sync_preview_btn.setEnabled(False)
        self.sync_preview_btn.setText(tr('Preparing synced preview...'))
        self.preview_status.setText(tr('Preparing synced preview...'))
        self._start_rebuild()

    # ---- 预览重建（去抖） ----
    def _start_rebuild(self):
        if self._rebuild_worker is not None:
            self._rebuild_pending = True
            return
        self._preview_rev += 1
        self.wave_dubbed.set_placeholder(tr("Rebuilding dubbed preview..."))
        out_name = preview_cache_name(self.state.items, self._duration_ms)
        worker = _PreviewRebuildWorker(
            serializable(self.state.items), self._duration_ms, self.cache_folder,
            out_name,
            prepare_peaks=self._advanced_mode or self._eager_dubbed_preview,
            parent=self)
        worker.done.connect(self._on_rebuild_done)
        worker.failed.connect(self._on_rebuild_failed)
        worker.progress.connect(self._on_rebuild_progress)
        self._rebuild_worker = worker
        worker.start()

    def _on_rebuild_progress(self, current: int, total: int):
        if total:
            self.preview_status.setText(
                tr('Preparing synced preview: {0}/{1}').format(current, total))

    def _on_rebuild_done(self, peaks, wav):
        self._rebuild_worker = None
        self.wave_dubbed.set_placeholder('')
        if peaks is not None:
            self.wave_dubbed.set_clips([(0, peaks)])
        self.player.set_dub_source(wav, offset_ms=0)
        self.dubbed_radio.setEnabled(True)
        self._continuous_preview_ready = True
        self._single_preview_end_ms = 0
        self.sync_preview_btn.setEnabled(True)
        self.sync_preview_btn.setText(tr('Play synced Chinese preview'))
        self.preview_status.setText(tr('Synced Chinese preview is ready'))
        # 删除上一版预览文件
        if self._prev_preview_wav and self._prev_preview_wav != wav:
            Path(self._prev_preview_wav).unlink(missing_ok=True)
        self._prev_preview_wav = wav
        if self._preview_requested:
            self._preview_requested = False
            self.dubbed_radio.setChecked(True)
            self.player.play()
        if self._rebuild_pending:
            self._rebuild_pending = False
            self._rebuild_timer.start()

    def _on_rebuild_failed(self, msg):
        self._rebuild_worker = None
        self.wave_dubbed.set_placeholder(f'{tr("anerror")}: {msg[:80]}')
        self._preview_requested = False
        self.sync_preview_btn.setEnabled(True)
        self.sync_preview_btn.setText(tr('Prepare synced video preview'))
        self.preview_status.setText(tr('Synced preview failed; click to retry'))
        if self._rebuild_pending:
            self._rebuild_pending = False
            self._rebuild_timer.start()

    # ---- 默认后台智能编排；音频生成仍由用户确认 ----
    def _auto_start_joint_planning(self):
        if self._joint_worker is None and self._joint_plan is None:
            self._start_joint_planning('auto')

    def _on_smart_button(self):
        if self._joint_worker is not None:
            return
        if self._joint_plan is not None and self._joint_project is not None:
            self._show_joint_plan()
        else:
            self._start_joint_planning('auto')

    def _start_joint_planning(self, candidate_backend: str):
        if self._joint_worker is not None:
            return
        if candidate_backend == 'deepseek':
            answer = QMessageBox.question(
                self, tr('Joint planning'),
                tr('This will call your configured DeepSeek API for the first 20 lines. Continue?'),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return

        state_dir = (self.project_dir or
                     str(Path(self.cache_folder) / 'joint-preview.tdproj'))
        candidate_dir = str(Path(self.cache_folder) / 'joint_candidates')
        self.joint_btn.setEnabled(False)
        self.joint_btn.setText(tr('Optimizing...'))
        self.joint_status.setText(tr('Smart optimization runs in background'))
        worker = JointPlanningWorker(
            queue_tts=serializable(self.state.items),
            source_video=self.video_path,
            source_language=self.source_language,
            target_language=self.language,
            name=Path(self.video_path).stem or 'untitled',
            candidate_dir=candidate_dir,
            project_dir=state_dir,
            candidate_backend=candidate_backend,
            limit=20,
            parent=None,
        )
        worker.done.connect(self._on_joint_plan_done)
        worker.failed.connect(self._on_joint_plan_failed)
        self._joint_worker = worker
        worker.start()

    def _finish_joint_worker(self):
        worker = self._joint_worker
        self._joint_worker = None
        self.joint_btn.setEnabled(True)
        self.joint_btn.setText(tr('View smart version'))
        if worker is not None:
            worker.deleteLater()

    def _on_joint_plan_done(self, _project, plan):
        self._finish_joint_worker()
        self._joint_project = _project
        self._joint_plan = plan
        generator = (plan.metadata or {}).get('candidate_generator', '')
        self.joint_status.setText(
            tr('{0} planned segments ({1})').format(len(plan.segments), generator))
        self._show_joint_plan()

    def _show_joint_plan(self):
        if self._joint_plan is None or self._joint_project is None:
            return
        can_synthesize = bool(self.state.items and self.state.items[0].get('tts_type') is not None)
        dialog = JointPlanPreviewDialog(
            self._joint_plan, project=self._joint_project,
            can_synthesize=can_synthesize, parent=self)
        dialog.seekRequested.connect(self._seek)
        dialog.synthesisRequested.connect(self._start_joint_synthesis)
        self._joint_dialog = dialog
        dialog.exec()
        self._joint_dialog = None

    def _on_joint_plan_failed(self, msg):
        self._finish_joint_worker()
        self.joint_btn.setText(tr('Retry smart optimization'))
        self.joint_status.setText(tr('Joint planning failed'))
        QMessageBox.warning(self, tr('Joint planning'), msg[:1000])

    def _start_joint_synthesis(self, plan_id: str):
        if (self._joint_synth_worker is not None or self._joint_project is None
                or self._quality_audit_worker is not None):
            return
        answer = QMessageBox.question(
            self._joint_dialog or self, tr('Generate A/B audio'),
            tr('Generate planned audio for up to 20 segments using the current TTS backend? This may take a long time.'),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        first = self.state.items[0] if self.state.items else {}
        tts_type = first.get('tts_type')
        if tts_type is None:
            QMessageBox.warning(self, tr('Generate A/B audio'), tr('No TTS backend is available.'))
            return
        if self._joint_dialog is not None:
            self._joint_dialog.set_synthesis_busy(
                True, tr('Generating planned audio...'))
        self.joint_status.setText(tr('Generating planned audio...'))
        worker = JointSynthesisWorker(
            project=self._joint_project,
            plan_id=plan_id,
            candidate_dir=str(Path(self.cache_folder) / 'joint_candidates'),
            tts_type=int(tts_type),
            language=self.language,
            project_dir=(self.project_dir or
                         str(Path(self.cache_folder) / 'joint-preview.tdproj')),
            parent=None,
        )
        worker.done.connect(self._on_joint_synthesis_done)
        worker.failed.connect(self._on_joint_synthesis_failed)
        self._joint_synth_worker = worker
        worker.start()

    def _finish_joint_synth_worker(self):
        worker = self._joint_synth_worker
        self._joint_synth_worker = None
        if worker is not None:
            worker.deleteLater()

    def _on_joint_synthesis_done(self, project, plan):
        self._finish_joint_synth_worker()
        self._joint_project = project
        self._joint_plan = plan
        ready = sum(1 for segment in plan.segments if segment.selected_audio_candidate_id)
        self.joint_status.setText(
            tr('A/B audio ready: {0}/{1} segments').format(ready, len(plan.segments)))
        if self._joint_dialog is not None:
            self._joint_dialog.update_result(project, plan)

    def _on_joint_synthesis_failed(self, msg):
        self._finish_joint_synth_worker()
        self.joint_status.setText(tr('A/B audio generation failed'))
        if self._joint_dialog is not None:
            self._joint_dialog.set_synthesis_busy(False, tr('A/B audio generation failed'))
        QMessageBox.warning(self, tr('Generate A/B audio'), msg[:1000])

    # ---- 退出路径 ----
    def _continue_synthesis(self):
        quality_failed = self.state.quality_failed_indices()
        if quality_failed:
            QMessageBox.warning(
                self, tr('Dubbing Studio'),
                tr('Resolve {0} quality issues before continuing.').format(
                    len(quality_failed)))
            return
        pending = self.redub_queue.pending()
        if pending:
            ret = QMessageBox.question(
                self, tr('Dubbing Studio'),
                tr('{0} lines still dubbing. Wait for them to finish?').format(len(pending)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ret == QMessageBox.StandardButton.Yes:
                return
        dirty = self.state.dirty_indices()
        if dirty:
            ret = QMessageBox.question(
                self, tr('Dubbing Studio'),
                tr('{0} lines modified but not re-dubbed. Continue anyway?').format(len(dirty)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ret != QMessageBox.StandardButton.Yes:
                return

        # 与旧弹窗一致：清空文本的行删除其音频
        for item in self.state.items:
            if not str(item.get('text') or '').strip():
                Path(item['filename']).unlink(missing_ok=True)

        try:
            self.state.save(self.cache_folder)
        except OSError as e:
            logger.exception(f'保存 queue_tts.json 失败: {e}', exc_info=True)
            QMessageBox.warning(self, tr('anerror'), str(e))
            return
        cleanup_previews(self.cache_folder)
        self._teardown()
        self._accepting = True
        if self._embedded:
            # 内嵌中途配音校对：不 accept，发信号交外层继续流水线
            self.proof_done.emit()
        else:
            self.accept()

    def _regenerate(self):
        """工程模式：保存编辑到工程，交由调用方跑 RealignWorker 只重对齐+合成。"""
        if self._quality_audit_worker is not None:
            QMessageBox.information(
                self, tr('Dubbing Studio'), tr('Starting strong quality review...'))
            return
        if str(self.language or '').lower().startswith('zh'):
            from videotrans.dub.quality_manifest import queue_quality_coverage
            from videotrans.tts._f5tts import F5TTS
            exact_coverage = queue_quality_coverage(
                self.state.items,
                self.project_dir,
                rules_version=F5TTS.QUALITY_RULES_VERSION,
                validator_model=F5TTS.VALIDATOR_MODEL,
                verify_audio_hashes=True,
            )
            if exact_coverage['missing']:
                self._quality_audit_required = True
                self._refresh_quality_summary()
        if self._quality_audit_required:
            QMessageBox.warning(
                self, tr('Dubbing Studio'),
                tr('Recheck this legacy project before exporting.'))
            return
        quality_failed = self.state.quality_failed_indices()
        if quality_failed:
            QMessageBox.warning(
                self, tr('Dubbing Studio'),
                tr('Resolve {0} quality issues before continuing.').format(
                    len(quality_failed)))
            return
        pending = self.redub_queue.pending()
        if pending:
            ret = QMessageBox.question(
                self, tr('Dubbing Studio'),
                tr('{0} lines still dubbing. Wait for them to finish?').format(len(pending)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ret == QMessageBox.StandardButton.Yes:
                return
        try:
            from videotrans.task.project import save_queue
            save_queue(self.project_dir, self.state.items)
        except OSError as e:
            logger.exception(f'保存工程 queue_tts.json 失败: {e}', exc_info=True)
            QMessageBox.warning(self, tr('anerror'), str(e))
            return
        cleanup_previews(self.cache_folder)
        self._teardown()
        self._accepting = True
        self.regenerate_requested.emit(self.project_dir)
        if not self._embedded:
            self.accept()

    def _on_back(self):
        """内嵌模式返回：停播放器、清理，交外层切态。"""
        self._teardown()
        self.back_requested.emit()

    def _terminate(self):
        ret = QMessageBox.question(
            self, tr('Dubbing Studio'), tr('Terminate this mission') + '?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ret == QMessageBox.StandardButton.Yes:
            self._teardown()
            self._accepting = True
            self.reject()

    def _teardown(self):
        self._rebuild_timer.stop()
        self.player.stop()
        for worker in (self._prep_worker, self._rebuild_worker,
                       self._joint_worker, self._joint_synth_worker,
                       self._quality_audit_worker):
            if worker is not None and worker.isRunning():
                cancel = getattr(worker, 'cancel', None)
                if callable(cancel):
                    cancel()
                for name in ('originalReady', 'dubbedReady', 'failed', 'done'):
                    sig = getattr(worker, name, None)
                    if sig is not None:
                        try:
                            sig.disconnect()
                        except RuntimeError:
                            pass
                worker.setParent(None)
                worker.finished.connect(worker.deleteLater)

    def closeEvent(self, event):
        if self._accepting:
            return super().closeEvent(event)
        # 流水线线程还阻塞在等待，X 关闭必须三选一，绝不静默放行
        event.ignore()
        box = QMessageBox(self)
        box.setWindowTitle(tr('Dubbing Studio'))
        box.setText(tr('Continue synthesis, terminate, or keep editing?'))
        cont = box.addButton(tr('Continue synthesis'), QMessageBox.ButtonRole.AcceptRole)
        term = box.addButton(tr('Terminate this mission'), QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is cont:
            self._continue_synthesis()
        elif box.clickedButton() is term:
            self._teardown()
            self._accepting = True
            self.reject()

    def keyPressEvent(self, event):
        from PySide6.QtWidgets import QPlainTextEdit
        focus = self.focusWidget()
        if event.key() == Qt.Key.Key_Space and not isinstance(focus, QPlainTextEdit):
            self.player.toggle()
        elif event.key() == Qt.Key.Key_Escape:
            # 屏蔽 QDialog 默认 ESC=reject，防止误触终止
            event.ignore()
        else:
            super().keyPressEvent(event)
