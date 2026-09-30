"""交互式命令行界面。

用法（三种都可以）：
    .venv\\Scripts\\python.exe -m job_agent      ← 推荐
    .venv\\Scripts\\python.exe job_agent\\cli.py
    双击项目根目录的 启动.bat，或在 IDE 里对 cli.py 点运行按钮

特点：
  * 直接打字聊天（流式输出，能看见模型边想边写）
  * 斜杠命令直接操作记忆库，不消耗 token
  * /new 开一段新对话，老对话和岗位记忆都还在
"""

from __future__ import annotations

# ---- 被直接执行时的自救 ----
# 正常启动是 `python -m job_agent`，这时 __package__ == "job_agent"。
# 直接运行本文件时 __package__ 是 None，下面的相对导入会报
#   ImportError: attempted relative import with no known parent package
# 所以这里按 PEP 366 手动补上「包名」和「项目根目录」，两种跑法就统一了。
if __package__ in (None, ""):
    import sys as _sys
    from pathlib import Path as _Path

    _ROOT = _Path(__file__).resolve().parent.parent
    if str(_ROOT) not in _sys.path:
        _sys.path.insert(0, str(_ROOT))
    __package__ = "job_agent"


# ============================================================================
#  入口自举：让这个文件在「任何」环境下都能跑起来。
#  按顺序解决三个真实踩过的坑：
#     1) 直接运行包内文件 → ImportError: attempted relative import ...（上面已解决）
#     2) IDE 里点运行，用的 Python 不是项目 .venv
#        → ModuleNotFoundError: No module named 'langchain_openai'
#     3) 换个解释器重启进程 → IDE 控制台打不了字（调试模式下必现）
# ============================================================================
def _venv_version_matches(venv_dir, info) -> bool:
    """比对 .venv/pyvenv.cfg 记录的版本 与 当前解释器的主次版本号是否一致。

    为什么要比：site-packages 里 pydantic_core、tiktoken 这类包带 C 扩展，
    跨大版本加载会直接崩，所以只有版本一致时才敢借用它的包目录。
    """
    from pathlib import Path

    cfg = Path(venv_dir) / "pyvenv.cfg"
    if not cfg.is_file():
        return False
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip().lower() != "version":
            continue
        parts = value.strip().split(".")
        return parts[:2] == [str(info.major), str(info.minor)]
    return False


def _bootstrap_deps() -> None:
    """坑 2 / 坑 3：让第三方依赖可用，而且尽量不换进程。"""
    import os
    import sys
    from pathlib import Path

    if os.environ.get("JOB_AGENT_BOOTSTRAPPED"):     # 已经处理过，别重复
        return
    os.environ["JOB_AGENT_BOOTSTRAPPED"] = "1"

    venv = Path(__file__).resolve().parent.parent / ".venv"
    if not venv.is_dir():
        return
    exe = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    # --- 当前用的就是 .venv 的解释器：什么都不用做 ---
    if exe.is_file():
        try:
            if Path(sys.executable).resolve() == exe.resolve():
                return
        except OSError:
            pass

    # --- 情况一（首选）：版本一致 → 把 .venv 的包目录挂到 sys.path 最前面 ---
    # 不换进程，所以 IDE 控制台的输入输出、调试器连接全都完好无损。
    site_dir = venv / "Lib" / "site-packages"        # Windows 布局
    if not site_dir.is_dir():
        site_dir = (venv / "lib"
                    / f"python{sys.version_info.major}.{sys.version_info.minor}"
                    / "site-packages")                # macOS / Linux 布局
    if site_dir.is_dir() and _venv_version_matches(venv, sys.version_info):
        if str(site_dir) not in sys.path:
            sys.path.insert(0, str(site_dir))
        import importlib

        importlib.invalidate_caches()                 # 让 import 重新扫描一遍
        return

    # --- 情况二（兜底）：版本不一致 → 只能换成 .venv 的解释器重新执行自己 ---
    # 跨大版本加载 C 扩展会崩，没有别的办法。副作用：IDE 的调试器会断开。
    if not exe.is_file():
        return
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execv(str(exe), [str(exe), "-X", "utf8", str(Path(__file__).resolve())])
    except OSError:
        return              # 换不成就照旧往下跑，让正常的报错提示接手


_bootstrap_deps()

import json
import sys
import uuid

from langchain_core.messages import HumanMessage

from .config import load_settings
from .store import STATUSES, STATUS_LABELS, format_job_line, get_store

