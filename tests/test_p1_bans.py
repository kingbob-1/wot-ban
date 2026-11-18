# -*- coding: utf-8 -*-
"""P1 名单库测试：archive 全量导入条数对账 / 去重 / 来源追溯 / 索引与查询 / 幂等。

固定口径（对齐计划文档 §2.4 E13-E14、§9 #2）：
- 源 = 96 期 CSV + 18 期 xlsx + 最新一期 wgid JSON = 物理行口径 **322,941** 条；
- 库内去重后 = 322,941 − 1（U+2029 昵称造成的物理行差）− 34（同批重复）
  − 100（最新 JSON 的 wgid 合并进同批 CSV 行）= **322,806** 条。
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import load_config  # noqa: E402
from src.store import bans, db  # noqa: E402
from src.models import MATCH_TYPE_ID, MATCH_TYPE_NICKNAME  # noqa: E402

ARCHIVE_DIR = load_config().archive_dir
CSV_FILES = 96
XLSX_FILES = 18
CSV_PHYSICAL_LINES = 255905
CSV_DATA_ROWS = 255904
CSV_LINE_ARTIFACTS = 1
CSV_DUPLICATES = 1
XLSX_RECORDS = 66936
XLSX_DUPLICATES = 33
WGID_ROWS = 100
SOURCE_ROWS_PHYSICAL = 322941
SOURCE_ROWS_DATA = 322940
DB_ROWS = 322806


@pytest.fixture(scope="module")
def imported(tmp_path_factory):
    """整库导入一次（module 级共享，避免每例重复导入 322k 条）。"""
    if not ARCHIVE_DIR.is_dir():
        pytest.skip("本机没有 research\\wot-ban\\archive 归档，跳过名单库测试")
    db_path = tmp_path_factory.mktemp("bans") / "wotban-test.db"
    conn = db.connect(db_path)
    report = bans.import_archive(conn, ARCHIVE_DIR, quiet=True)
    yield conn, report
    conn.close()


def test_import_counts_reconciliation(imported):
    """导入条数对账：物理行 322,941 -> 库内 322,806，四笔口径差逐笔闭合。"""
    _, report = imported
    assert report.ok, report.errors
    assert report.csv_files == CSV_FILES
    assert report.xlsx_files == XLSX_FILES
    assert report.wgid_files == 1
    assert report.csv_physical_lines == CSV_PHYSICAL_LINES
    assert report.csv_source_lines == CSV_DATA_ROWS
    assert report.csv_line_artifacts == CSV_LINE_ARTIFACTS
    assert report.csv_merged_split_lines == 0
    assert report.csv_records == CSV_DATA_ROWS - CSV_DUPLICATES
    assert report.csv_duplicate_rows == CSV_DUPLICATES
    assert report.xlsx_source_records == XLSX_RECORDS
    assert report.xlsx_duplicate_rows == XLSX_DUPLICATES
    assert report.wgid_rows == WGID_ROWS
    assert report.wgid_merged == WGID_ROWS
    assert report.wgid_inserted == 0
    assert report.source_rows_physical == SOURCE_ROWS_PHYSICAL
    assert report.source_rows_total == SOURCE_ROWS_DATA
    assert report.db_rows == DB_ROWS
    assert report.inserted_total == DB_ROWS
    assert report.reconciliation["consistent"] is True
    assert report.reconciled_rows == report.db_rows


def test_db_rows_and_dedup_key(imported):
    """库内条数固定；去重键四元组无重复。"""
    conn, _ = imported
    assert bans.count_bans(conn) == DB_ROWS
    duplicates = conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT batch_day, nickname, reason, duration, COUNT(*) AS c "
        "FROM bans GROUP BY batch_day, nickname, reason, duration HAVING c > 1)").fetchone()["n"]
    assert duplicates == 0, "去重键 (batch_day,nickname,reason,duration) 出现重复"
    assert conn.execute("SELECT COUNT(DISTINCT batch_day) AS n FROM bans").fetchone()["n"] == 114


def test_source_traceability(imported):
    """每条记录都有来源批次 / URL / 抓取时间；xlsx 期 URL 指向公告页。"""
    conn, _ = imported
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM bans WHERE source_url IS NULL OR source_url = '' "
        "OR fetched_at IS NULL").fetchone()["n"] == 0
    xlsx_row = conn.execute(
        "SELECT source_url, raw_sheet FROM bans WHERE source_type = 'xlsx' "
        "AND source_url LIKE '%/news/announcement/%' LIMIT 1").fetchone()
    assert xlsx_row is not None and xlsx_row["raw_sheet"]
    row = bans.query_bans(conn, wgid=7053765071)[0]
    assert row.batch_day == "2026-11-06"
    assert "wstop/down" in (row.source_url or "")


def test_xlsx_2020_10_09_case(imported):
    """2020-10-09 插件案例（xlsx 期双档）可查：7天 + 30天 两档均入库。"""
    conn, _ = imported
    rows = bans.query_bans(conn, batch_day="2020-10-09", limit=100000)
    assert len(rows) == 7554
    durations = {row.duration for row in rows}
    assert {"7天", "30天"} <= durations
    sample = rows[0]
    assert sample.source_type == "xlsx"
    assert sample.reason == "违规使用插件"
    assert sample.batch_day == "2020-10-09"


def test_dirty_values_cleaned_with_raw(imported):
    """脏值清洗留原始值：#NAME? 标注 dirty；含 U+2029 昵称保留 raw_nickname。"""
    conn, _ = imported
    dirty = conn.execute(
        "SELECT nickname, raw_nickname, credibility FROM bans WHERE nickname = '#NAME?' LIMIT 1").fetchone()
    assert dirty is not None and dirty["credibility"] == "dirty"
    row = bans.query_bans(conn, nickname="孤老终生 1", batch_day="2021-10-25")[0]
    assert row.raw_nickname == "孤老终生\u20291"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM bans WHERE reason LIKE ' %'").fetchone()["n"] == 0


