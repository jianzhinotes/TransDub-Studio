import json
import os

from videotrans.dub.run_state import (
    RunStateStore, effective_status, find_run_state, load_run_state,
    repair_stale_project_run)


def test_stage_journal_records_completion_and_failure(tmp_path):
    store = RunStateStore(tmp_path / 'demo.tdproj')
    store.begin_run('run-1')
    store.start_stage('prepare', {'cache_hit': False})
    store.complete_stage('prepare', {'outputs': 2})
    store.start_stage('dubbing')
    store.fail_stage('dubbing', RuntimeError('backend stopped'))
    store.finish_run('failed', 'backend stopped')

    payload = json.loads(store.path.read_text(encoding='utf-8'))
    assert payload['status'] == 'failed'
    assert payload['stages']['prepare']['status'] == 'completed'
    assert payload['stages']['prepare']['metadata']['outputs'] == 2
    assert payload['stages']['dubbing']['status'] == 'failed'
    assert 'backend stopped' in payload['stages']['dubbing']['error']


def test_begin_run_clears_previous_stage_visuals_but_keeps_history(tmp_path):
    root = tmp_path / 'demo.tdproj'
    first = RunStateStore(root)
    first.begin_run('run-1')
    first.start_stage('dubbing')

    resumed = RunStateStore(root)
    resumed.begin_run('run-2')

    assert resumed.data['run_id'] == 'run-2'
    assert resumed.data['status'] == 'running'
    assert resumed.data['stages']['dubbing']['status'] == 'pending'
    assert resumed.data['stages']['dubbing']['previous_status'] == 'interrupted'
    assert resumed.data['stages']['dubbing']['error'] == ''


def test_begin_run_does_not_show_a_previous_failure_as_current(tmp_path):
    root = tmp_path / 'demo.tdproj'
    store = RunStateStore(root)
    store.begin_run('run-1')
    store.start_stage('dubbing')
    store.fail_stage('dubbing', 'old failure')
    store.finish_run('failed', 'old failure')

    resumed = RunStateStore(root)
    resumed.begin_run('run-2')

    stage = resumed.data['stages']['dubbing']
    assert stage['status'] == 'pending'
    assert stage['previous_status'] == 'failed'
    assert stage['error'] == ''


def test_run_state_can_be_discovered_before_project_manifest_exists(tmp_path):
    root = tmp_path / 'outputs'
    project = root / 'nested' / 'demo.tdproj'
    store = RunStateStore(project)
    store.begin_run('run-1')

    found = find_run_state(root, 'demo')

    assert found == str(store.path)
    assert load_run_state(found)['pid'] == os.getpid()
    assert effective_status(load_run_state(found)) == 'running'


def test_dead_owner_is_reported_as_interrupted():
    assert effective_status({'status': 'running', 'pid': 999_999_999}) == 'interrupted'


def test_finish_interrupted_closes_running_stage(tmp_path):
    store = RunStateStore(tmp_path / 'demo.tdproj')
    store.begin_run('run-1')
    store.start_stage('quality_review')

    store.finish_run('interrupted')

    assert store.data['stages']['quality_review']['status'] == 'interrupted'


def test_finish_run_persists_expected_output_artifact(tmp_path):
    store = RunStateStore(tmp_path / 'demo.tdproj')
    store.begin_run('run-1')
    store.finish_run('completed', artifacts={
        'expect_video': True,
        'output_video': '/tmp/demo.mp4',
    })

    payload = load_run_state(store.path)
    assert payload['artifacts']['expect_video'] is True
    assert payload['artifacts']['output_video'] == '/tmp/demo.mp4'


def test_repair_stale_project_run_closes_both_journals(tmp_path):
    root = tmp_path / 'demo.tdproj'
    root.mkdir()
    payload = {
        'status': 'running', 'pid': 999_999_999, 'started_at': 100,
        'updated_at': 120,
        'stages': {'dubbing': {'status': 'running', 'started_at': 110}},
    }
    (root / 'run_state.json').write_text(json.dumps(payload), encoding='utf-8')
    (root / 'performance_report.json').write_text(
        json.dumps(payload), encoding='utf-8')

    assert repair_stale_project_run(root, 'process exited') == 2
    for name in ('run_state.json', 'performance_report.json'):
        repaired = json.loads((root / name).read_text(encoding='utf-8'))
        assert repaired['status'] == 'interrupted'
        assert repaired['stages']['dubbing']['status'] == 'interrupted'
        assert repaired['last_error'] == 'process exited'
