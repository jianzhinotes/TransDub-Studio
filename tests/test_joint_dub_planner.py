import copy
import json
import wave
from pathlib import Path

from videotrans.dub.backends.base import AudioArtifact, DubbingBackend, SynthesisRequest
from videotrans.dub.backends.legacy_tts import LegacyTTSBackend
from videotrans.dub.constraints import QualityProfile, candidate_loss
from videotrans.dub.duration import DurationModel
from videotrans.dub.legacy_adapter import make_project_id, plan_to_queue, project_from_queue
from videotrans.dub.llm_candidates import (
    DeepSeekCandidateGenerator,
    has_obvious_english_leak,
    _sanitize_obvious_english,
)
from videotrans.dub.planner import JointDubPlanner
from videotrans.dub.presentation import build_plan_view
from videotrans.dub.schema import TextCandidate
from videotrans.dub.segmentation import build_segmentation_options
from videotrans.dub.source_analysis import build_source_turns
from videotrans.dub.store import DubProjectStore
from videotrans.dub.translation import ChineseCandidateGenerator
from videotrans.task.joint_dub import run_joint_preview


def _project(queue, name='demo'):
    pid = make_project_id(f'/video/{name}.mp4', 'zh-cn')
    return project_from_queue(
        queue, project_id=pid, name=name,
        source_language='en', target_language='zh-cn')


def _row(line, text, start, end, *, spk='spk0', ref=None, filename=''):
    return {
        'line': line,
        'text': text,
        'ref_text': ref or f'Source line {line}.',
        'start_time': start,
        'end_time': end,
        'start_time_source': start,
        'end_time_source': end,
        'spk': spk,
        'role': 'clone',
        'tts_type': 8,
        'filename': filename,
    }


def test_source_turns_respect_speaker_and_gap():
    project = _project([
        _row(1, '第一句', 0, 1000, spk='spk0'),
        _row(2, '第二句', 1200, 2200, spk='spk0'),
        _row(3, '第三句', 2300, 3300, spk='spk1'),
        _row(4, '第四句', 5000, 6000, spk='spk1'),
    ])
    turns = build_source_turns(project)
    assert [len(turn.unit_ids) for turn in turns] == [2, 1, 1]
    assert turns[0].prosody['inter_unit_gaps_ms'] == [200]
    assert turns[0].speaker_id == 'spk0'


def test_segmentation_offers_short_fragment_merge():
    project = _project([
        _row(1, '对', 0, 600),
        _row(2, '所以我们继续', 700, 2400),
        _row(3, '这是完整的一句。', 3000, 5500),
    ])
    turn = build_source_turns(project)[0]
    options = build_segmentation_options(turn, {unit.id: unit for unit in project.units})
    assert {option.kind for option in options} == {'baseline', 'merge_short'}
    merged = next(option for option in options if option.kind == 'merge_short')
    assert merged.groups[0].unit_ids == [project.units[0].id, project.units[1].id]
    assert '对' in merged.groups[0].baseline_text
    assert '所以我们继续' in merged.groups[0].baseline_text


def test_chinese_candidates_are_conservative_and_keep_entities():
    generator = ChineseCandidateGenerator()
    model = DurationModel()
    candidates = generator.generate(
        segment_id='s1',
        baseline_text='实际上，我认为 Elon Musk 会在 2030 年继续。',
        target_duration_ms=3000,
        speaker_id='spk0',
        duration_model=model,
    )
    assert {candidate.kind for candidate in candidates} == {'baseline', 'spoken', 'compact'}
    assert all('Elon Musk' in candidate.text and '2030' in candidate.text for candidate in candidates)
    assert next(c for c in candidates if c.kind == 'spoken').text.startswith('实际上，我觉得')
    assert '实际上' not in next(c for c in candidates if c.kind == 'compact').text


def _deepseek_context(project):
    turn = build_source_turns(project)[0]
    options = build_segmentation_options(
        turn, {unit.id: unit for unit in project.units})
    return turn, options


