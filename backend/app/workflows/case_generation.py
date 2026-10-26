"""最小 LangGraph：普通字典状态；模型调用器由运行时闭包注入，不进入状态。"""

import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypedDict

from fastapi import HTTPException
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field

from ..schemas import CaseFields, FeatureItem

PROMPT_VERSION = "case-writer-v1"
PROMPT = (Path(__file__).parents[1] / "prompts" / "case_writer.md").read_text(encoding="utf-8")


class CaseState(TypedDict, total=False):
    requirement: dict
    feature: dict
    strategy: str
    source: str
    messages: list[dict]
    raw_reply: str
    cases: list[dict]
    trace: list[str]


class ParsedCases(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cases: list[CaseFields] = Field(min_length=1, max_length=8)


def mock_reply(state: CaseState) -> str:
    types = ["functional"] if state["strategy"] == "quick" else ["functional", "boundary", "exception"]
    return json.dumps({"cases": [{
        "title": f"Mock · {state['feature']['feature'][:120]} · {kind}",
        "priority": "P0" if kind == "functional" else "P1", "case_type": kind,
        "is_smoke": kind == "functional", "precondition": "Mock 示例，需人工根据需求核对",
        "steps": [f"根据需求准备 {kind} 场景的输入", "执行该功能的操作"],
        "expected_result": "Mock 示例：依据原文验收标准核对结果，具体规则待人工补充",
    } for kind in types]}, ensure_ascii=False)


def build_case_graph(call_model: Callable[[list[dict], int], Awaitable[str]]):
    def prepare(state: CaseState):
        feature = FeatureItem.model_validate(state["feature"])
        if not feature.included or state["strategy"] not in {"full", "quick"}:
            raise HTTPException(status_code=409, detail="请选择已确认且纳入范围的功能点。")
        return {"messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": json.dumps({
                "requirement": state["requirement"], "feature": state["feature"], "strategy": state["strategy"],
            }, ensure_ascii=False)},
        ], "trace": ["prepare"]}

    async def generate(state: CaseState):
        reply = mock_reply(state) if state["source"] == "mock" else await call_model(state["messages"], 4096)
        return {"raw_reply": reply, "trace": [*state["trace"], "generate"]}

    def validate(state: CaseState):
        raw = state["raw_reply"].strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL | re.IGNORECASE)
        if fenced:
            raw = fenced.group(1)
        try:
            if len(raw) > 200000:
                raise ValueError("response too large")
            cases = ParsedCases.model_validate_json(raw).cases
            titles = {"".join(case.title.split()).casefold() for case in cases}
            if len(titles) != len(cases):
                raise ValueError("duplicate titles")
            if state["strategy"] == "quick":
                if len(cases) > 3 or any(case.case_type != "functional" or not case.is_smoke for case in cases):
                    raise ValueError("invalid smoke cases")
            elif (len(cases) < 3 or {case.case_type for case in cases} != {"functional", "boundary", "exception"}
                  or not any(case.is_smoke and case.case_type == "functional" for case in cases)):
                raise ValueError("missing coverage")
        except ValueError:
            raise HTTPException(status_code=502, detail="用例输出结构或策略校验失败，请重试。已有草稿未被修改。") from None
        return {"cases": [case.model_dump() for case in cases], "trace": [*state["trace"], "validate"]}

    builder = StateGraph(CaseState)
    builder.add_node("prepare", prepare)
    builder.add_node("generate", generate)
    builder.add_node("validate", validate)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "generate")
    builder.add_edge("generate", "validate")
    builder.add_edge("validate", END)
    # 本模块先学习图执行；后续再增加持久化 checkpointer、暂停和恢复。
    return builder.compile()
