"""可重开编辑工程（.tdproj）：任务完成后把"重新对齐/合成"所需数据持久化到
输出目录旁，之后可反复打开工作台编辑、仅重跑 align+assembling 出新成品。

无 Qt 依赖，可单测。工程内容：
  project.json   v2 工程清单 + 任务 cfg 关键字段 + 成品/原视频路径
  dub_project.json 联合规划数据（单元、候选、质量报告；第一阶段与 queue 同步）
  queue_tts.json 旧流水线兼容数据（filename 相对化为 dubb/xxx.wav）
  dubb/*.wav     逐行配音片段（align 从这些重新拼接对齐，不依赖预拼 target_wav）
  novoice.mp4    无声视频（align 变速 + assembling 合成用）
  source.wav     原声（波形预览用，可选）
"""
import copy
import dataclasses
import json
import shutil
import time
from pathlib import Path

PROJECT_EXT = '.tdproj'
_PROJECT_JSON = 'project.json'
_QUEUE_JSON = 'queue_tts.json'
_DUBB_DIR = 'dubb'
_NOVOICE = 'novoice.mp4'
_SOURCE = 'source.wav'


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return default


def _project_id(manifest: dict, *, source_video: str, target_language: str) -> str:
    from videotrans.dub.legacy_adapter import make_project_id
    return manifest.get('project_id') or make_project_id(source_video, target_language)


def _sync_dub_project(proj: Path, manifest: dict, queue: list) -> None:
    """把兼容 queue 同步到 v2 状态文件，同时保留已有候选历史。"""
    from videotrans.dub.legacy_adapter import project_from_queue
    from videotrans.dub.store import DubProjectStore

    store = DubProjectStore(proj)
    existing = None
    if store.exists():
        try:
            existing = store.load()
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            existing = None
    cfg = manifest.get('cfg') or {}
    state = project_from_queue(
        queue,
        project_id=manifest['project_id'],
        name=cfg.get('noextname') or Path(manifest.get('source_video') or proj.stem).stem,
        source_language=cfg.get('source_language_code') or '',
        target_language=manifest.get('target_language_code') or cfg.get('target_language_code') or '',
        existing=existing,
    )
    store.save(state)


def _upgrade_manifest(proj: Path, manifest: dict, queue: list) -> dict:
    """把 v1/无版本工程原地升级到 v2；旧字段和 queue 文件全部保留。"""
    from videotrans.dub.legacy_adapter import ensure_queue_unit_ids
    from videotrans.dub.schema import PROJECT_SCHEMA_VERSION
    from videotrans.dub.store import STATE_FILE, atomic_write_json

    source_video = manifest.get('source_video') or (manifest.get('cfg') or {}).get('name') or ''
    target_language = (manifest.get('target_language_code')
                       or (manifest.get('cfg') or {}).get('target_language_code') or '')
    manifest = dict(manifest)
    manifest['schema_version'] = PROJECT_SCHEMA_VERSION
    manifest['project_id'] = _project_id(
        manifest, source_video=source_video, target_language=target_language)
    manifest['state_file'] = STATE_FILE
    manifest['queue_file'] = _QUEUE_JSON
    manifest['updated'] = int(time.time())
    ensure_queue_unit_ids(queue, manifest['project_id'])
    atomic_write_json(proj / _QUEUE_JSON, queue)
    atomic_write_json(proj / _PROJECT_JSON, manifest)
    _sync_dub_project(proj, manifest, queue)
    return manifest


def project_dir_for(target_dir: str, noextname: str) -> str:
    return str(Path(target_dir) / f'{noextname}{PROJECT_EXT}')


def find_project(root_dir: str, video_stem: str) -> str:
    """在输出根目录下按视频名递归查找工程（工程实际在 {视频名}-{ext}/ 子文件夹内）。"""
    if not root_dir:
        return None
    root = Path(root_dir)
    if not root.is_dir():
        return None
    for p in root.rglob(f'*{PROJECT_EXT}'):
        if p.is_dir() and p.stem == video_stem:
            return str(p)
    return None


def _copy_if_diff(src, dst):
    if Path(src).resolve() != Path(dst).resolve():
        shutil.copy2(src, dst)


