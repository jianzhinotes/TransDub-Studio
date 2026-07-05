"""预览播放器：视频(含原声) + 配音 wav 双 QMediaPlayer，静音切换 A/B。

视频播放器是主时钟；配音播放器跟随，通过低频纠偏消除漂移。
两路都是本地文件，切换只翻转两个 QAudioOutput 的 muted，零延迟不丢位置。
"""
from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget

AUDIO_ORIGINAL = 'original'
AUDIO_DUBBED = 'dubbed'

# 偏差超过该值才纠偏；过小会频繁 setPosition 产生可闻的咔哒声
_DRIFT_THRESHOLD_MS = 80
_DRIFT_CHECK_INTERVAL_MS = 250


class PreviewPlayer(QObject):
    positionChanged = Signal(int)   # ms，来自视频主时钟
    durationChanged = Signal(int)
    playStateChanged = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.video_widget = QVideoWidget()

        self.video_player = QMediaPlayer(self)
        self.video_audio = QAudioOutput(self)
        self.video_player.setAudioOutput(self.video_audio)
        self.video_player.setVideoOutput(self.video_widget)

        self.dub_player = QMediaPlayer(self)
        self.dub_audio = QAudioOutput(self)
        self.dub_player.setAudioOutput(self.dub_audio)

        self._has_dub = False
        self._mode = AUDIO_ORIGINAL
        self._poster_pending = False   # 待渲染首帧海报
        self._postering = False        # 正在跑首帧渲染，期间不对外冒播放状态

        # qlonglong 信号不能直连 Signal(int)，经 lambda 转发
        self.video_player.positionChanged.connect(
            lambda p: self.positionChanged.emit(int(p)))
        self.video_player.durationChanged.connect(
            lambda d: self.durationChanged.emit(int(d)))
        self.video_player.playbackStateChanged.connect(self._on_playback_state)
        # 播完自动复位到暂停态（两个播放器都停）
        self.video_player.mediaStatusChanged.connect(self._on_media_status)

        self._drift_timer = QTimer(self)
        self._drift_timer.setInterval(_DRIFT_CHECK_INTERVAL_MS)
        self._drift_timer.timeout.connect(self._correct_drift)

    # ---- 加载 ----
    def load(self, video_path: str, dub_wav_path: str = None):
        self.video_player.setSource(QUrl.fromLocalFile(video_path))
        self._has_dub = bool(dub_wav_path)
        if self._has_dub:
            self.dub_player.setSource(QUrl.fromLocalFile(dub_wav_path))
        self.set_audio_mode(self._mode)
        # 加载后暂停态不渲染首帧（黑屏），标记待渲染海报，媒体就绪后顶出首帧
        self._poster_pending = True

    def set_dub_source(self, dub_wav_path: str):
        # 波形/预览 wav 后台生成完成后再挂载
        pos = self.video_player.position()
        self.dub_player.setSource(QUrl.fromLocalFile(dub_wav_path))
        self._has_dub = True
        self.dub_player.setPosition(pos)
        if self.is_playing():
            self.dub_player.play()
        self.set_audio_mode(self._mode)

    # ---- 播放控制 ----
    def is_playing(self) -> bool:
        return self.video_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def play(self):
        self.video_player.play()
        if self._has_dub:
            self.dub_player.setPosition(self.video_player.position())
            self.dub_player.play()
        self._drift_timer.start()

    def pause(self):
        self._drift_timer.stop()
        self.video_player.pause()
        if self._has_dub:
            self.dub_player.pause()

    def toggle(self):
        if self.is_playing():
            self.pause()
        else:
            self.play()

    def stop(self):
        self._drift_timer.stop()
        self.video_player.stop()
        self.dub_player.stop()

    def seek(self, ms: int):
        was_playing = self.is_playing()
        if was_playing:
            self.pause()
        self.video_player.setPosition(ms)
        if self._has_dub:
            self.dub_player.setPosition(ms)
        if was_playing:
            self.play()

    def position(self) -> int:
        return self.video_player.position()

    def duration(self) -> int:
        return self.video_player.duration()

    # ---- A/B 切换 ----
    def set_audio_mode(self, mode: str):
        self._mode = mode
        dubbed = mode == AUDIO_DUBBED and self._has_dub
        self.video_audio.setMuted(dubbed)
        self.dub_audio.setMuted(not dubbed)

    def audio_mode(self) -> str:
        return self._mode

    # ---- 内部 ----
    def _correct_drift(self):
        if not self._has_dub or not self.is_playing():
            return
        drift = abs(self.dub_player.position() - self.video_player.position())
        if drift > _DRIFT_THRESHOLD_MS:
            self.dub_player.setPosition(self.video_player.position())

    def _on_playback_state(self, st):
        # 首帧海报渲染期间的 play/pause 属内部动作，不对外冒播放状态（避免按钮闪烁）
        if self._postering:
            return
        self.playStateChanged.emit(st == QMediaPlayer.PlaybackState.PlayingState)

    def _render_poster(self):
        """静音快速播放片刻再暂停回开头，把视频首帧顶出来当预览画面（类 YouTube 海报）。"""
        self._postering = True
        self.video_audio.setMuted(True)
        self.video_player.play()
        QTimer.singleShot(180, self._finish_poster)

    def _finish_poster(self):
        self.video_player.pause()
        self.video_player.setPosition(0)
        self._postering = False
        self.set_audio_mode(self._mode)     # 恢复正确的静音状态
        self.playStateChanged.emit(False)   # UI 复位到"未播放"（显示播放钮）

    def _on_media_status(self, status):
        if status in (QMediaPlayer.MediaStatus.LoadedMedia,
                      QMediaPlayer.MediaStatus.BufferedMedia) and self._poster_pending:
            self._poster_pending = False
            self._render_poster()
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._drift_timer.stop()
            self.dub_player.pause()
