import importlib.util
import json

import pytest

# StudioState 的信号依赖真实 PySide6；conftest 的 mock 环境下跳过
if importlib.util.find_spec('PySide6') is None:
    pytest.skip('requires real PySide6', allow_module_level=True)

from videotrans.component.timeline.studio_state import StudioState


def _queue():
    return [
        {'text': 'a', 'role': 'r1', 'start_time': 0, 'end_time': 1000,
         'start_time_source': 0, 'end_time_source': 1000,
         'startraw': '00:00:00,000', 'endraw': '00:00:01,000', 'dubbing_s': 1.0},
        {'text': 'b', 'role': 'r1', 'start_time': 2000, 'end_time': 3000,
         'start_time_source': 2000, 'end_time_source': 3000,
         'startraw': '00:00:02,000', 'endraw': '00:00:03,000', 'dubbing_s': 0.5},
    ]


class TestStudioState:
    def test_set_text_marks_dirty_and_signals(self):
        st = StudioState(_queue(), 5000)
        got = []
        st.textChanged.connect(got.append)
        dirty = []
        st.dirtyChanged.connect(lambda i, d: dirty.append((i, d)))

        st.set_text(0, 'new')
        assert st.items[0]['text'] == 'new'
        assert got == [0] and dirty == [(0, True)]
        assert st.is_dirty(0) and not st.is_dirty(1)

        st.set_text(0, 'new')  # 无变化不再发信号
        assert got == [0]

    def test_set_role_marks_dirty(self):
        st = StudioState(_queue(), 5000)
        st.set_role(1, 'r2')
        assert st.items[1]['role'] == 'r2'
        assert st.dirty_indices() == {1}

    def test_set_times_syncs_fields_no_dirty(self):
        st = StudioState(_queue(), 5000)
        times, status = [], []
        st.timesChanged.connect(times.append)
        st.statusChanged.connect(status.append)
        st.set_times(0, 500, 1500)
        it = st.items[0]
        assert it['start_time'] == 500 and it['endraw'] == '00:00:01,500'
        assert it['start_time_source'] == 500
        assert times == [0] and status == [0]
        assert not st.is_dirty(0)  # 时间编辑不需要重配

    def test_mark_clean_after_redub(self):
        st = StudioState(_queue(), 5000)
        st.set_text(0, 'x')
        st.set_dubbing_s(0, 1.4)
        st.mark_clean(0)
        assert st.items[0]['dubbing_s'] == 1.4
        assert not st.dirty_indices()

    def test_save_shares_reference(self, tmp_path):
        q = _queue()
        st = StudioState(q, 5000)
        st.set_text(0, 'edited')
        path = st.save(str(tmp_path))
        data = json.loads(open(path, encoding='utf-8').read())
        assert data[0]['text'] == 'edited'
        assert st.items is q  # 始终同一引用
