# -*- coding: utf-8 -*-
"""名单库：archive 归档 -> SQLite 导入、名单索引、按批/按人查询。

数据口径（对齐计划文档 §2.4 / §6.1 / §6.5 / §9 #2-#4）：
- 源 = 96 期 CSV（255,905 条物理数据行）+ 18 期 xlsx（66,936 条）+ 最新一期 wgid JSON（100 条）
  = 源记录 **322,941** 条；
- 去重键 ``(batch_day, nickname, reason, duration)``（``ux_bans_dedup`` 唯一索引兜底）；
- 库内条数与 322,941 的差 = 三笔可解释口径差（导入报告逐笔列出）：
  1. CSV 物理拆行合并：1（2021-10-25 含换行昵称被导出成两行，见 P2-2 ±1 口径差）；
  2. CSV 同批完全重复记录：见 ``duplicate_rows``（唯一索引只留一行）；
  3. 最新 JSON 的 100 条 wgid 合并进同批 CSV 行（只补 ``wgid``，不新增行）；
- 每条记录保留来源批次 / 来源 URL / 抓取时间 / 原始脏值；xlsx 期来源 URL 指向公告页。
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    from ..config import Config, load_config
    from ..fetcher.parsers import (
        IndexSnapshot,
        clean_nickname,
        is_dirty_nickname,
        normalize_day,
        parse_csv_text,
        parse_index_payload,
        parse_xlsx_file,
        reason_from_title,
    )
    from ..models import MATCH_TYPE_ID, MATCH_TYPE_NICKNAME, MATCH_TYPE_NONE, BanRecord
    from . import db
except ImportError:  # 直接以脚本方式运行（python src/store/bans.py）时的兜底
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.config import Config, load_config
    from src.fetcher.parsers import (
        IndexSnapshot,
        clean_nickname,
        is_dirty_nickname,
        normalize_day,
        parse_csv_text,
        parse_index_payload,
        parse_xlsx_file,
        reason_from_title,
    )
    from src.models import MATCH_TYPE_ID, MATCH_TYPE_NICKNAME, MATCH_TYPE_NONE, BanRecord
    from src.store import db

WHITESPACE_RE = re.compile(r"\s+")
INSERT_SQL = """
INSERT OR IGNORE INTO bans
    (batch_day, source_type, source_url, nickname, reason, duration, ban_times,
     wgid, raw_sheet, raw_nickname, fetched_at, credibility)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
SNAPSHOT_SQL = """
INSERT INTO source_snapshots (url, kind, status, bytes, fetched_at, batch_day)
VALUES (?, ?, ?, ?, ?, ?)
"""
WGID_UPDATE_SQL = """
UPDATE bans SET wgid = ?, raw_nickname = COALESCE(raw_nickname, ?),
                source_url = COALESCE(source_url, ?), fetched_at = COALESCE(fetched_at, ?)
 WHERE batch_day = ? AND nickname = ? AND reason IS ? AND duration IS ?
   AND (wgid IS NULL OR wgid <> ?)
"""


# ---------------------------------------------------------------- 数据类型
@dataclass
class PeriodImport:
    """单期导入结果（对账用）。"""

    batch_day: str = ""
    source_type: str = "csv"
    file: str = ""
    source_rows: int = 0
    physical_lines: int = 0
    line_artifacts: int = 0
    records: int = 0
    inserted: int = 0
    duplicates: int = 0
    merged_split_lines: int = 0
    wgid_updated: int = 0
    wgid_merged: int = 0

    def to_dict(self) -> dict:
        return {
            "batch_day": self.batch_day, "source_type": self.source_type, "file": self.file,
            "source_rows": self.source_rows, "physical_lines": self.physical_lines,
            "line_artifacts": self.line_artifacts, "records": self.records,
            "inserted": self.inserted, "duplicates": self.duplicates,
            "merged_split_lines": self.merged_split_lines, "wgid_updated": self.wgid_updated,
            "wgid_merged": self.wgid_merged,
        }


