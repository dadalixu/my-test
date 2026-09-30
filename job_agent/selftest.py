"""离线自测：一条命令验证全部功能，不联网、不花钱。

运行（两种都可以）：
    .venv\\Scripts\\python.exe -m job_agent.selftest
    .venv\\Scripts\\python.exe job_agent\\selftest.py

两个关键设计：
  1. 在任何项目模块被导入之前，先把数据目录指到临时目录、把 API Key 从环境里摘干净，
     保证测试永远不碰你的真实记忆库，也绝不会偷偷联网花钱。
  2. 用一个「假模型」注入到图里替代真模型 —— 自测要验的是图的接线和记忆读写逻辑，
     不需要真的调大模型。假模型不依赖任何在线服务，按关键词规则决定调哪个工具。

为什么假模型放在这个文件里而不是单独一个模块？
因为它是「测试替身」，只有自测需要它；放进生产代码里反而会被误用成"没 Key 时的降级方案"，
掩盖了配置问题。自测注入自己的假模型，才是干净的做法。
"""

from __future__ import annotations

# ---- 被直接执行时的自救（同 cli.py）----
# 直接运行本文件时 __package__ 是 None，下面的相对导入会报
#   ImportError: attempted relative import with no known parent package
# 这里按 PEP 366 补上包名和项目根目录，`-m` 和直接运行两种跑法就统一了。
if __package__ in (None, ""):
    import sys as _sys
    from pathlib import Path as _Path

    _ROOT = _Path(__file__).resolve().parent.parent
    if str(_ROOT) not in _sys.path:
        _sys.path.insert(0, str(_ROOT))
    __package__ = "job_agent"


# ============================================================================
#  入口自举（与 cli.py 里那段完全一致，改一处记得改两处）
#  坑：IDE 点运行用的 Python 不是项目 .venv → ModuleNotFoundError。
#      优先「借用 .venv 的包目录」（不换进程），实在不行才换解释器重启。
# ============================================================================
def _venv_version_matches(venv_dir, info) -> bool:
    """比对 .venv/pyvenv.cfg 记录的版本 与 当前解释器的主次版本号是否一致。"""
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
    """确保第三方依赖可用，而且尽量不换进程（换了进程 IDE 就没法输入）。"""
    import os
    import sys
    from pathlib import Path

    if os.environ.get("JOB_AGENT_BOOTSTRAPPED"):
        return
    os.environ["JOB_AGENT_BOOTSTRAPPED"] = "1"

    venv = Path(__file__).resolve().parent.parent / ".venv"
    if not venv.is_dir():
        return
    exe = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    if exe.is_file():
        try:
            if Path(sys.executable).resolve() == exe.resolve():
                return                                  # 已经是 .venv 的解释器
        except OSError:
            pass

    site_dir = venv / "Lib" / "site-packages"
    if not site_dir.is_dir():
        site_dir = (venv / "lib"
                    / f"python{sys.version_info.major}.{sys.version_info.minor}"
                    / "site-packages")
    if site_dir.is_dir() and _venv_version_matches(venv, sys.version_info):
        if str(site_dir) not in sys.path:
            sys.path.insert(0, str(site_dir))
        import importlib

        importlib.invalidate_caches()
        return

    if not exe.is_file():
        return
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execv(str(exe), [str(exe), "-X", "utf8", str(Path(__file__).resolve())])
    except OSError:
        return


_bootstrap_deps()

import itertools
import json
import os
import re
import sqlite3
import tempfile
import traceback
from pathlib import Path
from typing import Any, Optional, Sequence

