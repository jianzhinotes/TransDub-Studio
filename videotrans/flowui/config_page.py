"""Flow UI 配置页：单页智能配置（语言 + 三渠道卡 + 少量开关 + 一键开始）。

桥接策略 apply-on-start：仅在点「开始」时把选择回填进隐藏的旧主界面控件，
再调 win_action.check_start() 复用全部校验/持久化/Worker 启动/暂停路由。
"""
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QVBoxLayout, QWidget,
)

from videotrans.configure.config import app_cfg, logger, params, tr
from videotrans.flowui import curated, recent_tasks
from videotrans.flowui.channel_card import ChannelCard
from videotrans.task.simple_runnable_qt import run_in_threadpool

_QSS = """
#pageConfig QLabel#secTitle { font-size: 14px; font-weight: bold; color: #DFE1E2; }
#pageConfig QPushButton#startBtn {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1A72BB, stop:1 #6C5CE7);
    color: #FFFFFF; font-size: 16px; font-weight: bold; border-radius: 8px; border: none;
}
#pageConfig QPushButton#startBtn:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2286D8, stop:1 #7E6EF2);
}
#pageConfig QPushButton#startBtn:disabled { background: #455364; color: #788D9C; }
#pageConfig QLabel#startHint { color: #f39c12; font-size: 12px; }
#pageConfig QPushButton#linkBtn { border:none; background:transparent; color:#1A72BB; }
"""


