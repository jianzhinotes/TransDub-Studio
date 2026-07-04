import json

from videotrans.component.timeline.edit_logic import (
    MIN_DURATION_MS,
    clamp_block,
    compute_status,
    dump_queue,
    ms_to_srt,
    serializable,
    sync_time_fields,
)


def _items():
    return [
        {'start_time': 1000, 'end_time': 2000},
        {'start_time': 3000, 'end_time': 4000},
        {'start_time': 5000, 'end_time': 6000},
    ]


DUR = 10000


class TestClampBlock:
    def test_move_free_within_gap(self):
        assert clamp_block(_items(), 1, 3200, 4200, 'move', DUR) == (3200, 4200)

    def test_move_clamped_by_prev(self):
        s, e = clamp_block(_items(), 1, 1500, 2500, 'move', DUR)
        assert s == 2000 and e == 3000  # 顶到前一条 end_time，长度不变

    def test_move_clamped_by_next(self):
        s, e = clamp_block(_items(), 1, 4800, 5800, 'move', DUR)
        assert e == 5000 and s == 4000

    def test_first_item_floor_zero(self):
        s, e = clamp_block(_items(), 0, -500, 500, 'move', DUR)
        assert s == 0 and e == 1000

    def test_last_item_ceiling_duration(self):
        s, e = clamp_block(_items(), 2, 9500, 10500, 'move', DUR)
        assert e == DUR and s == 9000

    def test_left_edge_pins_end(self):
        s, e = clamp_block(_items(), 1, 2500, 9999, 'left', DUR)
        assert (s, e) == (2500, 4000)

    def test_left_edge_min_duration(self):
        s, e = clamp_block(_items(), 1, 3950, 0, 'left', DUR)
        assert e == 4000 and e - s == MIN_DURATION_MS

    def test_right_edge_pins_start(self):
        s, e = clamp_block(_items(), 1, 0, 4600, 'right', DUR)
        assert (s, e) == (3000, 4600)

    def test_right_edge_clamped_by_next(self):
        s, e = clamp_block(_items(), 1, 0, 5600, 'right', DUR)
        assert e == 5000

    def test_right_edge_min_duration(self):
        s, e = clamp_block(_items(), 1, 0, 3010, 'right', DUR)
        assert s == 3000 and e - s == MIN_DURATION_MS


class TestStatus:
    def test_no_audio(self):
        kind, dub, diff = compute_status({'start_time': 0, 'end_time': 2000, 'dubbing_s': 0})
        assert kind == 'no_audio'

    def test_exceeded(self):
        kind, dub, diff = compute_status({'start_time': 0, 'end_time': 2000, 'dubbing_s': 2.5})
        assert kind == 'exceeded' and diff == 0.5

    def test_shortened(self):
        kind, _, diff = compute_status({'start_time': 0, 'end_time': 2000, 'dubbing_s': 1.5})
        assert kind == 'shortened' and diff == -0.5

    def test_ok(self):
        kind, _, _ = compute_status({'start_time': 0, 'end_time': 2000, 'dubbing_s': 2.0})
        assert kind == 'ok'


class TestSyncAndSerialize:
    def test_ms_to_srt(self):
        assert ms_to_srt(3723456) == '01:02:03,456'
        assert ms_to_srt(0) == '00:00:00,000'

    def test_sync_time_fields(self):
        item = {'start_time': 0, 'end_time': 1, 'start_time_source': 0,
                'end_time_source': 1, 'startraw': '', 'endraw': ''}
        sync_time_fields(item, 1500, 2750)
        assert item['start_time'] == 1500 and item['end_time'] == 2750
        assert item['start_time_source'] == 1500 and item['end_time_source'] == 2750
        assert item['startraw'] == '00:00:01,500'
        assert item['endraw'] == '00:00:02,750'

    def test_serializable_strips_private(self):
        out = serializable([{'text': 'a', '_msg': 'x', '_duration': 1.0}])
        assert out == [{'text': 'a'}]

    def test_dump_queue_roundtrip(self, tmp_path):
        queue = [{'text': 'hello', 'start_time': 100, '_msg': 'x'}]
        path = dump_queue(queue, str(tmp_path))
        data = json.loads(open(path, encoding='utf-8').read())
        assert data == [{'text': 'hello', 'start_time': 100}]
