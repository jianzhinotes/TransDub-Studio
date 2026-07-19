import json

from videotrans.dub.legacy_adapter import (
    ensure_queue_unit_ids,
    make_project_id,
    project_from_queue,
    units_to_queue,
)
from videotrans.dub.schema import (
    AudioCandidate, DubProject, PlannedSegment, PlanningRevision,
    QualityReport, TextCandidate, PROJECT_SCHEMA_VERSION,
)
from videotrans.dub.store import DubProjectStore
from videotrans.task.taskcfg import SrtItem


def _queue():
    return [{
        'line': 1,
        'text': '你好，世界',
        'ref_text': 'Hello, world.',
        'start_time': 100,
        'end_time': 1600,
        'start_time_source': 120,
        'end_time_source': 1500,
        'spk': 'spk1',
        'role': 'clone',
        'tts_type': 8,
        'filename': 'dubb/a.wav',
        'dubbing_s': 1.4,
        'custom_legacy_field': {'keep': True},
    }]


def test_schema_nested_roundtrip_ignores_unknown_fields():
    project_id = make_project_id('/video/demo.mp4', 'zh-cn')
    project = project_from_queue(
        _queue(), project_id=project_id, name='demo',
        source_language='en', target_language='zh-cn')
    unit = project.units[0]
    unit.quality_reports.append(QualityReport(
        id='q1', unit_id=unit.id, audio_candidate_id=unit.selected_audio_candidate_id,
        passed=True, metrics={'duration_ratio': 1.02}))

    payload = project.to_dict()
    payload['future_manifest_field'] = 'ignored'
    payload['units'][0]['future_unit_field'] = 123
    loaded = DubProject.from_dict(payload)

    assert loaded.schema_version == PROJECT_SCHEMA_VERSION
    assert loaded.units[0].text_candidates[0].text == '你好，世界'
    assert loaded.units[0].audio_candidates[0].duration_ms == 1400
    assert loaded.units[0].quality_reports[0].passed is True


def test_store_roundtrip(tmp_path):
    project_id = make_project_id('/video/demo.mp4', 'zh-cn')
    project = project_from_queue(
        _queue(), project_id=project_id, name='demo',
        source_language='en', target_language='zh-cn')
    store = DubProjectStore(tmp_path)
    path = store.save(project)

    assert path.endswith('dub_project.json')
    assert not (tmp_path / 'dub_project.json.tmp').exists()
    loaded = store.load()
    assert loaded.project_id == project_id
    assert loaded.units[0].legacy_payload['custom_legacy_field'] == {'keep': True}


def test_legacy_adapter_stable_ids_and_lossless_roundtrip():
    q1 = _queue()
    q2 = _queue()
    project_id = make_project_id('/video/demo.mp4', 'zh-cn')
    ensure_queue_unit_ids(q1, project_id)
    ensure_queue_unit_ids(q2, project_id)
    assert q1[0]['dub_unit_id'] == q2[0]['dub_unit_id']

    project = project_from_queue(
        q1, project_id=project_id, name='demo',
        source_language='en', target_language='zh-cn')
    restored = units_to_queue(project.units)
    assert restored[0]['custom_legacy_field'] == {'keep': True}
    assert restored[0]['text'] == '你好，世界'
    assert restored[0]['speaker_id'] == 'spk1'
    assert restored[0]['filename'] == 'dubb/a.wav'


def test_adapter_accepts_srtitem_without_dict_setdefault():
    item = SrtItem(
        line=1, text='hello', start_time=0, end_time=1000,
        startraw='00:00:00,000', endraw='00:00:01,000')
    project_id = make_project_id('/video/demo.mp4', 'zh-cn')
    ensure_queue_unit_ids([item], project_id)
    assert item.get('dub_unit_id')
    assert item.get('revision') == 1


def test_sync_preserves_planner_candidate_history():
    project_id = make_project_id('/video/demo.mp4', 'zh-cn')
    first = project_from_queue(
        _queue(), project_id=project_id, name='demo',
        source_language='en', target_language='zh-cn')
    unit = first.units[0]
    unit.text_candidates.append(TextCandidate(id='alt-text', text='世界，你好', kind='compact'))
    unit.audio_candidates.append(AudioCandidate(
        id='alt-audio', text_candidate_id='alt-text', path='dubb/alt.wav',
        backend='f5-local', duration_ms=1200))

    edited = _queue()
    edited[0]['dub_unit_id'] = unit.id
    edited[0]['text'] = '编辑后的当前文本'
    synced = project_from_queue(
        edited, project_id=project_id, name='demo',
        source_language='en', target_language='zh-cn', existing=first)

    assert 'alt-text' in {c.id for c in synced.units[0].text_candidates}
    assert 'alt-audio' in {c.id for c in synced.units[0].audio_candidates}
    selected = next(c for c in synced.units[0].text_candidates
                    if c.id == synced.units[0].selected_text_candidate_id)
    assert selected.text == '编辑后的当前文本'


def test_planning_revision_roundtrip():
    project_id = make_project_id('/video/demo.mp4', 'zh-cn')
    project = project_from_queue(
        _queue(), project_id=project_id, name='demo',
        source_language='en', target_language='zh-cn')
    segment = PlannedSegment(
        id='seg1', unit_ids=[project.units[0].id], speaker_id='spk1',
        start_ms=100, end_ms=1600, source_text='Hello, world.',
        text_candidates=[TextCandidate(id='tc1', text='你好，世界')],
        selected_text_candidate_id='tc1')
    project.plans.append(PlanningRevision(
        id='plan1', scope_unit_ids=[project.units[0].id],
        segmentation_kind='baseline', segments=[segment], score=0.1))
    project.selected_plan_id = 'plan1'

    loaded = DubProject.from_dict(project.to_dict())
    assert loaded.selected_plan_id == 'plan1'
    assert loaded.plans[0].segments[0].text_candidates[0].text == '你好，世界'
