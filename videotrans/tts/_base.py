import asyncio
import copy
import inspect
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Any, Optional, Union, Tuple
from tenacity import RetryError
from videotrans.configure.base import BaseCon
from videotrans.configure.config import tr, settings, logger, ROOT_DIR
from videotrans.configure import config
from videotrans.util.help_misc import vail_file,pygameaudio,get_tts_type,get_md5

# 跨运行配音缓存：同一句话+同音色+同参数的合成结果直接复用，重跑不再全量重配。
# 键含参考音频内容指纹，参考变了自动失效。
DUBB_CACHE_DIR = f'{config.TEMP_ROOT}/dubb_cache'
_DUBB_CACHE_MAX_AGE_S = 30 * 86400
_dubb_cache_pruned = False

"""
edge-tts 当前线程中async异步任务
其他渠道多线程执行
run->exec->[local_mutli]->item_task
"""


@dataclass
class BaseTTS(BaseCon):
    # 配音渠道
    tts_type: int = 0
    # 存放字幕信息队列，扩展的SrtItem
    queue_tts: List[Dict[str, Any]] = field(default_factory=list, repr=False)
    # 参考音频或角色字典
    roledict: Dict[str, Any] = field(default_factory=dict, repr=False)
    # queue_tts 数量
    len: int = field(init=False)
    # 语言代码
    language: Optional[str] = None
    # 唯一uid
    uuid: Optional[str] = None
    # 是否立即播放
    play: bool = False
    # 是否测试
    is_test: bool = False

    # 音量 音速 音调，默认 edge-tts格式， % 号结尾
    volume: Union[float, str] = field(default='+0%', init=False)
    rate: Union[float, str] = field(default='+0%', init=False)
    pitch: Union[float, str] = field(default='+0Hz', init=False)

    # 是否完成
    has_done: int = field(default=0, init=False)

    # 每次任务后暂停时间
    wait_sec: float = float(settings.get('dubbing_wait', 0))
    # 并发线程数量
    dub_nums: int = int(float(settings.get('dubbing_thread', 1)))
    # 存放消息
    error: Union[str, Exception, None] = None
    # 配音api地址
    api_url: str = field(default='', init=False)
    # 启用CUDA，仅 qwen3-tts-local 游戏哦啊
    is_cuda: bool = False
    # 是否允许从跨运行配音缓存恢复（勾选"重新处理"时为 False；生成结果总是写入缓存）
    use_cache: bool = True

    def __post_init__(self):
        super().__post_init__()
        Path(f'{config.TEMP_DIR}/{self.uuid}').mkdir(parents=True, exist_ok=True)
        self.queue_tts = copy.deepcopy(self.queue_tts)
        self.len = len(self.queue_tts)
        self._cleantts()

    # ---- 跨运行配音缓存 ----
    @staticmethod
    def _file_sig(path) -> str:
        """音频文件内容指纹：前 1MB md5 + 大小，秒级且足够区分。"""
        try:
            p = Path(path)
            if not p.is_file():
                return ''
            import hashlib
            with open(p, 'rb') as f:
                head = f.read(1 << 20)
            return f'{hashlib.md5(head).hexdigest()}-{p.stat().st_size}'
        except Exception:
            return ''

    def _dubb_cache_key(self, item) -> Optional[str]:
        try:
            payload = '|'.join(str(x) for x in [
                self.tts_type, self.language, getattr(self, 'model_name', ''), self.api_url,
                item.get('text', ''), item.get('role', ''),
                item.get('rate', self.rate), item.get('volume', self.volume),
                item.get('pitch', self.pitch), item.get('ref_text', ''),
                self._file_sig(item.get('ref_wav', '')),
                self._file_sig(getattr(self, 'safe_ref_wav', '') or ''),
                # 多说话人模式下该行实际使用的簇参考；簇归属变化时缓存自动失效
                self._file_sig(item.get('cluster_ref', '')),
                # 渠道可通过该属性把额外的推理参数纳入键（如 F5 的 nfe/seed），
                # 避免参数调整后仍命中旧质量的缓存
                getattr(self, 'dubb_cache_extra', ''),
            ])
            return get_md5(payload)
        except Exception:
            return None

    def _dubb_cache_path(self, item) -> Optional[Path]:
        key = self._dubb_cache_key(item)
        if not key:
            return None
        suffix = Path(item.get('filename') or '').suffix or '.wav'
        return Path(DUBB_CACHE_DIR) / f'{key}{suffix}'

    def _dubb_cache_restore(self) -> None:
        """_exec 前调用：命中缓存的行直接落到目标 filename，各渠道的 vail_file 跳过逻辑随即生效。"""
        global _dubb_cache_pruned
        import shutil
        Path(DUBB_CACHE_DIR).mkdir(parents=True, exist_ok=True)
        if not _dubb_cache_pruned:
            _dubb_cache_pruned = True
            now = time.time()
            try:
                for f in Path(DUBB_CACHE_DIR).iterdir():
                    if f.is_file() and now - f.stat().st_mtime > _DUBB_CACHE_MAX_AGE_S:
                        f.unlink(missing_ok=True)
            except Exception:
                pass
        hits = 0
        for item in self.queue_tts:
            if not item.get('text', '').strip() or vail_file(item.get('filename')):
                continue
            cache = self._dubb_cache_path(item)
            if cache and vail_file(cache):
                try:
                    shutil.copy2(cache, item['filename'])
                    hits += 1
                except Exception as e:
                    logger.debug(f'配音缓存恢复失败,忽略: {e}')
        if hits:
            logger.debug(f'配音缓存命中 {hits}/{self.len} 条')
            self.signal(text=tr('Reused {} cached dubbing lines', hits))

    def _dubb_cache_store(self) -> None:
        """_exec 后调用：把本次成功生成且未被标记疑似泄漏的行写入缓存。"""
        import shutil
        try:
            Path(DUBB_CACHE_DIR).mkdir(parents=True, exist_ok=True)
            for item in self.queue_tts:
                if not item.get('text', '').strip() or not vail_file(item.get('filename')):
                    continue
                if item.get('lang_leak'):
                    continue
                cache = self._dubb_cache_path(item)
                if cache and not cache.exists():
                    shutil.copy2(item['filename'], cache)
        except Exception as e:
            logger.debug(f'配音缓存写入失败,忽略: {e}')

    # 子类未重写 _exec()方法: run() ->_exec() ->__local_mul_thread() -> _item_task() -> _run()
    # 子类重写  _exec()方法 run() -> _exec()
    def run(self) -> None:
        if self._exit(): return
        from videotrans.configure.excepts import DubbingSrtError
        _tts_name=get_tts_type(self.tts_type)
        logger.debug(f'当前使用配音渠道：{_tts_name}')
        self.signal(text=f"{_tts_name} starting: [len={self.len}]")
        if hasattr(self, '_download'):
            self.signal(text=f"Check and downloading models...")
            self._download()
        if not self.play and not self.is_test and self.use_cache and settings.get('dubb_cache', True):
            self._dubb_cache_restore()
        loop = None
        try:
            # edge-tts:检查 self._exec 是不是一个异步函数 (coroutine)
            if inspect.iscoroutinefunction(self._exec):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(self._exec())
                else:
                    loop.run_until_complete(self._exec())
            else:
                # 可能调用多线程
                self._exec()
        except RetryError as e:
            raise e.last_attempt.exception()
        except RuntimeError as e:
            logger.warning(f'TTS 线程运行时发生错误: {e}')
            if 'Event loop' in str(e):
                logger.warning("捕获到 'Event loop is closed' 错误，这通常是关闭时序问题。")
            else:
                raise
        finally:
            # edge-tts:只有当 self._exec 是异步函数时，才需要处理事件循环
            if inspect.iscoroutinefunction(self._exec) and loop and not loop.is_closed():
                try:
                    # 1: 取消所有剩余的任务
                    tasks = asyncio.all_tasks(loop=loop)
                    for task in tasks:
                        task.cancel()

                    # 2: 聚合所有任务，等待它们完成取消
                    group = asyncio.gather(*tasks, return_exceptions=True)
                    loop.run_until_complete(group)
                    import gc
                    gc.collect()
                    loop.run_until_complete(asyncio.sleep(0))
                    # 3: 关闭异步生成器
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception as e:
                    logger.exception(f'结束 edge-tts 时失败，忽略 {e}', exc_info=True)
                finally:
                    # 4: 最终关闭事件循环
                    loop.close()

        # 试听或测试时播放
        if self.play:
            if vail_file(self.queue_tts[0]['filename']):
                return pygameaudio(self.queue_tts[0]['filename'])

            logger.error(f'试听配音时发生错误{self.error}')
            if isinstance(self.error, RetryError):
                raise self.error.last_attempt.exception()
            raise self.error if isinstance(self.error, Exception) else DubbingSrtError(str(self.error))

        # 记录成功数量
        succeed_nums = 0
        for it in self.queue_tts:
            if not it['text'].strip() or vail_file(it['filename']):
                succeed_nums += 1
        # 只有全部配音都失败，才视为失败
        if succeed_nums < 1:
            if self._exit(): return
            logger.error(f'本次配音全部失败：{self.error}')
            if isinstance(self.error, Exception):
                raise self.error.last_attempt.exception() if isinstance(self.error, RetryError) else self.error

            raise DubbingSrtError(tr("Dubbing failed") + str(self.error))
        # 成功的行写入跨运行缓存（部分成功也写，方便失败重试时命中）
        if not self.play and not self.is_test and settings.get('dubb_cache', True):
            self._dubb_cache_store()
        logger.debug(f'本次 {_tts_name} 配音成功 {succeed_nums} 个，失败 {self.len - succeed_nums} 个')
        self.signal(text=tr("Dubbing succeeded {}，failed {}", succeed_nums, self.len - succeed_nums))
        if self.tts_type == 8 and succeed_nums != self.len:
            raise DubbingSrtError(
                f"F5-TTS 配音不完整：成功 {succeed_nums} 段，失败 {self.len - succeed_nums} 段。"
                "已停止合成，避免生成夹杂英文或静音的成品。"
            )

    # 若子类未重写  _exec(), 则默认调用该方法
    # 此方法内判断返回的错误是否 StopTask 类型，若是则直接终止任务
    def _local_mul_thread(self) -> None:
        if self._exit(): return
        from videotrans.configure.excepts import StopTask
        # 单个字幕行，无需多线程
        if len(self.queue_tts) == 1 or self.dub_nums == 1:
            logger.debug(f'设定最大配音线程: {self.dub_nums},实际 单线程配音, 待配音字幕长度: {self.len}, 配音后暂停{self.wait_sec}s')
            for k, item in enumerate(self.queue_tts):
                if self._exit(): return
                if not item.get('text').strip() or vail_file(item['filename']):
                    continue
                # 只记录最后一个错误
                error = self._item_task(item, k)
                self.error = error
                if error and isinstance(error, StopTask):
                    # 发送终止信号，终止时会将 uuid 加入 app_cfg.stop_uid
                    raise error

                self.signal(text=f'TTS[{k + 1}/{self.len}]')
                time.sleep(self.wait_sec)
            self.signal(text=f'TTS ended')
            return

        all_task = []
        _wok=max(min(self.dub_nums, len(self.queue_tts)),2)
        pool = ThreadPoolExecutor(max_workers=_wok)
        logger.debug(f'设定配音最大线程数: {self.dub_nums},实际 {_wok} 线程配音, 待配音字幕长度: {self.len}')
        try:
            completed_tasks = 0
            for k, item in enumerate(self.queue_tts):
                if self._exit(): return
                if not item.get('text').strip() or vail_file(item['filename']):
                    completed_tasks += 1
                    continue
                future = pool.submit(self._item_task, item, k)
                all_task.append(future)

            if all_task:
                for task in as_completed(all_task):
                    if self._exit(): return
                    # 只记录最后一个错误
                    error = task.result()
                    self.error = error
                    if error and isinstance(error, StopTask):
                        # 发送终止信号，终止时会将 uuid 加入 app_cfg.stop_uid
                        raise error
                    completed_tasks += 1
                    self.signal(text=f"TTS: [{completed_tasks}/{self.len}] ...")
            self.signal(text=f"TTS ended ...")
        finally:
            # 只能取消排队的任务，并让主线程不再等待。
            pool.shutdown(wait=False)

    # run() 调用此逻辑，子类可覆写此逻辑，实现全部 queue_tts 配音
    # 若不覆写，则进入多线程，挨个调用 _item_task() 一条条字幕配音，子类=必须实现 _run() 方法
    def _exec(self) -> None:
        self._local_mul_thread()

    # 每条字幕任务，由 _local_mul_thread 方法在多个线程中调用
    # data_item 是 queue_tts 中每个元素
    # 子类若没有覆写 _exec() 方法，则必须实现 _run() 方法
    # return 返回为None为成功，失败返回错误消息 或 抛出异常
    def _item_task(self, data_item: Union[Dict, List, None], idx: int = -1) -> Union[str, Exception, None]:
        if self._exit() or not data_item.get('text', '').strip() or vail_file(data_item.get('filename')):
            return
        # 有些不可恢复的错误，例如 404 sk错误 无权访问等，直接发送 error 信号，无需继续多线程
        try:
            self.signal(text=f'Dubbing {idx}/{self.len}')
            return self._run(data_item,idx)
        except RetryError as e:
            logger.exception(f'\n第{idx}条字幕配音失败,字幕文本:{data_item}\n{e}', exc_info=True)
            return e.last_attempt.exception()
        except Exception as e:
            logger.exception(f'\n第{idx}条字幕配音失败,字幕文本:{data_item}\n{e}', exc_info=True)
            return e

    # 子类未重写 _exec 方法时，则必须实现该方法
    def _run(self, data_item: Union[Dict, List, None], idx: int = -1) -> Union[str, None]:
        raise NotImplementedError

    # 文本规范化和清理音量等参数
    def _cleantts(self) -> None:
        normalizer = None
        if settings.get('normal_text'):
            if self.language[:2] == 'zh':
                from videotrans.util.cn_tn import TextNorm
                normalizer = TextNorm(to_banjiao=True)
            elif self.language[:2] == 'en':
                from videotrans.util.en_tn import EnglishNormalizer
                normalizer = EnglishNormalizer()

        for i, it in enumerate(self.queue_tts):
            if it['text'].strip() and normalizer:
                try:
                    self.queue_tts[i]['text'] = normalizer(it['text'])
                except Exception as e:
                    logger.exception(f'文本规范化失败，忽略:{it["text"]=},{e}', exc_info=True)

        volume = self.queue_tts[0].get('volume', '+0%')
        volume = f'+{volume}' if re.match(r'^\d+(\.\d+)?%$', volume) else volume
        self.volume = '+0%' if not re.match(r'^[+-]\d+(\.\d+)?%$', volume) else volume

        rate = self.queue_tts[0].get('rate', '+0%')
        rate = f'+{rate}' if re.match(r'^\d+(\.\d+)?%$', rate) else rate
        self.rate = '+0%' if not re.match(r'^[+-]\d+(\.\d+)?%$', rate) else rate

        pitch = self.queue_tts[0].get('pitch', '+0Hz').replace('hz', 'Hz')
        pitch = f'+{pitch}' if re.match(r'^\d+(\.\d+)?Hz$', pitch, re.I) else pitch
        self.pitch = '+0Hz' if not re.match(r'^[+-]\d+(\.\d+)?Hz$', pitch, flags=re.I) else pitch

        logger.debug(f'{self.volume=}, {self.rate=}, {self.pitch=}')

    # 将 百分比音量改为 小数形式
    def get_speed(self) -> float:
        speed = 1.0
        try:
            speed = round(1 + float(self.rate.replace('%', '')) / 100, 1)
        except (TypeError,ValueError):
            pass
        return speed

    def get_volume(self) -> float:
        volume = 1.0
        try:
            volume = round(1 + float(self.volume.replace('%', '')) / 100, 1)
        except (TypeError,ValueError):
            pass
        return volume

    def get_pitch(self) -> float:
        pitch = 1.0
        try:
            pitch = round(float(re.sub(r'[hz%]', '', self.pitch,flags=re.I)), 1)
        except (TypeError,ValueError):
            pass
        return pitch

    # 返回参考音频和参考文本
    def get_ref_wav(self, item) -> Tuple[str, str]:
        role = item['role']
        ref_wav, ref_text = None, None
        if role == 'clone':
            ref_wav = item.get('ref_wav', '')
            ref_text = item.get('ref_text').strip()
        elif role in self.roledict:
            if not isinstance(self.roledict[role],dict):
                return ref_wav,ref_text
            ref_text = self.roledict[role]['ref_text']
            ref_wav = ROOT_DIR + f"/f5-tts/{role}"

        if not ref_wav or not Path(ref_wav).exists():
            raise RuntimeError(tr('The role {} does not exist', role))
        return ref_wav, ref_text
