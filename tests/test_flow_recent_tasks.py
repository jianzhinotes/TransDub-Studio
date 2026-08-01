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
