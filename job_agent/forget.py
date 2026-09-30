"""清除记忆：按范围忘掉 agent 记住的东西。

为什么要单独一个模块，而不是塞进 store.py
-----------------------------------------
「清空」要动的东西跨了两个数据库：
  * `job_memory.sqlite3`  —— 岗位 + 时间线 + 长期偏好（store.py 管）
  * `conversation.sqlite3` —— 对话检查点（LangGraph 管的，store.py 碰不到）
所以这里做一个统一入口，让「工具」和「斜杠命令」都走同一条路，
删的范围、备份的时机、提示的文案只有一份，不会两边对不上。

三条原则
--------
1. **先备份再删**：范围内的记忆先导成一份 JSON 落到 `data/backup/`。
   删错了还能人工翻回来——清空记忆是不可逆的，备份是唯一的安全网。
2. **必须显式确认**：本模块只负责干活，确认逻辑放在调用方（工具要 `confirm=True`，
   命令行要敲 yes）。这样自测可以直接调它，不需要模拟交互。
3. **分范围**：岗位 / 偏好 / 对话 / 全部。别只给一个"全删"——用户多数时候
   只是想清掉对话记录重新聊，不想丢掉投递履历。
"""

from __future__ import annotations

# ---- 入口自举：直接跑本文件时补上包路径（被 import 时不会触发）----
if __package__ in (None, ""):
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    __package__ = "job_agent"

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 范围定义
SCOPES: dict[str, str] = {
    "jobs": "岗位记忆（岗位 + 时间线）",
    "profile": "长期偏好",
    "conversations": "对话记忆（聊天记录）",
    "all": "全部",
}

# 用户可能的说法 -> 标准范围
_ALIASES: dict[str, str] = {
    "job": "jobs", "岗位": "jobs", "工作": "jobs", "职位": "jobs",
    "岗位记忆": "jobs", "履历": "jobs", "jobs": "jobs",
    "profile": "profile", "偏好": "profile", "长期偏好": "profile", "要求": "profile",
    "conversation": "conversations", "conversations": "conversations", "对话": "conversations",
    "对话记忆": "conversations", "聊天": "conversations", "聊天记录": "conversations",
    "all": "all", "全部": "all", "所有": "all", "全部记忆": "all", "everything": "all",
}

SCOPE_HINT = "、".join(f"{k}={v}" for k, v in SCOPES.items())


def normalize_scope(text: str) -> str:
    """把用户/模型给的说法归一成标准范围，认不出来返回空字符串。"""
    text = (text or "").strip().lower()
    if text in SCOPES:
        return text
    return _ALIASES.get(text, "")


def describe(scope: str) -> str:
    return SCOPES.get(scope, scope)


# ---------------------------------------------------------------- 备份
def backup_dir() -> Path:
    """备份放哪：和记忆库同一个 data 目录下的 backup/（已 gitignore）。"""
    from .config import load_settings

    return load_settings().data_dir / "backup"


def backup(scope: str = "all") -> Path | None:
    """把指定范围的记忆导成一份 JSON，返回文件路径；没东西可导返回 None。"""
    from .config import load_settings
    from .store import get_store

    settings = load_settings()
    store = get_store()
    payload: dict[str, Any] = {
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scope": scope,
        "scope_desc": describe(scope),
    }

    if scope in ("jobs", "all"):
        jobs = store.list_jobs(limit=100000, order="first_seen")
        for job in jobs:
            job["timeline"] = store.timeline(job["id"], limit=1000)
        payload["jobs"] = jobs
    if scope in ("profile", "all"):
        payload["profile"] = store.all_profile()
    if scope in ("conversations", "all"):
        # 复用「记忆总览」那套解析，导出来的是能直接读的对话，不是二进制块
        try:
            from .memory_view import read_conversations

            payload["conversations"] = read_conversations(
                settings.checkpoint_path, max_threads=100, max_messages=1000
            )
        except Exception as exc:  # 备份失败不能挡住删除（下面 delete 会照常走）
            payload["conversations_error"] = f"{type(exc).__name__}: {exc}"
            payload["conversations"] = []

    if not any(payload.get(k) for k in ("jobs", "profile", "conversations")):
        return None

    target_dir = backup_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"memory_backup_{datetime.now():%Y%m%d_%H%M%S}.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