def test_load_ban_index_and_lookup(imported):
    """索引：条数 / wgid 覆盖 / 双档查询入口。"""
    conn, _ = imported
    index = bans.load_ban_index(conn)
    assert index.total_records == DB_ROWS
    assert index.wgid_records == WGID_ROWS
    assert index.latest_day == "2026-11-06"
    match_type, records = index.lookup(7053765071, "RE_7053765071")
    assert match_type == MATCH_TYPE_ID and records and records[0].batch_day == "2026-11-06"
    match_type, records = index.lookup(None, "夕阳落满山头")
    assert match_type == MATCH_TYPE_NICKNAME and records
    assert all(record.batch_day <= "2026-11-06" for record in records)
    assert index.lookup(123456789, "不存在的昵称xyz")[0] == "none"


def test_query_bans_sorted_desc(imported):
    """查询按封禁日期降序；昵称查询走清洗后等值。"""
    conn, _ = imported
    rows = bans.query_bans(conn, nickname="RE_7053765071")
    assert rows, "该昵称应至少命中 1 条"
    days = [row.batch_day for row in rows]
    assert days == sorted(days, reverse=True)
    assert bans.query_bans(conn, batch_day="不存在-0000-00") == []


def test_import_idempotent(imported):
    """重复导入幂等：新增 0 条、库内条数不变、口径仍闭合。"""
    conn, _ = imported
    before = bans.count_bans(conn)
    report = bans.import_archive(conn, ARCHIVE_DIR, quiet=True)
    assert report.inserted_total == 0
    assert report.db_rows == before == DB_ROWS
    assert report.reconciliation["consistent"] is True
    assert report.wgid_inserted == 0


def test_missing_days_against_meta(imported):
    """meta 的 days_json 与库内批次一致，无缺失。"""
    conn, _ = imported
    days = db.get_meta(conn, "days_json", [])
    assert len(days) == 114
    assert bans.missing_days(conn, days) == []
    coverage = db.get_meta(conn, "wgid_coverage", {})
    assert coverage["latest_day"] == "2026-11-06"
    assert coverage["with_wgid"] == WGID_ROWS


# ---------------------------------------------------------------- QA D1
SNAPSHOT_ROWS = 115  # 96 CSV + 18 xlsx + 1 JSON，一 (url, kind, batch_day) 一行


def test_source_snapshots_idempotent_across_reimports(imported):
    """QA D1：source_snapshots 按 (url, kind, batch_day) 幂等，重复导入不膨胀。"""
    conn, _ = imported
    counts = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM source_snapshots GROUP BY kind").fetchall()
    by_kind = {row["kind"]: row["n"] for row in counts}
    assert by_kind == {"csv": 96, "xlsx": 18, "json": 1}
    total = conn.execute("SELECT COUNT(*) AS n FROM source_snapshots").fetchone()["n"]
    distinct = conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT 1 FROM source_snapshots GROUP BY url, kind, batch_day)"
    ).fetchone()["n"]
    assert total == distinct == SNAPSHOT_ROWS

    for _ in range(2):
        report = bans.import_archive(conn, ARCHIVE_DIR, quiet=True)
        assert report.snapshots_deduped == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM source_snapshots").fetchone()["n"] == SNAPSHOT_ROWS


def test_dedupe_snapshots_cleans_legacy_rows(tmp_path):
    """QA D1 清理：历史重复快照只保留每个键最新 1 行，且不碰 bans 表。"""
    conn = db.connect(tmp_path / "snapshot-dedupe.db")
    try:
        conn.execute("INSERT INTO bans(batch_day, source_type, nickname, reason, duration, ban_times) "
                     "VALUES('2026-11-06', 'csv', '测试玩家', '违规使用脚本', '永久', 1)")
        for _ in range(3):
            conn.execute(
                "INSERT INTO source_snapshots(url, kind, status, bytes, fetched_at, batch_day) "
                "VALUES('https://example.invalid/down?day=2026-11-06', 'csv', 200, 100, "
                "'2026-11-18T10:00:00+08:00', '2026-11-06')")
        conn.execute(
            "INSERT INTO source_snapshots(url, kind, status, bytes, fetched_at, batch_day) "
            "VALUES('https://example.invalid/index', 'json', 200, 50, "
            "'2026-11-18T10:00:00+08:00', '2026-11-06')")
        conn.commit()
        before_bans = conn.execute("SELECT COUNT(*) AS n FROM bans").fetchone()["n"]
        stats = bans.dedupe_snapshots(conn)
        assert stats["before"] == 4 and stats["after"] == 2 and stats["deleted"] == 2
        assert stats["distinct_keys"] == 2
        assert conn.execute("SELECT COUNT(*) AS n FROM bans").fetchone()["n"] == before_bans
        # 幂等：再清理一次删除 0 行
        assert bans.dedupe_snapshots(conn)["deleted"] == 0
    finally:
        conn.close()
