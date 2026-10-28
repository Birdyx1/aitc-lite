"""等权 Reciprocal Rank Fusion。只使用召回名次，不相加原始分数。"""

RRF_K = 60
ALGORITHM = "rrf-equal-weight-k60-v1"


def fuse_rankings(rankings, k=RRF_K):
    """输入为 {路名: [chunk_id, ...]}。同一路重复 ID 只记首次，不留名次空洞。"""
    scores, ranks = {}, {}
    for source, ids in rankings.items():
        for rank, chunk_id in enumerate(dict.fromkeys(ids), 1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1 / (k + rank)
            ranks.setdefault(chunk_id, {})[source] = rank
    # 相同融合分数按稳定分块 ID 排序，不由异步完成顺序决定。
    return [(id, scores[id], ranks[id]) for id in sorted(scores, key=lambda id: (-scores[id], id))]
