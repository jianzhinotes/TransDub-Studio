"""Dubbing Studio：配音后精修工作台（替代旧的表格校对弹窗）。

上部：视频预览 + 逐句说话人卡片（原文/译文/音色/试听/重配）；
下部：可编辑时间轴（拖块移动、拉端点改时长）+ 原声/配音波形。
无倒计时——用户点「继续合成」才放行流水线，「终止任务」停止。
"""
import json
import threading
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
    build_dub_preview_wav, cleanup_previews, preview_loading_policy,
)
from videotrans.component.timeline.edit_logic import serializable
from videotrans.component.timeline.edit_track import EditableSubtitleTrack
from videotrans.component.timeline.joint_plan import (
    JointPlanPreviewDialog, JointPlanningWorker, JointSynthesisWorker,
)
from videotrans.component.timeline.peaks import extract_peaks
from videotrans.component.timeline.player import AUDIO_DUBBED, AUDIO_ORIGINAL, PreviewPlayer
from videotrans.component.timeline.redub import RedubQueue
from videotrans.component.timeline.studio_state import StudioState
from videotrans.component.timeline.timeline_view import TimelineView
from videotrans.util import tools
from videotrans.util.tools import vail_file

_REBUILD_DEBOUNCE_MS = 800


class _PreviewRebuildWorker(QThread):
    done = Signal(object, str)   # peaks, wav_path
    failed = Signal(str)

    def __init__(self, queue_snapshot, duration_ms, cache_folder, out_name, parent=None):
        super().__init__(parent=parent)
        self._queue = queue_snapshot
        self._duration_ms = duration_ms
        self._cache_folder = cache_folder
        self._out_name = out_name

    def run(self):
        try:
            wav = build_dub_preview_wav(self._queue, self._duration_ms,
                                        self._cache_folder, out_name=self._out_name)
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
                 source_language=None, auto_plan=True):
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
        estimated_duration = max(
            [int(item.get('end_time', 0) or 0) for item in queue_tts] or [1])
        self._duration_ms = max(estimated_duration, 1)
        self.state = StudioState(
            queue_tts, duration_ms=self._duration_ms, parent=self)
        self._auto_plan = bool(auto_plan)
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

        roles = self._compute_roles(queue_tts)
        self.redub_queue = RedubQueue(self.state, language, parent=self)

        # ---- 布局 ----
        layout = QVBoxLayout(self)

        splitter = QSplitter(Qt.Orientation.Horizontal)
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

        # 播放/时间已移入视频悬浮控件；此行仅保留 A/B 音轨切换
        ctrl = QHBoxLayout()
        ctrl.addStretch(1)
        self.original_radio = QRadioButton(tr("Original audio"))
        self.original_radio.setChecked(True)
        self.dubbed_radio = QRadioButton(tr("Dubbed audio"))
        self.dubbed_radio.setEnabled(False)
        self.original_radio.toggled.connect(
            lambda orig: self.player.set_audio_mode(AUDIO_ORIGINAL if orig else AUDIO_DUBBED))
        ctrl.addWidget(self.original_radio)
        ctrl.addWidget(self.dubbed_radio)
        left_layout.addLayout(ctrl)
        splitter.addWidget(left)

        # 右：卡片列表
        self.cards = SpeakerCardList(self.state, roles)
        splitter.addWidget(self.cards)
        splitter.setSizes([700, 560])
        layout.addWidget(splitter, stretch=1)

        # 下：可编辑时间轴
        hint = QLabel(tr("Drag block to move, drag edge to resize"))
        hint.setStyleSheet('color:#9AA7B4;font-size:12px;')
        layout.addWidget(hint)
        self.timeline = TimelineView(
            self._duration_ms, subtitle_track_cls=EditableSubtitleTrack)
        self.wave_original = self.timeline.add_waveform_track(tr("Original audio"))
        self.wave_original.set_placeholder(tr("Generating waveform..."))
        self.wave_dubbed = self.timeline.add_waveform_track(tr("Dubbed audio"))
        self.wave_dubbed.set_placeholder(tr("Generating waveform..."))
        self.timeline.set_subtitles(self.state.items)
        layout.addWidget(self.timeline)

        # 底部按钮行
        bottom = QHBoxLayout()
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
        bottom.addWidget(self.continue_btn)
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
        bottom.addWidget(cancel_btn)
        bottom.addStretch(1)
        layout.addLayout(bottom)

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
        self.player.set_dub_source(dub_wav)
        self.dubbed_radio.setEnabled(True)
        self._prev_preview_wav = dub_wav

    def _on_prep_failed(self, msg):
        short = msg.splitlines()[0] if msg else 'unknown'
        self.wave_original.set_placeholder(f'{tr("anerror")}: {short}')
        self.wave_dubbed.set_placeholder('')

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
        if self._eager_dubbed_preview:
            self._rebuild_timer.start()

    # ---- 重配 ----
    def _on_redub_requested(self, idx: int):
        card = self.cards.card(idx)
        if card:
            card.set_busy(True, queued=bool(self.redub_queue.pending()))
        self.redub_queue.enqueue(idx)

    def _on_redub_started(self, idx: int):
        card = self.cards.card(idx)
        if card:
            card.set_busy(True, queued=False)

    def _on_redub_finished(self, idx: int, ok: bool, err: str):
        card = self.cards.card(idx)
        if card:
            card.set_busy(False)
            card.refresh()
        if ok:
            if self._eager_dubbed_preview:
                self._rebuild_timer.start()
        else:
            QMessageBox.warning(self, tr('anerror'), err[:600])

    # ---- 单句试听 ----
    def _play_single_line(self, idx: int):
        filename = self.state.items[idx].get('filename')
        if not filename or not vail_file(filename):
            QMessageBox.information(self, tr('Dubbing Studio'), tr('No audio'))
            return
        threading.Thread(target=tools.pygameaudio, args=(filename,), daemon=True).start()

    # ---- 预览重建（去抖） ----
    def _start_rebuild(self):
        if self._rebuild_worker is not None:
            self._rebuild_pending = True
            return
        self._preview_rev += 1
        self.wave_dubbed.set_placeholder(tr("Rebuilding dubbed preview..."))
        worker = _PreviewRebuildWorker(
            serializable(self.state.items), self._duration_ms, self.cache_folder,
            f'dub_preview_{self._preview_rev}.wav', parent=self)
        worker.done.connect(self._on_rebuild_done)
        worker.failed.connect(self._on_rebuild_failed)
        self._rebuild_worker = worker
        worker.start()

    def _on_rebuild_done(self, peaks, wav):
        self._rebuild_worker = None
        self.wave_dubbed.set_placeholder('')
        self.wave_dubbed.set_clips([(0, peaks)])
        self.player.set_dub_source(wav)
        self.dubbed_radio.setEnabled(True)
        # 删除上一版预览文件
        if self._prev_preview_wav and self._prev_preview_wav != wav:
            Path(self._prev_preview_wav).unlink(missing_ok=True)
        self._prev_preview_wav = wav
        if self._rebuild_pending:
            self._rebuild_pending = False
            self._rebuild_timer.start()

    def _on_rebuild_failed(self, msg):
        self._rebuild_worker = None
        self.wave_dubbed.set_placeholder(f'{tr("anerror")}: {msg[:80]}')
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
        if self._joint_synth_worker is not None or self._joint_project is None:
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
                       self._joint_worker, self._joint_synth_worker):
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