try:  # 让中文 / 特殊符号在 Windows 控制台也能正常显示
    if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ---------------------------------------------------------------- 工具函数
def _rule(char: str = "-", width: int = 64) -> str:
    return char * width


def _ver_tuple(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in text.split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _supports_dual_stream() -> bool:
    """LangGraph >= 0.2 才支持 stream_mode 传列表（同时收 tokens 和节点更新）。"""
    try:
        from importlib.metadata import version

        return _ver_tuple(version("langgraph")) >= (0, 2, 0)
    except Exception:
        return False


HELP_TEXT = """可用命令（以 / 开头，不会消耗 token）：
  /help              显示本帮助
  /list [状态]       列出记忆库里的岗位，例：/list applied
  /find 关键词       在记忆库里搜（职位/公司/城市/备注）
  /detail ID         看某个岗位的完整信息和时间线
  /status ID 状态    修改状态，例：/status 3 applied
  /stats             求职进展统计
  /history           最近的操作记录
  /remember k=v      记一条长期偏好，例：/remember 目标城市=武汉
  /export            导出全部岗位为 CSV（放在 data 目录）
  /memory [html]     看它到底记住了什么；加 html 生成一个能双击打开的网页
  /forget 范围       清空记忆：jobs / profile / conversations / all
                     （先自动备份，再要你敲 yes 确认；不可逆，慎用）
  /new               开一段新对话（岗位记忆保留）
  /model             查看当前用的是哪个模型 / 数据存在哪
  /quit  或  /exit   退出

状态取值：new interested applied interview offer rejected archived
中文别名：新发现 感兴趣 已投递 面试中 已录用 已拒 已归档

也可以直接用中文聊，例如：
  帮我搜一下武汉的桥梁设计岗位
  我之前看过哪些工作？
  #3 改成已投递，备注：9/29 投的
  记住：只看武汉和宜昌的岗位，薪资不低于 12k
"""

_CN_STATUS = {label: key for key, label in STATUS_LABELS.items()}


def _normalize_status(text: str) -> str:
    text = (text or "").strip()
    if text in STATUSES:
        return text
    if text in _CN_STATUS:
        return _CN_STATUS[text]
    aliases = {"投了": "applied", "投递": "applied", "面试": "interview", "录用": "offer",
               "拒": "rejected", "被拒": "rejected", "不看了": "archived", "放弃": "archived"}
    return aliases.get(text, "")


def _forget_command(args: list[str], thread_id: str) -> str:
    """处理 /forget：先列清单 + 备份，敲 yes 才真删。返回（可能换过的）thread_id。"""
    from . import forget as forget_module  # 只在用到时才导入

    raw = (args[0] if args else "").strip()
    if not raw:
        print("用法：/forget 范围")
        print(f"  可选范围：{forget_module.SCOPE_HINT}")
        print("  例：/forget conversations   只清聊天记录，投递履历保留")
        print("      /forget jobs            清空岗位记忆")
        print("      /forget all             全部清空")
        print("  执行前会自动备份到 data/backup/，并且要你敲 yes 二次确认。")
        return thread_id

    scope = forget_module.normalize_scope(raw)
    if not scope:
        print(f"[提示] 范围「{raw}」不认识。可选：{forget_module.SCOPE_HINT}")
        return thread_id

    from .config import load_settings

    store = get_store()
    print(f"[清除] 将要清空：{forget_module.describe(scope)}")
    if scope in ("jobs", "all"):
        print(f"         岗位 {store.count_rows('jobs')} 条、操作记录 {store.count_rows('events')} 条")
    if scope in ("profile", "all"):
        print(f"         长期偏好 {store.count_rows('profile')} 条")
    if scope in ("conversations", "all"):
        print(f"         对话 {forget_module.count_conversations(load_settings().checkpoint_path)} 段")
    print("         这是不可逆操作，执行前会自动备份一份到 data/backup/。")

    try:
        answer = input("         确认清空吗？输入 yes 继续（其它任何输入都取消）：").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n[已取消] 什么都没删。")
        return thread_id
    if answer not in {"yes", "y", "确认", "确定"}:
        print("[已取消] 什么都没删。")
        return thread_id

    try:
        result = forget_module.forget(scope)
    except Exception as exc:
        print(f"[出错] {type(exc).__name__}: {exc}")
        return thread_id

    for text in result.summary_lines():
        print("  " + text)

    if scope in ("conversations", "all"):
        thread_id = uuid.uuid4().hex[:8]
        print(f"  顺带开了新对话，thread_id = {thread_id}（避免接在已删掉的历史后面）")
    return thread_id


# ---------------------------------------------------------------- 命令处理
def handle_command(line: str, thread_id: str) -> tuple[bool, str]:
    """处理斜杠命令。返回 (是否已处理, 新的 thread_id)。"""
    store = get_store()
    parts = line[1:].split()
    cmd = parts[0].lower() if parts else ""
    args = parts[1:]

    if cmd in {"help", "h", "?"}:
        print(HELP_TEXT)

    elif cmd in {"quit", "exit", "q"}:
        raise SystemExit(0)

    elif cmd in {"new", "reset"}:
        thread_id = uuid.uuid4().hex[:8]
        print(f"[对话] 已开启新对话，thread_id = {thread_id}（岗位记忆不受影响）")

    elif cmd == "model":
        s = load_settings()
        for line_text in s.summary_lines():
            print("  " + line_text)
        print(f"  当前对话编号: {thread_id}")

    elif cmd == "list":
        status = _normalize_status(args[0]) if args else ""
        if args and not status:
            print(f"[提示] 状态「{args[0]}」不认识，可选：{'、'.join(STATUSES)}")
            return True, thread_id
        jobs = store.list_jobs(status=status or None, limit=30)
        if not jobs:
            print("[记忆库] 还没有岗位记录。")
        else:
            print(f"[记忆库] 共 {len(jobs)} 条：")
            for job in jobs:
                print("  " + format_job_line(job, verbose=True).replace("\n", "\n  "))

    elif cmd == "find":
        query = " ".join(args)
        if not query:
            print("用法：/find 关键词")
            return True, thread_id
        jobs = store.search(query=query, limit=30)
        print(f"[记忆库] 匹配「{query}」共 {len(jobs)} 条：")
        for job in jobs:
            print("  " + format_job_line(job))

    elif cmd == "detail":
        try:
            job_id = int(args[0])
        except (IndexError, ValueError):
            print("用法：/detail 岗位编号，例：/detail 3")
            return True, thread_id
        job = store.get_job(job_id)
        if job is None:
            print(f"[记忆库] 没有 #{job_id} 这个岗位。")
        else:
            print(format_job_line(job, verbose=True))
            print(f"  来源：{job['source']}｜首次记录 {job['first_seen']}｜共出现 {job['seen_count']} 次")
            if job["description"]:
                print(f"  描述：{job['description'][:300]}")
            events = store.timeline(job_id)
            if events:
                print("  时间线（新->旧）：")
                for e in events:
                    print(f"    {e['ts']}  [{e['kind']}]  {e['detail']}")

    elif cmd == "status":
        if len(args) < 2:
            print("用法：/status 岗位编号 状态，例：/status 3 applied")
            return True, thread_id
        try:
            job_id = int(args[0])
        except ValueError:
            print("[提示] 第一个参数要是数字编号。")
            return True, thread_id
        status = _normalize_status(args[1])
        if not status:
            print(f"[提示] 状态「{args[1]}」不认识。可选：{'、'.join(STATUSES)}")
            return True, thread_id
        note = " ".join(args[2:]) or None
        job = store.update_status(job_id, status, note)
        print(f"[记忆库] 已更新：{format_job_line(job)}" if job else f"[记忆库] 找不到 #{job_id}")

    elif cmd == "stats":
        s = store.stats()
        print(f"[统计] 共记录 {s['total']} 个岗位，最近 7 天新增 {s['new_last_7_days']} 个。")
        for status, count in s["by_status"].items():
            if count:
                print(f"  {STATUS_LABELS.get(status, status)}：{count}")
        profile = store.all_profile()
        if profile:
            print("  长期偏好：")
            for k, v in profile.items():
                print(f"    {k} = {v}")

    elif cmd == "history":
        events = store.recent_events(limit=20)
        if not events:
            print("[记录] 暂无操作记录。")
        for e in events:
            name = f"#{e['job_id']} {e['title'] or ''}".strip()
            print(f"  {e['ts']}  [{e['kind']}] {name}  {e['detail']}")

    elif cmd in {"remember", "remember_"}:
        raw = " ".join(args)
        if "=" not in raw:
            print("用法：/remember 键=值，例：/remember 目标城市=武汉")
            return True, thread_id
        key, _, value = raw.partition("=")
        store.set_profile(key.strip(), value.strip())
        print(f"[记忆] 已记住：{key.strip()} = {value.strip()}")

    elif cmd == "export":
        path = store.export_csv()
        print(f"[导出] 已生成：{path}")

    elif cmd == "memory":
        from . import memory_view  # 只在用到时才导入

        if args and args[0].lower() in {"html", "web", "page", "网页"}:
            path = memory_view.write_html()
            print(f"[记忆] 已生成网页：{path}")
            print("       双击打开就能看到它记住的全部内容（岗位 / 进展 / 时间线 / 对话）。")
        else:
            print(memory_view.render_text())

    elif cmd in {"forget", "clear", "wipe"}:
        thread_id = _forget_command(args, thread_id)

    else:
        print(f"[提示] 未知命令 /{cmd}，输入 /help 看帮助。")

    return True, thread_id


# ---------------------------------------------------------------- 一轮对话
def run_turn(app, text: str, config: dict, dual: bool) -> None:
    """把一句话交给图去跑，边跑边打印。"""
    payload = {"messages": [HumanMessage(content=text)]}
    live_printed = False
    try:
        if dual:
            stream = app.stream(payload, config, stream_mode=["updates", "messages"])
        else:
            stream = app.stream(payload, config, stream_mode="updates")

        for event in stream:
            mode, data = event if dual else ("updates", event)

            if mode == "messages":
                chunk, meta = data
                if meta.get("langgraph_node") != "agent":
                    continue
                if chunk.__class__.__name__ != "AIMessageChunk":
                    continue
                content = chunk.content
                if isinstance(content, str) and content:
                    print(content, end="", flush=True)
                    live_printed = True
                continue

            # mode == "updates"
            for node, update in (data or {}).items():
                messages = (update or {}).get("messages") or []
                if node == "agent":
                    for msg in messages:
                        for call in getattr(msg, "tool_calls", []) or []:
                            args = json.dumps(call.get("args", {}), ensure_ascii=False)
                            print(f"\n[工具] 调用 {call.get('name')}  {args}")
                    if not dual:
                        for msg in messages:
                            if getattr(msg, "content", "") and not getattr(msg, "tool_calls", None):
                                print(msg.content)
                elif node == "tools":
                    for msg in messages:
                        first_line = str(getattr(msg, "content", "")).strip().splitlines()
                        brief = (first_line[0][:100] + "...") if first_line else ""
                        print(f"[工具] {getattr(msg, 'name', '?')} 执行完成  {brief}")
        print()
    except KeyboardInterrupt:
        print("\n[已中断本轮对话]")
    except Exception as exc:  # 网络 / Key / 额度等问题的友好提示
        print(f"\n[出错] {type(exc).__name__}: {exc}")
        print("  排查建议：1) 跑 python -m job_agent.config 确认 Key 读到没；2) 检查网络。")


# ---------------------------------------------------------------- 主程序
def main() -> None:
    settings = load_settings()
    settings.ensure_dirs()

    print(_rule("="))
    print("  job_agent —— 会记事的求职助手 (LangGraph)")
    print(_rule("="))
    for line in settings.summary_lines():
        print("  " + line)
    print(f"  解释器        : {sys.executable}")
    print(_rule("-"))

    from . import graph as graph_module

    try:
        app = graph_module.build_agent(settings)
    except Exception as exc:
        print(f"[致命错误] 图构建失败：{type(exc).__name__}: {exc}")
        print("  大概率是依赖没装全，试试： pip install -r job_agent/requirements.txt")
        return

    print(f"  对话记忆      : {graph_module.CHECKPOINTER_KIND}（sqlite = 重启后仍保留）")

    store = get_store()
    stats = store.stats()
    print(f"  记忆库现状    : {stats['total']} 个岗位" + ("" if stats["total"] else "（还是空的，先从搜岗位开始吧）"))
    print(_rule("-"))
    print("  输入 /help 看命令，直接说中文也能聊。Ctrl+C 中断本轮，/quit 退出。")
    print(_rule("-"))

    dual = _supports_dual_stream()
    thread_id = uuid.uuid4().hex[:8]
    config = graph_module.new_config(thread_id)

    while True:
        try:
            line = input("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见，祝早日拿到心仪的 offer。")
            break

        if not line:
            continue

        if line.startswith("/"):
            try:
                _, thread_id = handle_command(line, thread_id)
                config = graph_module.new_config(thread_id)
            except SystemExit:
                print("再见，祝早日拿到心仪的 offer。")
                break
            continue

        run_turn(app, line, config, dual)


if __name__ == "__main__":
    main()
