"""轻量声纹聚类：把若干音频片段按说话人分成两簇（完全离线，无模型下载）。

用于 F5-TTS 自动选克隆参考音频时排除"另一个说话人"（访谈里的主持人等），
以及多说话人模式下给每一句配音归属说话人。
特征用 MFCC 均值+方差，KMeans(k=2) 聚类；silhouette 过低视为单说话人，返回 None
表示"分不出/不必分"，调用方按原逻辑处理。准确率不及专用声纹模型（ECAPA 等），
但对双人对话的"主讲人 vs 其他人"区分足够，且零依赖增量、CPU 秒级。
"""
from typing import Dict, List, Optional, Tuple

from videotrans.configure.config import logger

# 每片段最多分析的秒数；再长对声纹均值没有增益，白费计算
_MAX_ANALYZE_S = 12.0
# 低于该 silhouette 视为聚类不可靠（大概率单说话人）
_MIN_SILHOUETTE = 0.25
# 参与拟合的片段数上限（均匀抽样），控制耗时
_MAX_CLIPS = 60


def _extract_feats(wav_paths: List[str], indices: List[int], sr: int) -> Tuple[list, List[int]]:
    """按 indices 提取 MFCC 声纹特征；单个失败跳过。返回 (特征列表, 成功下标)。"""
    import numpy as np
    import librosa

    feats, idx_ok = [], []
    for i in indices:
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
    return feats, idx_ok


def cluster_speakers(wav_paths: List[str], sr: int = 16000) -> Optional[Dict[int, int]]:
    """按说话人把 wav_paths 聚成两簇（抽样拟合，只对抽到的片段返回标签）。

    返回 {原列表下标: 簇号0/1}；不可靠或样本太少返回 None。
    """
    import numpy as np
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score
    from sklearn.preprocessing import StandardScaler

    picked = list(range(len(wav_paths)))
    if len(picked) > _MAX_CLIPS:
        step = len(picked) / _MAX_CLIPS
        picked = [int(i * step) for i in range(_MAX_CLIPS)]

    feats, idx_ok = _extract_feats(wav_paths, picked, sr)
    if len(idx_ok) < 6:
        return None

    X = StandardScaler().fit_transform(np.stack(feats))
    km = KMeans(n_clusters=2, n_init=10, random_state=0).fit(X)
    score = silhouette_score(X, km.labels_)
    logger.debug(f'声纹聚类: {len(idx_ok)} 片段, silhouette={score:.3f}')
    if score < _MIN_SILHOUETTE:
        return None
    return {i: int(label) for i, label in zip(idx_ok, km.labels_)}


def label_speakers(wav_paths: List[str], sr: int = 16000) -> Optional[Dict[int, int]]:
    """给**每个**片段打说话人标签（多说话人逐句归属用）。

    与 cluster_speakers 同特征同模型：片段多时在均匀抽样上拟合 KMeans，
    再对全量特征预测。不可靠（单说话人/样本少）返回 None。
    """
    import numpy as np
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score
    from sklearn.preprocessing import StandardScaler

    feats, idx_ok = _extract_feats(wav_paths, list(range(len(wav_paths))), sr)
    if len(idx_ok) < 6:
        return None

    scaler = StandardScaler().fit(np.stack(feats))
    X_all = scaler.transform(np.stack(feats))
    if len(idx_ok) > _MAX_CLIPS:
        step = len(idx_ok) / _MAX_CLIPS
        fit_rows = [int(i * step) for i in range(_MAX_CLIPS)]
    else:
        fit_rows = list(range(len(idx_ok)))
    X_fit = X_all[fit_rows]
    km = KMeans(n_clusters=2, n_init=10, random_state=0).fit(X_fit)
    score = silhouette_score(X_fit, km.labels_)
    logger.debug(f'声纹逐句归属: {len(idx_ok)} 片段(拟合 {len(fit_rows)}), silhouette={score:.3f}')
    if score < _MIN_SILHOUETTE:
        return None
    labels = km.predict(X_all)
    return {i: int(label) for i, label in zip(idx_ok, labels)}
