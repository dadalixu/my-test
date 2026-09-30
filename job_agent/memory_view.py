"""把 agent 的「记忆」渲染成人能直接看的页面。

之前记忆都躺在数据库文件里（`.sqlite3`），双击打不开、也没法直观地看。
这个模块负责把它读出来，生成一个自包含的 HTML —— 双击就能在浏览器里看。

看的内容分五块：
    ① 岗位记忆   它记住的每一个岗位（职位/公司/城市/薪资/状态/出现次数）
    ② 求职进展   各状态的数量分布
    ③ 操作时间线 什么时候看到的、什么时候投的
    ④ 长期偏好   你让它记住的偏好（比如 目标城市=武汉）
    ⑤ 对话记忆   它记得你们聊过什么

用法（三种都可以）::

    .venv\\Scripts\\python.exe -m job_agent.memory_view            # 生成网页并打开
    .venv\\Scripts\\python.exe job_agent\\memory_view.py           # 同上，直接跑文件
    .venv\\Scripts\\python.exe -m job_agent.memory_view --text    # 只在终端打印

也可以在对话里直接敲：/memory        （终端里看）
                      /memory html   （生成网页）
"""

from __future__ import annotations

# ---- 被直接执行时的自救（PEP 366）----
if __package__ in (None, ""):
    import sys as _sys
    from pathlib import Path as _Path

    _ROOT = _Path(__file__).resolve().parent.parent
    if str(_ROOT) not in _sys.path:
        _sys.path.insert(0, str(_ROOT))
    __package__ = "job_agent"

import html
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from ._bootstrap import bootstrap

bootstrap(__file__)

from .config import load_settings          # noqa: E402
from .store import STATUS_LABELS, STATUSES  # noqa: E402

try:  # 让中文在 Windows 控制台也正常显示
    if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ---------------------------------------------------------------- 常量
_STATUS_COLOR: dict[str, tuple[str, str]] = {
    # 状态 -> (文字色, 底色)
    "new": ("#6b7a8f", "#f4f5f7"),
    "interested": ("#1f6fb5", "#eef4fa"),
    "applied": ("#b4762a", "#fbf5ec"),
    "interview": ("#7d5aa6", "#f5f1fa"),
    "offer": ("#2f8a5b", "#eef8f2"),
    "rejected": ("#a94b45", "#fbf0ef"),
    "archived": ("#8a8f98", "#f4f5f6"),
}

_EVENT_KIND_LABEL = {
    "created": "首次记录",
    "seen": "再次遇到",
    "status": "状态变更",
    "note": "追加备注",
}

_ROLE_LABEL = {
    "HumanMessage": ("你", "user"),
    "AIMessage": ("助手", "assistant"),
    "ToolMessage": ("工具返回", "tool"),
    "SystemMessage": ("系统提示", "system"),
}

_DEFAULT_HTML = "记忆总览.html"


# ---------------------------------------------------------------- 读取记忆
def _text_of(message: Any) -> str:
    """把 LangChain 消息的 content 统一成纯文本。"""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for piece in content:
            if isinstance(piece, dict):
                parts.append(str(piece.get("text") or piece.get("content") or ""))
            else:
                parts.append(str(piece))
        return " ".join(p for p in parts if p).strip()
    return str(content).strip()