@dataclass
class ImportReport:
    """一次 archive 导入的对账报告（导入条数 / 口径差 / 每期明细）。"""

    ok: bool = True
    started_at: str = ""
    finished_at: str = ""
    archive_dir: str = ""
    csv_files: int = 0
    xlsx_files: int = 0
    wgid_files: int = 0
    csv_source_lines: int = 0
    csv_physical_lines: int = 0
    csv_line_artifacts: int = 0
    csv_records: int = 0
    csv_merged_split_lines: int = 0
    csv_duplicate_rows: int = 0
    xlsx_source_records: int = 0
    xlsx_duplicate_rows: int = 0
    wgid_rows: int = 0
    wgid_merged: int = 0
    wgid_inserted: int = 0
    inserted_total: int = 0
    db_rows_before: int = 0
    db_rows: int = 0
    periods: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    snapshots_deduped: int = 0

    @property
    def source_rows_total(self) -> int:
        """源记录条数（CSV 数据行 + xlsx + JSON，期望 322,940）。"""
        return self.csv_source_lines + self.xlsx_source_records + self.wgid_rows

    @property
    def source_rows_physical(self) -> int:
        """物理行口径的源记录数（含表头外被 splitlines 多切出来的 U+2029 行）。

        archive\\_verify_archive.json 的官方合计 **322,941** 即此口径（CSV 255,905）。
        """
        return self.csv_physical_lines + self.xlsx_source_records + self.wgid_rows

    @property
    def physical_split_rows(self) -> int:
        """物理行比 CSV 数据行多出来的行数（U+2029 昵称分隔符）。"""
        return self.csv_line_artifacts

    @property
    def internal_duplicates(self) -> int:
        return self.csv_duplicate_rows + self.xlsx_duplicate_rows

    @property
    def reconciled_rows(self) -> int:
        """按四笔口径差推回的库内应有条数。"""
        return (self.source_rows_physical - self.physical_split_rows
                - self.internal_duplicates - self.wgid_merged)

    @property
    def reconciliation(self) -> dict:
        return {
            "source_rows_physical_target": self.source_rows_physical,
            "source_rows_data": self.source_rows_total,
            "minus_csv_physical_line_artifacts": self.physical_split_rows,
            "minus_internal_duplicates": self.internal_duplicates,
            "minus_wgid_merged_into_csv": self.wgid_merged,
            "reconciled_db_rows": self.reconciled_rows,
            "actual_db_rows": self.db_rows,
            "consistent": self.reconciled_rows == self.db_rows,
        }

    def to_dict(self) -> dict:
        payload = {
            name: getattr(self, name)
            for name in ("ok", "started_at", "finished_at", "archive_dir", "csv_files", "xlsx_files",
                         "wgid_files", "csv_source_lines", "csv_physical_lines", "csv_line_artifacts",
                         "csv_records", "csv_merged_split_lines", "csv_duplicate_rows",
                         "xlsx_source_records", "xlsx_duplicate_rows", "wgid_rows", "wgid_merged",
                         "wgid_inserted", "inserted_total", "db_rows_before", "db_rows",
                         "snapshots_deduped", "errors", "notes")
        }
        payload["source_rows_total"] = self.source_rows_total
        payload["source_rows_physical"] = self.source_rows_physical
        payload["reconciliation"] = self.reconciliation
        payload["periods"] = [item.to_dict() for item in self.periods]
        return payload


# ---------------------------------------------------------------- 昵称规范化
def normalize_nickname(value: Any) -> str:
    """昵称规范化（匹配键）：NFKC（全半角统一）+ 去首尾空白 + 空白压缩 + casefold。"""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = WHITESPACE_RE.sub(" ", text).strip()
    return text.casefold()


# ---------------------------------------------------------------- 归档清单
def _read_jsonl(path: Path) -> list:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _csv_manifest(archive_dir: Path) -> dict:
    """CSV 归档清单：批次日 -> {fetched_at, records, bytes}。"""
    return {row.get("date"): row for row in _read_jsonl(archive_dir / "csv" / "_manifest.jsonl") if row.get("date")}


