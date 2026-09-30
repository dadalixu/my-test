"""LangGraph 主循环：agent 节点和 tools 节点来回转。

图长这样：

        START
          |
          v
      +--------+   有工具调用    +--------+
      | agent  | -------------> | tools  |
      +--------+                +--------+
          |                         |
          | 没有工具调用             | 结果回灌
          v                         |
         END  <---------------------+
                                    （tools -> agent，形成循环）

三层记忆一起用：
  1. 对话记忆  —— checkpointer 按 thread_id 保存消息（跨重启有效，SQLite）。
  2. 岗位记忆  —— store.py 里的 SQLite，由工具读写（真正的「找过的工作」）。
  3. 偏好记忆  —— profile 表，每次组装 system prompt 时注入，保证「记得你说过什么」。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

try:  # 不同版本位置略有差异，做个兼容
    from langgraph.graph import MessagesState
except ImportError:  # pragma: no cover
    from langgraph.graph.message import MessagesState

from langgraph.prebuilt import ToolNode, tools_condition

from .config import Settings, load_settings
from .store import get_store
from .tools import ALL_TOOLS

# 每轮最多带多少条历史消息给模型（防止对话越长越贵）
MAX_HISTORY_MESSAGES = 24
DEFAULT_RECURSION_LIMIT = 25

SYSTEM_PROMPT = """你是「求职小助手」，帮助谢力（土木工程·道桥方向）找工作的私人助理。

【你的记忆 —— 这一点最重要】
- 所有搜到的岗位会自动写入本地 SQLite 记忆库，每条都有唯一的 job_id。
- 你可以随时用工具把记忆查回来：search_saved_jobs / list_jobs / get_job_detail / job_stats。
- 用户的长期偏好（目标城市、薪资底线、不考虑的公司等）存在 remember / recall_all 里。
- 所以当用户问「我之前看过哪些」「上次那个岗位怎么样了」，你必须先查记忆再回答，
  绝对不要凭印象编造岗位、公司或 job_id。
- 用户要清空记忆时用 clear_memory，但**必须分两步**：先不带 confirm 调一次拿到清单，
  原样念给用户看，等他说「确认」再带 confirm=True 调。
  自己替用户点确认 = 把人家几个月的投递记录删了，这是最严重的错误。

【标准流程】
1. 用户想找某类工作 -> 调 search_jobs（结果会自动入库），再从结果里挑 3~5 个最贴合的推荐，
   并说明匹配理由（结合用户的专业背景和偏好）。
2. 用户决定投递 / 已投递 -> 调 update_job_status(job_id, "applied", note=...)，备注里写时间和关键信息。
3. 用户提到新的偏好 -> 调 remember 存下来，不要只答应不记录。
4. 涉及具体岗位时，job_id 必须来自工具返回结果，不能自己编号。

【状态取值】new（新发现）interested（感兴趣）applied（已投递）interview（面试中）
offer（已录用）rejected（已拒/被拒）archived（已归档）

【回答风格】
- 全程中文；先给结论，再给理由；能用列表就用列表。
- 一次最多推荐 5 个岗位，避免信息过载。
- 不要客套话，不要「好问题」这类填充语。"""

CHECKPOINTER_KIND = "memory"  # 由 make_checkpointer 覆盖


def _clip_history(messages: list[BaseMessage]) -> list[BaseMessage]:
    """只保留最近的若干条消息。

    注意：不能从一条孤儿 ToolMessage 或带 tool_calls 的 AIMessage 开头，
    否则部分模型接口会报错。
    """
    if len(messages) <= MAX_HISTORY_MESSAGES:
        return list(messages)
    clipped = list(messages[-MAX_HISTORY_MESSAGES:])
    while clipped and isinstance(clipped[0], ToolMessage):
        clipped.pop(0)
    while clipped and isinstance(clipped[0], AIMessage) and getattr(clipped[0], "tool_calls", None):
        clipped.pop(0)
    return clipped


def build_system_message() -> SystemMessage:
    """把「记忆快照」拼进 system prompt，让模型一眼看到现状。"""
    try:
        store = get_store()
        stats = store.stats()
        if stats["total"]:
            counts = "，".join(
                f"{k}:{v}" for k, v in stats["by_status"].items() if v
            )
            memory_line = f"当前记忆库共 {stats['total']} 个岗位（{counts}），最近 7 天新增 {stats['new_last_7_days']} 个。"
        else:
            memory_line = "记忆库目前是空的，这是一个新的开始。"
        profile = store.all_profile()
        profile_line = (
            "；".join(f"{k}={v}" for k, v in profile.items()) if profile else "（还没记录偏好）"
        )
    except Exception as exc:  # 数据库出错也不能让对话挂掉
        memory_line, profile_line = f"（记忆库读取异常：{exc}）", "（未知）"

    extra = f"\n\n【当前记忆快照】\n{memory_line}\n\n【用户长期偏好】\n{profile_line}"
    return SystemMessage(content=SYSTEM_PROMPT + extra)


def build_llm(settings: Settings | None = None) -> BaseChatModel:
    """按配置造模型（OpenAI 兼容接口，默认 DeepSeek）。"""
    settings = settings or load_settings()
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=settings.model,
        api_key=settings.api_key,
        base_url=settings.base_url,
        temperature=settings.temperature,
        streaming=True,
        max_retries=2,
        timeout=60,
    )


# ---------------------------------------------------------------- 检查点
_CONNECTIONS: dict[str, sqlite3.Connection] = {}


def make_checkpointer(settings: Settings | None = None):
    """优先用 SQLite 保存对话（重启还在）；环境不支持时退回内存。"""
    global CHECKPOINTER_KIND
    settings = settings or load_settings()
    settings.ensure_dirs()
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver

        key = str(settings.checkpoint_path)
        if key not in _CONNECTIONS:  # 连接必须保持存活，否则数据会丢
            _CONNECTIONS[key] = sqlite3.connect(key, check_same_thread=False)
        CHECKPOINTER_KIND = "sqlite"
        return SqliteSaver(_CONNECTIONS[key])
    except Exception:
        from langgraph.checkpoint.memory import MemorySaver

        CHECKPOINTER_KIND = "memory"
        return MemorySaver()


# ---------------------------------------------------------------- 建图
def build_agent(settings: Settings | None = None, llm: BaseChatModel | None = None):
    """组装并编译 LangGraph 图，返回可调用的 app。"""
    settings = settings or load_settings()
    settings.ensure_dirs()
    model = llm or build_llm(settings)
    model_with_tools = model.bind_tools(ALL_TOOLS)

    def agent_node(state: MessagesState) -> dict[str, Any]:
        history = _clip_history(state["messages"])
        response = model_with_tools.invoke([build_system_message(), *history])
        return {"messages": [response]}

    builder = StateGraph(MessagesState)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", ToolNode(ALL_TOOLS))

    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    builder.add_edge("tools", "agent")  # 工具执行完回到 agent，形成循环

    return builder.compile(checkpointer=make_checkpointer(settings))


def new_config(thread_id: str = "default") -> dict[str, Any]:
    """构造 LangGraph 的运行时配置。thread_id 就是「一段对话」的编号。"""
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": DEFAULT_RECURSION_LIMIT,
    }


def history_of(app, thread_id: str = "default") -> list[BaseMessage]:
    """读回某段对话的历史消息（用于 CLI 展示剩余上下文）。"""
    try:
        snapshot = app.get_state(new_config(thread_id))
        return list(snapshot.values.get("messages", []))
    except Exception:
        return []
