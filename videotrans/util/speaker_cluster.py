"""轻量声纹聚类：把若干音频片段按说话人分成两簇（完全离线，无模型下载）。

用于 F5-TTS 自动选克隆参考音频时排除"另一个说话人"（访谈里的主持人等）。
特征用 MFCC 均值+方差，KMeans(k=2) 聚类；silhouette 过低视为单说话人，返回 None
表示"分不出/不必分"，调用方按原逻辑处理。准确率不及专用声纹模型（ECAPA 等），
但对双人对话的"主讲人 vs 其他人"区分足够，且零依赖增量、CPU 秒级。
"""
from typing import Dict, List, Optional

from videotrans.configure.config import logger

# 每片段最多分析的秒数；再长对声纹均值没有增益，白费计算
_MAX_ANALYZE_S = 12.0
# 低于该 silhouette 视为聚类不可靠（大概率单说话人）
_MIN_SILHOUETTE = 0.12
# 参与聚类的片段数上限（均匀抽样），控制启动耗时
_MAX_CLIPS = 60


def cluster_speakers(wav_paths: List[str], sr: int = 16000) -> Optional[Dict[int, int]]:
    """按说话人把 wav_paths 聚成两簇。

    返回 {原列表下标: 簇号0/1}；不可靠或样本太少返回 None。
    单个文件读取/特征失败会被跳过，不影响整体。
    """
    import numpy as np
    import librosa
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score
    from sklearn.preprocessing import StandardScaler

    picked = list(range(len(wav_paths)))
    if len(picked) > _MAX_CLIPS:
        step = len(picked) / _MAX_CLIPS
        picked = [int(i * step) for i in range(_MAX_CLIPS)]

    feats, idx_ok = [], []
    for i in picked:
        try:
            y, _ = librosa.load(wav_paths[i], sr=sr, mono=True, duration=_MAX_ANALYZE_S)
            y, _ = librosa.effects.trim(y, top_db=30)
            if len(y) < sr:  # 有效人声不足 1s，声纹不稳
                continue
            m = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=20)[1:]  # 去能量维,降通道音量影响
            feats.append(np.concatenate([m.mean(axis=1), m.std(axis=1)]))
            idx_ok.append(i)
        except Exception as e:
            logger.debug(f'声纹特征提取失败,跳过 {wav_paths[i]}: {e}')
    if len(idx_ok) < 6:
        return None

    X = StandardScaler().fit_transform(np.stack(feats))
    km = KMeans(n_clusters=2, n_init=10, random_state=0).fit(X)
    score = silhouette_score(X, km.labels_)
    logger.debug(f'声纹聚类: {len(idx_ok)} 片段, silhouette={score:.3f}')
    if score < _MIN_SILHOUETTE:
        return None
    return {i: int(label) for i, label in zip(idx_ok, km.labels_)}
