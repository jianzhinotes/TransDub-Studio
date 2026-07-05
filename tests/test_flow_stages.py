from videotrans.flowui import stages


def _tr(key):
    # 模拟本地化：直接用键名加前缀
    return f'L:{key}'


class TestStageMapping:
    def test_markers_built_from_tr(self):
        m = stages.stage_markers(tr=_tr)
        assert m['L:kaishishibie'] == (stages.STAGE_RECOGN, False)
        assert m['L:kaishihebing'] == (stages.STAGE_ASSEMBLE, True)

    def test_text_exact_and_prefix(self):
        m = stages.stage_markers(tr=_tr)
        cur = stages.STAGE_PREPARE
        cur = stages.stage_from_text('L:kaishishibie', cur, m)
        assert cur == stages.STAGE_RECOGN
        # 合成阶段带耗时后缀 → 前缀匹配
        cur = stages.stage_from_text('L:kaishihebing 00:12', cur, m)
        assert cur == stages.STAGE_ASSEMBLE

    def test_never_decreases(self):
        m = stages.stage_markers(tr=_tr)
        cur = stages.STAGE_ALIGN
        assert stages.stage_from_text('L:kaishishibie', cur, m) == stages.STAGE_ALIGN
        assert stages.stage_from_text('随便的日志', cur, m) == stages.STAGE_ALIGN

    def test_parse_percent(self):
        assert stages.parse_percent('12???45') == (12, 45)
        assert stages.parse_percent('3.5???99.9') == (3, 99)
        assert stages.parse_percent('80') == (None, 80)
        assert stages.parse_percent('abc') == (None, None)
        assert stages.parse_percent('') == (None, None)
        assert stages.parse_percent('1???200') == (1, 100)  # 封顶

    def test_percent_fallback(self):
        assert stages.stage_from_percent(95, stages.STAGE_TRANS) == stages.STAGE_ASSEMBLE
        assert stages.stage_from_percent(50, stages.STAGE_TRANS) == stages.STAGE_TRANS
        assert stages.stage_from_percent(None, 2) == 2
