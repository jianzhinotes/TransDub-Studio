import time
from videotrans.flowui import recent_tasks
from videotrans.dub.run_state import RunStateStore


class TestRecentTasks:
    def test_load_missing_file(self, tmp_path):
        assert recent_tasks.load(str(tmp_path / 'none.json')) == []

    def test_load_corrupt_file(self, tmp_path):
        p = tmp_path / 'r.json'
        p.write_text('{broken', encoding='utf-8')
        assert recent_tasks.load(str(p)) == []

    def test_append_and_load(self, tmp_path):
        p = str(tmp_path / 'r.json')
        recent_tasks.append({'video_path': '/a/1.mp4', 'target_dir': '/o'}, p)
        data = recent_tasks.load(p)
        assert len(data) == 1
        assert data[0]['status'] == recent_tasks.STATUS_RUNNING
        assert data[0]['ts'] > 0

    def test_dedup_moves_to_front(self, tmp_path):
        p = str(tmp_path / 'r.json')
        recent_tasks.append({'video_path': '/a/1.mp4'}, p)
        recent_tasks.append({'video_path': '/a/2.mp4'}, p)
        recent_tasks.append({'video_path': '/a/1.mp4'}, p)
        data = recent_tasks.load(p)
        assert [e['video_path'] for e in data] == ['/a/1.mp4', '/a/2.mp4']

    def test_cap_20(self, tmp_path):
        p = str(tmp_path / 'r.json')
        for i in range(25):
            recent_tasks.append({'video_path': f'/a/{i}.mp4'}, p)
        data = recent_tasks.load(p)
        assert len(data) == recent_tasks.MAX_ENTRIES
        assert data[0]['video_path'] == '/a/24.mp4'

    def test_update_status(self, tmp_path):
        p = str(tmp_path / 'r.json')
        recent_tasks.append({'video_path': '/a/1.mp4'}, p)
        recent_tasks.update_status('/a/1.mp4', recent_tasks.STATUS_SUCCEED, p)
        assert recent_tasks.load(p)[0]['status'] == 'succeed'

    def test_update_status_unknown_noop(self, tmp_path):
        p = str(tmp_path / 'r.json')
        recent_tasks.append({'video_path': '/a/1.mp4'}, p)
        recent_tasks.update_status('/x.mp4', 'succeed', p)
        assert recent_tasks.load(p)[0]['status'] == 'running'

    def test_reconcile_from_durable_run_state(self, tmp_path):
        recent_file = str(tmp_path / 'recent.json')
        output = tmp_path / 'output'
        video = tmp_path / 'demo.mp4'
        recent_tasks.append({
            'video_path': str(video),
            'target_dir': str(output),
        }, recent_file)
        store = RunStateStore(output / 'nested' / 'demo.tdproj')
        store.begin_run('run-1')
        store.finish_run('failed', 'quality gate')

        entries = recent_tasks.reconcile_run_states(recent_file)

        assert entries[0]['status'] == recent_tasks.STATUS_ERROR
        assert entries[0]['run_state_file'] == str(store.path)


class TestFieldRollForward:
    def test_append_keeps_backfilled_paths(self, tmp_path):
        p = str(tmp_path / 'r.json')
        recent_tasks.append({'video_path': '/v/a.mp4', 'target_dir': '/out',
                             'project_dir': '/out/a.tdproj'}, path=p)
        # 重跑时只带最基本字段，不该丢掉上一轮回填的工程定位
        entries = recent_tasks.append({'video_path': '/v/a.mp4'}, path=p)
        assert entries[0]['project_dir'] == '/out/a.tdproj'
        assert entries[0]['target_dir'] == '/out'

    def test_append_prefers_new_values(self, tmp_path):
        p = str(tmp_path / 'r.json')
        recent_tasks.append({'video_path': '/v/a.mp4', 'target_dir': '/old'}, path=p)
        entries = recent_tasks.append(
            {'video_path': '/v/a.mp4', 'target_dir': '/new'}, path=p)
        assert entries[0]['target_dir'] == '/new'