def _deepseek_response(payload, text_factory):
    request = json.loads(payload['messages'][1]['content'])
    return {
        'options': [
            {
                'option_id': option['option_id'],
                'segments': [
                    {
                        'segment_id': segment['segment_id'],
                        'candidates': text_factory(segment),
                    }
                    for segment in option['segments']
                ],
            }
            for option in request['segmentation_options']
        ],
    }


def test_deepseek_generates_all_segmentation_options_in_one_turn_request(tmp_path):
    project = _project([
        _row(1, '实际上，我认为 Elon Musk 会在 2030 年继续。', 0, 900,
             ref='Elon Musk said this would continue through 2030.'),
        _row(2, '然而我们将会谨慎推进。', 1000, 2600,
             ref='However, we will proceed carefully.'),
    ])
    turn, options = _deepseek_context(project)
    calls = []

    def request(payload):
        calls.append(payload)

        def candidates(segment):
            baseline = segment['baseline_text']
            if 'Elon Musk' in baseline:
                natural = (baseline.replace('实际上，我认为', '我觉得')
                           .replace('然而', '不过').replace('我们将会', '我们会'))
                return [
                    {'kind': 'natural', 'text': natural},
                    {'kind': 'compact', 'text': natural.replace('我觉得', '我看')},
                ]
            return [
                {'kind': 'natural', 'text': f'{baseline.rstrip("。")}呢。'},
                {'kind': 'compact', 'text': f'{baseline.rstrip("。")}吧。'},
            ]

        return json.dumps(_deepseek_response(payload, candidates), ensure_ascii=False)

    generator = DeepSeekCandidateGenerator(
        api_key='test', model='deepseek-test', cache_dir=tmp_path,
        request_fn=request)
    generated = generator.generate_turn(
        turn=turn, options=options, target_language='zh-cn',
        duration_model=DurationModel())

    assert len(calls) == 1
    request_data = json.loads(calls[0]['messages'][1]['content'])
    assert len(request_data['segmentation_options']) == len(options)
    assert set(generated) == {option.id for option in options}
    all_candidates = [
        candidate
        for groups in generated.values()
        for candidates in groups.values()
        for candidate in candidates
    ]
    assert any(candidate.kind == 'deepseek_natural' for candidate in all_candidates)
    protected = [c for c in all_candidates if c.kind.startswith('deepseek_')
                 and '2030' in c.text]
    assert protected
    assert all('Elon Musk' in candidate.text for candidate in protected)
    assert generator.last_diagnostics['status'] == 'ok'


def test_deepseek_rejects_missing_numbers_and_english_leak(tmp_path):
    project = _project([
        _row(1, 'Elon Musk 会在 2030 年继续。', 0, 2000,
             ref='Elon Musk will continue in 2030.'),
    ])
    turn, options = _deepseek_context(project)

    def request(payload):
        return _deepseek_response(payload, lambda segment: [
            {'kind': 'natural', 'text': 'Elon Musk will definitely continue soon.'},
            {'kind': 'compact', 'text': 'Elon Musk 会继续。'},
        ])

    generator = DeepSeekCandidateGenerator(
        api_key='test', model='deepseek-test', cache_dir=tmp_path,
        request_fn=request)
    generated = generator.generate_turn(
        turn=turn, options=options, target_language='zh-cn',
        duration_model=DurationModel())

    assert generator.last_diagnostics['status'] == 'fallback'
    assert all(
        not candidate.kind.startswith('deepseek_')
        for groups in generated.values()
        for candidates in groups.values()
        for candidate in candidates
    )
    assert not list(tmp_path.glob('*.json'))


