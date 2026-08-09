"""Flow UI 配置页：单页智能配置（语言 + 三渠道卡 + 少量开关 + 一键开始）。

桥接策略 apply-on-start：仅在点「开始」时把选择回填进隐藏的旧主界面控件，
再调 win_action.check_start() 复用全部校验/持久化/Worker 启动/暂停路由。
"""
import os
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from videotrans.configure.config import app_cfg, logger, params, settings, tr
from videotrans.dub import presets
from videotrans.flowui import curated, recent_tasks
from videotrans.flowui.channel_card import ChannelCard
from videotrans.flowui.engine_summary import EngineSummary
from videotrans.styles import tokens
from videotrans.task.simple_runnable_qt import run_in_threadpool

_QSS = f"""
#pageConfig, #cfgScroll, #cfgContent {{ background: {tokens.WINDOW_BG}; }}
#pageConfig QLabel#secTitle {{ font-size: 13px; color: {tokens.TEXT_SECONDARY}; }}
#cfgPanel {{ background: {tokens.SURFACE}; border: 1px solid {tokens.BORDER}; border-radius: 10px; }}
#pageConfig QPushButton#startBtn {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {tokens.ACCENT}, stop:1 #6C5CE7);
    color: #FFFFFF; font-size: 16px; font-weight: bold; border-radius: 10px; border: none;
}}
#pageConfig QPushButton#startBtn:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2286D8, stop:1 #7E6EF2);
}}
#pageConfig QPushButton#startBtn:disabled {{ background: {tokens.BORDER}; color: #788D9C; }}
#pageConfig QLabel#startHint {{ color: {tokens.WARNING}; font-size: 12px; }}
#pageConfig QLabel#startHint[level="error"] {{ color: {tokens.ERROR}; }}
#pageConfig QPushButton#linkBtn {{ border:none; background:transparent; color:{tokens.ACCENT}; }}
#pageConfig QFrame#engineRow:hover {{ background: {tokens.ELEVATED}; border-radius: 6px; }}
#pageConfig QLabel#engineKind {{ color: {tokens.TEXT_SECONDARY}; font-size: 12px; }}
#pageConfig QLabel#engineName {{ color: {tokens.TEXT}; font-size: 13px; }}
"""


