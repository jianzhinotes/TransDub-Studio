"""重对齐 worker：从已保存的编辑工程只重跑 align + assembling 出新成品。

不重跑识别/翻译/配音。工程原始逐行配音是"未变速"的，align 会就地变速，
故先把工程复制到临时工作区变速合成，工程本身保持未变速，可反复重生成。
"""
import shutil
import uuid as _uuid
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from videotrans.configure import config
from videotrans.configure.config import logger, tr


class RealignWorker(QThread):
    progressed = Signal(str)
    succeeded = Signal(str)   # 新成品 mp4 路径
    failed = Signal(str)

    def __init__(self, proj_dir: str, parent=None):
        super().__init__(parent)
        self.proj_dir = proj_dir

    def run(self):
        from videotrans.task.project import load_project
        from videotrans.task.taskcfg import TaskCfgVTT
        from videotrans.task.trans_create import TransCreate
        from videotrans.util.tools import get_video_duration
        work = None
        try:
            project, queue = load_project(self.proj_dir)
            cfg_dict = dict(project['cfg'])
            proj = Path(self.proj_dir)

            # 临时工作区：复制工程文件进来变速合成，不污染工程原始未变速 wav。
            # 逐行 wav 必须放工作区**根目录**——SpeedRate 拼接以 cache_folder 为 cwd、
            # 按文件名查找（help_ffmpeg.create_concat_txt 用 path.name）。
            new_uuid = _uuid.uuid4().hex
            work = f"{config.TEMP_DIR}/realign-{new_uuid}"
            Path(work).mkdir(parents=True, exist_ok=True)
            shutil.copy2(proj / 'novoice.mp4', Path(work) / 'novoice.mp4')
            if (proj / 'source.wav').exists():
                shutil.copy2(proj / 'source.wav', Path(work) / 'source.wav')
            for it in queue:
                fn = it.get('filename')
                if fn and Path(fn).exists():
                    dst = Path(work) / Path(fn).name
                    shutil.copy2(fn, dst)
                    it['filename'] = str(dst)
                else:
                    it['filename'] = ''

            # 重建 cfg：指向工作区、用新 uuid（原 uuid 已在 stoped 集合会让 align 直接返回）、
            # 关闭清缓存（否则 __post_init__ 会删 target_dir 里的成品）
            cfg_dict['uuid'] = new_uuid
            cfg_dict['cache_folder'] = work
            cfg_dict['clear_cache'] = False
            cfg = TaskCfgVTT(**cfg_dict)

            self.progressed.emit(tr('duiqicaozuo'))
            trk = TransCreate(cfg=cfg)
            trk.queue_tts = queue
            trk.cfg.novoice_mp4 = str(Path(work) / 'novoice.mp4')
            trk.cfg.source_wav = str(Path(work) / 'source.wav')
            trk.cfg.target_wav = str(Path(work) / 'target.wav')
            trk.video_time = get_video_duration(trk.cfg.novoice_mp4)

            trk.align()
            self.progressed.emit(tr('kaishihebing'))
            trk.assembling()

            self.succeeded.emit(cfg.targetdir_mp4)
        except Exception as e:
            logger.exception(f'重新对齐合成失败: {e}', exc_info=True)
            self.failed.emit(str(e))
        finally:
            if work:
                shutil.rmtree(work, ignore_errors=True)
