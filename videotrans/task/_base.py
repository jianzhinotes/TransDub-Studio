import copy
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Union
from videotrans.configure.config import tr, app_cfg, logger, ROOT_DIR
from videotrans.configure.base import BaseCon
from videotrans.task.taskcfg import TaskCfgBase, SrtItem

@dataclass
class BaseTask(BaseCon):
    # 各项配置信息，例如 翻译、配音、识别渠道等
    cfg: TaskCfgBase = field(default_factory=TaskCfgBase, repr=False)
    # 进度记录
    precent: int = 1
    # 需要配音的原始字幕信息 List[dict]
    queue_tts: List = field(default_factory=list, repr=False)
    # 是否已结束
    hasend: bool = False
    # 是否需要语音识别
    should_recogn: bool = False
    # 是否需要字幕翻译
    should_trans: bool = False
    # 是否需要配音
    should_dubbing: bool = False
    # 是否需要人声分离
    should_separate: bool = False
    # 是否需要嵌入配音或字幕
    should_hebing: bool = False

    def __post_init__(self):
        super().__post_init__()
        if self.cfg.uuid:
            self.uuid = self.cfg.uuid

    # 预先处理，例如从视频中拆分音频、人声背景分离、转码等
    def prepare(self):
        pass

    # 语音识别创建原始语言字幕
    def recogn(self):
        pass

    # 说话人识别，Funasr/豆包语音识别大模型 /Deepgram 除外，再判断是否已有说话人，Gemini/openai gpt4-dia 会生成说话人
    def diariz(self):
        pass

    # 将原始语言字幕翻译到目标语言字幕
    def trans(self):
        pass

    # 根据 queue_tts 进行配音
    def dubbing(self):
        pass

    # 配音加速、视频慢速对齐
    def align(self):
        pass

    # 视频、音频、字幕合并生成结果文件
    def assembling(self):
        pass

    # 删除临时文件，移动或复制，发送成功消息
    def task_done(self):
        pass

    # 删掉尺寸为0的无效文件
    def _unlink_size0(self, file: Union[str, List[str]]):
        if not file: return
        files = [file] if isinstance(file, str) else file
        for f in files:
            p = Path(f)
            if p.exists() and p.stat().st_size == 0:
                p.unlink(missing_ok=True)

    # 保存字幕文件 到目标文件夹
    def _save_srt_target(self, srtstr: List[SrtItem], file: str):
        from videotrans.util.help_srt import get_srt_from_list
        try:
            txt = get_srt_from_list(srtstr)
            with open(file, "w", encoding="utf-8", errors="ignore") as f:
                f.write(txt)
        except Exception as e:
            from videotrans.configure.excepts import VideoTransError
            raise VideoTransError(f'保存字幕前格式化srt失败:{file=}') from e

        self.signal(text=Path(file).read_text(encoding='utf-8', errors="ignore"), type='replace_subtitle')
        return True

    # 如果启用了 LLM重新断句，则跳过该步骤，LLM断句后时间轴发生变更，无法和原始字幕对齐
    def check_target_sub(self, source_srt_list: List[SrtItem], target_srt_list: List[SrtItem]) -> List[SrtItem]:
        source_len = len(source_srt_list)
        target_len = len(target_srt_list)
        if source_len == target_len:
            # An SRT-capable LLM is allowed to translate text, never to own the
            # canonical source timeline.  Providers occasionally return one
            # malformed timestamp (for example 00:16:29 -> 00:00:29) while all
            # texts and the other timestamps remain correctly ordered.  Bind
            # the translated text back onto immutable source rows by position
            # after checking that the response is substantially the same SRT.
            exact = sum(
                int(source.get('start_time', 0) or 0) == int(target.get('start_time', 0) or 0)
                and int(source.get('end_time', 0) or 0) == int(target.get('end_time', 0) or 0)
                for source, target in zip(source_srt_list, target_srt_list)
            )
            minimum = 0 if source_len <= 3 else max(int(source_len * 0.8), 1)
            if exact < minimum:
                from videotrans.configure.excepts import DubbingSrtError
                raise DubbingSrtError(
                    f'翻译结果时间轴可信度过低：仅 {exact}/{source_len} 段与原文一致，'
                    '已停止以防止字幕顺序错位。')
            aligned = copy.deepcopy(source_srt_list)
            for index, target in enumerate(target_srt_list):
                aligned[index]['text'] = str(target.get('text') or '').strip()
            corrected = source_len - exact
            if corrected:
                logger.warning(
                    '翻译返回 %s 个异常时间戳，已保留译文并恢复原始时间轴', corrected)
            else:
                logger.debug(f'原始语言字幕和目标语言字幕行数一致，均为 {source_len=}')
            return aligned

        # Never try to repair a count mismatch by matching timestamps.  If an
        # LLM dropped one short block, later text may retain plausible-looking
        # timestamps while being semantically shifted by one or more rows.
        # The translator layer now retries malformed AI batches at a smaller
        # size; reaching here means the provider result is still unsafe.
        from videotrans.configure.excepts import DubbingSrtError
        raise DubbingSrtError(
            f'翻译结果无法与原文对齐：原文 {source_len} 段，译文 {target_len} 段。'
            '已停止以防止后续字幕和配音整体错位。')

    # 手动调用设为结束，成功完成或出错时
    def set_end(self, succeed=False):
        self.hasend = True
        if succeed:
            self.precent = 100
            if self.uuid in app_cfg.stoped_uuid_set:
                return
            self.signal(text=f"{self.cfg.name}", type='succeed')
            if app_cfg.exec_mode=="cli":
                print(f'Save to:[ {self.cfg.target_dir} ]')
            else:
                from videotrans.util.help_ffmpeg import send_notification
                send_notification(tr('Succeed'), f"{self.cfg.basename}")
            # 清理临时文件
            try:
                if self.cfg.cache_folder:
                    shutil.rmtree(self.cfg.cache_folder, ignore_errors=True)
            except Exception as e:
                logger.exception(f'任务结束后清理临时文件失败，跳过,{e}:{self.cfg.cache_folder=}', exc_info=True)
        app_cfg.stoped_uuid_set.add(self.uuid)

    async def _edgetts_single(self, target_audio, kwargs):
        from edge_tts import Communicate
        from io import BytesIO
        from videotrans.configure.excepts import DubbingSrtError

        useproxy_initial = None if not self.proxy_str or Path(
            f'{ROOT_DIR}/edgetts-noproxy.txt').exists() else self.proxy_str
        proxies_to_try = [useproxy_initial]
        if useproxy_initial is not None:
            proxies_to_try.append(None)

        for proxy in proxies_to_try:
            try:
                audio_buffer = BytesIO()
                communicate_task = Communicate(
                    text=kwargs['text'],
                    voice=kwargs['voice'],
                    rate=kwargs['rate'],
                    volume=kwargs['volume'],
                    proxy=proxy,
                    pitch=kwargs['pitch']
                )
                idx = 0
                async for chunk in communicate_task.stream():
                    if chunk["type"] == "audio":
                        audio_buffer.write(chunk["data"])
                        self.signal(text=f'{idx} segment')
                        idx += 1
                audio_buffer.seek(0)
                from pydub import AudioSegment
                au = AudioSegment.from_file(audio_buffer, format="mp3")
                au.export(target_audio, format='mp3')
                return
            except Exception as e:
                raise DubbingSrtError(f'edge-tts error:{target_audio=}') from e
        raise DubbingSrtError(f'Dubbing error')