def read_conversations(path: str | Path, max_threads: int = 10,
                       max_messages: int = 60) -> list[dict[str, Any]]:
    """从 LangGraph 的对话检查点库里，把「它记得的对话」读出来。

    读不出来（库不存在 / LangGraph 版本差异）就返回空列表，不影响其它部分。
    """
    target = Path(path)
    if not target.is_file():
        return []
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except Exception:
        return []

    threads: list[str] = []
    sessions: list[dict[str, Any]] = []
    try:
        with SqliteSaver.from_conn_string(str(target)) as saver:
            for checkpoint in saver.list(None):
                tid = (checkpoint.config.get("configurable") or {}).get("thread_id")
                if tid and tid not in threads:
                    threads.append(tid)

            for tid in threads[:max_threads]:
                tup = saver.get_tuple({"configurable": {"thread_id": tid}})
                if tup is None:
                    continue
                values = (tup.checkpoint or {}).get("channel_values") or {}
                messages = values.get("messages") or []
                items: list[dict[str, Any]] = []
                for message in messages[-max_messages:]:
                    kind = type(message).__name__
                    label, role = _ROLE_LABEL.get(kind, (kind, "other"))
                    text = _text_of(message)
                    if not text:
                        if kind == "AIMessage":
                            text = "（这一轮它只去调了工具，没有写文字回复）"
                        else:
                            continue
                    if len(text) > 900:
                        text = text[:900] + "…"
                    items.append({"label": label, "role": role, "text": text})
                if items:
                    sessions.append({"thread_id": tid, "messages": items})
    except Exception:
        return sessions
    return sessions