def _xlsx_manifest(archive_dir: Path) -> dict:
    """xlsx 归档清单：批次日 -> 最后一条清单行（含公告 URL/标题、附件 URL）。"""
    manifest = {}
    for row in _read_jsonl(archive_dir / "xlsx" / "_manifest.jsonl"):
        day = row.get("batch_day")
        if not day:
            continue
        if row.get("xlsx_file") or row.get("files"):
            manifest[day] = row
    return manifest


def _xlsx_source_url(entry: dict) -> Optional[str]:
    """xlsx 期来源 URL：优先公告页（用户可点开核对），再退附件 URL。"""
    if not entry:
        return None
    return entry.get("announcement_url") or entry.get("xlsx_url")


def _file_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")


def _count_bans(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM bans").fetchone()
    return int(row["n"] if row is not None else 0)


# ---------------------------------------------------------------- 写入
def _db_text(value) -> str:
    """去重键列不允许 NULL（SQLite 唯一索引把 NULL 视为互不相等，见 db.py 约定）。"""
    return "" if value is None else str(value)


def _insert_records(conn: sqlite3.Connection, records: Iterable[dict], batch_day: str = "") -> int:
    """批量插入（唯一索引去重），返回实际新增条数。

    ``reason`` / ``duration`` 为空时统一存空串 ``''``：db.py 明确约定「写入方需先清洗
    空值」，否则唯一索引对 NULL 不去重（xlsx 期无时长档位时会出现重复行）。
    """
    rows = [
        (record["batch_day"], record["source_type"], record.get("source_url"), record["nickname"],
         _db_text(record.get("reason")), _db_text(record.get("duration")), int(record.get("ban_times") or 1),
         record.get("wgid"), record.get("raw_sheet"), record.get("raw_nickname"),
         record.get("fetched_at"), record.get("credibility") or "official")
        for record in records
    ]
    if not rows:
        return 0
    before = _count_bans(conn)
    conn.executemany(INSERT_SQL, rows)
    conn.commit()
    return _count_bans(conn) - before


SNAPSHOT_SELECT_SQL = """
SELECT id, status, bytes, fetched_at FROM source_snapshots
 WHERE url = ? AND kind = ? AND batch_day = ?
 ORDER BY id DESC LIMIT 1
"""
SNAPSHOT_UPDATE_SQL = """
UPDATE source_snapshots SET status = ?, bytes = ?, fetched_at = ?
 WHERE id = ?
"""


def _insert_snapshot(conn: sqlite3.Connection, url: Optional[str], kind: str, size: Optional[int],
                     fetched_at: Optional[str], batch_day: str) -> None:
    """抓取快照幂等写入：按 ``(url, kind, batch_day)`` upsert（QA D1）。

    - 同键不存在 → 新增 1 行；
    - 同键已存在 → 仅当 status/bytes/fetched_at 变化时覆盖（不追加新行）。
    这样「重新导入 archive」与「每 6h 更新检查」都不会让快照表线性膨胀。
    """
    if not url:
        return
    row = conn.execute(SNAPSHOT_SELECT_SQL, (url, kind, batch_day)).fetchone()
    if row is None:
        conn.execute(SNAPSHOT_SQL, (url, kind, 200, size, fetched_at, batch_day))
        return
    current = (row["status"], row["bytes"], row["fetched_at"])
    if current != (200, size, fetched_at):
        conn.execute(SNAPSHOT_UPDATE_SQL, (200, size, fetched_at, row["id"]))


def dedupe_snapshots(conn: sqlite3.Connection) -> dict:
    """清理历史遗留的重复快照，只保留每个 ``(url, kind, batch_day)`` 的最新 1 行（QA D1）。

    仅操作 ``source_snapshots`` 表（不碰其他表）；幂等，可重复执行。返回清理统计。
    """
    before = conn.execute("SELECT COUNT(*) AS n FROM source_snapshots").fetchone()["n"]
    conn.execute(
        "DELETE FROM source_snapshots WHERE id NOT IN ("
        "SELECT MAX(id) FROM source_snapshots GROUP BY url, kind, batch_day)"
    )
    conn.commit()
    after = conn.execute("SELECT COUNT(*) AS n FROM source_snapshots").fetchone()["n"]
    groups = conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT 1 FROM source_snapshots GROUP BY url, kind, batch_day)"
    ).fetchone()["n"]
    return {"before": int(before or 0), "after": int(after or 0),
            "deleted": max(0, int(before or 0) - int(after or 0)), "distinct_keys": int(groups or 0)}


