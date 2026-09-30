"""长期记忆层：用 SQLite 把「找过的岗位」永久存下来。

设计要点
--------
1. 去重：岗位用 fingerprint（公司 + 职位 + 城市 归一化后的字符串）当唯一键。
   同一个岗位被搜到第二次，只把 seen_count +1、更新 last_seen，不会重复插入。
2. 可回溯：每次变化都往 events 表写一条，形成时间线
   （什么时候看到的、什么时候投的、什么时候面试的）。
3. 零依赖：只用标准库 sqlite3，不装东西也能跑。
4. 偏好记忆：profile 表存键值对（比如 目标城市=武汉、最低薪资=15k），
   模型可以用 remember / recall 工具读写。
"""

from __future__ import annotations

import csv
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 常量
STATUSES = ("new", "interested", "applied", "interview", "offer", "rejected", "archived")

STATUS_LABELS: dict[str, str] = {
    "new": "新发现",
    "interested": "感兴趣",
    "applied": "已投递",
    "interview": "面试中",
    "offer": "已录用",
    "rejected": "已拒/被拒",
    "archived": "已归档",
}

SHORT_LABELS: dict[str, str] = {
    "new": "新",
    "interested": "意",
    "applied": "投",
    "interview": "面",
    "offer": "聘",
    "rejected": "拒",
    "archived": "档",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint  TEXT    NOT NULL UNIQUE,
    title        TEXT    NOT NULL,
    title_norm   TEXT    NOT NULL DEFAULT '',
    company      TEXT    NOT NULL DEFAULT '',
    location     TEXT    NOT NULL DEFAULT '',
    salary       TEXT    NOT NULL DEFAULT '',
    url          TEXT    NOT NULL DEFAULT '',
    source       TEXT    NOT NULL DEFAULT 'manual',
    description  TEXT    NOT NULL DEFAULT '',
    status       TEXT    NOT NULL DEFAULT 'new',
    note         TEXT    NOT NULL DEFAULT '',
    first_seen   TEXT    NOT NULL,
    last_seen    TEXT    NOT NULL,
    seen_count   INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_jobs_status     ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_last_seen  ON jobs(last_seen);
CREATE INDEX IF NOT EXISTS idx_jobs_title_norm ON jobs(title_norm);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  INTEGER,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    detail  TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id);

