"""有上限、可复核原文偏移的字符分块；无需模型或外部分词服务。"""

import hashlib
from uuid import NAMESPACE_URL, uuid5

VERSION = "boundary-chars-v1"
CHUNK_SIZE = 800
CHUNK_OVERLAP = 120


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split_document(document_id: str, revision: int, content: str) -> list[dict]:
    chunks = []
    start = 0
    while start < len(content):
        end = min(start + CHUNK_SIZE, len(content))
        if end < len(content):
            # 在后 40% 范围内优先选择段落，再选择换行、句末和空格；长连续文本硬切。
            lower = start + int(CHUNK_SIZE * 0.6)
            for separators in (("\r\n\r\n", "\n\n"), ("\r\n", "\n", "\r"),
                               ("。", "！", "？", "；", ". ", "! ", "? "), (" ", "\t")):
                boundaries = [index + len(separator) for separator in separators
                              if (index := content.rfind(separator, lower, end)) >= 0]
                if boundaries:
                    end = max(boundaries)
                    break
        text = content[start:end]
        if text.strip():
            position = len(chunks) + 1
            fingerprint = text_hash(text)
            identity = f"aitc-lite:knowledge:{document_id}:{revision}:{VERSION}:{position}:{start}:{end}:{fingerprint}"
            chunks.append({"id": str(uuid5(NAMESPACE_URL, identity)), "document_id": document_id,
                           "document_revision": revision, "position": position,
                           "start_offset": start, "end_offset": end, "content": text, "content_hash": fingerprint})
        if end == len(content):
            break
        start = end - CHUNK_OVERLAP
    return chunks