def import_csv_file(conn: sqlite3.Connection, path: Path, batch_day: str = "",
                    source_url: Optional[str] = None, fetched_at: Optional[str] = None) -> PeriodImport:
    """导入一期 CSV（幂等）。"""
    path = Path(path)
    day = normalize_day(batch_day, path.stem)
    fetched_at = fetched_at or _file_iso(path)
    raw = path.read_bytes()
    parsed = parse_csv_text(raw, day, source_url=source_url, fetched_at=fetched_at)
    period = PeriodImport(batch_day=day or parsed.batch_day, source_type="csv", file=path.name,
                          source_rows=parsed.source_lines, physical_lines=max(0, parsed.physical_lines - 1),
                          line_artifacts=parsed.line_artifacts, records=len(parsed.rows),
                          duplicates=parsed.duplicate_rows, merged_split_lines=parsed.merged_split_lines)
    period.inserted = _insert_records(conn, parsed.rows, day)
    conn.commit()
    _insert_snapshot(conn, source_url, "csv", len(raw), fetched_at, period.batch_day)
    conn.commit()
    return period


def import_xlsx_file(conn: sqlite3.Connection, path: Path, batch_day: str = "",
                     source_url: Optional[str] = None, reason: Optional[str] = None,
                     fetched_at: Optional[str] = None) -> PeriodImport:
    """导入一期 xlsx（幂等；记录数与归档口径 66,936 一致）。"""
    path = Path(path)
    day = normalize_day(batch_day, path.stem)
    fetched_at = fetched_at or _file_iso(path)
    records = parse_xlsx_file(path, day, reason=reason, source_url=source_url, fetched_at=fetched_at)
    deduped, seen = [], set()
    duplicates = 0
    for record in records:
        key = (record["batch_day"], record["nickname"], record.get("reason"), record.get("duration"))
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        deduped.append(record)
    period = PeriodImport(batch_day=day or path.stem, source_type="xlsx", file=path.name,
                          source_rows=len(records), records=len(deduped), duplicates=duplicates)
    period.inserted = _insert_records(conn, deduped, period.batch_day)
    conn.commit()
    _insert_snapshot(conn, source_url, "xlsx", path.stat().st_size, fetched_at, period.batch_day)
    conn.commit()
    return period


def import_wgid_file(conn: sqlite3.Connection, path: Path, source_url: Optional[str] = None) -> PeriodImport:
    """导入一期 wgid JSON：同批 CSV 行补 wgid（不新增行），无对应行才插入。"""
    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    meta = payload.get("meta") or {}
    snapshot = parse_index_payload(payload)
    fetched_at = meta.get("fetched_at") or _file_iso(path)
    source_url = source_url or meta.get("source_url")
    day = normalize_day(snapshot.date or meta.get("date"), path.stem)
    period = PeriodImport(batch_day=day, source_type="json", file=path.name, source_rows=len(snapshot.items))
    inserted_records = []
    for item in snapshot.items:
        key = (day, item["nickname"], item.get("reason"), item.get("duration"))
        cursor = conn.execute(WGID_UPDATE_SQL, (
            item.get("wgid"), item.get("nickname"), source_url, fetched_at,
            key[0], key[1], key[2], key[3], item.get("wgid"),
        ))
        if cursor.rowcount:
            period.wgid_updated += 1
            period.wgid_merged += 1
            continue
        exists = conn.execute(
            "SELECT 1 FROM bans WHERE batch_day = ? AND nickname = ? AND reason IS ? AND duration IS ?",
            key,
        ).fetchone()
        if exists is not None:
            period.wgid_merged += 1
            continue
        inserted_records.append({
            "batch_day": day, "source_type": "json", "source_url": source_url,
            "nickname": item["nickname"], "reason": item.get("reason"), "duration": item.get("duration"),
            "ban_times": 1, "wgid": item.get("wgid"), "raw_sheet": None, "raw_nickname": None,
            "fetched_at": fetched_at, "credibility": "official",
        })
    period.inserted = _insert_records(conn, inserted_records, day)
    conn.commit()
    _insert_snapshot(conn, source_url, "json", path.stat().st_size, fetched_at, day)
    conn.commit()
    return period