def test_deepseek_does_not_protect_ordinary_english_from_bad_baseline(tmp_path):
    project = _project([
        _row(1, 'Yes The 地球的整个质量都归在微小的杂项类别里。', 0, 2000,
             ref='The mass of Earth is in the miscellaneous category.'),
    ])
    turn, options = _deepseek_context(project)

    def request(payload):
        return _deepseek_response(payload, lambda segment: [
            {'kind': 'natural', 'text': 'Yes The 地球的整个质量都归在微小的杂项类别里。'},
            {'kind': 'compact', 'text': '地球的整个质量都归在微小的杂项类别里。'},
        ])

    generator = DeepSeekCandidateGenerator(
        api_key='test', model='deepseek-test', cache_dir=tmp_path,
        request_fn=request)
    generated = generator.generate_turn(
        turn=turn, options=options, target_language='zh-cn',
        duration_model=DurationModel())
    all_candidates = [
        candidate
        for groups in generated.values()
        for candidates in groups.values()
        for candidate in candidates
    ]

    assert any(c.kind == 'deepseek_compact' for c in all_candidates)
    assert not any(c.kind == 'deepseek_natural' for c in all_candidates)
    assert has_obvious_english_leak('Yes The 地球质量很小。')
    assert not has_obvious_english_leak('xAI 使用 V3 卫星。')


def test_deepseek_targeted_second_pass_repairs_contaminated_fallback(tmp_path):
    project = _project([
        _row(1, 'Yeah The 照射到地球横截面的太阳能。', 0, 2400,
             ref='Yeah The incident solar energy on the cross-section of the Earth.'),
    ])
    turn, options = _deepseek_context(project)
    calls = []

    def request(payload):
        data = json.loads(payload['messages'][1]['content'])
        calls.append(data)
        if 'segmentation_options' in data:
            return _deepseek_response(payload, lambda segment: [
                {'kind': 'natural', 'text': segment['baseline_text']},
                {'kind': 'compact', 'text': segment['baseline_text']},
            ])
        assert all(
            'Yeah The' not in segment['protected_terms']
            for option in data['options'] for segment in option['segments'])
        return {
            'options': [
                {
                    'option_id': option['option_id'],
                    'segments': [
                        {
                            'segment_id': segment['segment_id'],
                            'candidates': [
                                {'kind': 'natural', 'text': '地球横截面接收到的太阳能。'},
                                {'kind': 'compact', 'text': '地球接收到的太阳能。'},
                            ],
                        }
                        for segment in option['segments']
                    ],
                }
                for option in data['options']
            ],
        }

    generator = DeepSeekCandidateGenerator(
        api_key='test', model='deepseek-test', cache_dir=tmp_path,
        request_fn=request)
    generated = generator.generate_turn(
        turn=turn, options=options, target_language='zh-cn',
        duration_model=DurationModel())
    candidates = [
        candidate
        for groups in generated.values()
        for group_candidates in groups.values()
        for candidate in group_candidates
    ]

    assert len(calls) == 2
    assert any(c.kind.startswith('deepseek_repair_') for c in candidates)
    assert generator.last_diagnostics['repaired_segments'] >= 1
    assert not any(
        has_obvious_english_leak(c.text)
        for c in candidates if c.kind.startswith('deepseek_repair_'))


def test_local_repair_prevents_bad_baseline_after_two_api_failures(tmp_path):
    project = _project([
        _row(1, "What's Starship's 使命是什么，它要做什么。", 0, 2400,
             ref="What's Starship's purpose of being?"),
    ])
    turn, options = _deepseek_context(project)
    calls = []

    def broken_request(payload):
        calls.append(payload)
        return 'not json'

    generator = DeepSeekCandidateGenerator(
        api_key='test', model='deepseek-test', cache_dir=tmp_path,
        request_fn=broken_request)
    generated = generator.generate_turn(
        turn=turn, options=options, target_language='zh-cn',
        duration_model=DurationModel())
    candidates = [
        candidate
        for groups in generated.values()
        for group_candidates in groups.values()
        for candidate in group_candidates
    ]

    assert len(calls) == 2
    assert generator.last_diagnostics['status'] == 'repaired_fallback'
    assert any(c.kind == 'local_english_repair' for c in candidates)
    assert all(
        not has_obvious_english_leak(c.text)
        for c in candidates if c.kind == 'local_english_repair')