class TestBackfillPaths:
    def test_from_project_dir(self):
        e = {'video_path': '/v/a.mp4', 'target_dir': '', 'project_dir': '/out/a.tdproj'}
        assert recent_tasks._backfill_paths(e) is True
        assert e['target_dir'] == '/out'

    def test_from_run_state_file(self):
        e = {'video_path': '/v/a.mp4', 'target_dir': '',
             'run_state_file': '/out/a.tdproj/run_state.json'}
        assert recent_tasks._backfill_paths(e) is True
        assert e['target_dir'] == '/out'

    def test_from_video_path(self):
        e = {'video_path': '/v/a.mp4', 'target_dir': ''}
        assert recent_tasks._backfill_paths(e) is True
        assert e['target_dir'].endswith('/_video_out')

    def test_noop_when_present(self):
        e = {'video_path': '/v/a.mp4', 'target_dir': '/keep'}
        assert recent_tasks._backfill_paths(e) is False
        assert e['target_dir'] == '/keep'


class TestZombieDetection:
    def test_dead_pid_marks_stopped(self, tmp_path):
        p = str(tmp_path / 'r.json')
        # 99999999 不可能是活进程
        recent_tasks.append({'video_path': str(tmp_path / 'a.mp4'), 'pid': 99999999,
                             'target_dir': str(tmp_path)}, path=p)
        entries = recent_tasks.reconcile_run_states(path=p)
        assert entries[0]['status'] == recent_tasks.STATUS_STOPPED
        assert entries[0]['stale_reason'] == 'owner_gone'

    def test_live_pid_stays_running(self, tmp_path):
        import os
        p = str(tmp_path / 'r.json')
        recent_tasks.append({'video_path': str(tmp_path / 'a.mp4'), 'pid': os.getpid(),
                             'target_dir': str(tmp_path)}, path=p)
        entries = recent_tasks.reconcile_run_states(path=p)
        assert entries[0]['status'] == recent_tasks.STATUS_RUNNING

    def test_legacy_entry_times_out(self, tmp_path):
        p = str(tmp_path / 'r.json')
        now = time.time()
        recent_tasks.append({'video_path': str(tmp_path / 'a.mp4'),
                             'ts': int(now - 7 * 3600)}, path=p)
        entries = recent_tasks.reconcile_run_states(path=p, now=now)
        assert entries[0]['status'] == recent_tasks.STATUS_STOPPED
        assert entries[0]['stale_reason'] == 'timeout'

    def test_recent_legacy_entry_kept_running(self, tmp_path):
        p = str(tmp_path / 'r.json')
        now = time.time()
        recent_tasks.append({'video_path': str(tmp_path / 'a.mp4'),
                             'ts': int(now - 3600)}, path=p)
        entries = recent_tasks.reconcile_run_states(path=p, now=now)
        assert entries[0]['status'] == recent_tasks.STATUS_RUNNING


class TestRemoveAndPrune:
    def test_remove(self, tmp_path):
        p = str(tmp_path / 'r.json')
        recent_tasks.append({'video_path': '/v/a.mp4'}, path=p)
        recent_tasks.append({'video_path': '/v/b.mp4'}, path=p)
        entries = recent_tasks.remove('/v/a.mp4', path=p)
        assert [e['video_path'] for e in entries] == ['/v/b.mp4']

    def test_prune_only_removes_succeeded(self, tmp_path):
        p = str(tmp_path / 'r.json')
        recent_tasks.append({'video_path': '/v/ok.mp4',
                             'status': recent_tasks.STATUS_SUCCEED}, path=p)
        recent_tasks.append({'video_path': '/v/run.mp4',
                             'status': recent_tasks.STATUS_RUNNING}, path=p)
        entries = recent_tasks.prune(path=p)
        assert [e['video_path'] for e in entries] == ['/v/run.mp4']