# ---------------------------------------------------------------- 环境隔离
# 必须在第一次读配置之前执行
_TMP = Path(tempfile.mkdtemp(prefix="job_agent_selftest_"))
os.environ["JOB_AGENT_DATA_DIR"] = str(_TMP)
for _env_key in ("JOB_AGENT_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "TAVILY_API_KEY"):
    os.environ.pop(_env_key, None)

from langchain_core.callbacks import CallbackManagerForLLMRun  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import (  # noqa: E402
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402

from . import config as config_module  # noqa: E402
from . import forget as forget_module  # noqa: E402
from . import graph as graph_module  # noqa: E402
from . import store as store_module  # noqa: E402
from . import tools as tools_module  # noqa: E402

# 别让项目根目录的真实 .env 影响自测。
# 上面只「摘掉」了环境变量，但 config 在导入时已经把 .env 读进内存了——
# 用户一旦往 .env 里填真 Key，自测就会真的联网、花真钱。所以这里再清一道。
config_module._FILE_ENV.clear()

# ---- 联网搜索替身 ----
# 真实搜索要联网、要花钱，自测不能走。但「搜索 -> 清洗 -> 入库 -> 去重」这条链路
# 是最容易出 bug 的地方，不能因为离线就不测。所以把联网那一步换成固定数据，
# 后面的处理逻辑照跑。
_REAL_TAVILY_SEARCH = tools_module._tavily_search

_FAKE_RESULTS: list[dict[str, str]] = [
    {"title": "桥梁结构设计工程师", "company": "中交二航局", "location": "武汉",
     "salary": "12-18k", "url": "https://example.invalid/1",
     "description": "负责桥梁上部结构计算与施工图设计。"},
    {"title": "道路设计师", "company": "湖北省交通规划设计院", "location": "武汉",
     "salary": "10-15k", "url": "https://example.invalid/2",
     "description": "公路路线与路基路面设计。"},
    {"title": "钢结构详图工程师", "company": "中铁大桥局", "location": "武汉",
     "salary": "9-14k", "url": "https://example.invalid/3",
     "description": "钢结构深化设计，Tekla Structures 建模与出图。"},
    {"title": "BIM 工程师（市政桥梁）", "company": "中建三局", "location": "武汉",
     "salary": "10-16k", "url": "https://example.invalid/4",
     "description": "桥梁专业 BIM 建模、碰撞检查与施工模拟。"},
    {"title": "施工技术员（桥梁）", "company": "湖北路桥集团", "location": "宜昌",
     "salary": "8-12k", "url": "https://example.invalid/5",
     "description": "现场施工技术管理、方案编制与技术交底。"},
    {"title": "隧道结构工程师", "company": "中交二公院", "location": "武汉",
     "salary": "13-20k", "url": "https://example.invalid/6",
     "description": "隧道支护结构计算与设计。"},
]


def _fake_tavily_search(query: str, limit: int) -> tuple[list[dict[str, str]], int]:
    """替身：返回固定岗位，第二项是「被过滤的栏目页数量」，这里恒为 0。"""
    return [dict(r) for r in _FAKE_RESULTS[:limit]], 0


tools_module._tavily_search = _fake_tavily_search


CITY_HINTS = ["武汉", "宜昌", "十堰", "襄阳", "荆州", "上海", "北京", "深圳",
              "广州", "杭州", "成都", "南京", "西安", "长沙", "重庆"]

STATUS_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("已投", "投了", "投递"), "applied"),
    (("面试", "约面"), "interview"),
    (("录用", "offer", "拿了"), "offer"),
    (("被拒", "拒了", "挂了"), "rejected"),
    (("感兴趣", "想投"), "interested"),
    (("归档", "不看了", "放弃"), "archived"),
    (("新发现", "待看"), "new"),
]

_CALL_COUNTER = itertools.count(1)


def _call(name: str, args: dict[str, Any]) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": f"fake-call-{next(_CALL_COUNTER)}"}],
    )


def _pick_status(text: str) -> str:
    lowered = text.lower()
    for words, status in STATUS_HINTS:
        if any(word in lowered for word in words):
            return status
    return ""


def _extract_keyword(text: str) -> tuple[str, str]:
    """从自然语言里粗略抠出「关键词 + 城市」，够测试用。"""
    city = next((c for c in CITY_HINTS if c in text), "")
    body = text
    for c in CITY_HINTS:
        body = body.replace(c, " ")
    body = re.sub(r"^(帮我|请|麻烦|你|我)+", " ", body)
    body = re.sub(r"(帮我|搜一下|搜索|搜搜|搜|找一下|找找|找|查一下|查查|查|看看|看|推荐|"
                  r"有没有|有哪些|有什么|一些)", " ", body)
    body = re.sub(r"(岗位|职位|工作|招聘信息|招聘|机会|方向|类的)", " ", body)
    body = re.sub(r"[\s,，。、；;：:的了吗呢啊]+", " ", body).strip()
    return " ".join(body.split()[:4]) or "工程", city