def test_local_english_repair_covers_observed_boundary_fragments():
    examples = (
        'Yeah The 照射到地球横截面的太阳能',
        "Yeah We're 太",
        'Yeah Yeah，那是我们的目标。',
        "What's Starship's 使命是什么，它要做什么",
        'An AI卫星基本上就是大量太阳能电池和一个散热器',
        '我们有Master Orbit，有Putting Solar，第三个是芯片。',
    )
    assert all(not has_obvious_english_leak(_sanitize_obvious_english(text))
               for text in examples)


def test_contaminated_short_candidate_cannot_beat_longer_clean_candidate():
    dirty = TextCandidate(
        id='dirty', text='Yeah The 照射到地球横截面的太阳能',
        semantic_score=0.0, naturalness_score=0.0,
        estimated_duration_ms=3500,
        metadata={'english_leak_fallback': True},
    )
    clean = TextCandidate(
        id='clean', text='特别是与地球消耗相比，照射到地球横截面的太阳能。',
        semantic_score=0.93, naturalness_score=0.90,
        estimated_duration_ms=5000,
    )
    profile = QualityProfile()

    assert candidate_loss(dirty, 3740, profile) > candidate_loss(clean, 3740, profile)


def test_deepseek_rejects_english_only_when_baseline_is_also_english(tmp_path):
    project = _project([
        _row(1, 'Elon Musk will continue in 2030.', 0, 2000,
             ref='Elon Musk will continue in 2030.'),
    ])
    turn, options = _deepseek_context(project)

    def request(payload):
        return _deepseek_response(payload, lambda segment: [
            {'kind': 'natural', 'text': 'Elon Musk will continue in 2030.'},
            {'kind': 'compact', 'text': 'Elon Musk 会在 2030 年继续。'},
        ])

    generator = DeepSeekCandidateGenerator(
        api_key='test', model='deepseek-test', cache_dir=tmp_path,
        request_fn=request)
    generated = generator.generate_turn(
        turn=turn, options=options, target_language='zh-cn',
        duration_model=DurationModel())
    candidates = next(iter(next(iter(generated.values())).values()))

    assert any(candidate.kind == 'deepseek_compact' for candidate in candidates)
    assert not any(candidate.kind == 'deepseek_natural' for candidate in candidates)


def test_deepseek_turn_candidate_cache_avoids_second_request(tmp_path):
    project = _project([
        _row(1, '这个方案会继续。', 0, 2000, ref='This plan will continue.'),
    ])
    turn, options = _deepseek_context(project)
    calls = []

    def request(payload):
        calls.append(payload)
        return _deepseek_response(payload, lambda segment: [
            {'kind': 'natural', 'text': '这个方案会继续推进。'},
            {'kind': 'compact', 'text': '这个方案继续推进。'},
        ])

    generator = DeepSeekCandidateGenerator(
        api_key='test', model='deepseek-test', cache_dir=tmp_path,
        request_fn=request)
    first = generator.generate_turn(
        turn=turn, options=options, target_language='zh-cn',
        duration_model=DurationModel())
    second = generator.generate_turn(
        turn=turn, options=options, target_language='zh-cn',
        duration_model=DurationModel())

    assert first.keys() == second.keys()
    assert len(calls) == 1
    assert generator.last_diagnostics['cache_hit'] is True
    assert len(list(tmp_path.glob('*.json'))) == 1


class TurnBatchGenerator(ChineseCandidateGenerator):
    name = 'turn-batch-test'

    def __init__(self):
        self.calls = []

    def generate_turn(self, **kwargs):
        self.calls.append([option.kind for option in kwargs['options']])
        return super().generate_turn(**kwargs)


