import hashlib
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

PROMPT_VERSION = 'project-agent-v2-mcp'
PROMPT = (Path(__file__).parents[1] / 'prompts' / 'project_agent.md').read_text(encoding='utf-8')
ENGINE_VERSION = hashlib.sha256(Path(__file__).read_bytes() + (Path(__file__).parent/'tools.py').read_bytes()
                               + (Path(__file__).parent/'protocol.py').read_bytes() + PROMPT.encode() + b''.join(path.read_bytes() for path in sorted((Path(__file__).parents[1]/'mcp').glob('*.py')))).hexdigest()


class AgentState(TypedDict):
    messages: list[dict]
    model_calls: int
    tool_calls: int
    successful_tools: int
    native_ids: list[str]
    answer: str


def build_agent_graph(model_step, tools_step):
    graph = StateGraph(AgentState)
    graph.add_node('model', model_step)
    graph.add_node('tools', tools_step)
    graph.add_edge(START, 'model')
    graph.add_conditional_edges('model', lambda state: 'tools' if state['messages'][-1].get('tool_calls') else 'end',
                                {'tools': 'tools', 'end': END})
    graph.add_edge('tools', 'model')
    return graph.compile()
