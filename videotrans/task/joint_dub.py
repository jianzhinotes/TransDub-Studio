"""20 单元联合规划纵向闭环的安全调用入口。

此入口不会修改传入 queue，也不会自动替换正式字幕/音轨。调用方明确传入
``synthesize=True`` 时才生成候选音频，适合先做局部预览和质量对照。
"""

from videotrans.dub.backends import LegacyTTSBackend
from videotrans.dub.legacy_adapter import make_project_id, project_from_queue
from videotrans.dub.llm_candidates import build_candidate_generator
from videotrans.dub.planner import JointDubPlanner
from videotrans.dub.store import DubProjectStore


def run_joint_preview(
        *,
        queue_tts,
        source_video: str,
        source_language: str,
        target_language: str,
        name: str,
        candidate_dir: str,
        limit: int = 20,
        tts_type: int = None,
        uuid: str = None,
        is_cuda: bool = False,
        synthesize: bool = False,
        project_dir: str = None,
        candidate_backend: str = "auto",
        candidate_cache_dir: str = None,
):
    project_id = make_project_id(source_video, target_language)
    store = DubProjectStore(project_dir) if project_dir else None
    existing = store.load() if store and store.exists() else None
    project = project_from_queue(
        queue_tts,
        project_id=project_id,
        name=name,
        source_language=source_language,
        target_language=target_language,
        existing=existing,
    )
    backend = None
    if synthesize:
        if tts_type is None:
            raise ValueError("tts_type is required when synthesize=True")
        backend = LegacyTTSBackend(
            tts_type=tts_type,
            language=target_language,
            uuid=uuid,
            is_cuda=is_cuda,
            source_audio=source_video,
            reference_dir=f"{candidate_dir}/_references",
        )
    candidate_generator = build_candidate_generator(
        candidate_backend,
        cache_dir=candidate_cache_dir or f"{candidate_dir}/llm_cache",
        target_language=target_language,
    )
    plan = JointDubPlanner(candidate_generator=candidate_generator).optimize(
        project,
        limit=limit,
        backend=backend,
        candidate_dir=candidate_dir,
        synthesize=synthesize,
    )
    if store:
        store.save(project)
    return project, plan


def synthesize_joint_plan(
        *, project, plan_id: str, candidate_dir: str, tts_type: int,
        language: str, uuid: str = None, is_cuda: bool = False,
        project_dir: str = None,
):
    """为已保存的规划生成候选音频；不会再次调用翻译或候选 LLM。"""
    plan = next((item for item in project.plans if item.id == plan_id), None)
    if plan is None:
        raise ValueError(f"Unknown planning revision: {plan_id}")
    backend = LegacyTTSBackend(
        tts_type=tts_type,
        language=language,
        uuid=uuid,
        is_cuda=is_cuda,
        source_audio=f"{project_dir}/source.wav" if project_dir else None,
        reference_dir=f"{candidate_dir}/_references",
    )
    JointDubPlanner().synthesize_existing_plan(
        project, plan, backend=backend, candidate_dir=candidate_dir)
    if project_dir:
        DubProjectStore(project_dir).save(project)
    return project, plan