class FakeToolCallingModel(BaseChatModel):
    """自测专用假模型：按关键词规则决定调哪个工具，最后把工具结果复述出来。"""

    @property
    def _llm_type(self) -> str:
        return "selftest-fake"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "FakeToolCallingModel":  # noqa: ARG002
        return self  # 假模型不需要真的绑定工具

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,  # noqa: ARG002
        run_manager: Optional[CallbackManagerForLLMRun] = None,  # noqa: ARG002
        **kwargs: Any,  # noqa: ARG002
    ) -> ChatResult:
        # 刚执行完工具 -> 复述结果收尾
        if messages and isinstance(messages[-1], ToolMessage):
            last = messages[-1]
            text = f"【假模型】工具 {last.name} 返回：\n{last.content}"
            return self._result(text)

        user_text = ""
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                user_text = str(msg.content)
                break

        decision = self._route(user_text)
        if decision is not None:
            return self._result("", decision)
        return self._result("【假模型】这句话我没匹配到任何工具，换个说法试试。")

    def _route(self, text: str) -> AIMessage | None:
        lowered = text.lower()

        if any(w in lowered for w in ("改成", "改为", "设为", "标记", "状态")):
            match = re.search(r"#?\s*(\d+)", text)
            status = _pick_status(text)
            if match and status:
                return _call("update_job_status",
                             {"job_id": int(match.group(1)), "status": status, "note": ""})

        if any(w in text for w in ("详情", "时间线")):
            match = re.search(r"#?\s*(\d+)", text)
            if match:
                return _call("get_job_detail", {"job_id": int(match.group(1))})

        if "记住" in text:
            body = re.split(r"记住", text, maxsplit=1)[-1].strip(" ：:，,。")
            key, value = "偏好", body
            for sep in ("：", ":", "=", "是"):
                if sep in body:
                    left, right = body.split(sep, 1)
                    if left.strip() and right.strip():
                        key, value = left.strip(), right.strip()
                        break
            return _call("remember", {"key": key, "value": value})

        if any(w in text for w in ("偏好", "我的要求")):
            return _call("recall_all", {})

        if any(w in text for w in ("搜", "找", "招聘", "有没有", "推荐")):
            keyword, city = _extract_keyword(text)
            return _call("search_jobs", {"keyword": keyword, "city": city, "limit": 5})

        if any(w in text for w in ("列出", "列表", "我找过", "找过哪些", "看过哪些",
                                   "投过", "记录", "记忆")):
            return _call("list_jobs", {"status": _pick_status(text), "limit": 10})

        if any(w in text for w in ("统计", "进展", "多少", "几个")):
            return _call("job_stats", {})

        return None

    @staticmethod
    def _result(text: str, message: AIMessage | None = None) -> ChatResult:
        msg = message or AIMessage(content=text)
        return ChatResult(generations=[ChatGeneration(message=msg)])


# ================================================================ 测试框架
PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [OK]   {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}  {detail}")


def test_store_crud() -> None:
    print("\n[1/8] 记忆库：增 / 查 / 改")
    db = store_module.configure(_TMP / "memory_test.sqlite3")
    db.upsert_job("桥梁结构设计工程师", "中交二航局", "武汉", salary="12-18k", source="test")

    job = db.search("桥梁")[0]
    job_id = job["id"]
    check("写入后能查到", job["title"] == "桥梁结构设计工程师")
    check("初始状态是 new", job["status"] == "new")
    check("首次记录时间已填写", bool(job["first_seen"]))

    updated = db.update_status(job_id, "applied", note="9/29 投递")
    check("状态能改为 applied", updated is not None and updated["status"] == "applied")
    check("备注已写入", updated is not None and "9/29" in updated["note"])

    timeline = db.timeline(job_id)
    check("时间线记录了 2 条以上事件", len(timeline) >= 2, f"实际 {len(timeline)}")

    stats = db.stats()
    check("统计总数 = 1", stats["total"] == 1, str(stats))
    check("统计已投递 = 1", stats["by_status"]["applied"] == 1)

    db.set_profile("目标城市", "武汉")
    check("偏好能读回", db.all_profile().get("目标城市") == "武汉")

    csv_path = db.export_csv(_TMP / "export.csv")
    check("CSV 导出成功且非空", csv_path.is_file() and csv_path.stat().st_size > 20)