# ---------------------------------------------------------------- 对话记忆
def count_conversations(path: str | Path) -> int:
    """对话检查点库有多少个 thread（不是消息数，thread 才是"一段对话"）。"""
    target = Path(path)
    if not target.is_file():
        return 0
    try:
        conn = sqlite3.connect(str(target))
        try:
            rows = conn.execute(
                "SELECT COUNT(DISTINCT thread_id) AS c FROM checkpoints"
            ).fetchone()
            return int(rows[0]) if rows else 0
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def clear_conversations(path: str | Path) -> dict[str, int]:
    """清空对话检查点库。只删数据、不删表，下次启动照常能存新对话。"""
    target = Path(path)
    counts: dict[str, int] = {}
    if not target.is_file():
        return counts
    conn = sqlite3.connect(str(target))
    try:
        # 先删 writes 再删 checkpoints —— 万一以后加了外键，顺序也不会出错
        for table in ("writes", "checkpoints"):
            try:
                counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                conn.execute(f"DELETE FROM {table}")
            except sqlite3.Error:
                continue
        conn.commit()
    finally:
        conn.close()
    return counts


# ---------------------------------------------------------------- 主入口
@dataclass
class ForgetResult:
    """一次清除的结果，给调用方拼提示语用。"""

    scope: str
    deleted: dict[str, int] = field(default_factory=dict)
    backup_path: Path | None = None
    backup_error: str = ""

    @property
    def total(self) -> int:
        return sum(self.deleted.values())

    def summary_lines(self) -> list[str]:
        lines = [f"已清空：{describe(self.scope)}"]
        names = {
            "jobs": "岗位", "events": "操作记录", "profile": "长期偏好",
            "checkpoints": "对话检查点", "writes": "对话中间状态",
        }
        for key, n in self.deleted.items():
            lines.append(f"  - {names.get(key, key)}：{n} 条")
        if not self.deleted:
            lines.append("  （本来就是空的，没删任何东西）")
        if self.backup_path:
            lines.append(f"  备份：{self.backup_path}")
            lines.append("  反悔的话，这份 JSON 里存着删掉的全部内容。")
        elif self.backup_error:
            lines.append(f"  备份失败（{self.backup_error}），已照常执行删除。")
        else:
            lines.append("  没有需要备份的内容。")
        return lines


def forget(scope: str = "jobs", *, backup_first: bool = True) -> ForgetResult:
    """按范围清除记忆。

    Args:
        scope: jobs / profile / conversations / all
        backup_first: 是否先备份（默认要，除非你明确知道自己在干什么）
    """
    from .config import load_settings
    from .store import get_store

    scope = normalize_scope(scope) or "jobs"
    result = ForgetResult(scope=scope)

    if backup_first:
        try:
            result.backup_path = backup(scope)
        except Exception as exc:
            result.backup_error = f"{type(exc).__name__}: {exc}"

    store = get_store()
    if scope in ("jobs", "profile", "all"):
        result.deleted.update(store.clear(scope))
        if scope in ("jobs", "all"):
            store.vacuum()

    if scope in ("conversations", "all"):
        settings = load_settings()
        result.deleted.update(clear_conversations(settings.checkpoint_path))

    return result


if __name__ == "__main__":  # 想直接清就从命令行跑： python -m job_agent.forget jobs --yes
    import sys

    from ._bootstrap import bootstrap

    bootstrap(__file__)

    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    key = normalize_scope(arg)
    if not key:
        print(f"用法：python -m job_agent.forget <范围>\n  可选范围：{SCOPE_HINT}")
        raise SystemExit(1)
    if "--yes" not in sys.argv:
        print(f"将要清空：{describe(key)}")
        print("确认请重新执行并加上 --yes，例如：python -m job_agent.forget jobs --yes")
        raise SystemExit(1)
    print("\n".join(forget(key).summary_lines()))