CREATE TABLE IF NOT EXISTS profile (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def make_fingerprint(title: str, company: str = "", location: str = "") -> str:
    """把公司/职位/城市归一化成去重键：只保留中文、字母、数字（小写）。"""
    raw = f"{company}|{title}|{location}".lower()
    cleaned = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", raw)
    return cleaned or raw.strip()


class JobStore:
    """岗位记忆库。所有工具、CLI 都通过它读写数据。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # check_same_thread=False：允许 LangGraph 的工具节点在别的线程里调用
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            self._migrate()

    def _migrate(self) -> None:
        """老数据库平滑升级：补上后加的列，避免用户删库重建。"""
        with self._lock:
            columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(jobs)")}
            if "title_norm" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN title_norm TEXT NOT NULL DEFAULT ''")
                self._conn.commit()
            rows = self._conn.execute(
                "SELECT id, title FROM jobs WHERE title_norm = '' OR title_norm IS NULL"
            ).fetchall()
            for row in rows:
                self._conn.execute(
                    "UPDATE jobs SET title_norm = ? WHERE id = ?",
                    (make_fingerprint(row["title"]), int(row["id"])),
                )
            if rows:
                self._conn.commit()

    # ------------------------------------------------------------ 基础
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    # ------------------------------------------------------------ 写入
    def upsert_job(
        self,
        title: str,
        company: str = "",
        location: str = "",
        salary: str = "",
        url: str = "",
        source: str = "manual",
        description: str = "",
        status: str = "new",
        note: str = "",
    ) -> tuple[int, bool]:
        """按 fingerprint 去重写入。返回 (job_id, 是否新建)。

        去重分两级，这是「履历不会乱」的关键：
          1. 精确命中 fingerprint（公司 + 职位 + 城市）-> 更新，seen_count +1；
          2. 精确没命中，但「职位标题」相同 -> 合并到已有那条。
             典型场景：联网搜到的结果只有网页标题（公司为空），之后用 save_job
             把公司和城市补全时，会落到同一条记录上，而不是变成两条。
        """
        title = (title or "").strip() or "未命名岗位"
        company = (company or "").strip()
        location = (location or "").strip()
        salary = (salary or "").strip()
        url = (url or "").strip()
        description = (description or "").strip()
        note = (note or "").strip()
        if status not in STATUSES:
            status = "new"

        fp = make_fingerprint(title, company, location)
        title_norm = make_fingerprint(title)
        now = _now()

        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE fingerprint = ?", (fp,)
            ).fetchone()

            merged = False
            if row is None and title_norm:
                row = self._conn.execute(
                    """SELECT * FROM jobs WHERE title_norm = ?
                       ORDER BY (company <> '') DESC, (location <> '') DESC, seen_count DESC, id
                       LIMIT 1""",
                    (title_norm,),
                ).fetchone()
                merged = row is not None

            if row is None:
                cur = self._conn.execute(
                    """INSERT INTO jobs (fingerprint, title, title_norm, company, location, salary, url,
                                         source, description, status, note,
                                         first_seen, last_seen, seen_count)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                    (fp, title, title_norm, company, location, salary, url,
                     source, description, status, note, now, now),
                )
                job_id = int(cur.lastrowid)
                self._conn.execute(
                    "INSERT INTO events (job_id, ts, kind, detail) VALUES (?,?,?,?)",
                    (job_id, now, "created", f"首次记录（来源：{source}）"),
                )
                self._conn.commit()
                return job_id, True

            job_id = int(row["id"])

            # 合并时如果这次带的信息更全，把去重键升级成更精确的那个
            new_fp = row["fingerprint"]
            if merged and (company or location) and fp != row["fingerprint"]:
                taken = self._conn.execute(
                    "SELECT id FROM jobs WHERE fingerprint = ?", (fp,)
                ).fetchone()
                if taken is None:
                    new_fp = fp

            self._conn.execute(
                """UPDATE jobs SET fingerprint = ?, last_seen = ?, seen_count = seen_count + 1,
                          title    = CASE WHEN ? <> '' THEN ? ELSE title    END,
                          company  = CASE WHEN ? <> '' THEN ? ELSE company  END,
                          location = CASE WHEN ? <> '' THEN ? ELSE location END,
                          salary   = CASE WHEN ? <> '' THEN ? ELSE salary   END,
                          url      = CASE WHEN ? <> '' THEN ? ELSE url      END,
                          description = CASE WHEN ? <> '' THEN ? ELSE description END
                   WHERE id = ?""",
                (new_fp, now,
                 title, title, company, company, location, location,
                 salary, salary, url, url, description, description, job_id),
            )
            detail = (
                f"合并同一职位的另一条记录（来源：{source}）" if merged
                else f"第 {int(row['seen_count']) + 1} 次遇到（来源：{source}）"
            )
            self._conn.execute(
                "INSERT INTO events (job_id, ts, kind, detail) VALUES (?,?,?,?)",
                (job_id, now, "seen", detail),
            )
            self._conn.commit()
            return job_id, False

    def update_status(self, job_id: int, status: str, note: str | None = None) -> dict[str, Any] | None:
        """改状态（并可附一条备注）。返回更新后的岗位，找不到返回 None。"""
        status = (status or "").strip()
        if status not in STATUSES:
            return None
        job = self.get_job(job_id)
        if job is None:
            return None
        now = _now()
        with self._lock:
            if note:
                self._conn.execute(
                    "UPDATE jobs SET status = ?, note = ?, last_seen = ? WHERE id = ?",
                    (status, note.strip(), now, job_id),
                )
            else:
                self._conn.execute(
                    "UPDATE jobs SET status = ?, last_seen = ? WHERE id = ?",
                    (status, now, job_id),
                )
            detail = f"{job['status']} -> {status}"
            if note:
                detail += f" | {note.strip()}"
            self._conn.execute(
                "INSERT INTO events (job_id, ts, kind, detail) VALUES (?,?,?,?)",
                (job_id, now, "status", detail),
            )
            self._conn.commit()
        return self.get_job(job_id)

    def append_note(self, job_id: int, note: str) -> dict[str, Any] | None:
        """往备注后面追加文字（用 / 分隔），不改状态。"""
        job = self.get_job(job_id)
        if job is None:
            return None
        note = (note or "").strip()
        if not note:
            return job
        merged = f"{job['note']} / {note}" if job["note"] else note
        now = _now()
        with self._lock:
            self._conn.execute("UPDATE jobs SET note = ? WHERE id = ?", (merged, job_id))
            self._conn.execute(
                "INSERT INTO events (job_id, ts, kind, detail) VALUES (?,?,?,?)",
                (job_id, now, "note", note),
            )
            self._conn.commit()
        return self.get_job(job_id)

    def set_profile(self, key: str, value: str) -> None:
        """记一条长期偏好（例如 目标城市 -> 武汉）。"""
        key, value = (key or "").strip(), (value or "").strip()
        if not key:
            return
        self._exec(
            """INSERT INTO profile (key, value, updated_at) VALUES (?,?,?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              updated_at = excluded.updated_at""",
            (key, value, _now()),
        )

    def all_profile(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM profile ORDER BY key").fetchall()
        return {r["key"]: r["value"] for r in rows}

    # ------------------------------------------------------------ 读取
    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row(row)

    def list_jobs(self, status: str | None = None, limit: int = 20,
                  order: str = "last_seen") -> list[dict[str, Any]]:
        order_col = "first_seen" if order == "first_seen" else "last_seen"
        sql = f"SELECT * FROM jobs {{where}} ORDER BY {order_col} DESC, id DESC LIMIT ?"
        params: tuple
        if status and status in STATUSES:
            sql = sql.format(where="WHERE status = ?")
            params = (status, max(1, int(limit)))
        else:
            sql = sql.format(where="")
            params = (max(1, int(limit)),)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def search(self, query: str = "", status: str | None = None,
               limit: int = 20) -> list[dict[str, Any]]:
        """在本地记忆库里模糊检索（职位 / 公司 / 城市 / 备注 / 描述）。"""
        clauses, params = [], []
        query = (query or "").strip()
        if query:
            like = f"%{query}%"
            clauses.append(
                "(title LIKE ? OR company LIKE ? OR location LIKE ? OR note LIKE ? OR description LIKE ?)"
            )
            params.extend([like] * 5)
        if status and status in STATUSES:
            clauses.append("status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, int(limit)))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM jobs {where} ORDER BY last_seen DESC, id DESC LIMIT ?", tuple(params)
            ).fetchall()
        return [dict(r) for r in rows]

    def timeline(self, job_id: int, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, kind, detail FROM events WHERE job_id = ? ORDER BY id DESC LIMIT ?",
                (job_id, max(1, int(limit))),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_events(self, limit: int = 15) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT e.ts, e.kind, e.detail, j.id AS job_id, j.title, j.company
                   FROM events e LEFT JOIN jobs j ON j.id = e.job_id
                   ORDER BY e.id DESC LIMIT ?""",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = int(self._conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"])
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS c FROM jobs GROUP BY status"
            ).fetchall()
            week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
            new_week = int(
                self._conn.execute(
                    "SELECT COUNT(*) AS c FROM jobs WHERE first_seen >= ?", (week_ago,)
                ).fetchone()["c"]
            )
        by_status = {s: 0 for s in STATUSES}
        for r in rows:
            by_status[r["status"]] = int(r["c"])
        return {"total": total, "by_status": by_status, "new_last_7_days": new_week}

    # ------------------------------------------------------------ 导出
    def export_csv(self, path: str | Path | None = None) -> Path:
        """把全部岗位导出成 CSV（Excel 能直接打开，带 UTF-8 BOM）。"""
        target = Path(path) if path else self.path.parent / f"jobs_export_{datetime.now():%Y%m%d_%H%M%S}.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        columns = ["id", "title", "company", "location", "salary", "status",
                   "note", "url", "source", "first_seen", "last_seen", "seen_count"]
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {', '.join(columns)} FROM jobs ORDER BY id"
            ).fetchall()
        with target.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(["ID", "职位", "公司", "城市", "薪资", "状态", "备注",
                             "链接", "来源", "首次记录", "最近出现", "出现次数"])
            for r in rows:
                item = dict(r)
                item["status"] = STATUS_LABELS.get(item["status"], item["status"])
                writer.writerow([item[c] for c in columns])
        return target

    # ------------------------------------------------------------ 清除
    def count_rows(self, table: str) -> int:
        """某张表有多少行（清除前先报个数给用户看）。"""
        if table not in ("jobs", "events", "profile"):
            return 0
        with self._lock:
            return int(self._conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])

    def clear(self, scope: str = "jobs") -> dict[str, int]:
        """清空记忆库的指定部分，返回 {表名: 删掉的行数}。

        scope:
          jobs    岗位 + 操作时间线（events）
          profile 长期偏好
          all     以上全部

        只删数据、不删表，所以清完不用重建就能继续用。
        顺便把自增计数器也归零——否则清空后新记的岗位会从 #57 开始。
        """
        scope = (scope or "jobs").strip().lower()
        counts: dict[str, int] = {}
        with self._lock:
            if scope in ("jobs", "all"):
                counts["jobs"] = self.count_rows("jobs")
                counts["events"] = self.count_rows("events")
                self._conn.execute("DELETE FROM jobs")
                self._conn.execute("DELETE FROM events")
                self._conn.execute(
                    "DELETE FROM sqlite_sequence WHERE name IN ('jobs', 'events')"
                )
            if scope in ("profile", "all"):
                counts["profile"] = self.count_rows("profile")
                self._conn.execute("DELETE FROM profile")
            self._conn.commit()
        return counts

    def vacuum(self) -> None:
        """把删完留下的空洞收回去，让数据库文件真的变小。失败就算了，不影响使用。"""
        with self._lock:
            try:
                self._conn.execute("VACUUM")
            except sqlite3.Error:
                pass


# ---------------------------------------------------------------- 默认实例
_DEFAULT_STORE: JobStore | None = None
_DEFAULT_PATH: Path | None = None
_FORCED: bool = False


def get_store() -> JobStore:
    """取全局默认记忆库（按配置里的路径惰性创建）。"""
    global _DEFAULT_STORE, _DEFAULT_PATH
    if _FORCED and _DEFAULT_STORE is not None:
        return _DEFAULT_STORE  # configure() 手动指定过，就别自作主张换库

    from .config import load_settings

    settings = load_settings()
    settings.ensure_dirs()
    if _DEFAULT_STORE is None or _DEFAULT_PATH != settings.db_path:
        if _DEFAULT_STORE is not None:
            _DEFAULT_STORE.close()
        _DEFAULT_STORE = JobStore(settings.db_path)
        _DEFAULT_PATH = settings.db_path
    return _DEFAULT_STORE


def configure(path: str | Path) -> JobStore:
    """强制指定记忆库路径（自测 / 换库时用）。之后 get_store() 都返回它。"""
    global _DEFAULT_STORE, _DEFAULT_PATH, _FORCED
    if _DEFAULT_STORE is not None:
        _DEFAULT_STORE.close()
    _DEFAULT_STORE = JobStore(path)
    _DEFAULT_PATH = Path(path)
    _FORCED = True
    return _DEFAULT_STORE


def reset_default() -> None:
    """清掉手动指定，回到按配置自动选择。"""
    global _DEFAULT_STORE, _DEFAULT_PATH, _FORCED
    if _DEFAULT_STORE is not None:
        _DEFAULT_STORE.close()
    _DEFAULT_STORE = None
    _DEFAULT_PATH = None
    _FORCED = False


def format_job_line(job: dict[str, Any], verbose: bool = False) -> str:
    """把一条岗位格式化成一行文字，给模型/用户看。"""
    label = SHORT_LABELS.get(job["status"], job["status"])
    parts = [
        f"[#{job['id']}]",
        job["title"] or "未命名岗位",
        f"| {job['company'] or '公司未知'}",
    ]
    if job["location"]:
        parts.append(f"| {job['location']}")
    if job["salary"]:
        parts.append(f"| {job['salary']}")
    parts.append(f"| 状态:{label}({job['status']})")
    if job["seen_count"] > 1:
        parts.append(f"| 出现{job['seen_count']}次")
    parts.append(f"| 最近:{job['last_seen'][:10]}")
    line = " ".join(parts)
    if verbose:
        if job["note"]:
            line += f"\n      备注：{job['note']}"
        if job["url"]:
            line += f"\n      链接：{job['url']}"
    return line
