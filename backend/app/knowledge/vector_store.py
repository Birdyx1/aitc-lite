"""Chroma 只接收显式向量，禁止默认模型下载；正文与来源仍以 SQLite 为准。"""

from pathlib import Path
from threading import Lock


class VectorStore:
    def __init__(self, path):
        self.path = str(path)
        self._client = None
        self._lock = Lock()

    def client(self):
        with self._lock:
            if self._client is None:
                import chromadb
                from chromadb.config import Settings
                self._client = chromadb.PersistentClient(
                    path=self.path, settings=Settings(anonymized_telemetry=False))
            return self._client

    def add(self, name, ids, vectors, first=False):
        client = self.client()
        collection = (client.create_collection(name, embedding_function=None,
                      configuration={"hnsw": {"space": "cosine"}}) if first
                      else client.get_collection(name, embedding_function=None))
        collection.add(ids=ids, embeddings=vectors)

    def query(self, name, vector, count):
        collection = self.client().get_collection(name, embedding_function=None)
        result = collection.query(query_embeddings=[vector], n_results=count, include=["distances"])
        return list(zip(result["ids"][0], result["distances"][0]))

    def cleanup(self, keep):
        # 尚未使用向量库时不初始化；仅清理由本模块拥有的 collection。
        if self._client is None and not Path(self.path).exists():
            return
        client = self.client()
        for collection in client.list_collections():
            if collection.name.startswith("aitc-v13-") and collection.name not in keep:
                client.delete_collection(collection.name)
