"""交给模型的工具集。

工具返回的都是「人类也能读懂的文字」，顺带带上 job_id，
这样模型下一次讲话时能准确引用某一岗位，而不是瞎编编号。
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.tools import tool

from .store import STATUSES, STATUS_LABELS, format_job_line, get_store

# ---------------------------------------------------------------- 配置读取
def _settings():
    from .config import load_settings
    return load_settings()


# ---------------------------------------------------------------- 网页结果清洗
# 联网搜到的只是网页标题，很脏（例：桥梁设计师招聘_武汉理工大设计研究院有限公司 - 建筑英才网）。
# 下面几个小函数负责把「职位 / 公司 / 城市 / 薪资」从标题和摘要里抠出来，
# 抠不准也没关系——之后用 save_job 补全会合并到同一条记录，不会重复。
_SPLIT_RE = re.compile(r"[_\-|｜、·]|--")
_UNCLOSED_RE = re.compile(r"[（(【\[]+[^（()）【】\[\]]*$")
_CITY_KEYWORDS = (
    "武汉", "宜昌", "十堰", "襄阳", "荆州", "黄石", "孝感", "咸宁", "荆门",
    "上海", "北京", "广州", "深圳", "杭州", "成都", "重庆", "南京", "西安",
    "长沙", "郑州", "合肥", "南昌", "天津", "苏州", "青岛", "济南", "福州",
)
# 直接匹配「xx有限公司 / xx设计院 / xx工程局」这类公司名尾巴。
# 用贪婪量词 + 左侧边界：贪婪保证拿到完整公司名（否则「武汉理工大设计研究院有限公司」
# 会被截成「武汉理工大设计研究院」），左侧边界避免从某个词的中间开始截。
_COMPANY_RE = re.compile(
    r"(?:^|[^\u4e00-\u9fffA-Za-z0-9（）()])"
    r"([\u4e00-\u9fffA-Za-z0-9（）()]{2,22}"
    r"(?:股份有限公司|有限责任公司|有限公司|设计研究院|设计院|研究院|工程局|事务所|集团|公司))"
)
_COMPANY_REJECT = ("招聘", "求职", "应聘", "岗位", "职位", "正在")
_TITLE_NOISE_RE = re.compile(
    r"(正在招聘|正在|诚聘|急聘|热招|高薪|双休|五险一金|招聘信息汇总|招聘信息|招聘|求职|找工作|应届生|人才网|职位|岗位)"
)
# 尾巴上的站点名（有些网页标题不写分隔符，直接接一个「网」）
_SITE_TAIL_RE = re.compile(
    r"[-_|｜]?\s*(招聘网|建筑英才网|英才网|高校人才网|人才网|应届生网|猎聘网|猎聘|智联招聘|"
    r"BOSS直聘|前程无忧|58同城|官网|网站|网)\s*$"
)
_SALARY_RE = re.compile(
    r"(\d+(?:\.\d+)?\s*[-~至]\s*\d+(?:\.\d+)?\s*(?:k|K|万(?:/月|/年)?|元/月|元/年))"
)
# 岗位名一般会带这些词。用来把「网站首页 / 列表页」这类噪音结果挡在记忆库之外。
_JOB_WORDS = (
    "工程师", "设计师", "技术员", "经理", "主管", "专员", "助理", "实习生", "负责人",
    "总监", "总工", "施工员", "预算", "造价", "监理", "资料员", "测量", "检测",
    "安全员", "招聘", "岗位", "设计", "施工", "技术", "管理", "教师", "研究员",
)


def looks_like_job_title(title: str) -> bool:
    """粗略判断清理后的标题是不是一个具体岗位（而不是网站首页/栏目页）。"""
    title = (title or "").strip()
    if len(title) < 3:
        return False
    return any(word in title for word in _JOB_WORDS)


def _segments(raw: str) -> list[str]:
    return [p.strip() for p in _SPLIT_RE.split(raw or "") if p.strip()]


def guess_company(text: str) -> str:
    """从文本里抠公司名。找不到返回空字符串。"""
    for match in _COMPANY_RE.finditer(text or ""):
        cand = match.group(1).strip()
        if len(cand) < 4 or any(bad in cand for bad in _COMPANY_REJECT):
            continue
        return cand[:40]
    return ""


def clean_web_title(raw: str, company: str = "") -> str:
    """把脏标题压成「职位名」：去掉公司名、站点名和招聘噪音词。

    清理后什么都不剩就返回空串，交给调用方丢弃这条结果
    （说明那个网页的标题纯属噪音，比如「招聘职位」这种列表页）。
    """
    parts = _segments(raw)
    head = parts[0] if parts else (raw or "").strip()
    if company and company in head:
        head = head.replace(company, "")
    head = _SITE_TAIL_RE.sub("", head)
    head = _TITLE_NOISE_RE.sub("", head)
    head = _SITE_TAIL_RE.sub("", head)
    head = head.strip("（）()【】[] ,，。·、-_")
    for city in _CITY_KEYWORDS:  # 去掉开头残留的城市名：「武汉桥梁设计师」->「桥梁设计师」
        if head.startswith(city) and len(head) > len(city) + 1:
            head = head[len(city):].strip()
            break
    head = _UNCLOSED_RE.sub("", head).strip("（）()【】[] ,，。·、-_")  # 去掉截断的半个括号
    return head[:40]


def guess_city(text: str) -> str:
    """先找「工作地区：湖北-武汉市」这类显式字段，找不到就在全文里匹配城市名。"""
    match = re.search(
        r"(?:工作地区|工作地点|工作城市|所在地区|地区|城市)[：:\s]*([^\s，,。；;、]{2,20})",
        text or "",
    )
    if match:
        seg = match.group(1)
        for city in _CITY_KEYWORDS:
            if city in seg:
                return city
        return seg[:12]
    for city in _CITY_KEYWORDS:
        if city in (text or ""):
            return city
    return ""


def guess_salary(text: str) -> str:
    match = _SALARY_RE.search(text or "")
    return match.group(1).replace(" ", "") if match else ""


def _tavily_search(query: str, limit: int) -> tuple[list[dict[str, str]], int]:
    """用 Tavily 做联网搜索。返回 (岗位列表, 被过滤掉的非岗位页数量)。

    网页结果只有标题和摘要，所以这里要顺手做一次清洗：
    先抠公司名，再用公司名把标题里的公司前缀去掉，得到干净的职位名。
    最后用 looks_like_job_title 把网站首页 / 栏目页这类噪音挡掉。
    """
    from tavily import TavilyClient  # 没装就 ImportError，由调用方处理

    key = _settings().tavily_api_key
    if not key:
        raise RuntimeError("未配置 TAVILY_API_KEY")
    client = TavilyClient(api_key=key)
    resp = client.search(query=query, max_results=limit, search_depth="basic")
    items: list[dict[str, str]] = []
    skipped = 0
    for r in resp.get("results", [])[:limit]:
        raw_title = (r.get("title") or "").strip()
        if not raw_title:
            continue
        content = (r.get("content") or "").strip()
        blob = f"{raw_title} {content}"
        company = guess_company(raw_title) or guess_company(content)
        clean_title = clean_web_title(raw_title, company)
        if not looks_like_job_title(clean_title):
            skipped += 1  # 网站首页 / 栏目页，不进记忆库
            continue
        items.append({
            "title": clean_title,
            "company": company,
            "location": guess_city(blob),
            "salary": guess_salary(blob),
            "url": r.get("url", ""),
            "description": content[:400],
        })
    return items, skipped


# ---------------------------------------------------------------- 工具
@tool
def search_jobs(keyword: str, city: str = "", limit: int = 5) -> str:
    """找工作：按关键词搜索招聘岗位（可限定城市）。

    所有搜到的岗位会自动存进长期记忆库并分配 job_id，
    所以「找过的工作」不需要用户再说一遍就能查回来。

    Args:
        keyword: 岗位关键词，例如「桥梁 设计」「BIM」「施工 技术员」。
        city: 城市，例如「武汉」。留空表示不限。
        limit: 返回条数，默认 5，最多 10。
    """
    limit = max(1, min(int(limit or 5), 10))
    query = f"{keyword} {city} 招聘".strip()

    try:
        # 多要几条，因为网站首页 / 栏目页会被过滤掉
        jobs, skipped = _tavily_search(query, min(limit + 4, 10))
    except Exception as exc:
        return (
            f"搜索失败（{type(exc).__name__}: {exc}）\n"
            "      岗位搜索依赖 Tavily 联网。跑 python -m job_agent.config "
            "可以确认 TAVILY_API_KEY 实际读的是哪一份配置。"
        )

    if not jobs:
        tail = f"（本次 {skipped} 条结果被判定为网站栏目页，已过滤）" if skipped else ""
        return (
            f"搜索「{keyword}」{(' 城市：' + city) if city else ''} 没找到具体的岗位信息{tail}。\n"
            "      换个关键词，或者去掉城市限制再试。"
        )
    jobs = jobs[:limit]

    store = get_store()
    lines = []
    for item in jobs:
        job_id, created = store.upsert_job(
            title=item.get("title", ""),
            company=item.get("company", ""),
            location=item.get("location", ""),
            salary=item.get("salary", ""),
            url=item.get("url", ""),
            description=item.get("description", ""),
            source="tavily",
        )
        job = store.get_job(job_id) or {}
        tag = "新增" if created else "记忆中已有（已合并、出现次数 +1）"
        lines.append(f"{format_job_line(job)}\n      说明：{item.get('description', '')[:120]}\n      记忆操作：{tag}")

    header = f"搜索「{keyword}」{(' 城市：' + city) if city else ''}，共 {len(lines)} 条，已全部写入记忆库：\n"
    note = ("\n已过滤 " + str(skipped) + " 条非岗位页面（网站首页 / 栏目页）。" if skipped else "")
    note += (
        "\n提示：联网结果的公司 / 城市 / 薪资是按网页标题和摘要推断的，可能不全或不准。"
        "跟用户确认后请用 save_job 把准确信息补上——会自动合并到同一条记录，不会产生重复条目。"
    )
    return header + "\n".join(lines) + note


@tool
def save_job(title: str, company: str = "", location: str = "", salary: str = "",
             url: str = "", status: str = "interested", note: str = "") -> str:
    """手动记住一个岗位（用户从别处看到的、朋友推荐的、招聘网站上的都可以）。

    如果记忆库里已有同一岗位，不会重复添加，只会更新空缺信息并累加出现次数。

    Args:
        title: 岗位名称。
        company: 公司名称。
        location: 城市。
        salary: 薪资。
        url: 岗位链接。
        status: 初始状态，取值 new/interested/applied/interview/offer/rejected/archived。
        note: 备注，例如「内推」「要求一注结构」。
    """
    job_id, created = get_store().upsert_job(
        title=title, company=company, location=location, salary=salary,
        url=url, source="manual", status=status, note=note,
    )
    job = get_store().get_job(job_id) or {}
    action = "已新增" if created else "记忆库中已存在，已更新信息（未重复添加）"
    return f"{action}：\n{format_job_line(job, verbose=True)}"


@tool
def list_jobs(status: str = "", limit: int = 10) -> str:
    """列出记忆库里的岗位（最近的在前），这是「我找过哪些工作」的主要出口。

    Args:
        status: 只看某个状态，取值 new/interested/applied/interview/offer/rejected/archived。
                留空则列出全部。
        limit: 最多返回几条，默认 10。
    """
    status = (status or "").strip()
    if status and status not in STATUSES:
        return f"状态「{status}」不认识。可选：{'、'.join(STATUSES)}"
    jobs = get_store().list_jobs(status=status or None, limit=max(1, min(int(limit or 10), 50)))
    if not jobs:
        return "记忆库里还没有岗位记录。可以先让我搜一搜，或者告诉我一个岗位名称。"
    where = f"（状态：{STATUS_LABELS.get(status, status)}）" if status else "（全部）"
    body = "\n".join(format_job_line(j) for j in jobs)
    return f"记忆库内的岗位{where}，共 {len(jobs)} 条：\n{body}"


@tool
def get_job_detail(job_id: int) -> str:
    """查看某个岗位的完整信息，以及它的历史时间线（何时发现、何时投递等）。

    Args:
        job_id: 岗位编号（就是列表里 [#数字] 的那个数字）。
    """
    store = get_store()
    job = store.get_job(int(job_id))
    if job is None:
        return f"记忆库里没有 #{job_id} 这个岗位。可以先用 list_jobs 或 search_saved_jobs 查一下编号。"
    lines = [
        f"#{job['id']} {job['title']}",
        f"  公司：{job['company'] or '未知'}",
        f"  城市：{job['location'] or '未知'}",
        f"  薪资：{job['salary'] or '未注明'}",
        f"  状态：{STATUS_LABELS.get(job['status'], job['status'])}",
        f"  备注：{job['note'] or '无'}",
        f"  链接：{job['url'] or '无'}",
        f"  来源：{job['source']}｜首次记录 {job['first_seen']}｜最近出现 {job['last_seen']}｜共出现 {job['seen_count']} 次",
    ]
    if job["description"]:
        lines.append(f"  描述：{job['description'][:300]}")
    events = store.timeline(int(job_id), limit=15)
    if events:
        lines.append("  时间线（新->旧）：")
        lines.extend(f"    {e['ts']}  [{e['kind']}]  {e['detail']}" for e in events)
    return "\n".join(lines)


@tool
def update_job_status(job_id: int, status: str, note: str = "") -> str:
    """更新某个岗位的进展状态（投了、约面了、被拒了……）。

    Args:
        job_id: 岗位编号。
        status: 新状态，必须是 new/interested/applied/interview/offer/rejected/archived 之一。
        note: 可选备注，例如「9/29 投递，等笔试」。
    """
    status = (status or "").strip()
    if status not in STATUSES:
        return f"状态「{status}」无效。可选：{'、'.join(STATUSES)}"
    job = get_store().update_status(int(job_id), status, note or None)
    if job is None:
        return f"更新失败：记忆库里没有 #{job_id} 这个岗位。"
    return f"已更新：\n{format_job_line(job, verbose=True)}"


@tool
def search_saved_jobs(query: str = "", status: str = "") -> str:
    """在「自己的记忆库」里搜索（职位、公司、城市、备注、描述都能匹配）。

    用户问「我之前是不是看过 XX」「有没有武汉的岗位」时用这个，不要凭印象编。

    Args:
        query: 关键词。
        status: 可选，限定状态。
    """
    jobs = get_store().search(query=query, status=(status or None),
                             limit=20)
    if not jobs:
        hint = f"「{query}」" if query else "该条件下"
        return f"记忆库里暂时没有匹配 {hint} 的岗位记录。"
    body = "\n".join(format_job_line(j) for j in jobs)
    return f"在记忆库里找到 {len(jobs)} 条：\n{body}"


@tool
def job_stats() -> str:
    """统计自己的求职进展：共记录多少岗位、各状态数量、最近 7 天新增多少。"""
    store = get_store()
    s = store.stats()
    if s["total"] == 0:
        return "记忆库是空的，还没有记录任何岗位。"
    parts = [f"共记录 {s['total']} 个岗位，最近 7 天新增 {s['new_last_7_days']} 个。"]
    for status, count in s["by_status"].items():
        if count:
            parts.append(f"{STATUS_LABELS.get(status, status)}：{count}")
    profile = store.all_profile()
    if profile:
        parts.append("长期偏好：" + "；".join(f"{k}={v}" for k, v in profile.items()))
    return "\n".join(parts)


@tool
def remember(key: str, value: str) -> str:
    """记住用户的一条长期偏好（跨对话有效），例如目标城市、薪资底线、不考虑的公司。

    Args:
        key: 偏好名称，例如「目标城市」。
        value: 偏好内容，例如「武汉，可接受宜昌出差」。
    """
    get_store().set_profile(key, value)
    return f"已记住：{key} = {value}"


@tool
def recall_all() -> str:
    """查看已经记住的全部长期偏好。"""
    profile = get_store().all_profile()
    if not profile:
        return "目前还没有记录任何长期偏好。"
    return "已记住的长期偏好：\n" + "\n".join(f"- {k}：{v}" for k, v in profile.items())


@tool
def clear_memory(scope: str = "jobs", confirm: bool = False) -> str:
    """清空记忆（**危险、不可逆**，必须先拿到用户的明确同意）。

    什么时候用：用户主动说「把记忆清掉」「忘掉之前搜的岗位」「重新开始」这类话。
    用户只是抱怨记错了、或没提过要清空时，**不要**调用本工具。

    调用方式分两步，不要合并：
      第一步  confirm 留空 -> 返回「将要删掉什么」的清单；
      第二步  把清单念给用户，等他说「确认」/「删吧」，再带 confirm=True 调一次。

    Args:
        scope: 清哪个范围。jobs=岗位记忆 / profile=长期偏好 /
            conversations=对话记忆 / all=全部。
        confirm: 用户明确同意后才传 True。
    """
    from .forget import SCOPE_HINT, count_conversations, describe, forget, normalize_scope

    key = normalize_scope(scope)
    if not key:
        return f"范围「{scope}」不认识。可选：{SCOPE_HINT}"

    if not confirm:
        store = get_store()
        lines = [f"将要清空：{describe(key)}——这是不可逆操作。"]
        if key in ("jobs", "all"):
            lines.append(
                f"  - 岗位 {store.count_rows('jobs')} 条、操作记录 {store.count_rows('events')} 条"
            )
        if key in ("profile", "all"):
            lines.append(f"  - 长期偏好 {store.count_rows('profile')} 条")
        if key in ("conversations", "all"):
            from .config import load_settings

            lines.append(f"  - 对话 {count_conversations(load_settings().checkpoint_path)} 段")
        lines.append("")
        lines.append("把上面这些如实告诉用户，等他说「确认删除」之后，再带 confirm=True 调一次。")
        lines.append("（执行前会自动备份到 data/backup/，但那只是保险，不构成随便删的理由。）")
        return "\n".join(lines)

    return "\n".join(forget(key).summary_lines())


ALL_TOOLS: list[Any] = [
    search_jobs,
    save_job,
    list_jobs,
    get_job_detail,
    update_job_status,
    search_saved_jobs,
    job_stats,
    remember,
    recall_all,
    clear_memory,
]
