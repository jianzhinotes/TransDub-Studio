"""时间轴预览对话框：视频 + 原声/配音波形 + 字幕块 同步对照。

只读预览：拖动播放头/点字幕块跳转、原声⇄配音切换、缩放对照，
在最终合成前即可检查配音与字幕的时间轴对齐问题。
"""
import traceback

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QRadioButton,
    QSizePolicy, QVBoxLayout,
)

from videotrans.configure.config import ROOT_DIR, logger, tr
from videotrans.component.timeline.dub_preview import build_dub_preview_wav
from videotrans.component.timeline.peaks import extract_peaks
from videotrans.component.timeline.player import AUDIO_DUBBED, AUDIO_ORIGINAL, PreviewPlayer
from videotrans.component.timeline.timeline_view import TimelineView


class PrepWorker(QThread):
    """后台准备波形与配音预览 wav，避免阻塞对话框弹出。"""
    originalReady = Signal(object, int)   # peaks, duration_ms
    dubbedReady = Signal(object, str)     # peaks, preview_wav_path
    failed = Signal(str)

    def __init__(self, *, source_media, cache_dir, dubbed_audio=None,
                 queue_tts=None, parent=None):
        super().__init__(parent=parent)
        self.source_media = source_media
        self.cache_dir = cache_dir
        self.dubbed_audio = dubbed_audio
        self.queue_tts = queue_tts

    def run(self):
        try:
            peaks, duration_ms = extract_peaks(self.source_media, self.cache_dir)
            self.originalReady.emit(peaks, duration_ms)

            dub_wav = self.dubbed_audio
            if not dub_wav and self.queue_tts:
                dub_wav = build_dub_preview_wav(self.queue_tts, duration_ms, self.cache_dir)
            if dub_wav:
                dub_peaks, _ = extract_peaks(dub_wav, self.cache_dir)
                self.dubbedReady.emit(dub_peaks, dub_wav)
        except Exception as e:
            logger.exception(f'时间轴预览波形准备失败: {e}', exc_info=True)
            self.failed.emit(f'{e}\n{traceback.format_exc()}')


_PrepWorker = PrepWorker  # 兼容旧名


