"""长期记忆的检索侧：BM25 + 向量语义搜索，按 7:3 权重混合排序。

对应笔记 5 节里对 OpenClaw 长期记忆的描述：

    在检索侧，系统会对这些文件建立混合搜索索引，同时使用向量语义搜索和
    BM25 关键词搜索，按 7:3 权重做混合排序。

为什么必须混合？两种检索的强项正好互补：

    BM25（关键词）   擅长精确术语。查询里出现"CA1234"这种罕见串，
                     只有 BM25 能一击命中。但它对同义、改写完全无感。
    向量（语义）     擅长模糊匹配。"退票"能匹配到"退款"，"改期"能匹配到"改签"，
                     但它对罕见专有名词反而不敏感。

所以单独用任何一个都会漏。这个文件把两者都实现出来，让差距在输出里看得见。

实现说明：真实系统里向量来自 embedding 模型。这里没有外部依赖，用一个
**字符特征 + IDF 加权的显式词表**向量空间模型代替 —— 它不是神经网络 embedding，
但对"部分重叠 / 词序打乱 / 同字不同词"这类情况的表现，
足以演示"语义检索和关键词检索行为不同"这件事。这个取舍在注释里标清楚，
免得读者以为这就是生产做法。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# 分词：不做中文分词，用「英文词 + 中文 bigram」的经典无词典做法
# --------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-zA-Z0-9_]+|[一-鿿]")


def tokenize(text: str) -> list[str]:
    """英文/数字按词切，中文按相邻两字切（bigram）。

    中文 bigram 不需要词典就能覆盖大部分查询，是检索里常用的兜底方案。
    """
    text = text.lower()
    units = _WORD_RE.findall(text)
    tokens: list[str] = []
    cjk_run: list[str] = []

    def flush() -> None:
        if not cjk_run:
            return
        if len(cjk_run) == 1:
            tokens.append(cjk_run[0])
        else:
            tokens.extend(cjk_run[i] + cjk_run[i + 1] for i in range(len(cjk_run) - 1))
        cjk_run.clear()

    for unit in units:
        if len(unit) == 1 and "一" <= unit <= "鿿":
            cjk_run.append(unit)
        else:
            flush()
            tokens.append(unit)
    flush()
    return tokens


# --------------------------------------------------------------------------
# BM25
# --------------------------------------------------------------------------


@dataclass
class Document:
    doc_id: str
    text: str
    source: str = ""
    tokens: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.tokens:
            self.tokens = tokenize(self.text)


class BM25Retriever:
    """关键词检索。对罕见词敏感，对同义改写无感。"""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.documents: list[Document] = []
        self._df: Counter[str] = Counter()
        self._avg_len: float = 0.0

    def index(self, documents: list[Document]) -> None:
        self.documents = list(documents)
        self._df = Counter()
        for doc in self.documents:
            for term in set(doc.tokens):
                self._df[term] += 1
        total_len = sum(len(doc.tokens) for doc in self.documents)
        self._avg_len = total_len / len(self.documents) if self.documents else 0.0

    def _idf(self, term: str) -> float:
        n = self._df.get(term, 0)
        if n == 0:
            return 0.0
        total = len(self.documents)
        return math.log(1 + (total - n + 0.5) / (n + 0.5))

    def search(self, query: str) -> dict[str, float]:
        if not self.documents:
            return {}
        query_terms = tokenize(query)
        scores: dict[str, float] = {}
        for doc in self.documents:
            freqs = Counter(doc.tokens)
            length = len(doc.tokens) or 1
            score = 0.0
            for term in query_terms:
                tf = freqs.get(term, 0)
                if tf == 0:
                    continue
                idf = self._idf(term)
                denom = tf + self.k1 * (1 - self.b + self.b * length / (self._avg_len or 1))
                score += idf * tf * (self.k1 + 1) / denom
            if score > 0:
                scores[doc.doc_id] = score
        return scores


# --------------------------------------------------------------------------
# 向量检索（bigram 哈希 + 余弦）
# --------------------------------------------------------------------------

class VectorRetriever:
    """语义向量的替身：字符特征 + IDF 加权的词袋向量，再做余弦相似度。

    **这是替身，不是真正的 embedding。** 生产系统里这一路是 embedding 模型产出的
    稠密向量。能力边界要说清楚：

      能做到：部分重叠也算相关。查询"改期怎么操作"和文档"改签规则"共享一个"改"字，
              向量能捞到它；BM25 一个词都对不上，直接零分。
      做不到：真正的同义理解。完全不含共同字符的同义词（"座位" ↔ "席别"）它抓不到，
              而真 embedding 可以 —— 所以真实系统的混合检索效果比这里更好。

    和 BM25 的关键差别在于**匹配粒度**：BM25 走 bigram，要求连续两字完全一致；
    向量额外吃单字，容忍部分重叠。粒度不同，漏检的地方就不同 ——
    这才是混合检索有意义的前提。

    实现上用**显式词表**而不是哈希分桶：语料是固定的，建一次词表就没有碰撞，
    每个维度对应哪个 token 都能在调试器里直接看到。哈希分桶是给
    "词表未知且巨大"的流式场景用的，用在这里只会平白引入噪声。
    （顺带说：这也正是这套实现和真 embedding 最大的差距 —— 词表里没有的词，
      它给不出任何相似度，而 embedding 能。）
    """

    def __init__(self, dim: int | None = None):
        self.dim = dim  # 显式词表下维度由语料决定，这里只作为参考信息
        self.documents: list[Document] = []
        self.vocabulary: dict[str, int] = {}
        self._vectors: dict[str, dict[int, float]] = {}
        self._norms: dict[str, float] = {}
        self._idf: dict[str, float] = {}
        self._default_idf: float = 1.0

    # -- 特征 -------------------------------------------------------------

    @staticmethod
    def _features(text: str) -> Counter[str]:
        """bigram/英文词权重 2，单字权重 1。

        单字是"模糊匹配"的来源，但它也最容易撞车（"的""了""用"到处都有），
        所以下面还要乘 IDF 把它压下去。
        """
        features: Counter[str] = Counter()
        for token in tokenize(text):
            features[token] += 2.0
        for ch in text.lower():
            if "一" <= ch <= "鿿":
                features[ch] += 1.0
        return features

    def _vectorize(self, features: Counter[str]) -> tuple[dict[int, float], float]:
        """特征 -> 稀疏向量（下标 -> 权重），并返回它的模长。

        IDF 是必需的：没有它，任何两个中文句子都会因为共享"的/是/在"算出
        一大坨虚假相似度，检索结果就没法看了。
        """
        vector: dict[int, float] = {}
        for token, weight in features.items():
            index = self.vocabulary.get(token)
            if index is None:
                continue  # 词表外的词，给不出相似度（真 embedding 在这里能做得更好）
            vector[index] = vector.get(index, 0.0) + weight * self._idf.get(token, self._default_idf)
        norm = math.sqrt(sum(v * v for v in vector.values()))
        return vector, norm

    # -- 索引与检索 --------------------------------------------------------

    def index(self, documents: list[Document]) -> None:
        self.documents = list(documents)
        feature_sets = [self._features(doc.text) for doc in self.documents]

        # 建词表 + 算 IDF
        df: Counter[str] = Counter()
        for features in feature_sets:
            for token in features:
                df[token] += 1
        self.vocabulary = {token: i for i, token in enumerate(sorted(df))}
        self.dim = len(self.vocabulary)
        total = len(feature_sets) or 1
        self._idf = {token: math.log(1 + total / (1 + count)) for token, count in df.items()}
        self._default_idf = math.log(1 + total)

        self._vectors = {}
        self._norms = {}
        for doc, features in zip(self.documents, feature_sets):
            vector, norm = self._vectorize(features)
            self._vectors[doc.doc_id] = vector
            self._norms[doc.doc_id] = norm

    def search(self, query: str) -> dict[str, float]:
        if not self.documents:
            return {}
        q_vector, q_norm = self._vectorize(self._features(query))
        if q_norm == 0:
            return {}

        scores: dict[str, float] = {}
        for doc_id, vector in self._vectors.items():
            norm = self._norms[doc_id]
            if norm == 0:
                continue
            dot = sum(weight * vector.get(index, 0.0) for index, weight in q_vector.items())
            if dot > 0:
                scores[doc_id] = dot / (q_norm * norm)
        return scores


# --------------------------------------------------------------------------
# 混合检索：7:3
# --------------------------------------------------------------------------


@dataclass
class SearchHit:
    doc_id: str
    text: str
    source: str
    hybrid: float
    bm25: float
    vector: float
    # 归一化**之前**的原始分。
    # 留着它们是因为归一化会抹掉一些东西（见 _normalize），
    # 调试检索效果时你得能看见被抹掉的到底是什么。
    bm25_raw: float = 0.0
    vector_raw: float = 0.0

    def render(self) -> str:
        return f"[{self.source}] {self.text}"


class HybridRetriever:
    """向量 0.7 + BM25 0.3。

    两路分数量纲不同（BM25 是无上界的累加值，余弦在 [0,1]），
    所以先各自归一化，再按权重**平均** —— 不做归一化直接加权，权重就失去意义了。
    """

    def __init__(self, vector_weight: float = 0.7, bm25_weight: float = 0.3):
        self.vector_weight = vector_weight
        self.bm25_weight = bm25_weight
        self.bm25 = BM25Retriever()
        self.vector = VectorRetriever()
        self.documents: list[Document] = []

    def index(self, documents: list[Document]) -> None:
        self.documents = list(documents)
        self.bm25.index(documents)
        self.vector.index(documents)

    def search(self, query: str, top_k: int = 3) -> list[SearchHit]:
        bm25_scores = self.bm25.search(query)
        vector_scores = self.vector.search(query)
        bm25_norm = _normalize(bm25_scores)
        vector_norm = _normalize(vector_scores)

        # ★ 权重只在**这一路真的有结果**之间分配（加权平均），不是死的 0.7/0.3。
        #
        # 以前是直接写 `0.7 * v + 0.3 * b`。某一路零命中时，那个 0.3 不是
        # "贡献 0"，而是**从公式里整体消失**：另一路兜回来的文档，
        # 天花板只剩 0.7；而且同一个文档的分数会随"另一路这次有没有命中"跳变，
        # 跨查询根本没法比。
        #
        # 理论说"两路互补、一路漏了另一路兜底"。代码里的兜底却是**带折扣的**，
        # 折扣大小还随查询变 —— 这就把"兜底"悄悄变成了"降级"。
        # 除以 active 之后，"另一路没结果"就只是"这一路独自决定排序"，
        # 而不是"分数被打七折"。
        active = 0.0
        if vector_norm:
            active += self.vector_weight
        if bm25_norm:
            active += self.bm25_weight

        by_id = {doc.doc_id: doc for doc in self.documents}
        hits: list[SearchHit] = []
        for doc_id in set(bm25_norm) | set(vector_norm):
            b = bm25_norm.get(doc_id, 0.0)
            v = vector_norm.get(doc_id, 0.0)
            doc = by_id[doc_id]
            weighted = self.vector_weight * v + self.bm25_weight * b
            hits.append(
                SearchHit(
                    doc_id=doc_id,
                    text=doc.text,
                    source=doc.source,
                    hybrid=weighted / active if active else 0.0,
                    bm25=b,
                    vector=v,
                    bm25_raw=bm25_scores.get(doc_id, 0.0),
                    vector_raw=vector_scores.get(doc_id, 0.0),
                )
            )
        hits.sort(key=lambda hit: hit.hybrid, reverse=True)
        return hits[:top_k]


def _normalize(scores: dict[str, float]) -> dict[str, float]:
    """把一路的分数压到 [0,1]：除以这一路的最高分。

    这么做的代价要说清楚：**每路的第一名恒为 1.0**。
    BM25 的「只命中 1 条、分数很高」和向量的「命中 4 条、全是 0.01 量级噪声」，
    归一化之后顶部都是 1.0 —— 两路的**绝对量级差异被抹平了**，只剩相对比例。

    这恰恰是不能让两路共用一个分母的原因：BM25 是无上界的累加值、
    余弦在 [0,1]，量纲根本不同，共用一个分母会让 BM25 单方面压倒 0.3 的权重。

    但代价也要看得见 —— 所以 SearchHit 里同时留了 bm25_raw / vector_raw，
    调试时能对照出被抹掉的到底是什么。
    """
    if not scores:
        return {}
    top = max(scores.values())
    if top <= 0:
        return {}
    return {key: value / top for key, value in scores.items()}