# ---------------------------------------------------------------- 全量导入
def import_archive(conn: sqlite3.Connection, archive_dir=None, cfg: Optional[Config] = None,
                   progress_cb=None, quiet: bool = True) -> ImportReport:
    """把 ``archive_dir`` 的 96 CSV + 18 xlsx + wgid JSON 全量导入 SQLite（幂等）。

    参数
    ----
    conn: SQLite 连接（``db.connect`` 返回，已建表）。
    archive_dir: 归档目录；默认取 ``Config.archive_dir``。
    progress_cb: 可选回调 ``(percent:int, message:str)``，供 GUI 进度条使用。
    """
    cfg = cfg or load_config()
    root = Path(archive_dir or cfg.archive_dir)
    report = ImportReport(started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                          archive_dir=str(root), db_rows_before=_count_bans(conn))
    if progress_cb is not None:
        progress_cb(2, "正在读取归档清单：%s" % root)
    csv_files = sorted((root / "csv").glob("*.csv"))
    xlsx_files = sorted((root / "xlsx").glob("*.xlsx"))
    wgid_files = sorted((root / "wgid").glob("*.json"))
    wgid_files = [path for path in wgid_files if not path.name.startswith("_")]
    csv_manifest = _csv_manifest(root)
    xlsx_manifest = _xlsx_manifest(root)
    if not csv_files and not xlsx_files and not wgid_files:
        report.ok = False
        report.errors.append("归档目录为空或路径不对：%s" % root)
        report.finished_at = report.started_at
        return report

    total_steps = max(1, len(csv_files) + len(xlsx_files) + len(wgid_files))
    step = 0

    def advance(message: str) -> None:
        nonlocal step
        step += 1
        if progress_cb is not None:
            progress_cb(min(99, 2 + int(96 * step / total_steps)), message)

    for path in csv_files:
        day = path.stem
        entry = csv_manifest.get(day) or {}
        url = "%s?day=%s" % (cfg.official_csv_url, day)
        try:
            period = import_csv_file(conn, path, day, source_url=url, fetched_at=entry.get("fetched_at"))
        except (OSError, ValueError, sqlite3.Error) as exc:
            report.ok = False
            report.errors.append("CSV 导入失败 %s: %s" % (path.name, exc))
            advance("CSV 失败：%s" % path.name)
            continue
        report.csv_files += 1
        report.csv_source_lines += period.source_rows
        report.csv_physical_lines += period.physical_lines
        report.csv_line_artifacts += period.line_artifacts
        report.csv_records += period.records
        report.csv_merged_split_lines += period.merged_split_lines
        report.csv_duplicate_rows += period.duplicates
        report.periods.append(period)
        advance("CSV 第 %d 期：%s（%d 条）" % (report.csv_files, period.batch_day, period.records))

    for path in xlsx_files:
        day = path.stem
        entry = xlsx_manifest.get(day) or {}
        reason = reason_from_title(entry.get("announcement_title") or "")
        try:
            period = import_xlsx_file(conn, path, day, source_url=_xlsx_source_url(entry), reason=reason,
                                      fetched_at=entry.get("fetched_at"))
        except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
            report.ok = False
            report.errors.append("xlsx 导入失败 %s: %s" % (path.name, exc))
            advance("xlsx 失败：%s" % path.name)
            continue
        report.xlsx_files += 1
        report.xlsx_source_records += period.source_rows
        report.xlsx_duplicate_rows += period.duplicates
        report.periods.append(period)
        advance("xlsx 第 %d 期：%s（%d 条）" % (report.xlsx_files, period.batch_day, period.records))

    for path in wgid_files:
        try:
            period = import_wgid_file(conn, path, source_url=cfg.official_index_url)
        except (OSError, ValueError, sqlite3.Error) as exc:
            report.ok = False
            report.errors.append("wgid JSON 导入失败 %s: %s" % (path.name, exc))
            advance("wgid 失败：%s" % path.name)
            continue
        report.wgid_files += 1
        report.wgid_rows += period.source_rows
        report.wgid_merged += period.wgid_merged
        report.wgid_inserted += period.inserted
        report.periods.append(period)
        advance("wgid 期：%s（%d 条，补 ID %d 条）" % (period.batch_day, period.source_rows, period.wgid_updated))

    report.inserted_total = _count_bans(conn) - report.db_rows_before
    report.db_rows = _count_bans(conn)
    report.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
    report.snapshots_deduped = dedupe_snapshots(conn)["deleted"]
    _write_meta_after_import(conn, report)
    if report.reconciled_rows != report.db_rows:
        report.notes.append("口径差未闭合：按源记录推回 %d 条，实际库内 %d 条"
                            % (report.reconciled_rows, report.db_rows))
        report.ok = False
    if progress_cb is not None:
        progress_cb(100, "导入完成：%d 期 / 库内 %d 条" % (len(report.periods), report.db_rows))
    if not quiet:
        print("[import] 源记录 %d 条 -> 库内 %d 条（新增 %d），口径闭合=%s"
              % (report.source_rows_total, report.db_rows, report.inserted_total,
                 report.reconciliation["consistent"]))
    return report


