"""项目内 BM25 检索。词法检索，不调用模型；不保留业务语料索引缓存。"""

import hashlib
import json
import logging
import math
import re
import unicodedata
from dataclasses import dataclass

import jieba
from rank_bm25 import BM25Okapi
from sqlalchemy import select

from ..models import KnowledgeChunk, KnowledgeDocument

ALGORITHM = "bm25-okapi-positive-idf-v1"
TOKENIZER = "nfkc-casefold-jieba-0.42.1-hmm-off-v1"
K1, B = 1.5, 0.75
jieba.setLogLevel(logging.WARNING)
_tokenizer = jieba.Tokenizer()
# 英数标识保持完整，包括 E1001、x-request-id、/api/login 一类输入中的标识段。
_PARTS = re.compile(r"[a-z0-9]+(?:[_.:/-][a-z0-9]+)*|[\u3400-\u4dbf\u4e00-\u9fff]+")


def tokenize(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens = []
    for match in _PARTS.finditer(normalized):
        value = match.group()
        if value[0].isascii():
            tokens.append(value)
        else:
            tokens.extend(word for word in _tokenizer.cut(value, HMM=False) if word.strip())
    return tokens


class PositiveBM25(BM25Okapi):
    def _calc_idf(self, frequencies):
        # log(1 + ...) 保持小语料中的匹配词正分，不采用库默认的负/零 IDF 下限。
        self.idf = {term: math.log1p((self.corpus_size - count + 0.5) / (count + 0.5))
                    for term, count in frequencies.items()}


@dataclass(frozen=True)
class SearchChunk:
    chunk_id: str
    document_id: str
    document_title: str
    source_filename: str
    document_revision: int
    position: int
    start_offset: int
    end_offset: int
    content: str
    content_hash: str


def read_chunks(session, project_id):
    rows = session.execute(select(KnowledgeChunk, KnowledgeDocument.id, KnowledgeDocument.title,
                           KnowledgeDocument.source_filename, KnowledgeDocument.revision)
                           .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
                           .where(KnowledgeDocument.project_id == project_id,
                                  KnowledgeChunk.document_revision == KnowledgeDocument.revision)
                           .order_by(KnowledgeDocument.id, KnowledgeChunk.position, KnowledgeChunk.id)).all()
    return [SearchChunk(chunk.id, doc_id, title, filename, revision, chunk.position,
                        chunk.start_offset, chunk.end_offset, chunk.content, chunk.content_hash)
            for chunk, doc_id, title, filename, revision in rows]


def corpus_signature(chunks):
    identity = sorted((chunk.chunk_id, chunk.document_revision, chunk.content_hash) for chunk in chunks)
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def current_signature(session, project_id):
    rows = session.execute(select(KnowledgeChunk.id, KnowledgeDocument.revision, KnowledgeChunk.content_hash)
                           .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
                           .where(KnowledgeDocument.project_id == project_id,
                                  KnowledgeChunk.document_revision == KnowledgeDocument.revision)).all()
    identity = sorted(tuple(row) for row in rows)
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def rank_chunks(chunks, query_tokens):
    indexed = [(chunk, tokenize(chunk.content)) for chunk in chunks]
    indexed = [(chunk, tokens) for chunk, tokens in indexed if tokens]
    if not indexed:
        return [], 0
    index = PositiveBM25([tokens for _, tokens in indexed], k1=K1, b=B)
    scores = index.get_scores(query_tokens)
    matches = []
    for (chunk, tokens), raw_score in zip(indexed, scores):
        score = float(raw_score)
        vocabulary = set(tokens)
        matched = [token for token in query_tokens if token in vocabulary]
        if math.isfinite(score) and score > 0 and matched:
            matches.append((chunk, score, matched))
    matches.sort(key=lambda item: (-item[1], item[0].document_id, item[0].position, item[0].chunk_id))
    return matches, len(indexed)