def test_store_dedup() -> None:
    print("\n[2/8] 记忆库：同一个岗位不会重复记录")
    db = store_module.get_store()
    before = db.stats()["total"]

    _, created = db.upsert_job("桥梁结构设计工程师", "中交二航局", "武汉",
                               salary="13-19k", source="again")
    check("第二次写入不算新建", created is False)
    check("总数没变（没有重复插入）", db.stats()["total"] == before,
          f"{before} -> {db.stats()['total']}")

    row = db.search("中交二航局")[0]
    check("seen_count 累加为 2 以上", row["seen_count"] >= 2, str(row["seen_count"]))
    check("空缺字段被补全", row["salary"] != "")

    _, created2 = db.upsert_job("完全不同的岗位", "另一家公司", "宜昌")
    check("不同岗位会新建记录", created2 is True)

    # 联网搜索只拿得到网页标题（公司为空），之后补全信息时必须合并，不能变两条
    before_merge = db.stats()["total"]
    id_a, _ = db.upsert_job("某桥梁设计岗位", source="tavily")
    id_b, created_b = db.upsert_job("某桥梁设计岗位", "某某设计院", "武汉",
                                    salary="12-18k", source="manual")
    check("标题相同的岗位会合并成同一条", id_a == id_b and created_b is False,
          f"#{id_a} vs #{id_b}")
    check("合并后总数只 +1", db.stats()["total"] == before_merge + 1,
          f"{before_merge} -> {db.stats()['total']}")
    merged = db.get_job(id_a) or {}
    check("合并后公司被补全", merged.get("company") == "某某设计院", str(merged.get("company")))
    check("合并后城市被补全", merged.get("location") == "武汉", str(merged.get("location")))
    check("合并后出现次数累加", merged.get("seen_count", 0) >= 2, str(merged.get("seen_count")))

    _, created_c = db.upsert_job("某桥梁设计岗位", "某某设计院", "武汉", source="manual")
    check("补全信息后再存不算新建（精确去重生效）", created_c is False)
    check("总数没有继续增长", db.stats()["total"] == before_merge + 1)

    columns = {r["name"] for r in db._conn.execute("PRAGMA table_info(jobs)").fetchall()}  # noqa: SLF001
    check("数据库带了 title_norm 列（老库可平滑升级）", "title_norm" in columns, str(columns))


def test_web_parsing() -> None:
    print("\n[3/8] 网页结果清洗（联网只给标题和摘要，字段得自己抠）")
    t = tools_module

    raw = "桥梁设计师招聘_武汉理工大设计研究院有限公司 - 建筑英才网"
    company = t.guess_company(raw)
    check("能从脏标题里抽出公司名", company == "武汉理工大设计研究院有限公司", company)
    cleaned = t.clean_web_title(raw, company)
    check("标题清洗成干净职位名", cleaned == "桥梁设计师", cleaned)
    check("能从「正在招聘」句子里抽公司名",
          t.guess_company("中卓国际建筑设计有限公司正在招聘桥梁设计工程师(武汉)")
          == "中卓国际建筑设计有限公司")
    check("能从摘要里抽城市", t.guess_city("工作地区： 湖北-武汉市 · 学历要求：本科") == "武汉")
    check("能从摘要里抽薪资", t.guess_salary("桥梁设计师 · 5-10K · 本科") == "5-10K")
    check("纯噪音标题会被丢弃（不污染记忆库）", t.clean_web_title("招聘职位") == "",
          repr(t.clean_web_title("招聘职位")))
    check("无公司名时标题不受影响", t.clean_web_title("道路设计师") == "道路设计师")
    check("标题开头的城市名会被去掉",
          t.clean_web_title("武汉桥梁设计师招聘 · 某某科技有限公司") == "桥梁设计师",
          t.clean_web_title("武汉桥梁设计师招聘 · 某某科技有限公司"))
    check("去掉被截断的半个括号",
          t.clean_web_title("桥梁设计工程师(武汉") == "桥梁设计工程师",
          t.clean_web_title("桥梁设计工程师(武汉"))
    check("网站首页标题被判为非岗位（会被过滤）",
          not t.looks_like_job_title("人才")
          and not t.looks_like_job_title("建筑英才网 建筑行业英才网 汇聚建筑英才"))
    check("正常岗位标题能通过筛选",
          t.looks_like_job_title("桥梁设计工程师") and t.looks_like_job_title("道路设计师"))