def _write_meta_after_import(conn: sqlite3.Connection, report: ImportReport) -> None:
    """导入后写 meta：days_json / latest_day / wgid_coverage / last_update_at。"""
    rows = conn.execute("SELECT batch_day, COUNT(*) AS n FROM bans GROUP BY batch_day ORDER BY batch_day DESC").fetchall()
    days = [row["batch_day"] for row in rows]
    wgid_rows = conn.execute(
        "SELECT batch_day, COUNT(*) AS n, SUM(CASE WHEN wgid IS NOT NULL THEN 1 ELSE 0 END) AS with_id "
        "FROM bans GROUP BY batch_day HAVING with_id > 0 ORDER BY batch_day DESC"
    ).fetchall()
    coverage = {
        "latest_day": days[0] if days else "",
        "days": [row["batch_day"] for row in wgid_rows],
        "items": sum(int(row["n"] or 0) for row in wgid_rows),
        "with_wgid": sum(int(row["with_id"] or 0) for row in wgid_rows),
    }
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    db.set_meta(conn, "days_json", days)
    db.set_meta(conn, "latest_day", coverage["latest_day"])
    db.set_meta(conn, "wgid_coverage", coverage)
    db.set_meta(conn, "last_update_at", now)
    db.set_meta(conn, "archive_import", report.reconciliation)
    db.set_meta(conn, "record_count", report.db_rows)


# ---------------------------------------------------------------- 索引 / 查询
def _row_to_record(row: sqlite3.Row) -> BanRecord:
    return BanRecord(
        batch_day=row["batch_day"], nickname=row["nickname"], source_type=row["source_type"],
        reason=row["reason"], duration=row["duration"], ban_times=int(row["ban_times"] or 1),
        wgid=row["wgid"], source_url=row["source_url"], raw_sheet=row["raw_sheet"],
        raw_nickname=row["raw_nickname"], fetched_at=row["fetched_at"], credibility=row["credibility"],
        id=row["id"],
    )


