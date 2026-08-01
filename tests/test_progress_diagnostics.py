import pytest

from videotrans.dub.performance_report import PerformanceReporter
from videotrans.dub.run_state import RunStateStore


@pytest.fixture(scope='module')
def qapp():
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_task_card_recovers_stage_and_exposes_diagnostics(tmp_path, qapp):
    from videotrans.flowui.progress_page import TaskCard
    from videotrans.flowui import stages

    output = tmp_path / 'output'
    project = output / 'nested' / 'demo.tdproj'
    state = RunStateStore(project)
    state.begin_run('run-1')
    state.start_stage('dubbing')
    performance = PerformanceReporter(project)
    performance.start('run-1', {'tts_type': 8}, background=False)
    performance.start_stage('dubbing')

    card = TaskCard(
        uuid='run-1', video_path=str(tmp_path / 'demo.mp4'),
        target_dir=str(output))
    status = card.sync_run_state()

    assert status == 'running'
    assert card.stage == stages.STAGE_DUBBING
    assert not card.diagnostics_btn.isHidden()
    performance.finish('interrupted')
    card.deleteLater()
    qapp.processEvents()


def test_running_diagnostics_show_live_elapsed_without_none():
    from videotrans.flowui.progress_page import _diagnostics_message

    message = _diagnostics_message({
        'status': 'running',
        'started_at': 100,
        'duration_s': None,
        'context': {'tts_type': 8, 'recogn_model': 'large-v3-turbo'},
        'resources': {},
        'stages': {
            'dubbing': {
                'status': 'running', 'started_at': 102, 'duration_s': None,
                'metadata': {},
            },
        },
    }, now=112.5)

    assert '总耗时: 12.5s' in message
    assert 'dubbing: running  10.5s' in message
    assert 'Nones' not in message
    assert '质量核对: 尚未开始' in message
    assert '配音缓存命中: 统计中' in message


def test_task_card_clears_red_terminal_state_on_new_running_attempt(tmp_path, qapp):
    from videotrans.flowui.progress_page import TaskCard

    output = tmp_path / 'output'
    project = output / 'nested' / 'demo.tdproj'
    state = RunStateStore(project)
    state.begin_run('run-1')
    state.start_stage('dubbing')
    state.fail_stage('dubbing', 'old failure')
    state.finish_run('failed', 'old failure')
    card = TaskCard(
        uuid='run-1', video_path=str(tmp_path / 'demo.mp4'), target_dir=str(output))
    card.sync_run_state()
    assert card.done
    assert card.state_label.objectName() == 'errState'

    state.begin_run('run-2')
    state.start_stage('translate')
    assert card.sync_run_state() == 'running'

    assert not card.done
    assert card.state_label.text() == ''
    assert card.state_label.objectName() == ''
    card.deleteLater()
    qapp.processEvents()


def test_task_card_never_claims_video_success_without_a_render(tmp_path, qapp):
    """A queue-level success signal is not evidence that FFmpeg wrote media."""
    from videotrans.flowui.progress_page import TaskCard

    output = tmp_path / 'output'
    output.mkdir()
    card = TaskCard(
        uuid='run-1', video_path=str(tmp_path / 'demo.mp4'), target_dir=str(output))

    assert card.set_done(True, expect_video=True) is False
    assert card.done
    assert card.state_label.objectName() == 'errState'
    assert '导出核验失败' in card.last_log.text()
    card.deleteLater()
    qapp.processEvents()


def test_task_card_requires_a_real_file_before_video_success(tmp_path, qapp):
    from videotrans.flowui.progress_page import TaskCard

    output = tmp_path / 'output'
    output.mkdir()
    (output / 'demo.mp4').write_bytes(b'not-empty-media')
    card = TaskCard(
        uuid='run-1', video_path=str(tmp_path / 'demo.mp4'), target_dir=str(output))

    assert card.set_done(True, expect_video=True) is True
    assert card.output_video_path() == str(output / 'demo.mp4')
    assert card.state_label.objectName() == 'doneBanner'
    assert not card.preview_btn.isHidden()
    card.deleteLater()
    qapp.processEvents()


def test_task_card_does_not_mistake_source_for_only_mp4_output(tmp_path, qapp):
    from videotrans.flowui.progress_page import TaskCard

    # A custom output root can be the source directory. The source MP4 must
    # never satisfy output verification merely because it has the same stem.
    source = tmp_path / 'demo.mp4'
    source.write_bytes(b'original-video')
    output = tmp_path / '_video_out' / 'demo-mp4'
    output.mkdir(parents=True)
    card = TaskCard(uuid='run-1', video_path=str(source), target_dir=str(output))

    assert card.set_done(True, expect_video=True) is False
    rendered = tmp_path / '_video_out' / 'demo.mp4'
    rendered.write_bytes(b'final-video')
    card.reset_for_run()
    assert card.set_done(True, expect_video=True, output_video=str(rendered)) is True
    assert card.output_video_path() == str(rendered)
    card.deleteLater()
    qapp.processEvents()


def test_task_card_labels_intentional_subtitle_only_completion(tmp_path, qapp):
    from videotrans.flowui.progress_page import TaskCard

    output = tmp_path / 'output'
    output.mkdir()
    card = TaskCard(
        uuid='run-1', video_path=str(tmp_path / 'demo.mp4'), target_dir=str(output))

    assert card.set_done(True, expect_video=False) is True
    assert card.state_label.objectName() == 'doneBanner'
    assert card.preview_btn.isHidden()
    card.deleteLater()
    qapp.processEvents()


def test_progress_100_and_queue_end_cannot_create_false_success(tmp_path, qapp):
    from types import SimpleNamespace
    from videotrans.flowui.progress_page import ProgressPage, TaskCard

    flow = SimpleNamespace(win_action=SimpleNamespace(
        uuid_queue_mp4={}, uuid_output_dirs={}))
    page = ProgressPage(flow=flow)
    output = tmp_path / 'output'
    output.mkdir()
    card = TaskCard(
        uuid='run-1', video_path=str(tmp_path / 'demo.mp4'), target_dir=str(output))
    page.cards['run-1'] = card

    page.on_message('run-1', {'type': 'set_precent', 'text': 'merge???100'})
    assert not card.done
    page.on_message('', {'type': 'end', 'text': ''})
    assert card.done
    assert card.state_label.objectName() == 'errState'
    page.deleteLater()
    card.deleteLater()
    qapp.processEvents()