class TimelinePreviewDialog(QDialog):
    def __init__(self, *, video_path, subtitle_items,
                 source_audio=None, dubbed_audio=None,
                 queue_tts=None, cache_folder=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Timeline Preview"))
        self.setWindowIcon(QIcon(f"{ROOT_DIR}/videotrans/styles/icon.ico"))
        self.setMinimumSize(960, 640)
        self.setWindowFlags(Qt.WindowTitleHint | Qt.WindowSystemMenuHint
                            | Qt.WindowMaximizeButtonHint | Qt.WindowCloseButtonHint)

        self._subtitle_items = list(subtitle_items or [])
        self._duration_ms = 1

        layout = QVBoxLayout(self)

        # 播放器与视频区（悬浮控件承载播放/暂停/进度）
        self.player = PreviewPlayer(self)
        from videotrans.component.timeline.video_overlay import VideoOverlay
        self.video_area = VideoOverlay(self.player)
        self.video_area.setMinimumSize(480, 270)
        self.video_area.setSizePolicy(QSizePolicy.Policy.Expanding,
                                      QSizePolicy.Policy.Expanding)
        layout.addWidget(self.video_area, stretch=1)

        # 当前字幕
        self.subtitle_label = QLabel('')
        self.subtitle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setStyleSheet('font-size:15px;color:#DFE1E2;padding:2px 8px;')
        self.subtitle_label.setMinimumHeight(40)
        layout.addWidget(self.subtitle_label)

        # 控制条（播放/时间已移入视频悬浮控件）
        ctrl = QHBoxLayout()
        ctrl.addStretch(1)

        self.original_radio = QRadioButton(tr("Original audio"))
        self.original_radio.setChecked(True)
        self.dubbed_radio = QRadioButton(tr("Dubbed audio"))
        self.dubbed_radio.setEnabled(False)
        self.original_radio.toggled.connect(self._on_audio_mode)
        ctrl.addWidget(self.original_radio)
        ctrl.addWidget(self.dubbed_radio)
        ctrl.addStretch(1)

        for text, fn in ((tr("Zoom out"), lambda: self.timeline.zoom_out()),
                         (tr("Zoom in"), lambda: self.timeline.zoom_in()),
                         (tr("Fit"), lambda: self.timeline.zoom_fit())):
            btn = QPushButton(text)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(fn)
            ctrl.addWidget(btn)

        close_btn = QPushButton(tr("Close"))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.close)
        ctrl.addWidget(close_btn)
        layout.addLayout(ctrl)

        # 时间轴
        self.timeline = TimelineView(1)
        self.wave_original = self.timeline.add_waveform_track(tr("Original audio"))
        self.wave_original.set_placeholder(tr("Generating waveform..."))
        self.wave_dubbed = self.timeline.add_waveform_track(tr("Dubbed audio"))
        self.wave_dubbed.set_placeholder(tr("Generating waveform..."))
        self.timeline.set_subtitles(self._subtitle_items)
        layout.addWidget(self.timeline)

        # 接线
        self.timeline.seekRequested.connect(self.player.seek)
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)

        # 视频立即可拖看；波形后台生成
        self.player.load(video_path)
        self._worker = PrepWorker(
            source_media=source_audio or video_path,
            cache_dir=cache_folder or ROOT_DIR + '/tmp',
            dubbed_audio=dubbed_audio,
            queue_tts=queue_tts,
        )
        self._worker.originalReady.connect(self._on_original_ready)
        self._worker.dubbedReady.connect(self._on_dubbed_ready)
        self._worker.failed.connect(self._on_prep_failed)
        self._worker.start()

        QTimer.singleShot(0, self.timeline.zoom_fit)

    # ---- worker 回调 ----
    def _on_original_ready(self, peaks, duration_ms):
        self._duration_ms = max(self._duration_ms, int(duration_ms))
        self.timeline.scale.set_duration(self._duration_ms)
        self.wave_original.set_clips([(0, peaks)])
        self.timeline.zoom_fit()

    def _on_dubbed_ready(self, peaks, dub_wav):
        self.wave_dubbed.set_clips([(0, peaks)])
        self.player.set_dub_source(dub_wav)
        self.dubbed_radio.setEnabled(True)

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
            self.timeline.scale.set_duration(self._duration_ms)

    def _on_position(self, ms: int):
        self.timeline.set_position(ms)
        idx = self.timeline.subtitle_track.index_for_ms(ms)
        if idx >= 0 and ms <= int(self._subtitle_items[idx]['end_time']):
            self.timeline.subtitle_track.set_active(idx)
            self.subtitle_label.setText(str(self._subtitle_items[idx]['text']))
        else:
            self.timeline.subtitle_track.set_active(-1)
            self.subtitle_label.setText('')

    def _on_audio_mode(self, original_checked: bool):
        self.player.set_audio_mode(AUDIO_ORIGINAL if original_checked else AUDIO_DUBBED)

    # ---- 键盘 ----
    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space:
            self.player.toggle()
        elif event.key() == Qt.Key.Key_Left:
            self.player.seek(max(self.player.position() - 1000, 0))
        elif event.key() == Qt.Key.Key_Right:
            self.player.seek(min(self.player.position() + 1000, self._duration_ms))
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        self.player.stop()
        if self._worker.isRunning():
            # 不阻塞关闭：断开信号后让线程自行结束并清理
            for sig in (self._worker.originalReady, self._worker.dubbedReady, self._worker.failed):
                try:
                    sig.disconnect()
                except RuntimeError:
                    pass
            self._worker.setParent(None)
            self._worker.finished.connect(self._worker.deleteLater)
        super().closeEvent(event)