def collect() -> dict[str, Any]:
    """把三层记忆全部读出来，攒成一个字典。"""
    from .store import get_store

    settings = load_settings()
    settings.ensure_dirs()

    # 用全局单例而不是自己开一个连接：selftest 里可能用 configure() 把库指到临时目录，
    # 这样写出来的东西才和对话里看到的一致。它是常驻连接，这里不要 close。
    store = get_store()
    jobs = store.list_jobs(limit=500, order="first_seen")
    for job in jobs:
        job["timeline"] = store.timeline(job["id"], limit=50)
    data: dict[str, Any] = {
        "jobs": jobs,
        "stats": store.stats(),
        "profile": store.all_profile(),
        "events": store.recent_events(limit=200),
    }

    data["conversations"] = read_conversations(settings.checkpoint_path)
    data["db_path"] = str(settings.db_path)
    data["ckpt_path"] = str(settings.checkpoint_path)
    data["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data["model"] = settings.model
    return data


# ---------------------------------------------------------------- 渲染：终端
def render_text(data: dict[str, Any] | None = None) -> str:
    """给命令行用的极简版（/memory 命令走这里）。"""
    data = data or collect()
    stats = data["stats"]
    lines: list[str] = []
    lines.append("=" * 66)
    lines.append("job_agent 记住了什么")
    lines.append("=" * 66)
    lines.append(f"岗位 {stats['total']} 条 · 对话 {len(data['conversations'])} 段 · "
                 f"偏好 {len(data['profile'])} 条 · 操作记录 {len(data['events'])} 条")

    lines.append("")
    lines.append("-- 岗位记忆 " + "-" * 53)
    if not data["jobs"]:
        lines.append("  （还是空的。去搜一句「帮我搜一下武汉的桥梁设计岗位」就有了）")
    for job in data["jobs"]:
        label = STATUS_LABELS.get(job["status"], job["status"])
        bits = [f"[#{job['id']}]", job["title"] or "未命名岗位"]
        bits.append("| " + (job["company"] or "公司未知"))
        if job["location"]:
            bits.append("| " + job["location"])
        if job["salary"]:
            bits.append("| " + job["salary"])
        bits.append(f"| {label}")
        lines.append("  " + " ".join(bits))
        if job["note"]:
            lines.append(f"        备注：{job['note']}")

    lines.append("")
    lines.append("-- 求职进展 " + "-" * 53)
    for status in STATUSES:
        count = stats["by_status"].get(status, 0)
        if count:
            lines.append(f"  {STATUS_LABELS[status]:<6} {count}")

    lines.append("")
    lines.append("-- 长期偏好 " + "-" * 53)
    if data["profile"]:
        for key, value in data["profile"].items():
            lines.append(f"  {key} = {value}")
    else:
        lines.append("  （还没有。可以说「记住：只看武汉的岗位」）")

    lines.append("")
    lines.append("-- 对话记忆 " + "-" * 53)
    if data["conversations"]:
        for session in data["conversations"]:
            lines.append(f"  对话 {session['thread_id']}（{len(session['messages'])} 条消息）")
            for message in session["messages"][-6:]:
                text = message["text"].replace("\n", " ")
                lines.append(f"    {message['label']}｜{text[:64]}")
    else:
        lines.append("  （还没有对话记录）")

    lines.append("")
    lines.append(f"数据文件：{data['db_path']}")
    lines.append("想看带样式的完整版：输入 /memory html，会生成一个网页给你。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 渲染：HTML
def _rich(text: str) -> str:
    """先转义，再把 **加粗** / `代码` 变成 HTML。"""
    out = html.escape(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    return out


def _status_tag(status: str) -> str:
    fg, bg = _STATUS_COLOR.get(status, ("#6b7a8f", "#f4f5f7"))
    label = STATUS_LABELS.get(status, status)
    return (f'<span class="tag" style="color:{fg};background:{bg};border-color:{fg}33">'
            f'{html.escape(label)}</span>')


_CSS = """
*, *::before, *::after { box-sizing: border-box; }
body {
  margin: 0; background: #f2f4f6; color: #29323d;
  font-family: "Segoe UI", "Microsoft YaHei", "PingFang SC", system-ui, sans-serif;
  font-size: 14px; line-height: 1.72; -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1060px; margin: 0 auto; padding: 34px 24px 72px; }

/* ---------- 页头 ---------- */
.head { padding-bottom: 18px; border-bottom: 2px solid #1f3a5f; margin-bottom: 22px; }
.head h1 { margin: 0; font-size: 22px; font-weight: 600; color: #1f3a5f; letter-spacing: .6px; }
.head .sub { margin-top: 6px; color: #7d8a9b; font-size: 13px; }
.head .sub b { color: #4d5c6e; font-weight: 600; }

/* ---------- 概览数字 ---------- */
.metrics { display: flex; background: #fff; border: 1px solid #e2e6ec; margin-bottom: 20px; }
.metric { flex: 1; padding: 16px 20px; border-right: 1px solid #edf0f4; }
.metric:last-child { border-right: 0; }
.metric .v { font-size: 25px; font-weight: 600; color: #1f3a5f; font-variant-numeric: tabular-nums; line-height: 1.25; }
.metric .k { font-size: 12px; color: #8794a5; margin-top: 3px; letter-spacing: .3px; }

/* ---------- 卡片 ---------- */
.card { background: #fff; border: 1px solid #e2e6ec; border-radius: 3px; padding: 20px 22px 22px; margin-bottom: 18px; }
.card > h2 {
  margin: 0 0 16px; padding-bottom: 10px; border-bottom: 1px solid #edf0f4;
  font-size: 15px; font-weight: 600; color: #1f3a5f; letter-spacing: .4px;
}
.card > h2 .n { color: #a6b1c0; font-weight: 400; font-size: 13px; margin-left: 8px; }
.card > h2 .idx {
  display: inline-block; width: 18px; height: 18px; line-height: 16px; text-align: center;
  border: 1px solid #c6cfdb; background: #f7f8fa; color: #4d5c6e;
  font-size: 11px; border-radius: 2px; margin-right: 9px; vertical-align: 1px;
  font-variant-numeric: tabular-nums;
}
.empty { color: #9aa5b3; font-size: 13px; padding: 6px 0 2px; }
.empty code { background: #f4f5f7; padding: 1px 5px; border-radius: 2px; color: #5b6b7f; }

/* ---------- 表格 ---------- */
table { width: 100%; border-collapse: collapse; font-size: 13px; table-layout: fixed; }
th {
  text-align: left; font-weight: 600; color: #5f6e80; background: #f7f8fa;
  padding: 8px 10px; border-bottom: 1px solid #e2e6ec; white-space: nowrap;
}
td { padding: 10px; border-bottom: 1px solid #eef1f4; vertical-align: top; }
tbody tr:last-child td { border-bottom: 0; }
tbody tr:hover { background: #fafbfc; }
td.id { color: #a6b1c0; font-variant-numeric: tabular-nums; white-space: nowrap; }
td.num { color: #6b7a8f; font-variant-numeric: tabular-nums; white-space: nowrap; }
td.when { color: #8794a5; font-variant-numeric: tabular-nums; white-space: nowrap; font-size: 12px; }
.t-title { color: #29323d; font-weight: 600; }
.t-desc {
  color: #8794a5; font-size: 12px; margin-top: 3px; line-height: 1.6;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
}

/* ---------- 状态标签 ---------- */
.tag {
  display: inline-block; padding: 1px 8px; font-size: 12px; line-height: 1.7;
  border: 1px solid; border-radius: 2px; white-space: nowrap;
}

/* ---------- 进度条 ---------- */
.bars { display: flex; flex-direction: column; gap: 9px; }
.bar { display: flex; align-items: center; gap: 12px; font-size: 13px; }
.bar .name { width: 76px; color: #5f6e80; flex: none; }
.bar .track { flex: 1; height: 9px; background: #f1f3f6; border-radius: 2px; overflow: hidden; }
.bar .fill { height: 100%; }
.bar .num { width: 30px; text-align: right; color: #8794a5; font-variant-numeric: tabular-nums; flex: none; }

/* ---------- 时间线 ---------- */
.tl { list-style: none; margin: 0; padding: 0; }
.tl li { position: relative; padding: 0 0 15px 20px; border-left: 1px solid #e0e5eb; }
.tl li:last-child { border-left-color: transparent; padding-bottom: 0; }
.tl li::before {
  content: ""; position: absolute; left: -3.5px; top: 7px;
  width: 6px; height: 6px; border-radius: 50%; background: #fff; border: 1px solid #1f3a5f;
}
.tl .ts { color: #8794a5; font-size: 12px; font-variant-numeric: tabular-nums; margin-right: 10px; }
.tl .kd { color: #4d5c6e; font-size: 12px; border: 1px solid #e2e6ec; background: #f8f9fb;
          padding: 0 6px; border-radius: 2px; margin-right: 8px; }
.tl .dt { color: #5f6e80; font-size: 13px; }
.tl .who { color: #1f3a5f; font-weight: 600; font-size: 13px; margin-right: 8px; }

/* ---------- 对话 ---------- */
.session { margin-bottom: 16px; }
.session:last-child { margin-bottom: 0; }
.session .sid { font-size: 12px; color: #8794a5; margin-bottom: 9px; font-variant-numeric: tabular-nums; }
.msg { border: 1px solid #e2e6ec; border-radius: 3px; padding: 10px 13px; margin-bottom: 9px; background: #fff; }
.msg:last-child { margin-bottom: 0; }
.msg .who { font-size: 12px; color: #8794a5; margin-bottom: 4px; letter-spacing: .3px; }
.msg .body { white-space: pre-wrap; word-break: break-word; font-size: 13px; }
.msg.user { background: #f2f6fb; border-color: #dbe5f0; }
.msg.user .who { color: #1f6fb5; }
.msg.tool { background: #f8f9fb; }
.msg.tool .body { color: #6b7a8f; font-size: 12px; }
.msg.assistant .who { color: #1f3a5f; }

/* ---------- 页脚 ---------- */
.foot { margin-top: 26px; padding-top: 16px; border-top: 1px solid #e2e6ec; color: #8794a5; font-size: 12px; line-height: 1.9; }
.foot code { background: #eceff3; padding: 1px 5px; border-radius: 2px; color: #4d5c6e; }
.foot .path { color: #5f6e80; word-break: break-all; }
strong { color: #1f3a5f; font-weight: 600; }
"""


def _table(jobs: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for job in jobs:
        desc = (job.get("description") or "").strip()
        desc_html = (f'<div class="t-desc">{_rich(desc[:150])}'
                     f'{"…" if len(desc) > 150 else ""}</div>') if desc else ""
        rows.append(
            "<tr>"
            f'<td class="id">#{job["id"]}</td>'
            f'<td><div class="t-title">{_rich(job["title"] or "未命名岗位")}</div>{desc_html}</td>'
            f'<td>{_rich(job["company"] or "—")}</td>'
            f'<td>{_rich(job["location"] or "—")}</td>'
            f'<td>{_rich(job["salary"] or "—")}</td>'
            f'<td>{_status_tag(job["status"])}</td>'
            f'<td class="num">{job["seen_count"]}</td>'
            f'<td class="when">{job["first_seen"][:16]}</td>'
            "</tr>"
        )
    head = ("<tr><th>编号</th><th>职位</th><th>公司</th><th>城市</th>"
            "<th>薪资</th><th>状态</th><th>出现</th><th>首次记录</th></tr>")
    # 定死列宽，否则「职位/公司」的长文本会把窄列挤变形
    cols = ('<colgroup><col style="width:50px"><col><col style="width:20%">'
            '<col style="width:8%"><col style="width:10%"><col style="width:9%">'
            '<col style="width:6%"><col style="width:15%"></colgroup>')
    return (f"<table>{cols}<thead>{head}</thead>"
            f"<tbody>{''.join(rows)}</tbody></table>")


def _bars(stats: dict[str, Any]) -> str:
    total = max(1, stats["total"])
    out: list[str] = []
    for status in STATUSES:
        count = stats["by_status"].get(status, 0)
        if not count:
            continue
        fg, _bg = _STATUS_COLOR.get(status, ("#6b7a8f", "#f4f5f7"))
        width = max(2, round(count / total * 100))
        out.append(
            '<div class="bar">'
            f'<span class="name">{html.escape(STATUS_LABELS[status])}</span>'
            f'<span class="track"><span class="fill" style="width:{width}%;background:{fg}"></span></span>'
            f'<span class="num">{count}</span>'
            "</div>"
        )
    return "".join(out)


def _timeline(events: list[dict[str, Any]]) -> str:
    out: list[str] = []
    for event in events:
        who = f"#{event['job_id']} {event.get('title') or ''}".strip()
        kind = _EVENT_KIND_LABEL.get(event["kind"], event["kind"])
        out.append(
            "<li>"
            f'<span class="ts">{event["ts"]}</span>'
            f'<span class="kd">{html.escape(kind)}</span>'
            f'<span class="who">{_rich(who)}</span>'
            f'<span class="dt">{_rich(event["detail"])}</span>'
            "</li>"
        )
    return f'<ul class="tl">{"".join(out)}</ul>'


def _conversations(sessions: list[dict[str, Any]]) -> str:
    out: list[str] = []
    for session in sessions:
        bubbles: list[str] = []
        for message in session["messages"]:
            bubbles.append(
                f'<div class="msg {message["role"]}">'
                f'<div class="who">{html.escape(message["label"])}</div>'
                f'<div class="body">{_rich(message["text"])}</div>'
                "</div>"
            )
        out.append(
            '<div class="session">'
            f'<div class="sid">对话编号 {html.escape(session["thread_id"])}'
            f' · 共 {len(session["messages"])} 条消息</div>'
            f'{"".join(bubbles)}</div>'
        )
    return "".join(out)


def render_html(data: dict[str, Any] | None = None) -> str:
    data = data or collect()
    stats = data["stats"]

    parts: list[str] = []
    parts.append('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    parts.append("<title>job_agent 记忆总览</title>")
    parts.append(f"<style>{_CSS}</style></head><body><div class=\"wrap\">")

    # --- 页头 ---
    parts.append(
        '<div class="head">'
        "<h1>job_agent 记忆总览</h1>"
        '<div class="sub">这是求职助手<b>自己记住</b>的全部内容 —— 不是你整理的笔记，'
        "是它每次搜岗位、改状态时自己写进数据库的。<br>"
        f"生成时间：{data['generated_at']}　·　模型：{html.escape(data['model'])}</div>"
        "</div>"
    )

    # --- 概览 ---
    parts.append(
        '<div class="metrics">'
        f'<div class="metric"><div class="v">{stats["total"]}</div><div class="k">记住的岗位</div></div>'
        f'<div class="metric"><div class="v">{stats["by_status"].get("applied", 0)}</div>'
        '<div class="k">已投递</div></div>'
        f'<div class="metric"><div class="v">{len(data["conversations"])}</div><div class="k">对话片段</div></div>'
        f'<div class="metric"><div class="v">{len(data["profile"])}</div><div class="k">长期偏好</div></div>'
        f'<div class="metric"><div class="v">{len(data["events"])}</div><div class="k">操作记录</div></div>'
        "</div>"
    )

    # --- ① 岗位记忆 ---
    parts.append('<div class="card"><h2><span class="idx">1</span>岗位记忆'
                 '<span class="n">它记住的每一个岗位</span></h2>')
    if data["jobs"]:
        parts.append(_table(data["jobs"]))
    else:
        parts.append('<div class="empty">还是空的。去对话里说一句 '
                     "<code>帮我搜一下武汉的桥梁设计岗位</code>，这里就会长出内容。</div>")
    parts.append("</div>")

    # --- ② 求职进展 ---
    parts.append('<div class="card"><h2><span class="idx">2</span>求职进展'
                 '<span class="n">按状态分布</span></h2>')
    if stats["total"]:
        parts.append(f'<div class="bars">{_bars(stats)}</div>')
    else:
        parts.append('<div class="empty">还没有岗位，暂时画不出分布。</div>')
    parts.append("</div>")

    # --- ③ 操作时间线 ---
    parts.append('<div class="card"><h2><span class="idx">3</span>操作时间线'
                 '<span class="n">什么时候看到的、什么时候投的</span></h2>')
    if data["events"]:
        parts.append(_timeline(data["events"]))
    else:
        parts.append('<div class="empty">还没有任何操作记录。</div>')
    parts.append("</div>")

    # --- ④ 长期偏好 ---
    parts.append('<div class="card"><h2><span class="idx">4</span>长期偏好'
                 '<span class="n">你让它记住的</span></h2>')
    if data["profile"]:
        rows = "".join(
            f'<div class="bar"><span class="name">{html.escape(k)}</span>'
            f'<span class="dt">{_rich(v)}</span></div>'
            for k, v in data["profile"].items()
        )
        parts.append(f'<div class="bars">{rows}</div>')
    else:
        parts.append('<div class="empty">还没有。跟它说 <code>记住：只看武汉的岗位</code> 就会出现在这里。</div>')
    parts.append("</div>")

    # --- ⑤ 对话记忆 ---
    parts.append('<div class="card"><h2><span class="idx">5</span>对话记忆'
                 '<span class="n">它记得你们聊过什么</span></h2>')
    if data["conversations"]:
        parts.append(_conversations(data["conversations"]))
    else:
        parts.append('<div class="empty">还没有对话记录。'
                     "（这段记忆存在对话检查点里，关闭程序后依然保留。）</div>")
    parts.append("</div>")

    # --- 页脚 ---
    parts.append(
        '<div class="foot">'
        "<b>数据存在哪</b><br>"
        f'岗位记忆：<span class="path">{html.escape(data["db_path"])}</span><br>'
        f'对话记忆：<span class="path">{html.escape(data["ckpt_path"])}</span><br><br>'
        "<b>怎么更新这一页</b><br>"
        "在对话里输入 <code>/memory html</code>，或者运行 "
        "<code>.venv\\Scripts\\python.exe -m job_agent.memory_view</code>，重新生成一份即可。"
        "</div>"
    )

    parts.append("</div></body></html>")
    return "".join(parts)


# ---------------------------------------------------------------- 落盘
def default_html_path() -> Path:
    """默认生成到项目根目录，和 启动.bat 放一起，双击就能开。"""
    return Path(__file__).resolve().parent.parent / _DEFAULT_HTML


def write_html(path: str | Path | None = None) -> Path:
    target = Path(path) if path else default_html_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_html(), encoding="utf-8")
    return target


def main() -> None:
    argv = sys.argv[1:]
    if "--text" in argv or "-t" in argv:
        print(render_text())
        return

    target = write_html()
    print(f"[记忆] 已生成网页：{target}")
    print("       双击就能用浏览器打开，里面是 agent 记住的全部内容。")
    if "--no-open" not in argv:
        try:
            import webbrowser

            webbrowser.open(target.as_uri())
        except Exception:
            pass


if __name__ == "__main__":
    main()