def test_tools() -> None:
    print("\n[4/8] 工具：模型能调的那些函数")
    db = store_module.get_store()
    before = db.stats()["total"]

    out = tools_module.search_jobs.invoke({"keyword": "桥梁", "city": "武汉", "limit": 3})
    check("search_jobs 返回文字", isinstance(out, str) and len(out) > 20)
    check("搜索结果自动入库", db.stats()["total"] > before, f"{before} -> {db.stats()['total']}")
    check("返回值带 job_id 标记", "[#" in out)

    # 换回真实搜索：确认「搜不了」的时候给的是可读提示，而不是崩掉或静默返回空
    tools_module._tavily_search = _REAL_TAVILY_SEARCH
    try:
        failed = tools_module.search_jobs.invoke({"keyword": "桥梁", "limit": 3})
        check("搜索不可用时报错可读（不崩、不静默返回空）",
              "搜索失败" in failed and "TAVILY_API_KEY" in failed,
              failed[:90].replace("\n", " "))
    finally:
        tools_module._tavily_search = _fake_tavily_search

    listed = tools_module.list_jobs.invoke({"status": "", "limit": 5})
    check("list_jobs 能列出岗位", "[#" in listed)

    detail = tools_module.get_job_detail.invoke({"job_id": 1})
    check("get_job_detail 能取详情", "时间线" in detail)

    bad = tools_module.get_job_detail.invoke({"job_id": 99999})
    check("查询不存在的编号会给出提示", "没有" in bad)

    upd = tools_module.update_job_status.invoke(
        {"job_id": 1, "status": "interview", "note": "已约面"})
    check("update_job_status 生效", "面试" in upd or "interview" in upd)

    invalid = tools_module.update_job_status.invoke({"job_id": 1, "status": "瞎写"})
    check("非法状态被拦住", "无效" in invalid)

    found = tools_module.search_saved_jobs.invoke({"query": "中交"})
    check("search_saved_jobs 能在记忆里搜", "[#" in found)

    stats_text = tools_module.job_stats.invoke({})
    check("job_stats 有输出", "共记录" in stats_text)

    tools_module.remember.invoke({"key": "薪资底线", "value": "12k"})
    check("remember 能写偏好", "12k" in tools_module.recall_all.invoke({}))