class BanIndex:
    """名单匹配索引：wgid / 规范化昵称 -> bans 主键，记录明细按需懒加载。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.by_wgid: dict = {}
        self.by_nickname: dict = {}
        self.total_records = 0
        self.wgid_records = 0
        self.days: list = []
        self.latest_day = ""
        self._cache: dict = {}
        self.reload()

    def reload(self) -> None:
        """重建索引（名单库更新后调用）。"""
        self.by_wgid = {}
        self.by_nickname = {}
        self._cache = {}
        rows = self.conn.execute("SELECT id, wgid, nickname FROM bans").fetchall()
        for row in rows:
            record_key = int(row["id"])
            wgid = row["wgid"]
            if wgid is not None:
                self.by_wgid.setdefault(int(wgid), []).append(record_key)
                self.wgid_records += 1
            key = normalize_nickname(row["nickname"])
            if key:
                self.by_nickname.setdefault(key, []).append(record_key)
        self.total_records = len(rows)
        days = self.conn.execute(
            "SELECT batch_day FROM bans GROUP BY batch_day ORDER BY batch_day DESC").fetchall()
        self.days = [row["batch_day"] for row in days]
        self.latest_day = self.days[0] if self.days else ""

    def records(self, ids: Iterable[int]) -> list:
        """按主键取封禁记录（BanRecord，按封禁日期降序）；带小缓存。"""
        wanted = [record_id for record_id in dict.fromkeys(ids) if record_id not in self._cache]
        for offset in range(0, len(wanted), 500):
            chunk = wanted[offset:offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = self.conn.execute(
                "SELECT * FROM bans WHERE id IN (%s)" % placeholders, chunk).fetchall()
            for row in rows:
                self._cache[int(row["id"])] = _row_to_record(row)
        result = [self._cache[record_id] for record_id in ids if record_id in self._cache]
        result.sort(key=lambda item: (item.batch_day or "", item.id or 0), reverse=True)
        return result

    def records_for_nickname(self, nickname: str) -> list:
        return self.records(self.by_nickname.get(normalize_nickname(nickname), []))

    def records_for_wgid(self, wgid: int) -> list:
        return self.records(self.by_wgid.get(int(wgid), []))

    def lookup(self, dbid: Optional[int], nickname: Optional[str]) -> tuple:
        """双档匹配入口：返回 ``(match_type, records)``。

        - 有 dbid 且命中 wgid -> ``MATCH_TYPE_ID``（ID 确认）；
        - 否则规范化昵称命中 -> ``MATCH_TYPE_NICKNAME``（同昵称视为同一人）；
        - 都不命中 -> ``MATCH_TYPE_NONE`` / 空列表。

        归并口径（计划 §6.4）：「同昵称跨批次累加 + 有 ID 时优先按 ID 归并」——
        ID 命中时仍把同昵称的历史记录一并归集（去重按主键），避免改名前的历史
        封禁次数被漏算；匹配档位始终以 ID 优先。
        """
        id_records = []
        if dbid is not None:
            try:
                id_records = self.records_for_wgid(int(dbid))
            except (TypeError, ValueError):
                id_records = []
        nickname_records = self.records_for_nickname(nickname) if nickname else []
        if not id_records and not nickname_records:
            return MATCH_TYPE_NONE, []
        merged = {}
        for record in id_records + nickname_records:
            merged.setdefault(record.id, record)
        records = sorted(merged.values(), key=lambda item: (item.batch_day or "", item.id or 0),
                         reverse=True)
        if id_records:
            return MATCH_TYPE_ID, records
        return MATCH_TYPE_NICKNAME, records


def load_ban_index(conn: Optional[sqlite3.Connection] = None, db_path=None) -> BanIndex:
    """构建名单索引（应用层契约 ``load_ban_index(conn)``；无连接时按配置开库）。"""
    if conn is None:
        conn = db.connect(db_path)
    return BanIndex(conn)


def query_bans(conn: sqlite3.Connection, nickname: Optional[str] = None, wgid: Optional[int] = None,
               batch_day: Optional[str] = None, day_from: Optional[str] = None,
               day_to: Optional[str] = None, source_type: Optional[str] = None,
               limit: int = 200, offset: int = 0) -> list:
    """按昵称（规范化等值）/wgid/批次区间查询封禁记录，按封禁日期降序。"""
    clauses, params = [], []
    if wgid is not None:
        clauses.append("wgid = ?")
        params.append(int(wgid))
    if nickname is not None:
        fallback = clean_nickname(nickname)
        clauses.append("(nickname = ? OR nickname = ?)")
        params.extend([fallback, str(nickname)])
    if batch_day:
        clauses.append("batch_day = ?")
        params.append(batch_day)
    if day_from:
        clauses.append("batch_day >= ?")
        params.append(day_from)
    if day_to:
        clauses.append("batch_day <= ?")
        params.append(day_to)
    if source_type:
        clauses.append("source_type = ?")
        params.append(source_type)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = ("SELECT * FROM bans" + where +
           " ORDER BY batch_day DESC, id DESC LIMIT ? OFFSET ?")
    params.extend([max(1, int(limit)), max(0, int(offset))])
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_record(row) for row in rows]


def batch_summary(conn: sqlite3.Connection) -> list:
    """每期条数与 ID 覆盖（按批次日降序），供数据状态页/对账使用。"""
    rows = conn.execute(
        "SELECT batch_day, source_type, COUNT(*) AS records, "
        "SUM(CASE WHEN wgid IS NOT NULL THEN 1 ELSE 0 END) AS wgid_records "
        "FROM bans GROUP BY batch_day, source_type ORDER BY batch_day DESC").fetchall()
    return [{
        "batch_day": row["batch_day"], "source_type": row["source_type"],
        "records": int(row["records"] or 0), "wgid_records": int(row["wgid_records"] or 0),
    } for row in rows]


def missing_days(conn: sqlite3.Connection, days: Iterable[str]) -> list:
    """对照官方 days 白名单，返回库内缺失的批次日（补漏用）。"""
    known = {row["batch_day"] for row in
             conn.execute("SELECT DISTINCT batch_day FROM bans").fetchall()}
    return [str(day) for day in days if str(day) not in known]


def count_bans(conn: sqlite3.Connection) -> int:
    """库内封禁记录总条数。"""
    return _count_bans(conn)


def ensure_fresh(conn: Optional[sqlite3.Connection] = None, now=None, force: bool = False, **kwargs):
    """兼容入口：实际实现在 ``src.fetcher.update.ensure_fresh``（更新语义 A1）。"""
    from ..fetcher import update as update_module

    return update_module.ensure_fresh(conn=conn, now=now, force=force, **kwargs)


def main(argv: list) -> int:
    """命令行：把 archive 全量导入本地库并打印对账报告（独立运行入口）。"""
    start = time.time()
    cfg = load_config(argv[0] if argv else None)
    archive_dir = argv[1] if len(argv) > 1 else None
    cfg.ensure_dirs()
    print("[1/3] 打开数据库：%s" % cfg.db_path)
    conn = db.connect(cfg.db_path)
    try:
        print("[2/3] 导入 archive：%s（base_path=%r）" % (archive_dir or cfg.archive_dir, base_path_placeholder()))
        report = import_archive(conn, archive_dir, cfg=cfg, quiet=False)
    finally:
        conn.close()
    print("[3/3] 对账：源记录 %d 条 / 库内 %d 条 / 新增 %d 条 / 口径闭合 %s"
          % (report.source_rows_total, report.db_rows, report.inserted_total,
             report.reconciliation["consistent"]))
    for key, value in report.reconciliation.items():
        print("      %-28s %s" % (key, value))
    print("      %-28s %s" % ("source_snapshots_deduped", report.snapshots_deduped))
    for error in report.errors[:5]:
        print("      [error] %s" % error)
    print("[done] 耗时 %.2f 秒" % (time.time() - start))
    return 0 if report.ok else 1


def base_path_placeholder() -> str:
    """主函数统一相对工作路径占位（规范要求 base_path 集中在主函数）。"""
    return "."


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
