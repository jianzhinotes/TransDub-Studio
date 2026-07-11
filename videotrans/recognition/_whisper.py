import time, json,datetime
from pathlib import Path
from dataclasses import dataclass
from typing import Union, List

from videotrans.configure.config import settings, ROOT_DIR, logger
from videotrans.configure import config
from videotrans.recognition._base import BaseRecogn
from videotrans.task.taskcfg import SrtItem

from videotrans.util import tools
from pydub import AudioSegment
from videotrans.process import openai_whisper, faster_whisper
from videotrans.configure.contants import FASTER_MODELS_DICT
from videotrans.configure.excepts import SttTimeoutError

# mlx-whisper（Apple Silicon Metal 加速）的 HF 仓库名与通用命名模式不一致的特例
_MLX_REPO_SPECIAL = {
    'large-v3': 'mlx-community/whisper-large-v3-mlx',
}

@dataclass
class FasterAll(BaseRecogn):
    def __post_init__(self):
        super().__post_init__()
        local_dir = f'{ROOT_DIR}/models/models--'
        if self.model_name in FASTER_MODELS_DICT:
            local_dir += FASTER_MODELS_DICT[self.model_name].replace('/', '--')
        else:
            local_dir += self.model_name.replace('/', '--')
        self.local_dir = local_dir
        self.audio_duration=len(AudioSegment.from_wav(self.audio_file))
        self.speech_timestamps_file=None
        self.mlx_local_dir=None


    # ---- mlx-whisper（可选加速后端） ----
    def _mlx_ready(self) -> bool:
        """settings['use_mlx_whisper'] 开启且在 Apple Silicon 上、mlx_whisper 可导入。"""
        if self.recogn_type != 0 or not settings.get('use_mlx_whisper'):
            return False
        import sys, platform, importlib.util
        return (sys.platform == 'darwin' and platform.machine() == 'arm64'
                and importlib.util.find_spec('mlx_whisper') is not None)

    def _mlx_repo(self) -> str:
        custom = settings.get('mlx_whisper_repo')
        if custom:
            return custom
        return _MLX_REPO_SPECIAL.get(self.model_name, f'mlx-community/whisper-{self.model_name}')

    def _exec(self)->Union[List[SrtItem], None]:
        if self._exit(): return
        self.error = ''
        self.signal(text="STT starting, hold on...")
        if self.recogn_type == 1:  # openai-whisper
            raws = self._openai()
        else:
            raws = None
            if self._mlx_ready() and self.mlx_local_dir:
                try:
                    raws = self._mlx()
                except Exception as e:
                    logger.warning(f'mlx-whisper 识别失败，回退 faster-whisper: {e}')
                    raws = None
            if not raws:
                raws = self._faster()
        return raws

    def _download(self):
        if self.recogn_type == 0:
            if self._mlx_ready():
                repo_id = self._mlx_repo()
                local_dir = f"{ROOT_DIR}/models/mlx--{repo_id.replace('/', '--')}"
                try:
                    # model_id 传 repo_id：避免命中 FASTER_MODELS_DICT 的 modelscope
                    # 回退分支（那会把 CT2 模型下进 mlx 目录）
                    tools.check_and_down_hf(repo_id, repo_id, local_dir,
                                            callback=self._process_callback)
                    self.mlx_local_dir = local_dir
                except Exception as e:
                    logger.warning(f'mlx-whisper 模型 {repo_id} 下载失败，将使用 faster-whisper: {e}')
                    self.mlx_local_dir = None
            if self.model_name in FASTER_MODELS_DICT:
                repo_id = FASTER_MODELS_DICT[self.model_name]
            else:
                repo_id = self.model_name
            tools.check_and_down_hf(self.model_name,repo_id,self.local_dir,callback=self._process_callback)
        # 批量时预先vad切分
        # 否则后断句处理

        if settings.get('whisper_prepare'):
            self._vad_split()
            self.speech_timestamps_file=f'{self.cache_folder}/speech_timestamps_{time.time()}.json'
            Path(self.speech_timestamps_file).write_text(json.dumps(self.speech_timestamps),encoding='utf-8')


    def _openai(self)->Union[List[SrtItem], None]:
        title=f'STT use {self.model_name}'
        self.signal(text=title)
        # 起一个进程
        logs_file = f'{config.TEMP_DIR}/{self.uuid}/openai-{self.detect_language}-{time.time()}.log'
        # 最长持续时长>2000ms
        _max_speech=max(int(float(settings.get('max_speech_duration_s', 5)) * 1000),2000)
        if self.recogn2pass:
            # 2次识别， 生成简短的字幕,  最长持续时长>500ms
            _max_speech = max(int(float(settings.get('max_speech_duration_s2', 2)) * 1000),500)
        kwargs = {
            "prompt": settings.get(
                f'initial_prompt_{self.detect_language}') if self.detect_language != 'auto' else None,
            "detect_language": self.detect_language,
            "model_name": self.model_name,
            "logs_file": logs_file,
            "is_cuda": self.is_cuda,
            "no_speech_threshold": float(settings.get('no_speech_threshold', 0.6)),
            "condition_on_previous_text": settings.get('condition_on_previous_text', False),
            "speech_timestamps": self.speech_timestamps_file,
            "audio_file": self.audio_file,
            "jianfan": self.jianfan,
            
            "audio_duration":self.audio_duration,
            "temperature":settings.get('temperature'),
            "compression_ratio_threshold":float(settings.get('compression_ratio_threshold',2.4)),
            "max_speech_ms":_max_speech
        }
        raws=self._new_process(callback=openai_whisper,title=title,is_cuda=self.is_cuda,kwargs=kwargs)
        return raws


    def _mlx(self)->Union[List[SrtItem], None]:
        from videotrans.process.mlx_stt import mlx_whisper_fun
        title = f"STT use mlx-whisper {self.model_name}"
        self.signal(text=title)
        logs_file = f'{config.TEMP_DIR}/{self.uuid}/mlx-{self.detect_language}-{time.time()}.log'
        _max_speech = max(int(float(settings.get('max_speech_duration_s', 5)) * 1000), 2000)
        if self.recogn2pass:
            _max_speech = max(int(float(settings.get('max_speech_duration_s2', 2)) * 1000), 500)
        kwargs = {
            "detect_language": self.detect_language,
            "model_dir": self.mlx_local_dir,
            "logs_file": logs_file,
            "no_speech_threshold": float(settings.get('no_speech_threshold', 0.6)),
            "condition_on_previous_text": settings.get('condition_on_previous_text', False),
            "speech_timestamps": self.speech_timestamps_file,
            "audio_file": self.audio_file,
            "jianfan": self.jianfan,
            "temperature": settings.get('temperature'),
            "prompt": settings.get(f'initial_prompt_{self.detect_language}') if self.detect_language != 'auto' else None,
            "max_speech_ms": _max_speech,
        }
        return self._new_process(callback=mlx_whisper_fun, title=title, is_cuda=False, kwargs=kwargs)

    def _faster(self)->Union[List[SrtItem], None]:
        title=f"STT use {self.model_name}"
        self.signal(text=title)
        logs_file = f'{config.TEMP_DIR}/{self.uuid}/faster-{self.detect_language}-{time.time()}.log'
        _max_speech=max(int(float(settings.get('max_speech_duration_s', 5)) * 1000),2000)
        if self.recogn2pass:
            # 2次识别， 生成简短的字幕
            _max_speech = max(int(float(settings.get('max_speech_duration_s2', 2)) * 1000),500)
        
        
        subtitle_srt=f'{config.TEMP_ROOT}/faster-{datetime.datetime.now().strftime("%Y%m%d-%H_%M_%S")}.srt'
        kwargs = {
            "detect_language": self.detect_language,
            "model_name": self.model_name,
            "logs_file": logs_file,
            "is_cuda": self.is_cuda,
            "no_speech_threshold": float(settings.get('no_speech_threshold', 0.6)),
            "condition_on_previous_text": settings.get('condition_on_previous_text', False),
            "speech_timestamps": self.speech_timestamps_file,
            "audio_file": self.audio_file,
            "local_dir": self.local_dir,
            "compute_type": settings.get('cuda_com_type', 'int8'),
            "jianfan": self.jianfan,
            "audio_duration":self.audio_duration,
            "hotwords":settings.get('hotwords'),
            "prompt": settings.get(f'initial_prompt_{self.detect_language}') if self.detect_language != 'auto' else None,
            "beam_size": int(settings.get('beam_size', 5)),
            "best_of": int(settings.get('best_of', 5)),
            "temperature":settings.get('temperature'),
            "repetition_penalty":float(settings.get('repetition_penalty',1.0)),
            "compression_ratio_threshold":float(settings.get('compression_ratio_threshold',2.2)),
            "max_speech_ms":_max_speech,
            "subtitle_srt":subtitle_srt
        }
        try:
            raws=self._new_process(callback=faster_whisper,title=title,is_cuda=self.is_cuda,kwargs=kwargs)
            return raws
        except SttTimeoutError:
            logger.debug(f'捕获到强制抛出的 SttTimeoutError, 使用已识别的文件 {subtitle_srt}')
            return tools.get_subtitle_from_srt(subtitle_srt, is_file=True)