def test_graph_structure() -> None:
    print("\n[5/8] LangGraph：图结构 + 配置校验")
    settings = graph_module.load_settings()

    # 造模型需要 Key，这里临时塞一个假的——只构造对象、不发请求，不会联网
    os.environ["DEEPSEEK_API_KEY"] = "sk-selftest-fake-not-a-real-key"
    try:
        llm = graph_module.build_llm(graph_module.load_settings())
        check("能按配置构造出模型对象", llm is not None, type(llm).__name__)
    except Exception as exc:
        check("能按配置构造出模型对象", False, f"{type(exc).__name__}: {exc}")
    finally:
        os.environ.pop("DEEPSEEK_API_KEY", None)

    check("自测里读不到任何 Key（保证不联网、不花钱）",
          not settings.api_key and not settings.tavily_api_key,
          f"api_key={bool(settings.api_key)} tavily_key={bool(settings.tavily_api_key)}")

    # ---- 配置来源诊断：用户「改了配置不生效」时靠它定位 ----
    probe = "__SELFTEST_PROBE__"
    os.environ[probe] = "from-env"
    config_module._FILE_ENV[probe] = "from-file"
    check("来源诊断：环境变量优先于 .env",
          config_module._source_of(probe)[1] == f"系统环境变量 {probe}",
          config_module._source_of(probe)[1])
    del os.environ[probe]
    check("来源诊断：能认出 .env 文件",
          config_module._source_of(probe)[1] == f".env 文件 {probe}",
          config_module._source_of(probe)[1])
    del config_module._FILE_ENV[probe]
    check("来源诊断：都没配时标为默认值",
          config_module._source_of(probe)[1] == "默认值（没配置过）",
          config_module._source_of(probe)[1])
    check("Key 脱敏只露开头 6 位 + 长度",
          config_module._masked("sk-abcdef1234567890").startswith("sk-abc"),
          config_module._masked("sk-abcdef1234567890"))

    app = graph_module.build_agent(settings, llm=FakeToolCallingModel())
    node_names = set(app.get_graph().nodes)
    check("有 agent 节点", "agent" in node_names, str(node_names))
    check("有 tools 节点", "tools" in node_names, str(node_names))
    check("注册了 10 个工具", len(tools_module.ALL_TOOLS) == 10, str(len(tools_module.ALL_TOOLS)))
    check("对话检查点用 sqlite（重启不丢）", graph_module.CHECKPOINTER_KIND == "sqlite",
          f"实际 {graph_module.CHECKPOINTER_KIND}（缺 langgraph-checkpoint-sqlite 时会退回内存）")
    return app


def test_graph_loop(app) -> None:
    print("\n[6/8] LangGraph：agent -> tools -> agent 完整回路")
    config = graph_module.new_config("selftest-1")
    db = store_module.get_store()
    before = db.stats()["total"]

    result = app.invoke({"messages": [HumanMessage(content="帮我搜一下武汉的桥梁设计岗位")]}, config)
    messages = result["messages"]

    tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
    ai_msgs = [m for m in messages if isinstance(m, AIMessage)]

    check("产生了工具调用", any(getattr(m, "tool_calls", None) for m in ai_msgs))
    check("工具真的执行了", len(tool_msgs) >= 1, f"ToolMessage 数 = {len(tool_msgs)}")
    check("工具执行结果回灌给模型并收尾", bool(str(ai_msgs[-1].content).strip()))
    check("这轮搜索写进了记忆库", db.stats()["total"] > before)

    ai_tool_calls = [tc for m in ai_msgs for tc in (getattr(m, "tool_calls", None) or [])]
    check("调用的是 search_jobs", any(tc["name"] == "search_jobs" for tc in ai_tool_calls),
          str([tc["name"] for tc in ai_tool_calls]))


def test_conversation_memory(app) -> None:
    print("\n[7/8] 对话记忆：同一个 thread 能接着聊")
    config = graph_module.new_config("selftest-2")

    app.invoke({"messages": [HumanMessage(content="帮我搜一下宜昌的施工技术员岗位")]}, config)
    first_len = len(graph_module.history_of(app, "selftest-2"))

    app.invoke({"messages": [HumanMessage(content="统计一下我的求职进展")]}, config)
    second_len = len(graph_module.history_of(app, "selftest-2"))

    check("第一轮有历史记录", first_len > 0, str(first_len))
    check("第二轮历史继续累积", second_len > first_len, f"{first_len} -> {second_len}")

    other = graph_module.history_of(app, "completely-new-thread")
    check("新 thread 是空白（互不串台）", len(other) == 0, str(len(other)))


