"""入口自举：让「任何入口文件」在「任何环境」下都能跑起来。

这里解决的都是真实踩过的坑，按顺序：

  1) **直接运行包内文件**（IDE 里点运行按钮、或者双击 .py）时 `__package__` 是 None，
     相对导入会报 `ImportError: attempted relative import with no known parent package`。
     —— 由入口文件顶部的 PEP 366 自救块解决（见下面用法）。

  2) **IDE 用的解释器不是项目 .venv** —— 报
     `ModuleNotFoundError: No module named 'langchain_openai'`。
     依赖只装在 .venv 里，所以这里替它把包目录接上去。

  3) **直接换解释器（os.execv）会让 IDE 收不到键盘输入**。
     所以换进程只是最后兜底，主路径是「借用包目录、不换进程」。

用法（放在入口文件最顶部，任何第三方 import 之前）::

    if __package__ in (None, ""):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        __package__ = "job_agent"

    from ._bootstrap import bootstrap
    bootstrap(__file__)

注：`cli.py` 和 `selftest.py` 里各有一份行为等价的内联实现（当时为了少一个文件），
    两边逻辑保持一致；后续可以统一迁移到这里。
"""

from __future__ import annotations


def _venv_version_matches(venv_dir, info) -> bool:
    """比对 .venv/pyvenv.cfg 里记录的版本 与 当前解释器的主次版本号。

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
        return value.strip().split(".")[:2] == [str(info.major), str(info.minor)]
    return False


def bootstrap(entry_file: str) -> None:
    """按上面三步，把当前进程调整到「能用 .venv 依赖」的状态。"""
    import os
    import sys
    from pathlib import Path

    if os.environ.get("JOB_AGENT_BOOTSTRAPPED"):     # 已经处理过，别重复
        return
    os.environ["JOB_AGENT_BOOTSTRAPPED"] = "1"

    venv = Path(entry_file).resolve().parent.parent / ".venv"
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
    # 注意要把命令行参数原样带过去，否则 `--text` 这类开关会在重启后丢失
    forwarded = [str(exe), "-X", "utf8", str(Path(entry_file).resolve())] + list(sys.argv[1:])
    try:
        os.execv(str(exe), forwarded)
    except OSError:
        return              # 换不成就照旧往下跑，让正常的报错提示接手
