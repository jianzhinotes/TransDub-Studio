import copy
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Union

from tenacity import RetryError

from videotrans import translator
from videotrans.configure.base import BaseCon
from videotrans.configure.config import tr, settings, logger, TEMP_ROOT
from videotrans.task.taskcfg import SrtItem
from videotrans.util.help_srt import get_subtitle_from_srt,cleartext
from videotrans.util.help_misc import get_md5,serial

@dataclass
class BaseTrans(BaseCon):
    # 翻译渠道
    translate_type: int = 0
    # 存放待翻译的字幕列表字典
    text_list: List[SrtItem] = None
    # 唯一任务id
    uuid: Optional[str] = None
    # 测试时不使用缓存
    is_test: bool = False
    # 原始语言代码
    source_code: str = ""
    # 目标语言代码
    target_code: str = ""
    # 对于AI渠道，这是目标语言的自然语言表达，其他渠道等于 target_code
    target_language_name: str = ""
    cache_dir: Optional[str] = None

    # 翻译API 地址
    api_url: str = field(default="", init=False)
    # 模型名
    model_name: str = field(default="", init=False)
    # 同时翻译的字幕行数量
    trans_thread: int = 5
    # 翻译后暂停秒
    wait_sec: float = float(settings.get('translation_wait', 0))


    #  是AI翻译渠道并且选中了以完整srt格式字幕发送
    aisendsrt: bool = False

    def __post_init__(self):
        super().__post_init__()
        self.cache_dir = str(self.cache_dir or (Path(TEMP_ROOT) / 'translate_cache'))
        Path(self.cache_dir).mkdir(parents=True, exist_ok=True)
        self.aisendsrt = settings.get('aisendsrt', False) and self.translate_type in translator.AI_TRANS_CHANNELS
        if self.aisendsrt:
            self.trans_thread = int(settings.get('aitrans_thread', 20)) if not settings.get('aitrans_context') else len(self.text_list)
        else:
            self.trans_thread = int(settings.get('trans_thread', 5))

    def _item_task(self, data: Union[List[str], str]):
        raise NotImplementedError

    # 实际操作 run  -> run_text|run_srt -> _item_task
    def run(self) -> List[SrtItem]:
        if hasattr(self, '_download'):
            self._download()
        try:
            if not self.aisendsrt:
                # 是文字列表  [str,...]
                source_text = [t['text'].replace("\n", " ") for t in self.text_list]
                return self._run_text(
                    [source_text[i:i + self.trans_thread] for i in range(0, len(source_text), self.trans_thread)])
            # 是srt格式字幕列表 [SrtItem,...]
            return self._run_srt(
                    [self.text_list[i:i + self.trans_thread] for i in range(0, len(self.text_list), self.trans_thread)])
        except RetryError as e:
            raise e.last_attempt.exception()
        finally:
            if hasattr(self, '_unload'):
                self._unload()


    def _run_text(self, split_source_text: List[List[str]]):
        # 传统翻译渠道或AI翻译渠道以按行形式翻译
        """
        split_source_text=[
            ["字幕文本1","字幕文本2",...],
            ["字幕文本1","字幕文本2",...],
            ["字幕文本1","字幕文本2",...],
            ...
        ]
        """
        target_list = []
        logger.debug(f'以纯文本行形式翻译，每次翻译{self.trans_thread}行，翻译后暂停{self.wait_sec}s')
        for i, it in enumerate(split_source_text):
            """ it=['你好啊我的朋友','第二行']  此时 _item_task 接收的是 list[str] """
            if self._exit(): return
            self.signal(text=tr('starttrans') + f' {i} ')
            result = self._get_cache(it)
            if not result:
                result = cleartext(self._item_task(it))
                self._set_cache(it, result)
            sep_res = result.split("\n")
            for x, result_item in enumerate(sep_res):
                if x < len(it):
                    target_list.append(result_item.strip())
                    self.signal(text=result_item + "\n", type='subtitle')
            # 行数不匹配填充空行
            if len(sep_res) < len(it):
                logger.debug(f'行数不匹配，原始：{len(it)}, 结果：{len(sep_res)}\n{it=}\n{sep_res=}')
                tmp = ["" for x in range(len(it) - len(sep_res))]
                target_list += tmp
            time.sleep(self.wait_sec)
        max_i = len(target_list)
        logger.debug(f'原始行数:{len(self.text_list)},翻译后行数:{max_i}')
        _empty_line = 0
        for i, it in enumerate(self.text_list):
            text = target_list[i].strip() if i < max_i else ""
            if not text:
                _empty_line += 1
            self.text_list[i]['text'] = text

        if _empty_line >= len(self.text_list):
            from videotrans.configure.excepts import TranslateSrtError
            raise TranslateSrtError(tr("Translate result is empty")+f'\n{self.api_url}')
        return self.text_list

    # 发送完整字幕格式内容进行翻译
    # 此时 _item_task 接收的是 srt 格式的字符串
    def _run_srt(self, split_source_text: List[List[SrtItem]]):
        """
        split_source_text=[
            [{text:"",start_time:"",line:""},{...},...]
            ...
        ]
        """
        logger.debug(f'以SRT字幕块翻译，每次翻译 {self.trans_thread} 条字幕块，翻译后暂停{self.wait_sec}s')
        from videotrans.configure.excepts import TranslateSrtError
        raws_list = []
        for i, it in enumerate(split_source_text):
            if self._exit(): return
            self.signal(text=tr('starttrans') + f' {i} ')
            translated = self._translate_srt_batch(it)
            self.signal(text=self._srt_batch_text(translated), type='subtitle')
            raws_list.extend(translated)
            time.sleep(self.wait_sec)

        _empty_line = 0
        for it in raws_list:
            if not it['text'].strip():
                _empty_line += 1
        if _empty_line >= len(raws_list):
            raise TranslateSrtError(tr("Translate result is empty")+f'\n{self.api_url}')
        logger.debug(f'原始字幕行数：{len(self.text_list)}, 翻译后行数:{len(raws_list)}')
        return raws_list

    @staticmethod
    def _srt_batch_text(rows: List[SrtItem]) -> str:
        return "\n\n".join(
            f"{item['line']}\n{item['time']}\n{str(item.get('text') or '').strip()}"
            for item in rows)

    @staticmethod
    def _validate_srt_batch(source_rows: List[SrtItem], result: str):
        """Validate and bind an LLM response to the immutable source rows.

        A syntactically valid SRT can still be dangerous: models sometimes
        delete one short fragment and shift every later translation onto the
        preceding timestamp.  Accepting that response poisons both the cache
        and all downstream dubbing.  Structural and high-confidence semantic
        checks therefore happen before a batch is cached.
        """
        parsed = get_subtitle_from_srt(result, is_file=False)
        if len(parsed) != len(source_rows):
            return None, f'count {len(parsed)}/{len(source_rows)}'
        if any(not str(item.get('text') or '').strip() for item in parsed):
            return None, 'empty subtitle block'

        exact_timestamps = sum(
            str(source.get('time') or '') == str(target.get('time') or '')
            for source, target in zip(source_rows, parsed)
        )
        minimum = 0 if len(source_rows) == 1 else max(1, int(len(source_rows) * 0.8))
        if exact_timestamps < minimum:
            return None, f'timeline {exact_timestamps}/{len(source_rows)}'

        from videotrans.dub.semantic_guard import audit_translation_pair
        semantic_failures = []
        for index, (source, target) in enumerate(zip(source_rows, parsed)):
            failures = audit_translation_pair(source.get('text', ''), target.get('text', ''))
            if failures:
                semantic_failures.append(f"{index + 1}:{','.join(failures)}")
        if semantic_failures:
            return None, 'semantic ' + ';'.join(semantic_failures[:3])

        aligned = copy.deepcopy(source_rows)
        for source, target in zip(aligned, parsed):
            source['text'] = str(target.get('text') or '').strip()
        return aligned, ''

    def _translate_srt_batch(self, source_rows: List[SrtItem]) -> List[SrtItem]:
        """Translate one SRT batch, recursively shrinking malformed batches."""
        from videotrans.configure.excepts import TranslateSrtError

        srt_str = self._srt_batch_text(source_rows)
        cached = self._get_cache(srt_str)
        if not cached:
            # Older builds used the list as the cache key.  Read it once, but
            # only migrate it after the same validation as a fresh response.
            cached = self._get_cache(source_rows)
        if cached:
            aligned, reason = self._validate_srt_batch(source_rows, cached)
            if aligned is not None:
                self._set_cache(srt_str, self._srt_batch_text(aligned))
                return aligned
            logger.warning('拒绝无效翻译缓存（%s 行）: %s', len(source_rows), reason)

        result = self._item_task(srt_str)
        if not result or not result.strip():
            raise TranslateSrtError(tr("Translate result is empty") + f'\n{self.api_url}')
        aligned, reason = self._validate_srt_batch(source_rows, result)
        if aligned is not None:
            self._set_cache(srt_str, self._srt_batch_text(aligned))
            return aligned

        if len(source_rows) > 1:
            midpoint = len(source_rows) // 2
            logger.warning(
                '翻译批次未通过对齐检查（%s 行，%s），自动拆分为 %s+%s 行重译',
                len(source_rows), reason, midpoint, len(source_rows) - midpoint)
            aligned = (
                self._translate_srt_batch(source_rows[:midpoint])
                + self._translate_srt_batch(source_rows[midpoint:])
            )
            # Replace a poisoned parent cache with a validated, source-aligned
            # synthesis so later retries can resume without any API request.
            self._set_cache(srt_str, self._srt_batch_text(aligned))
            return aligned

        # A singleton cannot be split further.  Give transient model
        # formatting one clean retry before returning an actionable failure.
        retry_result = self._item_task(srt_str)
        aligned, retry_reason = self._validate_srt_batch(source_rows, retry_result or '')
        if aligned is not None:
            self._set_cache(srt_str, self._srt_batch_text(aligned))
            return aligned
        raise TranslateSrtError(
            f'第 {source_rows[0].get("line", "?")} 段翻译两次无法与原文对齐：'
            f'{reason}; {retry_reason}')

    def _set_cache(self, it, res_str):
        if not res_str.strip(): return
        cache_dir = Path(self.cache_dir or (Path(TEMP_ROOT) / 'translate_cache'))
        cache_dir.mkdir(parents=True, exist_ok=True)
        file_cache = cache_dir / f'{self._get_key(it)}.txt'
        Path(file_cache).write_text(res_str, encoding='utf-8')

    def _get_cache(self, it) -> Union[str,None]:
        if self.is_test: return
        cache_dir = Path(self.cache_dir or (Path(TEMP_ROOT) / 'translate_cache'))
        file_cache = cache_dir / f'{self._get_key(it)}.txt'
        if Path(file_cache).exists():
            logger.debug(f'本次跳过翻译，使用缓存')
            return Path(file_cache).read_text(encoding='utf-8')
        return

    def _get_key(self, it) -> str:
        it=serial(it)
        # AI prompt changes affect the translation result. Include the effective
        # prompt so an old cached subtitle cannot bypass a prompt correction.
        prompt = getattr(self, 'prompt', '')
        key_str = f'{self.translate_type}-{self.api_url}-{self.aisendsrt}-{self.model_name}-{self.source_code}-{self.target_code}-{prompt}-{it}'
        return get_md5(key_str)