def test_forget() -> None:
    print("\n[8/8] 清除记忆：分范围 / 先备份 / 需二次确认")
    db = store_module.get_store()

    # ---- 范围别名 ----
    check("范围别名认得出来（中文 / 英文都行）",
          forget_module.normalize_scope("岗位") == "jobs"
          and forget_module.normalize_scope("对话记忆") == "conversations"
          and forget_module.normalize_scope("all") == "all",
          forget_module.normalize_scope("对话记忆"))
    check("乱输入被挡住（不会误当成 jobs 删掉）",
          forget_module.normalize_scope("随便写点啥") == "")

    # ---- 最重要的一条：没确认时一个字都不能删 ----
    before = db.stats()["total"]
    hint = tools_module.clear_memory.invoke({"scope": "jobs"})
    check("不带 confirm 时只返回清单、不真删",
          db.stats()["total"] == before and "不可逆" in hint,
          f"库内 {before} -> {db.stats()['total']}")
    check("清单里如实报了条数", f"{before} 条" in hint, hint.replace("\n", " ")[:90])

    # ---- 确认之后才删，并且留下备份 ----
    backup_root = forget_module.backup_dir()
    backups_before = len(list(backup_root.glob("*.json"))) if backup_root.is_dir() else 0

    result = forget_module.forget("jobs")
    check("确认后岗位记忆被清空", db.stats()["total"] == 0, str(db.stats()["total"]))
    check("操作记录（events）一起被清", result.deleted.get("events", 0) > 0, str(result.deleted))
    check("备份文件确实生成了",
          result.backup_path is not None and result.backup_path.is_file(),
          str(result.backup_path))
    if result.backup_path and result.backup_path.is_file():
        payload = json.loads(result.backup_path.read_text(encoding="utf-8"))
        check("备份里真的有删掉的内容", bool(payload.get("jobs")),
              f"{len(payload.get('jobs', []))} 条")
        check("新备份没覆盖旧的",
              len(list(backup_root.glob("*.json"))) == backups_before + 1)

    # 自增 id 必须归零，否则清空后第一条岗位会是 #57
    new_id, created = db.upsert_job("清空后的第一个岗位", "测试公司", "武汉")
    check("清空后 id 从 #1 重新开始", new_id == 1 and created, f"#{new_id}")

    # ---- 范围要互相独立：清岗位不该动偏好 ----
    db.set_profile("目标城市", "武汉")
    db.clear("jobs")
    check("清岗位不影响长期偏好", db.all_profile().get("目标城市") == "武汉")
    expected = len(db.all_profile())
    dropped = forget_module.forget("profile")
    check("能单独清偏好（清掉的条数对得上）",
          not db.all_profile() and dropped.deleted.get("profile") == expected,
          f"删了 {dropped.deleted.get('profile')}，删前有 {expected}")

    # ---- 对话记忆：临时造个小库，验证 SQL 本身没问题 ----
    fake = _TMP / "forget_ckpt.sqlite3"
    conn = sqlite3.connect(str(fake))
    conn.executescript(
        "CREATE TABLE checkpoints (thread_id TEXT, checkpoint_id TEXT, payload BLOB);"
        "CREATE TABLE writes (thread_id TEXT, idx INTEGER, payload BLOB);"
        "INSERT INTO checkpoints VALUES ('t1','c1',x'00');"
        "INSERT INTO checkpoints VALUES ('t1','c2',x'00');"
        "INSERT INTO writes VALUES ('t1',0,x'00');"
    )
    conn.commit()
    conn.close()
    check("能数出对话段数（2 个检查点算同 1 段）",
          forget_module.count_conversations(fake) == 1,
          str(forget_module.count_conversations(fake)))
    counts = forget_module.clear_conversations(fake)
    check("对话库被清空",
          counts.get("checkpoints") == 2 and counts.get("writes") == 1, str(counts))
    check("清空后段数归零", forget_module.count_conversations(fake) == 0)
    check("库不存在时也不报错",
          forget_module.count_conversations(_TMP / "nope.sqlite3") == 0
          and forget_module.clear_conversations(_TMP / "nope.sqlite3") == {})


def main() -> int:
    print("=" * 64)
    print(f"  job_agent 离线自测｜临时目录 {_TMP}")
    print("=" * 64)
    try:
        test_store_crud()
        test_store_dedup()
        test_web_parsing()
        test_tools()
        app = test_graph_structure()
        test_graph_loop(app)
        test_conversation_memory(app)
        test_forget()          # 放最后：它会把记忆库清空
    except Exception:
        print("\n[异常] 测试中断：")
        traceback.print_exc()
        FAILED.append("未捕获异常")

    print("\n" + "=" * 64)
    print(f"  通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        for name in FAILED:
            print(f"    - {name}")
        print("=" * 64)
        return 1
    print("  全部通过，可以放心运行： python -m job_agent")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