class ConfigPage(QWidget):
    back_requested = Signal()
    started = Signal()
    _voicesFetched = Signal(int, list)   # tts_id, roles（工作线程发出，槽在 GUI 线程执行）

    def __init__(self, *, flow, parent=None):
        super().__init__(parent)
        self.flow = flow
        self.files = []
        self._workers_ready = False
        self.setObjectName('pageConfig')
        self.setStyleSheet(_QSS)

        from videotrans.translator import LANGNAME_DICT
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 16, 32, 16)
        layout.setSpacing(12)

        # 顶栏：返回 + 文件摘要 + 输出目录
        top = QHBoxLayout()
        back = QPushButton('← ' + tr('flow_back'))
        back.setObjectName('linkBtn')
        back.setCursor(Qt.CursorShape.PointingHandCursor)
        back.clicked.connect(self.back_requested)
        top.addWidget(back)
        self.files_label = QLabel('')
        self.files_label.setStyleSheet('color:#8a9ba8;')
        top.addWidget(self.files_label)
        top.addStretch(1)
        self.outdir_btn = QPushButton(tr('flow_output_dir'))
        self.outdir_btn.setObjectName('linkBtn')
        self.outdir_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.outdir_btn.clicked.connect(self._pick_outdir)
        top.addWidget(self.outdir_btn)
        layout.addLayout(top)

        # 语言行
        lang_row = QHBoxLayout()
        lang_row.addWidget(QLabel(tr('Source language')))
        self.source_lang = QComboBox()
        self.source_lang.addItems(list(LANGNAME_DICT.values()))
        lang_row.addWidget(self.source_lang, stretch=1)
        lang_row.addSpacing(24)
        lang_row.addWidget(QLabel(tr('Target lang')))
        self.target_lang = QComboBox()
        self.target_lang.addItems(list(LANGNAME_DICT.values()))
        lang_row.addWidget(self.target_lang, stretch=1)
        layout.addLayout(lang_row)

        # 三渠道卡
        cards_row = QHBoxLayout()
        self.recogn_card = ChannelCard(kind=curated.KIND_RECOGN, curated_ids=curated.CURATED_RECOGN)
        self.trans_card = ChannelCard(kind=curated.KIND_TRANS, curated_ids=curated.CURATED_TRANS)
        self.tts_card = ChannelCard(kind=curated.KIND_TTS, curated_ids=curated.CURATED_TTS)
        for c in (self.recogn_card, self.trans_card, self.tts_card):
            cards_row.addWidget(c, stretch=1)
        layout.addLayout(cards_row)

        # 开关行
        toggles = QHBoxLayout()
        toggles.addWidget(QLabel(tr('flow_subtitle_label')))
        self.subtitle_box = QComboBox()
        self.subtitle_box.addItems([tr('nosubtitle'), tr('embedsubtitle'), tr('softsubtitle')])
        toggles.addWidget(self.subtitle_box)
        toggles.addSpacing(16)
        self.auto_align = QCheckBox(tr('flow_auto_align'))
        toggles.addWidget(self.auto_align)
        self.keep_bgm = QCheckBox(tr('flow_keep_bgm'))
        toggles.addWidget(self.keep_bgm)
        toggles.addStretch(1)
        layout.addLayout(toggles)

        layout.addStretch(1)

        # 开始
        self.start_hint = QLabel('')
        self.start_hint.setObjectName('startHint')
        self.start_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.start_hint)
        self.start_btn = QPushButton('✨ ' + tr('flow_start'))
        self.start_btn.setObjectName('startBtn')
        self.start_btn.setMinimumHeight(48)
        self.start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_btn.clicked.connect(self._on_start)
        layout.addWidget(self.start_btn)

        # 联动
        self._voicesFetched.connect(self._apply_voices)
        self.tts_card.channel_changed.connect(lambda _id: self._reload_voices())
        self.recogn_card.channel_changed.connect(lambda _id: self._reload_models())
        self.target_lang.currentTextChanged.connect(lambda _t: self._reload_voices())
        self.source_lang.currentTextChanged.connect(lambda _t: self._check_langs())

        # 状态点轮询（winform 保存 Key 后 1s 内变绿）
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._refresh_all_status)

        self._load_defaults()

    # ---- 生命周期 ----
    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_all_status()
        self._status_timer.start()

    def hideEvent(self, event):
        self._status_timer.stop()
        super().hideEvent(event)

    def load(self, files: list):
        self.files = list(files)
        self.files_label.setText(tr('flow_files_count').replace('{0}', str(len(self.files))))
        self.files_label.setToolTip('\n'.join(self.files))

    def set_workers_ready(self, ready: bool):
        self._workers_ready = ready
        self._update_start_enabled()

    # ---- 默认值 ----
    def _load_defaults(self):
        src = params.get('source_language')
        if src:
            self.source_lang.setCurrentText(src)
        tgt = params.get('target_language')
        if tgt and tgt != '-':
            self.target_lang.setCurrentText(tgt)

        for card, key in ((self.recogn_card, 'recogn_type'),
                          (self.trans_card, 'translate_type'),
                          (self.tts_card, 'tts_type')):
            saved = params.get(key)
            if isinstance(saved, int) and saved in curated.CURATED[card.kind]:
                card.select_channel(saved)

        st = params.get('subtitle_type')
        if isinstance(st, int) and 0 <= st <= 2:
            self.subtitle_box.setCurrentIndex(st)
        self.auto_align.setChecked(bool(params.get('voice_autorate', True)))
        self.keep_bgm.setChecked(bool(params.get('is_separate', False)))

        self._reload_models()
        self._reload_voices()

    # ---- 次级下拉 ----
    def _reload_models(self):
        from videotrans import recognition
        cid = self.recogn_card.current_channel_id()
        if cid in recognition.ALLOW_CHANGE_MODEL:
            models = recognition.get_model_by_type(cid) or []
            self.recogn_card.set_secondary_items(models, params.get('model_name'))
            self.recogn_card.secondary_box.setVisible(True)
        else:
            self.recogn_card.set_secondary_items([])
            self.recogn_card.secondary_box.setVisible(False)
        self._check_langs()

    def _reload_voices(self):
        # role_menu 可能联网（如 ElevenLabs），放线程池，结果回 GUI 线程
        from videotrans.translator import get_code
        tts_id = self.tts_card.current_channel_id()
        code = get_code(show_text=self.target_lang.currentText())
        lang = code if code and code != '-' else None

        def fetch():
            from videotrans.util.help_role import role_menu
            try:
                roles = role_menu(tts_id, lang) or ['No']
            except Exception as e:
                logger.warning(f'获取音色列表失败: {e}')
                roles = [params.get('voice_role') or 'No']
            try:
                self._voicesFetched.emit(tts_id, list(roles))
            except RuntimeError:
                pass   # 页面已销毁（应用退出），丢弃结果

        run_in_threadpool(fetch)

    def _apply_voices(self, tts_id: int, roles: list):
        # 渠道在等待期间又变了则丢弃过期结果
        if self.tts_card.current_channel_id() != tts_id:
            return
        self.tts_card.set_secondary_items(roles, params.get('voice_role'))
        self._check_langs()

    def _check_langs(self):
        from videotrans import recognition, tts
        from videotrans.translator import get_code
        src = get_code(show_text=self.source_lang.currentText())
        tgt = get_code(show_text=self.target_lang.currentText())
        warn = recognition.is_allow_lang(
            langcode=src, recogn_type=self.recogn_card.current_channel_id(),
            model_name=self.recogn_card.current_secondary() or '')
        self.recogn_card.set_warning('' if warn is True else str(warn))
        if tgt and tgt != '-':
            warn2 = tts.is_allow_lang(langcode=tgt, tts_type=self.tts_card.current_channel_id())
            self.tts_card.set_warning('' if warn2 is True else str(warn2))

    # ---- 状态与开始门控 ----
    def _refresh_all_status(self):
        for c in (self.recogn_card, self.trans_card, self.tts_card):
            c.refresh_status()
        self._update_start_enabled()

    def _update_start_enabled(self):
        reasons = []
        if not self._workers_ready:
            reasons.append(tr('flow_waiting_workers'))
        for c in (self.recogn_card, self.trans_card, self.tts_card):
            if not c.is_ready():
                reasons.append(tr('flow_need_key') + ': ' + c.provider().name)
        self.start_hint.setText('；'.join(reasons))
        self.start_btn.setDisabled(bool(reasons))

    def _pick_outdir(self):
        self.flow.win_action.get_save_dir()
        if self.flow.main.target_dir:
            self.outdir_btn.setText(tr('flow_output_dir') + ': ' + self.flow.main.target_dir)

    # ---- 桥接与启动 ----
    def apply_to_classic_ui(self) -> bool:
        """把本页选择回填进旧控件；返回 False 表示回填失败应中止。"""
        main = self.flow.main
        wa = self.flow.win_action

        wa.queue_mp4 = list(self.files)
        main.source_mp4.setText(f'{len(self.files)} videos')

        main.source_language.setCurrentText(self.source_lang.currentText())
        main.target_language.setCurrentText(self.target_lang.currentText())
        main.translate_type.setCurrentIndex(self.trans_card.current_channel_id())

        # 同值 setCurrentIndex 不发信号，显式触发 handler 重建模型/音色列表
        main.recogn_type.setCurrentIndex(self.recogn_card.current_channel_id())
        wa.recogn_type_change()
        model = self.recogn_card.current_secondary()
        if model:
            main.model_name.setCurrentText(model)

        tts_id = self.tts_card.current_channel_id()
        main.tts_type.setCurrentIndex(tts_id)
        wa.tts_type_change(tts_id)
        voice = self.tts_card.current_secondary() or 'No'
        main.voice_role.setCurrentText(voice)
        # 最尖锐的坑：音色不在重建后的列表 → 静默停在 'No' → set_mode 会切成提取模式
        if voice != 'No' and main.voice_role.currentText() != voice:
            QMessageBox.warning(self, tr('flow_start'),
                                tr('flow_voice_missing').replace('{0}', voice))
            self._reload_voices()
            return False

        main.subtitle_type.setCurrentIndex(self.subtitle_box.currentIndex())
        main.voice_autorate.setChecked(self.auto_align.isChecked())
        main.is_separate.setChecked(self.keep_bgm.isChecked())
        if self.keep_bgm.isChecked():
            main.embed_bgm.setChecked(True)
        main.app_mode = 'biaozhun'
        return True

    def _on_start(self):
        if not self.files:
            self.back_requested.emit()
            return
        if not self.apply_to_classic_ui():
            return

        for f in self.files:
            recent_tasks.append({
                'video_path': f,
                'target_dir': self.flow.main.target_dir or '',
                'source_language': self.source_lang.currentText(),
                'target_language': self.target_lang.currentText(),
            })

        self.started.emit()
        self.flow.win_action.check_start()
        # 校验失败时 check_start 已弹窗并复位状态；watchdog 把用户带回本页
        QTimer.singleShot(2000, self._watchdog)

    def _watchdog(self):
        if app_cfg.current_status != 'ing':
            self.flow.setCurrentWidget(self)
