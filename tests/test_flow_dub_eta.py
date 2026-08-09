"""配音 ETA 与进度插值：纯函数，无 Qt。"""
import json

import pytest

from videotrans.flowui.dub_telemetry import (
    DUB_BAND_CEIL, dubbing_percent, estimate_eta, format_duration,
    quantize_eta, read_dubbing_telemetry,
)


class TestDubbingPercent:
    def test_starts_at_base(self):
        assert dubbing_percent(18, 0, 100) == 18

    def test_never_exceeds_ceiling(self):
        # base+span 会超过 85，必须被天花板挡住：越过 90 会让阶段步进器
        # 提前点亮"合成"
        assert dubbing_percent(40, 50, 50) <= DUB_BAND_CEIL
        assert dubbing_percent(80, 100, 100) <= DUB_BAND_CEIL

    def test_monotonic_in_completed(self):
        values = [dubbing_percent(18, i, 100) for i in range(0, 101)]
        assert values == sorted(values)
        assert values[-1] > values[0]

    def test_zero_total_returns_base(self):
        assert dubbing_percent(18, 5, 0) == 18

    def test_base_at_ceiling_does_not_regress(self):
        assert dubbing_percent(90, 50, 100) == 90


class TestEstimateEta:
    def test_wall_clock_rate(self):
        eta, rate = estimate_eta(
            {'total': 100, 'completed': 13, 'prefilled': 3, 'elapsed_s': 100})
        assert rate == pytest.approx(10.0)          # 100s / 10 段真实合成
        assert eta == pytest.approx(870.0)          # 剩余 87 段

    def test_prefilled_is_excluded_from_rate(self):
        # 50 条来自缓存(耗时≈0)，只有 3 条真的合成过
        _eta, rate = estimate_eta(
            {'total': 100, 'completed': 53, 'prefilled': 50, 'elapsed_s': 30})
        assert rate == pytest.approx(10.0)          # 而不是 30/53≈0.57

    def test_falls_back_to_median_when_few_samples(self):
        eta, rate = estimate_eta(
            {'total': 100, 'completed': 1, 'prefilled': 0,
             'elapsed_s': 4, 'median_s': 8.0})
        assert rate == pytest.approx(8.0)
        assert eta == pytest.approx(8.0 * 99)

    def test_returns_none_without_any_signal(self):
        eta, rate = estimate_eta({'total': 100, 'completed': 1, 'prefilled': 0})
        assert eta is None and rate is None

    def test_zero_total_returns_none(self):
        eta, rate = estimate_eta({'total': 0, 'completed': 5, 'elapsed_s': 50})
        assert eta is None and rate is None

    def test_ewma_smooths_against_previous(self):
        _eta, rate = estimate_eta(
            {'total': 100, 'completed': 10, 'prefilled': 0, 'elapsed_s': 200},
            previous_rate=10.0)
        # 0.3*20 + 0.7*10 = 13
        assert rate == pytest.approx(13.0)


class TestQuantizeEta:
    @pytest.mark.parametrize('raw,expected', [
        (None, None), (97, 90), (14, 30), (200, 180), (3700, 3600), (0, 30),
    ])
    def test_buckets(self, raw, expected):
        assert quantize_eta(raw) == expected


class TestReadTelemetry:
    def _write(self, path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding='utf-8')

    def test_progress_only(self, tmp_path):
        self._write(tmp_path / 'tts_progress.json',
                    {'total': 10, 'completed': 4, 'prefilled': 1, 'elapsed_s': 12.0})
        tel = read_dubbing_telemetry(tmp_path)
        assert tel['total'] == 10 and tel['completed'] == 4 and tel['prefilled'] == 1

    def test_supervisor_supplies_median_and_faults(self, tmp_path):
        self._write(tmp_path / 'tts_progress.json', {'total': 10, 'completed': 4})
        self._write(tmp_path / 'synthesis_supervisor.json',
                    {'completed': 4, 'rolling_median_s': 7.5, 'timeouts': 2, 'recycles': 1})
        tel = read_dubbing_telemetry(tmp_path)
        assert tel['median_s'] == 7.5 and tel['timeouts'] == 2 and tel['recycles'] == 1

    def test_supervisor_only_still_gives_completed(self, tmp_path):
        self._write(tmp_path / 'synthesis_supervisor.json',
                    {'completed': 7, 'rolling_median_s': 3.0})
        tel = read_dubbing_telemetry(tmp_path)
        assert tel['completed'] == 7 and tel['total'] == 0   # 无分母 → 不算 ETA

    def test_project_fallback_when_cache_gone(self, tmp_path):
        project = tmp_path / 'proj'
        self._write(project / 'checkpoints' / 'dubbing' / 'supervisor.json',
                    {'completed': 9, 'rolling_median_s': 5.0})
        self._write(project / 'checkpoints' / 'dubbing' / 'manifest.json',
                    {'entries': {'a': 1, 'b': 2}})
        tel = read_dubbing_telemetry(tmp_path / 'gone', project)
        assert tel['completed'] == 2 and tel['median_s'] == 5.0

    def test_missing_returns_none(self, tmp_path):
        assert read_dubbing_telemetry(tmp_path / 'nope') is None

    def test_corrupt_json_is_tolerated(self, tmp_path):
        (tmp_path / 'tts_progress.json').write_text('{not json', encoding='utf-8')
        assert read_dubbing_telemetry(tmp_path) is None


class TestFormatDuration:
    @pytest.mark.parametrize('raw,expected', [
        (0, '0秒'), (45, '45秒'), (90, '1分30秒'), (3700, '1小时1分'), (None, '0秒'),
    ])
    def test_human_readable(self, raw, expected):
        assert format_duration(raw) == expected