class ConfigPage(QWidget):
    back_requested = Signal()
    started = Signal()
    start_failed = Signal()              # check_start 未进入运行态时发出，工作区据此切回配置
    # tts_id, request serial, roles（工作线程发出，槽在 GUI 线程执行）
    # The serial prevents a slow, stale role request from replacing a voice
    # the user selected after changing language/provider.
    _voicesFetched = Signal(int, int, list)

    def __init__(self, *, flow, parent=None):
        super().__init__(parent)
        self.flow = flow
        self.files = []
        self._workers_ready = False
        self._workers_error = ''
        self._boot_started = time.monotonic()
        self._voice_request_serial = 0
        self.setObjectName('pageConfig')
        self.setStyleSheet(_QSS)

        from videotrans.translator import LANGNAME_DICT
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)
        self._advanced_visible = False

        # 顶栏：返回 + 文件摘要 + 输出目录
        top = QHBoxLayout()
        back = QPushButton('← ' + tr('flow_back'))
        back.setObjectName('linkBtn')
        back.setCursor(Qt.CursorShape.PointingHandCursor)
        back.clicked.connect(self.back_requested)
        top.addWidget(back)
        self.files_label = QLabel('')
        self.files_label.setStyleSheet(f'color:{tokens.TEXT_SECONDARY};')
        top.addWidget(self.files_label)
        top.addStretch(1)
        self.outdir_btn = QPushButton(tr('flow_output_dir'))
        self.outdir_btn.setObjectName('linkBtn')
        self.outdir_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.outdir_btn.clicked.connect(self._pick_outdir)
        top.addWidget(self.outdir_btn)
        layout.addLayout(top)

        # 三渠道卡先构造（此处不设父子，下面 clay.addWidget 时才 reparent），
        # 以便主面板的只读摘要条能镜像它们的状态。
        self.recogn_card = ChannelCard(kind=curated.KIND_RECOGN, curated_ids=curated.CURATED_RECOGN)
        self.trans_card = ChannelCard(kind=curated.KIND_TRANS, curated_ids=curated.CURATED_TRANS)
        self.tts_card = ChannelCard(kind=curated.KIND_TTS, curated_ids=curated.CURATED_TTS)

        # 默认只展示用户必须理解的两个选择；模型和工程参数全部折叠。
        quick_panel = self._panel()
        # 面板按内容收紧：否则隐藏高级区时它会分走一半的富余空间被撑成大片留白
        quick_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        quick = QVBoxLayout(quick_panel)
        quick.setContentsMargins(20, 18, 20, 18)
        quick.setSpacing(10)
        quick_title = QLabel('✨ ' + tr('flow_smart_ready_title'))
        quick_title.setStyleSheet(f'font-size:20px;font-weight:bold;color:{tokens.TEXT};')
        quick_title.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        quick.addWidget(quick_title)
        self.quick_summary = QLabel(tr('flow_smart_ready_summary'))
        self.quick_summary.setWordWrap(True)
        self.quick_summary.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.quick_summary.setStyleSheet(f'color:{tokens.TEXT_SECONDARY};')
        quick.addWidget(self.quick_summary)
        delivery_row = QHBoxLayout()
        delivery_row.addWidget(QLabel(tr('flow_delivery_label')))
        self.delivery_box = QComboBox()
        self.delivery_box.addItem(tr('flow_delivery_dubbed'), 'dubbed')
        self.delivery_box.addItem(tr('flow_delivery_bilingual'), 'bilingual')
        self.delivery_box.setToolTip(tr('flow_delivery_bilingual_tip'))
        delivery_row.addWidget(self.delivery_box, stretch=1)
        quick.addLayout(delivery_row)
        target_row = QHBoxLayout()
        target_row.addWidget(QLabel(tr('Target lang')))
        self.target_lang = QComboBox()
        self.target_lang.addItems(list(LANGNAME_DICT.values()))
        target_row.addWidget(self.target_lang, stretch=1)
        quick.addLayout(target_row)
        # 引擎可见性：一眼看到本次用什么引擎、配好没有；点行展开高级设置更换
        self.engine_summary = EngineSummary(
            cards=(self.recogn_card, self.trans_card, self.tts_card))
        self.engine_summary.expandRequested.connect(self._expand_advanced_to)
        quick.addWidget(self.engine_summary)
        layout.addWidget(quick_panel)

        self.advanced_btn = QPushButton(tr('flow_show_advanced'))
        self.advanced_btn.setObjectName('linkBtn')
        self.advanced_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.advanced_btn.clicked.connect(self._toggle_advanced)
        layout.addWidget(self.advanced_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        # 可滚动配置内容（面板竖排铺满，任何窗口高度都协调）
        scroll = QScrollArea()
        scroll.setObjectName('cfgScroll')
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content.setObjectName('cfgContent')
        clay = QVBoxLayout(content)
        clay.setContentsMargins(0, 0, 0, 0)
        clay.setSpacing(14)

        # 高级设置中的源语言；目标语言留在默认主面板。
        lang_panel = self._panel()
        lp = QHBoxLayout(lang_panel)
        lp.setContentsMargins(16, 14, 16, 14)
        lp.setSpacing(20)
        src_col = QVBoxLayout()
        src_col.setSpacing(6)
        src_col.addWidget(self._sec_title(tr('Source language')))
        self.source_lang = QComboBox()
        self.source_lang.addItems(list(LANGNAME_DICT.values()))
        src_col.addWidget(self.source_lang)
        lp.addLayout(src_col, stretch=1)
        clay.addWidget(lang_panel)

        # 三渠道卡竖排（全宽，舒展）；实例已在主面板摘要条之前构造
        for c in (self.recogn_card, self.trans_card, self.tts_card):
            clay.addWidget(c)

        # 选项面板：字幕 / 自动对齐 / 可选背景音
        opt_panel = self._panel()
        op = QVBoxLayout(opt_panel)
        op.setContentsMargins(16, 14, 16, 14)
        op.setSpacing(10)
        op.addWidget(self._sec_title(tr('flow_options')))
        # 配音质量预设：把 6 个"时间↔质量"开关收敛成一个选择
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel(tr('dub_preset_label')))
        self.preset_box = QComboBox()
        for name in (presets.PRESET_FAST, presets.PRESET_BALANCED,
                     presets.PRESET_QUALITY, presets.PRESET_CUSTOM):
            self.preset_box.addItem(tr(presets.PRESET_LABELS[name]), name)
        self.preset_box.currentIndexChanged.connect(lambda _i: self._on_preset_changed())
        preset_row.addWidget(self.preset_box, stretch=1)
        op.addLayout(preset_row)
        sub_row = QHBoxLayout()
        sub_row.addWidget(QLabel(tr('flow_subtitle_label')))
        self.subtitle_box = QComboBox()
        self.subtitle_box.addItems([tr('nosubtitle'), tr('embedsubtitle'), tr('softsubtitle')])
        sub_row.addWidget(self.subtitle_box, stretch=1)
        op.addLayout(sub_row)
        self.auto_align = QCheckBox(tr('flow_auto_align'))
        op.addWidget(self.auto_align)
        self.keep_bgm = QCheckBox(tr('flow_keep_bgm'))
        # 人声分离不能保证把说话声完全从背景轨剔除。把它标明为可选项，
        # 并在默认关闭时优先保证中文配音的清晰度。
        self.keep_bgm.setToolTip(tr('flow_keep_bgm_tip'))
        op.addWidget(self.keep_bgm)
        # 默认不勾选 = 复用输出目录里已有的识别/翻译字幕（增量重跑，只重做配音+合成）；
        # 勾选 = 清空该视频的输出目录和缓存，全部从头跑
        self.fresh_run = QCheckBox(tr('flow_fresh_run'))
        self.fresh_run.setToolTip(tr('flow_fresh_run_tip'))
        op.addWidget(self.fresh_run)
        clay.addWidget(opt_panel)

        clay.addStretch(1)
        scroll.setWidget(content)
        scroll.setVisible(False)
        self.advanced_scroll = scroll
        layout.addWidget(scroll, stretch=1)

        # 高级区隐藏时它的 maximumSize 归零、吃不到 stretch，富余空间会被 Qt
        # 均分给其余项（实测把卡片和提示各撑到 362px）。用一个可切换的填充件
        # 明确接住这块空间。
        self._filler = QWidget()
        self._filler.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        layout.addWidget(self._filler)

        # 开始（固定底部）
        self.start_hint = QLabel('')
        self.start_hint.setObjectName('startHint')
        self.start_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.start_hint.setWordWrap(True)
        self.start_hint.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.start_hint.setMinimumHeight(20)   # 有无提示都不让主按钮上下跳动
        layout.addWidget(self.start_hint)
        self.retry_workers_btn = QPushButton(tr('flow_retry_workers'))
        self.retry_workers_btn.setObjectName('linkBtn')
        self.retry_workers_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.retry_workers_btn.clicked.connect(self._retry_workers)
        self.retry_workers_btn.setVisible(False)
        layout.addWidget(self.retry_workers_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        self.start_btn = QPushButton('✨ ' + tr('flow_smart_start'))
        self.start_btn.setObjectName('startBtn')
        self.start_btn.setMinimumHeight(50)
        self.start_btn.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        # Only an intentional click starts paid/local work; a leftover Enter key
        # from launching the app must not activate the primary action.
        self.start_btn.setAutoDefault(False)
        self.start_btn.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_btn.clicked.connect(self._on_start)
        layout.addWidget(self.start_btn)

        # 联动
        self._voicesFetched.connect(self._apply_voices)
        self.tts_card.channel_changed.connect(lambda _id: self._reload_voices())
        self.recogn_card.channel_changed.connect(lambda _id: self._reload_models())
        self.target_lang.currentTextChanged.connect(lambda _t: self._reload_voices())
        self.source_lang.currentTextChanged.connect(lambda _t: self._check_langs())
        self.delivery_box.currentIndexChanged.connect(lambda _i: self._update_delivery_mode())

        # 状态点轮询（winform 保存 Key 后 1s 内变绿）
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._refresh_all_status)
        # 初始化等待计时：让"正在初始化"能显示已等待多久，而不是一句永恒的静态提示
        self._boot_timer = QTimer(self)
        self._boot_timer.setInterval(1000)
        self._boot_timer.timeout.connect(self._update_start_enabled)
        self._boot_timer.start()

        self._load_defaults()

    # ---- 小部件工厂 ----
    @staticmethod
    def _panel() -> QFrame:
        f = QFrame()
        f.setObjectName('cfgPanel')
        return f

    @staticmethod
    def _sec_title(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName('secTitle')
        return lbl

    # ---- 生命周期 ----
    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_all_status()
        self._status_timer.start()
        if not self._workers_ready and not self._workers_error:
            self._boot_timer.start()

    def hideEvent(self, event):
        self._status_timer.stop()
        self._boot_timer.stop()
        super().hideEvent(event)

    def load(self, files: list):
        self.files = list(files)
        self.files_label.setText(tr('flow_files_count').replace('{0}', str(len(self.files))))
        self.files_label.setToolTip('\n'.join(self.files))
        name = Path(self.files[0]).name if self.files else ''
        if self._is_bilingual_delivery():
            self.quick_summary.setText(
                tr('flow_bilingual_file_summary').replace('{0}', name))
        else:
            self.quick_summary.setText(
                tr('flow_smart_file_summary').replace('{0}', name))

    def set_workers_ready(self, ready: bool):
        self._workers_ready = ready
        if ready:
            self._workers_error = ''
            self._boot_timer.stop()
        self._update_start_enabled()

    def set_workers_failed(self, message: str):
        """AI 运行时启动失败：说清真实原因，并给一个重试入口。

        原先失败分支只弹一次 show_error 就再不通知本页，按钮会永久停在
        "正在初始化，请稍候"——用户既不知道坏了，也无从恢复。
        """
        self._workers_ready = False
        self._workers_error = str(message or '')[:300]
        self._boot_timer.stop()
        self._update_start_enabled()

    def _retry_workers(self):
        main = getattr(self.flow, 'main', None)
        if main is None or not hasattr(main, 'restart_ai_loader'):
            return
        self._workers_error = ''
        self._boot_started = time.monotonic()
        self._boot_timer.start()
        self._update_start_enabled()
        main.restart_ai_loader()

    # ---- 默认值 ----
    def _load_defaults(self):
        src = params.get('source_language')
        if src:
            self.source_lang.setCurrentText(src)
        tgt = params.get('target_language')
        if tgt and tgt != '-':
            self.target_lang.setCurrentText(tgt)
        elif self.target_lang.findText('简体中文') >= 0:
            self.target_lang.setCurrentText('简体中文')

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
        # 智能配音页不能继承经典页的旧勾选：旧项目若曾保留背景声，
        # 会把人声分离残留无声地混进每一个新项目。需要背景音乐时由用户
        # 在本次任务中显式开启。
        self.keep_bgm.setChecked(False)
        self._load_preset()

        self._reload_models()
        self._reload_voices()
        self._update_delivery_mode()

    def _load_preset(self):
        name = presets.current(settings)
        index = self.preset_box.findData(name)
        self.preset_box.blockSignals(True)
        self.preset_box.setCurrentIndex(index if index >= 0 else 0)
        self.preset_box.blockSignals(False)
        self._update_preset_tip()

    def _on_preset_changed(self):
        """立即落盘并写进内存 settings，本次任务即可生效。"""
        name = self.preset_box.currentData() or presets.DEFAULT_PRESET
        try:
            settings.parse_init({'f5tts_preset': name})
        except Exception as error:
            logger.warning(f'保存配音预设失败: {error}')
        presets.apply(name, settings)
        self._update_preset_tip()

    def _update_preset_tip(self):
        name = self.preset_box.currentData() or presets.DEFAULT_PRESET
        detail = presets.describe(name)
        self.preset_box.setToolTip(
            f"{tr('dub_preset_tip')}\n{detail}" if detail else tr('dub_preset_custom_tip'))

    def _is_bilingual_delivery(self) -> bool:
        return self.delivery_box.currentData() == 'bilingual'

    def _update_delivery_mode(self):
        """将“只做双语字幕”变成明确的成片模式，而非隐含的 No 音色。"""
        bilingual = self._is_bilingual_delivery()
        self.tts_card.setVisible(not bilingual)
        self.engine_summary.set_kind_visible(curated.KIND_TTS, not bilingual)
        self.auto_align.setVisible(not bilingual)
        self.keep_bgm.setVisible(not bilingual)
        if bilingual:
            self.quick_summary.setText(tr('flow_bilingual_summary'))
            self.start_btn.setText('✨ ' + tr('flow_bilingual_start'))
        else:
            self.start_btn.setText('✨ ' + tr('flow_smart_start'))
            # load() 会用文件名补全这段摘要；切换回来时保留未选择文件的说明。
            if not self.files:
                self.quick_summary.setText(tr('flow_smart_ready_summary'))
        self._check_langs()
        self._update_start_enabled()

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
        self._voice_request_serial += 1
        request_serial = self._voice_request_serial
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
                self._voicesFetched.emit(tts_id, request_serial, list(roles))
            except RuntimeError:
                pass   # 页面已销毁（应用退出），丢弃结果

        run_in_threadpool(fetch)

    def _apply_voices(self, tts_id: int, request_serial: int, roles: list):
        # 渠道/语言在等待期间又变了则丢弃过期结果。此前只按渠道判断，
        # 同一渠道的旧请求会把用户刚选择的中文音色重置成 params 里的 No。
        if (self.tts_card.current_channel_id() != tts_id
                or request_serial != self._voice_request_serial):
            return
        selected = self.tts_card.current_secondary()
        preferred = selected if selected in roles and selected not in ('No', '', ' ') else None
        if not preferred:
            preferred = self._preferred_dubbing_voice(roles)
        self.tts_card.set_secondary_items(roles, preferred)
        self._check_langs()

    @staticmethod
    def _preferred_dubbing_voice(roles):
        """Choose a safe *dubbing* default, never the legacy No sentinel."""
        roles = [str(role) for role in (roles or [])]
        saved = params.get('voice_role')
        if saved in roles and saved not in ('No', '', ' '):
            return saved
        # F5-style local backends expose clone alongside reference voices.
        # For interview dubbing it is the best no-setup default: the source
        # speaker becomes the reference instead of an unrelated stock voice.
        if 'clone' in roles:
            return 'clone'
        return next((role for role in roles if role not in ('No', '', ' ')), 'No')

    def _check_langs(self):
        from videotrans import recognition, tts
        from videotrans.translator import get_code
        src = get_code(show_text=self.source_lang.currentText())
        tgt = get_code(show_text=self.target_lang.currentText())
        warn = recognition.is_allow_lang(
            langcode=src, recogn_type=self.recogn_card.current_channel_id(),
            model_name=self.recogn_card.current_secondary() or '')
        self.recogn_card.set_warning('' if warn is True else str(warn))
        if tgt and tgt != '-' and not self._is_bilingual_delivery():
            warn2 = tts.is_allow_lang(langcode=tgt, tts_type=self.tts_card.current_channel_id())
            self.tts_card.set_warning('' if warn2 is True else str(warn2))
        else:
            self.tts_card.set_warning('')
        self.engine_summary.refresh()

    # ---- 状态与开始门控 ----
    def _refresh_all_status(self):
        for c in (self.recogn_card, self.trans_card, self.tts_card):
            c.refresh_status()
        self.engine_summary.refresh()
        self._update_start_enabled()

    def _update_start_enabled(self):
        reasons = []
        critical = False
        if self._workers_error:
            reasons.append(tr('flow_workers_failed') + '：' + self._workers_error)
            critical = True
        elif not self._workers_ready:
            # 真实成因是首次 import torch，冷启动可能几十秒。给出已等待秒数，
            # 用户才能区分"正在加载"和"卡死了"。
            waited = int(time.monotonic() - self._boot_started)
            reasons.append(
                f"{tr('flow_waiting_workers')}（{waited}s · {tr('flow_first_boot_slow')}）")
        cards = (self.recogn_card, self.trans_card)
        if not self._is_bilingual_delivery():
            cards += (self.tts_card,)
        for c in cards:
            if not c.is_ready():
                reasons.append(tr('flow_need_key') + ': ' + c.provider().name)
        self.start_hint.setText('；'.join(reasons))
        self.start_hint.setProperty('level', 'error' if critical else 'warn')
        self.start_hint.style().unpolish(self.start_hint)
        self.start_hint.style().polish(self.start_hint)
        self.retry_workers_btn.setVisible(critical)
        self.start_btn.setDisabled(bool(reasons))

    def _toggle_advanced(self):
        self._advanced_visible = not self._advanced_visible
        self.advanced_scroll.setVisible(self._advanced_visible)
        self._filler.setVisible(not self._advanced_visible)
        self.advanced_btn.setText(tr(
            'flow_hide_advanced' if self._advanced_visible else 'flow_show_advanced'))

    def _expand_advanced_to(self, card):
        """点摘要行 → 展开高级设置并滚到对应渠道卡。"""
        if not self._advanced_visible:
            self._toggle_advanced()
        self.advanced_scroll.ensureWidgetVisible(card)

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

        if self._is_bilingual_delivery():
            # 3 = 双语硬字幕；No + 双字幕仍是标准视频模式，保留原声并跳过全部 TTS。
            if main.voice_role.findText('No') < 0:
                main.voice_role.addItem('No')
            main.voice_role.setCurrentText('No')
            main.subtitle_type.setCurrentIndex(3)
            # 1 = 原文在上、译文在下，适合中英文访谈的阅读顺序。
            main.output_srt.setCurrentIndex(1)
            main.voice_autorate.setChecked(False)
            main.is_separate.setChecked(False)
            main.embed_bgm.setChecked(False)
        else:
            tts_id = self.tts_card.current_channel_id()
            main.tts_type.setCurrentIndex(tts_id)
            wa.tts_type_change(tts_id)
            voice = self.tts_card.current_secondary() or 'No'
            # “智能配音版”绝不能静默降级为字幕任务。音色列表还在加载、
            # 或旧异步回调重置选择时，明确阻止开始并告诉用户该补什么。
            if voice in ('No', '', ' '):
                QMessageBox.warning(self, tr('flow_start'), tr('flow_voice_required'))
                return False
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
                # 分离轨常含轻微人声残留；智能流程使用保守混音比例，
                # 让中文人声始终是前景。
                main.bgmvolume.setText('0.25')
            else:
                # 必须同时关闭重混开关，不能让经典页残留的 embed_bgm
                # 在缓存中存在 instrument 轨时意外参与最终混音。
                main.embed_bgm.setChecked(False)
        main.clear_cache.setChecked(self.fresh_run.isChecked())
        # 简洁智能页没有暴露“指定说话人数”。绝不能继承经典页某次
        # 手工设定的 2/3 人限制，否则单人视频会被强行拆成多个身份。
        # clone 模式仍会按需要走自动身份识别，但不再携带陈旧的硬限制。
        main.enable_diariz.setChecked(False)
        main.nums_diariz.setCurrentIndex(0)
        main.app_mode = 'biaozhun'
        return True

    def _on_start(self):
        if not self.files:
            self.back_requested.emit()
            return
        if not self.apply_to_classic_ui():
            return
        self.flow.win_action.smart_auto_mode = True

        from pathlib import Path as _P
        from videotrans.task.project import project_dir_for
        # 任务实际输出在这个根目录的“视频名-后缀”子目录中；即使用户未手选
        # 输出目录，也持久化默认的 _video_out 根目录，供最近任务恢复和定位。
        selected_root = self.flow.main.target_dir or ''
        for f in self.files:
            target_dir = selected_root or (_P(f).parent / '_video_out').as_posix()
            recent_tasks.append({
                'video_path': f,
                'target_dir': target_dir,
                'project_dir': project_dir_for(target_dir, _P(f).stem) if target_dir else '',
                'source_language': self.source_lang.currentText(),
                'target_language': self.target_lang.currentText(),
                # 记录发起进程：应用崩溃后可据此精确判定"进行中"是否已成僵尸
                'pid': os.getpid(),
            })

        self.started.emit()
        self.flow.win_action.check_start()
        # 校验失败时 check_start 已弹窗并复位状态；watchdog 把用户带回本页
        QTimer.singleShot(2000, self._watchdog)

    def _watchdog(self):
        if app_cfg.current_status != 'ing':
            self.start_failed.emit()
