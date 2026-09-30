"""配置中心：所有可调项都来自「环境变量」或「项目根目录的 .env 文件」。

优先级：  系统环境变量  >  .env 文件  >  代码里的默认值

为什么要自己写 .env 解析？因为这样就不依赖 python-dotenv，少装一个包。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------- 路径
PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_DATA_DIR = PACKAGE_DIR / "data"


# ---------------------------------------------------------------- .env 解析
def _read_env_file(path: Path) -> dict[str, str]:
    """读 .env。支持 KEY=VALUE、# 注释、值带引号。读不到就返回空字典。"""
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="gbk", errors="replace")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            result[key] = value
    return result


_FILE_ENV: dict[str, str] = _read_env_file(ENV_FILE)


def env(name: str, default: str = "") -> str:
    """按 环境变量 -> .env -> 默认值 的顺序取值。"""
    return os.environ.get(name) or _FILE_ENV.get(name) or default


def _env_first(names: list[str], default: str = "") -> str:
    for name in names:
        value = env(name)
        if value:
            return value
    return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(env(name, str(default)))
    except ValueError:
        return default


# ---------------------------------------------------------------- 来源诊断
def _source_of(*names: str) -> tuple[str, str]:
    """按 环境变量 -> .env -> 默认值 的顺序找第一个有值的，返回 (值, 来源说明)。

    用途：用户改了配置不生效时，能一眼看出程序实际读的是哪一份。
    """
    for name in names:
        if os.environ.get(name):
            return os.environ[name], f"系统环境变量 {name}"
        if _FILE_ENV.get(name):
            return _FILE_ENV[name], f".env 文件 {name}"
    return "", "默认值（没配置过）"


def _pad(text: str, width: int) -> str:
    """按「显示宽度」右补空格（中文/中文标点按 2 格算），否则终端里中文标签对不齐。"""
    display = sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)
    return text + " " * max(0, width - display)


def _masked(value: str) -> str:
    """脱敏显示 Key：留开头 6 位 + 长度，够认出是哪个，又不会泄露全文。"""
    if not value:
        return "未设置"
    if len(value) <= 10:
        return f"已设置（长度 {len(value)}）"
    return f"{value[:6]}...（长度 {len(value)}）"


# ---------------------------------------------------------------- 配置对象
@dataclass(frozen=True)
class Settings:
    """一次运行的完整配置。用 Settings.load() 创建。"""

    api_key: str
    base_url: str
    model: str
    temperature: float
    tavily_api_key: str
    data_dir: Path

    # ---- 派生属性 ----
    @property
    def db_path(self) -> Path:
        """长期记忆库（岗位、状态、偏好）。"""
        return self.data_dir / "job_memory.sqlite3"

    @property
    def checkpoint_path(self) -> Path:
        """对话检查点库（跨重启保留聊天记录）。"""
        return self.data_dir / "conversation.sqlite3"

    @property
    def model_label(self) -> str:
        return f"{self.model} @ {self.base_url}"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def summary_lines(self) -> list[str]:
        return [
            f"模型          : {self.model_label}",
            f"长期记忆库    : {self.db_path}",
            f"对话记忆库    : {self.checkpoint_path}",
            "岗位搜索源    : Tavily 联网搜索",
        ]


def load_settings() -> Settings:
    """从环境变量 / .env 组装配置。"""
    api_key = _env_first(["JOB_AGENT_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"])
    base_url = env("JOB_AGENT_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model = env("JOB_AGENT_MODEL", "deepseek-chat")
    data_dir = Path(env("JOB_AGENT_DATA_DIR", str(DEFAULT_DATA_DIR))).expanduser()

    return Settings(
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=_env_float("JOB_AGENT_TEMPERATURE", 0.3),
        tavily_api_key=env("TAVILY_API_KEY"),
        data_dir=data_dir,
    )


if __name__ == "__main__":  # 想单独检查配置就运行： python -m job_agent.config
    s = load_settings()
    print("=== 当前配置 ===")
    for line in s.summary_lines():
        print(line)

    # ---- 每个值实际是从哪儿读来的（「我明明改了怎么不生效」就看这段）----
    print()
    print("=== 配置来源（优先级：系统环境变量 > .env 文件 > 默认值）===")
    _key_val, key_src = _source_of("JOB_AGENT_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")
    _tv_val, tv_src = _source_of("TAVILY_API_KEY")
    _base_val, base_src = _source_of("JOB_AGENT_BASE_URL")
    _model_val, model_src = _source_of("JOB_AGENT_MODEL")
    _data_val, data_src = _source_of("JOB_AGENT_DATA_DIR")

    rows = [
        ("API Key", _masked(s.api_key), key_src),
        ("Tavily Key", _masked(s.tavily_api_key), tv_src),
        ("Base URL", s.base_url, base_src),
        ("模型名", s.model, model_src),
        ("数据目录", str(s.data_dir), data_src),
    ]
    for label, value, src in rows:
        print(f"  {_pad(label, 11)}: {value}")
        print(f"  {' ' * 11}  <- {src}")

    print()
    print(f"  {_pad('.env 文件', 11)}: {ENV_FILE}  ({'存在' if ENV_FILE.is_file() else '不存在'})")
