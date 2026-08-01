import json

from videotrans.dub.performance_report import PerformanceReporter, load_performance_report


def test_performance_report_records_stage_and_resources(tmp_path):
    reporter = PerformanceReporter(tmp_path / 'demo.tdproj')
    reporter.start('run-1', {'tts_type': 8}, background=False)
    reporter.start_stage('dubbing')
    reporter.finish_stage('dubbing', metadata={'segments_total': 12})
    reporter.finish('completed')

    payload = load_performance_report(reporter.path)
    assert payload['status'] == 'completed'
    assert payload['context']['tts_type'] == 8
    assert payload['stages']['dubbing']['status'] == 'completed'
    assert payload['stages']['dubbing']['metadata']['segments_total'] == 12
    assert payload['resources']['samples'] >= 2
    assert json.loads(reporter.path.read_text())['schema_version'] == 2
    assert payload['resources']['peak_pressure'] in {
        'normal', 'elevated', 'high', 'critical'
    }


def test_interrupted_report_closes_current_stage(tmp_path):
    reporter = PerformanceReporter(tmp_path / 'demo.tdproj')
    reporter.start('run-2', background=False)
    reporter.start_stage('quality_review')

    reporter.finish('interrupted')

    assert reporter.data['stages']['quality_review']['status'] == 'interrupted'