def test_planner_calls_candidate_generator_once_per_source_turn():
    project = _project([
        _row(1, '对', 0, 600),
        _row(2, '所以我们继续', 700, 2400),
    ])
    generator = TurnBatchGenerator()
    plan = JointDubPlanner(candidate_generator=generator).optimize(project)

    assert generator.calls == [['baseline', 'merge_short']]
    assert plan.metadata['candidate_generator'] == 'turn-batch-test'


def test_duration_model_calibrates_per_speaker():
    queue = [
        {**_row(1, '一二三四', 0, 1200, spk='spk0', filename='a.wav'), 'dubbing_s': 1.0},
        {**_row(2, '五六七八', 1300, 2500, spk='spk0', filename='b.wav'), 'dubbing_s': 1.0},
    ]
    model = DurationModel.from_project(_project(queue))
    assert 'spk0' in model.speaker_rates
    assert 900 <= model.estimate('天地玄黄', 'spk0') <= 1100


def test_dry_plan_limits_scope_and_keeps_joint_decision():
    queue = [
        _row(i + 1, '然而我们将会继续', i * 1100, i * 1100 + 1000)
        for i in range(25)
    ]
    project = _project(queue)
    plan = JointDubPlanner().optimize(project, limit=20)
    assert len(plan.scope_unit_ids) == 20
    assert plan.status == 'planned'
    assert project.selected_plan_id == plan.id
    assert plan.segments
    assert all(segment.text_candidates for segment in plan.segments)
    assert all(segment.selected_text_candidate_id for segment in plan.segments)
    assert plan.metadata['turn_choices']


def test_plan_view_exposes_candidates_timing_risk_and_fallbacks():
    project = _project([_row(1, '然而我们将会继续', 0, 1000)])
    plan = JointDubPlanner().optimize(project)
    plan.segments[0].metrics['predicted_duration_ms'] = 1300
    plan.segments[0].metrics['predicted_stretch_ratio'] = 1.3
    plan.metadata['generator_diagnostics'] = {
        'turn-1': {'status': 'fallback', 'cache_hit': False},
        'turn-2': {'status': 'ok', 'cache_hit': True},
    }

    view = build_plan_view(plan)
    row = view['rows'][0]
    assert row['baseline_text'] == '然而我们将会继续'
    assert row['selected_text']
    assert row['candidate_count'] >= 2
    assert 'loss=' in row['candidate_details']
    assert row['risk'] == 'overflow'
    assert view['risk_counts']['overflow'] == 1
    assert view['diagnostic_counts'] == {'ok': 1, 'partial': 0, 'fallback': 1}
    assert view['cache_hits'] == 1


class RetryGenerator:
    def generate(self, *, segment_id, baseline_text, target_duration_ms,
                 speaker_id, duration_model):
        return [
            TextCandidate(
                id=f'{segment_id}:long', text='语义完整的长版本', kind='baseline',
                semantic_score=1.0, naturalness_score=1.0,
                estimated_duration_ms=800),
            TextCandidate(
                id=f'{segment_id}:short', text='短版本', kind='compact',
                semantic_score=0.95, naturalness_score=0.9,
                estimated_duration_ms=650),
        ]


class RetryBackend(DubbingBackend):
    name = 'fake-retry'

    def __init__(self):
        self.calls = []

    def synthesize_batch(self, requests):
        self.calls.append([request.text for request in requests])
        return [AudioArtifact(
            request_id=request.id,
            path=request.output_path,
            duration_ms=1500 if '长版本' in request.text else 900,
            backend=self.name,
        ) for request in requests]


def test_actual_audio_feedback_retries_shorter_candidate(tmp_path):
    project = _project([_row(1, '原始译文', 0, 1000)])
    backend = RetryBackend()
    plan = JointDubPlanner(candidate_generator=RetryGenerator()).optimize(
        project,
        limit=20,
        backend=backend,
        candidate_dir=tmp_path,
        synthesize=True,
    )
    segment = plan.segments[0]
    selected = next(c for c in segment.text_candidates
                    if c.id == segment.selected_text_candidate_id)
    assert plan.status == 'validated'
    assert selected.text == '短版本'
    assert len(segment.audio_candidates) == 2
    assert segment.quality_reports[0].hard_failures == ['duration_overflow']
    assert segment.quality_reports[1].passed is True
    assert backend.calls == [['语义完整的长版本'], ['短版本']]