def save_project(cfg, queue_tts, cache_folder: str) -> str:
    """把编辑工程持久化到 {target_dir}/{noextname}.tdproj/，返回工程目录。"""
    proj = Path(project_dir_for(cfg.target_dir, cfg.noextname))
    dubb = proj / _DUBB_DIR
    dubb.mkdir(parents=True, exist_ok=True)

    old_manifest = _read_json(proj / _PROJECT_JSON, {}) or {}

    # 逐行配音复制进工程并把 filename 相对化（源==目标时跳过复制，兼容工程内重存）
    queue = copy.deepcopy(list(queue_tts))
    for it in queue:
        fn = it.get('filename') if hasattr(it, 'get') else it['filename']
        if fn and Path(fn).exists():
            dst = dubb / Path(fn).name
            try:
                _copy_if_diff(fn, dst)
                it['filename'] = f'{_DUBB_DIR}/{dst.name}'
            except OSError:
                it['filename'] = ''
        else:
            it['filename'] = ''

    # 无声视频 / 原声
    novoice_src = getattr(cfg, 'novoice_mp4', None)
    if novoice_src and Path(novoice_src).exists():
        _copy_if_diff(novoice_src, proj / _NOVOICE)
    source_src = getattr(cfg, 'source_wav', None)
    if source_src and Path(source_src).exists():
        _copy_if_diff(source_src, proj / _SOURCE)

    from videotrans.dub.legacy_adapter import ensure_queue_unit_ids
    from videotrans.dub.schema import PROJECT_SCHEMA_VERSION
    from videotrans.dub.store import STATE_FILE, atomic_write_json

    source_video = getattr(cfg, 'name', None) or ''
    target_language = getattr(cfg, 'target_language_code', None) or ''
    project_id = _project_id(
        old_manifest, source_video=source_video, target_language=target_language)
    ensure_queue_unit_ids(queue, project_id)
    atomic_write_json(proj / _QUEUE_JSON, queue)
    project = {
        'schema_version': PROJECT_SCHEMA_VERSION,
        'project_id': project_id,
        'state_file': STATE_FILE,
        'queue_file': _QUEUE_JSON,
        'cfg': dataclasses.asdict(cfg),
        'output_mp4': getattr(cfg, 'targetdir_mp4', None),
        'source_video': source_video,
        'target_language_code': target_language,
        'created': old_manifest.get('created') or int(time.time()),
        'updated': int(time.time()),
    }
    atomic_write_json(proj / _PROJECT_JSON, project)
    _sync_dub_project(proj, project, queue)
    return str(proj)


def save_queue(proj_dir: str, queue) -> None:
    """工作台编辑后把 queue_tts 写回工程：重配产生的新 wav 收进 dubb/，filename 相对化。"""
    proj = Path(proj_dir)
    dubb = proj / _DUBB_DIR
    dubb.mkdir(parents=True, exist_ok=True)
    out = copy.deepcopy(list(queue))
    for it in out:
        fn = it.get('filename') if hasattr(it, 'get') else it['filename']
        if fn and Path(fn).exists():
            dst = dubb / Path(fn).name
            try:
                _copy_if_diff(fn, dst)
                it['filename'] = f'{_DUBB_DIR}/{dst.name}'
            except OSError:
                it['filename'] = ''
        else:
            it['filename'] = ''
    manifest = _read_json(proj / _PROJECT_JSON, {}) or {}
    _upgrade_manifest(proj, manifest, out)


def load_project(proj_dir: str):
    """返回 (project_dict, queue_tts)：queue_tts 的 filename 还原为绝对路径。"""
    proj = Path(proj_dir)
    project = json.loads((proj / _PROJECT_JSON).read_text(encoding='utf-8'))
    queue = json.loads((proj / _QUEUE_JSON).read_text(encoding='utf-8'))
    from videotrans.dub.schema import PROJECT_SCHEMA_VERSION
    from videotrans.dub.store import DubProjectStore
    if (int(project.get('schema_version') or 1) < PROJECT_SCHEMA_VERSION
            or not DubProjectStore(proj).exists()):
        project = _upgrade_manifest(proj, project, queue)
    for it in queue:
        fn = it.get('filename')
        it['filename'] = str(proj / fn) if fn else ''
    return project, queue


def project_paths(proj_dir: str) -> dict:
    """工程内关键文件的绝对路径（供 realign 重建 cfg 就地指向工程目录）。"""
    proj = Path(proj_dir)
    return {
        'cache_folder': str(proj),
        'novoice_mp4': str(proj / _NOVOICE),
        'source_wav': str(proj / _SOURCE),
        'target_wav': str(proj / 'target.wav'),   # align 输出，重生成时产生
    }


def delete_project(proj_dir: str) -> None:
    try:
        shutil.rmtree(proj_dir, ignore_errors=True)
    except OSError:
        pass
