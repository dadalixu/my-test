"""包入口：支持 `python -m job_agent` 启动。

同时也支持被「直接运行」——IDE 里对本文件点运行按钮、或
`python job_agent/__main__.py`，都不会再抱错。
"""

from __future__ import annotations

# ---- 被直接执行时的自救 ----
# 正常启动是 `python -m job_agent`，这时 __package__ == "job_agent"。
# 直接运行本文件时 __package__ 是 None，下面这行相对导入会报
#   ImportError: attempted relative import with no known parent package
# 所以这里按 PEP 366 手动补上「包名」和「项目根目录」，两种跑法就统一了。
if __package__ in (None, ""):
    import sys
    from pathlib import Path

    _ROOT = Path(__file__).resolve().parent.parent
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    __package__ = "job_agent"

from .cli import main

if __name__ == "__main__":
    main()