class MustNotGenerateCandidates:
    def generate(self, **_kwargs):
        raise AssertionError('existing-plan synthesis must not generate text candidates')

    def generate_turn(self, **_kwargs):
        raise AssertionError('existing-plan synthesis must not call an LLM or rules')


class ExistingPlanBackend(DubbingBackend):
    name = 'fake-existing-plan'

    def synthesize_batch(self, requests):
        return [AudioArtifact(
            request_id=request.id,
            path=request.output_path,
            duration_ms=900,
            backend=self.name,
        ) for request in requests]


def test_existing_plan_audio_does_not_retranslate_or_replan(tmp_path):
    project = _project([_row(1, '自然的中文候选', 0, 1600, filename='current.wav')])
    plan = JointDubPlanner().optimize(project)

    result = JointDubPlanner(
        candidate_generator=MustNotGenerateCandidates()).synthesize_existing_plan(
            project, plan, backend=ExistingPlanBackend(),
            candidate_dir=tmp_path / 'ab')

    assert result is plan
    assert plan.status == 'validated'
    assert plan.segments[0].selected_audio_candidate_id
    view = build_plan_view(plan, project)
    assert view['rows'][0]['current_audio_paths'] == ['current.wav']
    assert view['rows'][0]['planned_audio_path'].endswith('.wav')
    assert view['rows'][0]['audio_validated'] is True


def test_joint_plan_materializes_as_tts_queue_before_synthesis(tmp_path):
    queue = [
        _row(1, '第一句', 0, 1200, filename=str(tmp_path / 'old-1.wav')),
        _row(2, '第二句', 1200, 2400, filename=str(tmp_path / 'old-2.wav')),
    ]
    project = _project(queue)
    plan = JointDubPlanner().optimize(project, limit=None)

    smart_queue = plan_to_queue(project, plan)

    assert smart_queue
    assert len(smart_queue) == len(plan.segments)
    assert all(item.get('planned_segment_id') for item in smart_queue)
    assert all(Path(item['filename']).name.startswith('smart-') for item in smart_queue)
    assert [item['line'] for item in smart_queue] == list(range(1, len(smart_queue) + 1))
    assert smart_queue[0]['startraw'] == '00:00:00,000'


class IsolatingBackend(DubbingBackend):
    name = 'fake-isolate'

    def synthesize_batch(self, requests):
        if any('坏段' in request.text for request in requests):
            raise RuntimeError('bad segment')
        return [AudioArtifact(
            request_id=request.id, path=request.output_path,
            duration_ms=700, backend=self.name) for request in requests]


def test_batch_failure_isolated_to_one_segment(tmp_path):
    project = _project([
        _row(1, '正常片段内容', 0, 2000, spk='spk0'),
        _row(2, '坏段内容较长', 3000, 5000, spk='spk1'),
    ])
    plan = JointDubPlanner().optimize(
        project,
        backend=IsolatingBackend(),
        candidate_dir=tmp_path,
        synthesize=True,
    )
    assert plan.status == 'failed'
    good, bad = plan.segments
    assert good.selected_audio_candidate_id
    assert not bad.selected_audio_candidate_id
    assert bad.quality_reports[0].hard_failures == ['synthesis_error']


