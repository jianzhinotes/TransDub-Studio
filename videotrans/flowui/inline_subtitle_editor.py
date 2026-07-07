"""内嵌字幕校对组件：识别后校原文 / 翻译后校译文（含单句重译）。

作为工作区里的一步（非弹窗）：加载 SRT → 表格校对 → 「下一步」保存并继续流水线。
- mode='source'：行/时间/原文（可编辑），写回 source_sub。
- mode='target'：行/原文(只读)/译文(可编辑)+每行「重译」，写回 target_sub。
"""
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from videotrans.configure.config import logger, params, tr
from videotrans.styles import tokens
from videotrans.util import tools

MODE_SOURCE = 'source'
MODE_TARGET = 'target'

_QSS = f"""
#inlineProof {{ background: {tokens.WINDOW_BG}; }}
#inlineProof QLabel#proofTitle {{ font-size: 16px; font-weight: bold; color: {tokens.TEXT}; }}
#inlineProof QLabel#proofHint {{ color: {tokens.TEXT_SECONDARY}; font-size: 12px; }}
#inlineProof QTableWidget {{ background: {tokens.SURFACE}; border: 1px solid {tokens.BORDER}; }}
#inlineProof QPushButton#proofNext {{
    background: {tokens.ACCENT}; color: #FFFFFF; border: none; border-radius: 8px;
    min-height: 40px; font-size: 15px;
}}
#inlineProof QPushButton#proofNext:hover {{ background: {tokens.ACCENT_HOVER}; }}
"""


class InlineSubtitleEditor(QWidget):
    proofDone = Signal()        # 保存并继续
    proofTerminate = Signal()   # 终止任务

    def __init__(self, *, mode: str, sub_path: str, source_sub: str = None,
                 translate_type: int = 0, source_code: str = None,
                 target_code: str = None, parent=None):
        super().__init__(parent)
        self.mode = mode
        self.sub_path = sub_path
        self.source_sub = source_sub
        self.translate_type = translate_type
        self.source_code = source_code
        self.target_code = target_code
        self.setObjectName('inlineProof')
        self.setStyleSheet(_QSS)

        try:
            self.items = tools.get_subtitle_from_srt(sub_path)
        except Exception as e:
            logger.warning(f'加载字幕校对失败: {e}')
            self.items = []
        # target 模式并排显示原文：按行号取原文
        self._src_by_line = {}
        if mode == MODE_TARGET and source_sub and Path(source_sub).exists():
            try:
                for it in tools.get_subtitle_from_srt(source_sub):
                    self._src_by_line[int(it['line'])] = it['text']
            except Exception:
                pass

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(10)

        title = QLabel(tr('flow_proof_source') if mode == MODE_SOURCE else tr('flow_proof_target'))
        title.setObjectName('proofTitle')
        layout.addWidget(title)
        hint = QLabel(tr('flow_proof_hint'))
        hint.setObjectName('proofHint')
        layout.addWidget(hint)

        self.table = QTableWidget()
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._build_table()
        layout.addWidget(self.table, stretch=1)

        bottom = QHBoxLayout()
        term = QPushButton(tr('Terminate this mission'))
        term.setCursor(Qt.CursorShape.PointingHandCursor)
        term.setStyleSheet('background:transparent;')
        term.clicked.connect(self._on_terminate)
        bottom.addWidget(term)
        bottom.addStretch(1)
        self.next_btn = QPushButton(tr('flow_proof_next'))
        self.next_btn.setObjectName('proofNext')
        self.next_btn.setMinimumWidth(280)
        self.next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.next_btn.clicked.connect(self._on_next)
        bottom.addWidget(self.next_btn)
        bottom.addStretch(1)
        layout.addLayout(bottom)

    # ---- 表格 ----
    def _build_table(self):
        if self.mode == MODE_SOURCE:
            headers = [tr('Line'), tr('Subtitles') + tr('Start Time'), tr('Subtitle Text')]
            self.table.setColumnCount(3)
            self._text_col = 2
        else:
            headers = [tr('Line'), tr('Source text'), tr('Translated text'), '']
            self.table.setColumnCount(4)
            self._text_col = 2
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setRowCount(len(self.items))
        for r, it in enumerate(self.items):
            line = str(it['line'])
            self._set_ro(r, 0, line)
            if self.mode == MODE_SOURCE:
                self._set_ro(r, 1, f"{it['startraw']} --> {it['endraw']}")
                self.table.setItem(r, 2, QTableWidgetItem(str(it['text'])))
            else:
                self._set_ro(r, 1, self._src_by_line.get(int(it['line']), ''))
                self.table.setItem(r, 2, QTableWidgetItem(str(it['text'])))
                btn = QPushButton(tr('flow_retranslate'))
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.clicked.connect(lambda _c, row=r: self._retranslate_row(row))
                self.table.setCellWidget(r, 3, btn)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(self._text_col, QHeaderView.ResizeMode.Stretch)
        if self.mode == MODE_TARGET:
            hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

    def _set_ro(self, r, c, text):
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(r, c, item)

    # ---- 单句重译 ----
    def _retranslate_row(self, row: int):
        if not (0 <= row < len(self.items)):
            return
        src = self._src_by_line.get(int(self.items[row]['line']), '')
        if not src:
            return
        from videotrans import translator
        try:
            res = translator.run(
                translate_type=self.translate_type,
                text_list=[{'text': src, 'line': self.items[row]['line'],
                            'time': self.items[row].get('time', '')}],
                source_code=self.source_code, target_code=self.target_code, is_test=True)
            new_text = res[0]['text'] if res else ''
            if new_text:
                self.table.item(row, self._text_col).setText(new_text)
        except Exception as e:
            logger.warning(f'单句重译失败: {e}')

    # ---- 保存/继续 ----
    def _collect_and_save(self):
        lines = []
        for r, it in enumerate(self.items):
            cell = self.table.item(r, self._text_col)
            text = (cell.text() if cell else str(it['text'])).strip()
            lines.append(f"{it['line']}\n{it['startraw']} --> {it['endraw']}\n{text}")
        Path(self.sub_path).write_text("\n\n".join(lines), encoding='utf-8')

    def _on_next(self):
        try:
            self._collect_and_save()
        except OSError as e:
            logger.warning(f'保存字幕校对失败: {e}')
        self.proofDone.emit()

    def _on_terminate(self):
        self.proofTerminate.emit()
