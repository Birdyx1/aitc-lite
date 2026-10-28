"""当前三节点字典图的 SQLAlchemy JSON checkpointer；不加载 pickle 或任意对象。"""

import asyncio
import json

from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple, WRITES_IDX_MAP
from sqlalchemy import delete, select

from .models import GraphCheckpoint, GraphCheckpointWrite


class TaskPaused(Exception):
    pass


def plain(value):
    # 只允许本图使用的 JSON 数据。没有对象反序列化、pickle 或通用类型回退。
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def graph_config(task_id, namespace="", checkpoint_id=None):
    values = {"thread_id": task_id, "checkpoint_ns": namespace}
    if checkpoint_id:
        values["checkpoint_id"] = checkpoint_id
    return {"configurable": values}


class TaskCheckpointSaver(BaseCheckpointSaver):
    def __init__(self, session_factory):
        super().__init__()
        self.session_factory = session_factory

    def get_tuple(self, config):
        c = config["configurable"]
        task_id, ns = c["thread_id"], c.get("checkpoint_ns", "")
        with self.session_factory() as session:
            query = select(GraphCheckpoint).where(GraphCheckpoint.task_id == task_id, GraphCheckpoint.namespace == ns)
            if c.get("checkpoint_id"):
                query = query.where(GraphCheckpoint.checkpoint_id == c["checkpoint_id"])
            row = session.scalar(query.order_by(GraphCheckpoint.checkpoint_id.desc()).limit(1))
            if not row:
                return None
            writes = session.scalars(select(GraphCheckpointWrite).where(
                GraphCheckpointWrite.task_id == task_id, GraphCheckpointWrite.namespace == ns,
                GraphCheckpointWrite.checkpoint_id == row.checkpoint_id,
            ).order_by(GraphCheckpointWrite.task_path, GraphCheckpointWrite.node_task_id, GraphCheckpointWrite.index)).all()
            return CheckpointTuple(
                config=graph_config(task_id, ns, row.checkpoint_id), checkpoint=row.checkpoint,
                metadata=row.checkpoint_metadata,
                parent_config=graph_config(task_id, ns, row.parent_id) if row.parent_id else None,
                pending_writes=[(w.node_task_id, w.channel, w.value) for w in writes],
            )

    def put(self, config, checkpoint, metadata, new_versions):
        c = config["configurable"]
        task_id, ns = c["thread_id"], c.get("checkpoint_ns", "")
        with self.session_factory() as session:
            session.merge(GraphCheckpoint(
                task_id=task_id, namespace=ns, checkpoint_id=checkpoint["id"], parent_id=c.get("checkpoint_id"),
                checkpoint=plain(checkpoint), checkpoint_metadata=plain(metadata),
            ))
            session.commit()
        return graph_config(task_id, ns, checkpoint["id"])

    def put_writes(self, config, writes, task_id, task_path=""):
        c = config["configurable"]
        with self.session_factory() as session:
            for index, (channel, value) in enumerate(writes):
                index = WRITES_IDX_MAP.get(channel, index)
                key = (c["thread_id"], c.get("checkpoint_ns", ""), c["checkpoint_id"], task_id, index)
                old = session.get(GraphCheckpointWrite, key)
                if old and index >= 0:
                    continue
                # 异常只保留固定标记，避免异常对象/原始服务响应进入检查点。
                stored = "节点执行失败" if channel == "__error__" else plain(value)
                session.merge(GraphCheckpointWrite(
                    task_id=key[0], namespace=key[1], checkpoint_id=key[2], node_task_id=key[3], index=index,
                    channel=channel, value=stored, task_path=task_path,
                ))
            session.commit()

    def list(self, config, *, filter=None, before=None, limit=None):
        with self.session_factory() as session:
            query = select(GraphCheckpoint)
            if config:
                c = config["configurable"]
                query = query.where(GraphCheckpoint.task_id == c["thread_id"],
                                    GraphCheckpoint.namespace == c.get("checkpoint_ns", ""))
            if before:
                query = query.where(GraphCheckpoint.checkpoint_id < before["configurable"]["checkpoint_id"])
            rows = session.scalars(query.order_by(GraphCheckpoint.checkpoint_id.desc())).all()
            configs = [graph_config(row.task_id, row.namespace, row.checkpoint_id) for row in rows
                       if not filter or all(row.checkpoint_metadata.get(k) == v for k, v in filter.items())]
        for saved in configs[:limit] if limit is not None else configs:
            yield self.get_tuple(saved)

    def delete_thread(self, thread_id):
        with self.session_factory() as session:
            session.execute(delete(GraphCheckpointWrite).where(GraphCheckpointWrite.task_id == thread_id))
            session.execute(delete(GraphCheckpoint).where(GraphCheckpoint.task_id == thread_id))
            session.commit()

    async def aget_tuple(self, config):
        return await asyncio.to_thread(self.get_tuple, config)

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def alist(self, config, *, filter=None, before=None, limit=None):
        rows = await asyncio.to_thread(lambda: list(self.list(config, filter=filter, before=before, limit=limit)))
        for row in rows:
            yield row

    async def adelete_thread(self, thread_id):
        await asyncio.to_thread(self.delete_thread, thread_id)


def checkpoint_trace(session, task_id):
    saved = session.scalar(select(GraphCheckpoint).where(GraphCheckpoint.task_id == task_id,
                           GraphCheckpoint.namespace == "").order_by(GraphCheckpoint.checkpoint_id.desc()).limit(1))
    if not saved:
        return []
    trace = saved.checkpoint.get("channel_values", {}).get("trace", [])
    writes = session.scalars(select(GraphCheckpointWrite).where(
        GraphCheckpointWrite.task_id == task_id, GraphCheckpointWrite.namespace == "",
        GraphCheckpointWrite.checkpoint_id == saved.checkpoint_id,
    )).all()
    for write in writes:
        if write.channel == "trace" and isinstance(write.value, list) and len(write.value) > len(trace):
            trace = write.value
    return trace