def test_safe_entry_does_not_mutate_queue_and_can_persist(tmp_path):
    queue = [_row(1, '然而我们将会继续', 0, 2000)]
    original = copy.deepcopy(queue)
    state_dir = tmp_path / 'preview.tdproj'
    project, plan = run_joint_preview(
        queue_tts=queue,
        source_video='/video/demo.mp4',
        source_language='en',
        target_language='zh-cn',
        name='demo',
        candidate_dir=str(tmp_path / 'candidates'),
        synthesize=False,
        project_dir=str(state_dir),
        candidate_backend='rules',
    )
    assert queue == original
    assert plan.status == 'planned'
    assert DubProjectStore(state_dir).load().selected_plan_id == project.selected_plan_id

    _, second = run_joint_preview(
        queue_tts=queue,
        source_video='/video/demo.mp4',
        source_language='en', target_language='zh-cn', name='demo',
        candidate_dir=str(tmp_path / 'candidates'),
        synthesize=False, project_dir=str(state_dir), candidate_backend='rules')
    persisted = DubProjectStore(state_dir).load()
    assert len(persisted.plans) == 2
    assert persisted.selected_plan_id == second.id


def test_legacy_tts_backend_wraps_existing_batch_api(tmp_path, monkeypatch):
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        for item in kwargs['queue_tts']:
            path = Path(item['filename'])
            path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(path), 'wb') as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(b'\0\0' * 1600)
        first_dir = Path(kwargs['queue_tts'][0]['filename']).parent
        (first_dir / 'lang_leak.json').write_text(
            json.dumps({Path(kwargs['queue_tts'][1]['filename']).name: 'leaked'}),
            encoding='utf-8')

    import videotrans.tts
    monkeypatch.setattr(videotrans.tts, 'run', fake_run)
    backend = LegacyTTSBackend(tts_type=8, language='zh-cn', uuid='test')
    requests = [
        SynthesisRequest(
            id=f'r{i}', segment_id=f's{i}', text_candidate_id=f't{i}',
            text=f'候选{i}', output_path=str(tmp_path / f'c{i}.wav'),
            language='zh-cn', speaker_id='spk0',
            legacy_payload={'ref_text': f'Reference {i}.', 'ref_wav': f'ref{i}.wav', 'role': 'clone'})
        for i in range(2)
    ]
    artifacts = backend.synthesize_batch(requests)

    assert captured['tts_type'] == 8
    assert [item['text'] for item in captured['queue_tts']] == ['候选0', '候选1']
    assert captured['queue_tts'][0]['ref_text'] == 'Reference 0.'
    assert [artifact.duration_ms for artifact in artifacts] == [100, 100]
    assert artifacts[0].metadata['language_leak'] is None
    assert artifacts[1].metadata['language_leak'] == 'leaked'
    assert backend.capabilities().supports_voice_clone is True


def test_legacy_tts_backend_repairs_expired_clone_reference(tmp_path, monkeypatch):
    source = tmp_path / 'source.wav'
    with wave.open(str(source), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b'\0\0' * 32000)

    captured = {}

    def fake_runffmpeg(args):
        target = Path(args[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'RIFF-rebuilt-reference')
        return True

    def fake_tts_run(**kwargs):
        captured.update(kwargs)
        path = Path(kwargs['queue_tts'][0]['filename'])
        with wave.open(str(path), 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b'\0\0' * 1600)

    import videotrans.tts
    import videotrans.util.help_ffmpeg
    monkeypatch.setattr(videotrans.tts, 'run', fake_tts_run)
    monkeypatch.setattr(videotrans.util.help_ffmpeg, 'runffmpeg', fake_runffmpeg)
    backend = LegacyTTSBackend(
        tts_type=8, language='zh-cn', source_audio=str(source),
        reference_dir=str(tmp_path / 'refs'))
    request = SynthesisRequest(
        id='r1', segment_id='s1', text_candidate_id='t1', text='你好',
        output_path=str(tmp_path / 'candidate.wav'), language='zh-cn',
        speaker_id='spk0', legacy_payload={
            'line': 7, 'role': 'clone', 'ref_text': 'Hello',
            'ref_wav': str(tmp_path / 'expired.wav'),
            'start_time_source': 250, 'end_time_source': 1250,
        })

    backend.synthesize_batch([request])

    repaired = Path(captured['queue_tts'][0]['ref_wav'])
    assert repaired.is_file()
    assert repaired.name == 'clone-7-250-1250.wav'
