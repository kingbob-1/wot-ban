# -*- coding: utf-8 -*-
"""P1 更新链路测试：临时 DB + 假时钟 + 假客户端。

覆盖（对齐计划文档 §6.1 更新检测 / §9 #8）：
- 启动首次：库空 + 本地归档 → 从归档导入；
- 运行期每 6h：未到间隔不请求、到间隔才请求、force 立即请求；
- 新批次：JSON 先存 wgid → 下载/归档 CSV → 入库，meta 更新，重复执行幂等不重复下载；
- 遇 403/412/429/503 → 风控中止，不更新 last_check_at；
- 超过 24h 未检查 → 提示；
- 不建计划任务（源码无 schtasks/TaskScheduler 依赖）。
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import Config  # noqa: E402
from src.fetcher import update as update_module  # noqa: E402
from src.fetcher.client import RiskControlAbort  # noqa: E402
from src.store import bans, db  # noqa: E402

NOW = datetime(2026, 11, 18, 12, 0, 0).astimezone()
CSV_HEADER = "封禁昵称,封禁原因,封禁时长,封禁日期\n"


# ---------------------------------------------------------------- 假件
class FakeClient:
    """假 HTTP 客户端：index 文本 + 按批次日返回 CSV 字节；可模拟风控。"""

    def __init__(self, index_text="", csv_map=None, abort_status=None):
        self.index_text = index_text
        self.csv_map = csv_map or {}
        self.abort_status = abort_status
        self.text_calls = 0
        self.byte_calls = 0
        self.urls = []

    def get_text(self, url, params=None):
        self.text_calls += 1
        self.urls.append(url)
        if self.abort_status:
            raise RiskControlAbort(self.abort_status, url)
        return self.index_text

    def get_bytes(self, url, params=None):
        self.byte_calls += 1
        self.urls.append(url)
        if self.abort_status:
            raise RiskControlAbort(self.abort_status, url)
        day = url.rsplit("day=", 1)[-1]
        return self.csv_map[day]


def _index_text(date, days, items):
    return json.dumps({"data": {"date": date, "days": days, "count": len(items), "items": items}},
                      ensure_ascii=False)


def _items(date, names):
    return [{"id": index + 1, "nickname": name, "reason": "违规使用脚本", "duration": "永久",
             "day": date, "wgid": 7050000000 + index} for index, name in enumerate(names)]


def _csv_bytes(names, day):
    return (CSV_HEADER + "".join("%s,违规使用脚本,永久,%s\n" % (name, day) for name in names)).encode("utf-8-sig")


def _write_wgid_json(archive, date, days, items):
    path = Path(archive) / "wgid" / ("%s.json" % date)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "meta": {"date": date, "fetched_at": "2026-11-18T10:00:00+08:00",
                 "source_url": "https://profile.wg.360.cn/wstop/index"},
        "data": {"date": date, "days": days, "count": len(items), "items": items},
        "raw": "",
    }, ensure_ascii=False), encoding="utf-8")
    return path


def _seed_archive(tmp_path):
    """本地归档：1 期 CSV + 同批 wgid JSON（模拟本机已有的 archive 目录）。"""
    archive = tmp_path / "archive"
    (archive / "csv").mkdir(parents=True, exist_ok=True)
    (archive / "csv" / "2026-11-01.csv").write_bytes(_csv_bytes(["种子玩家甲", "种子玩家乙"], "2026-11-01"))
    _write_wgid_json(archive, "2026-11-01", ["2026-11-01"], _items("2026-11-01", ["种子玩家甲", "种子玩家乙"]))
    return archive


def _config(tmp_path, archive):
    return Config(project_root=tmp_path, archive_dir=Path(archive),
                  db_path=tmp_path / "wotban-update.db", data_dir=tmp_path, log_dir=tmp_path)


def _db_rows(conn):
    return bans.count_bans(conn)


# ---------------------------------------------------------------- 用例
def test_bootstrap_and_interval(tmp_path):
    """首次启动：空库从归档导入；6h 内不再请求；force 才请求。"""
    archive = _seed_archive(tmp_path)
    cfg = _config(tmp_path, archive)
    conn = db.connect(cfg.db_path)
    try:
        client = FakeClient(_index_text("2026-11-01", ["2026-11-01"], _items("2026-11-01", ["种子玩家甲"])))
        result = update_module.ensure_fresh(conn, now=NOW, cfg=cfg, client=client)
        assert result.ok, result.errors
        assert result.imported_records == _db_rows(conn) > 0
        assert result.new_days == []
        assert client.text_calls == 1
        assert db.get_meta(conn, "last_check_at", "") == update_module.iso_time(NOW)
        assert db.get_meta(conn, "days_json", "") == ["2026-11-01"]
        assert db.get_meta(conn, "wgid_coverage", {})["with_wgid"] == 2

        # 1 小时后：未到 6h 检查间隔，不发请求
        result = update_module.ensure_fresh(conn, now=NOW + timedelta(hours=1), cfg=cfg, client=client)
        assert result.ok and client.text_calls == 1
        assert "未到检查间隔" in result.message

        # force：忽略间隔立即检查
        result = update_module.ensure_fresh(conn, now=NOW + timedelta(hours=1), force=True,
                                            cfg=cfg, client=client)
        assert result.ok and client.text_calls == 2
    finally:
        conn.close()


def test_new_day_downloaded_and_imported_idempotent(tmp_path):
    """7 小时后到检查间隔：新批次存档 wgid + 下载 CSV + 入库；重复执行不重复下载。"""
    archive = _seed_archive(tmp_path)
    cfg = _config(tmp_path, archive)
    conn = db.connect(cfg.db_path)
    try:
        seed_client = FakeClient(_index_text("2026-11-01", ["2026-11-01"], _items("2026-11-01", ["种子玩家甲"])))
        update_module.ensure_fresh(conn, now=NOW, cfg=cfg, client=seed_client)
        before = _db_rows(conn)

        new_items = _items("2026-11-15", ["新批玩家甲", "新批玩家乙"])
        client = FakeClient(
            _index_text("2026-11-15", ["2026-11-15", "2026-11-01"], new_items),
            csv_map={"2026-11-15": _csv_bytes(["新批玩家甲", "新批玩家乙", "新批玩家丙"], "2026-11-15")})
        later = NOW + timedelta(hours=7)
        result = update_module.ensure_fresh(conn, now=later, cfg=cfg, client=client)
        assert result.ok, result.errors
        assert result.latest_day == "2026-11-15"
        assert result.new_days == ["2026-11-15"]
        assert result.saved_wgid == ["2026-11-15"]
        assert result.downloaded_csv == ["2026-11-15"]
        assert client.text_calls == 1 and client.byte_calls == 1
        assert _db_rows(conn) == before + 3
        assert (Path(archive) / "wgid" / "2026-11-15.json").is_file()
        assert (Path(archive) / "csv" / "2026-11-15.csv").is_file()
        assert db.get_meta(conn, "last_check_at", "") == update_module.iso_time(later)
        assert db.get_meta(conn, "days_json", "")[0] == "2026-11-15"
        assert db.get_meta(conn, "wgid_coverage", {})["with_wgid"] == 4  # 两期各 2 条补到 wgid

        # 幂等：同一期再跑（force）不重复下载、不新增
        again = FakeClient(_index_text("2026-11-15", ["2026-11-15", "2026-11-01"], new_items),
                           csv_map={"2026-11-15": _csv_bytes(["新批玩家甲"], "2026-11-15")})
        result = update_module.ensure_fresh(conn, now=later + timedelta(hours=7), force=True,
                                            cfg=cfg, client=again)
        assert result.ok and result.new_days == []
        assert again.byte_calls == 0
        assert _db_rows(conn) == before + 3
    finally:
        conn.close()


def test_risk_control_aborts_without_updating_check_time(tmp_path):
    """403/412/429/503：风控中止，错误文案可展示，last_check_at 不变。"""
    archive = _seed_archive(tmp_path)
    cfg = _config(tmp_path, archive)
    conn = db.connect(cfg.db_path)
    try:
        seed_client = FakeClient(_index_text("2026-11-01", ["2026-11-01"], _items("2026-11-01", ["种子玩家甲"])))
        update_module.ensure_fresh(conn, now=NOW, cfg=cfg, client=seed_client)
        last_check_before = db.get_meta(conn, "last_check_at", "")

        abort_client = FakeClient(abort_status=403)
        result = update_module.ensure_fresh(conn, now=NOW + timedelta(hours=7), force=True,
                                            cfg=cfg, client=abort_client)
        assert result.ok is False
        assert result.aborted is True
        assert "403" in result.message
        assert "风控" in result.message
        assert db.get_meta(conn, "last_check_at", "") == last_check_before
    finally:
        conn.close()


def test_stale_check_hint(tmp_path):
    """超过 24 小时未检查：给出提示但检查照常执行。"""
    archive = _seed_archive(tmp_path)
    cfg = _config(tmp_path, archive)
    conn = db.connect(cfg.db_path)
    try:
        client = FakeClient(_index_text("2026-11-01", ["2026-11-01"], _items("2026-11-01", ["种子玩家甲"])))
        update_module.ensure_fresh(conn, now=NOW, cfg=cfg, client=client)
        old = NOW - timedelta(hours=30)
        db.set_meta(conn, "last_check_at", update_module.iso_time(old))

        client2 = FakeClient(_index_text("2026-11-01", ["2026-11-01"], _items("2026-11-01", ["种子玩家甲"])))
        result = update_module.ensure_fresh(conn, now=NOW, cfg=cfg, client=client2)
        assert result.ok and client2.text_calls == 1
        assert any("超过 24 小时" in error for error in result.errors)
        assert db.get_meta(conn, "last_check_at", "") == update_module.iso_time(NOW)
    finally:
        conn.close()


def test_update_check_snapshots_idempotent(tmp_path):
    """QA D1：重复更新检查（含 wgid 补 ID 入库）不追加 source_snapshots 行。"""
    archive = _seed_archive(tmp_path)
    cfg = _config(tmp_path, archive)
    conn = db.connect(cfg.db_path)
    try:
        client = FakeClient(_index_text("2026-11-01", ["2026-11-01"], _items("2026-11-01", ["种子玩家甲"])))
        for hours in (0, 7, 14, 21):
            result = update_module.ensure_fresh(conn, now=NOW + timedelta(hours=hours), force=True,
                                                cfg=cfg, client=client)
            assert result.ok, result.errors
        snapshot_rows = conn.execute("SELECT COUNT(*) AS n FROM source_snapshots").fetchone()["n"]
        distinct = conn.execute(
            "SELECT COUNT(*) AS n FROM (SELECT 1 FROM source_snapshots GROUP BY url, kind, batch_day)"
        ).fetchone()["n"]
        # 1 期 CSV + 1 期 wgid JSON（wgid 存档已存在但导入仍走幂等快照）
        assert snapshot_rows == distinct == 2
    finally:
        conn.close()


def test_no_scheduled_task(monkeypatch):
    """不建计划任务：更新模块不依赖 schtasks / TaskScheduler。"""
    source = Path(update_module.__file__).read_text(encoding="utf-8").lower()
    assert "schtasks" not in source
    assert "taskscheduler" not in source.casefold()
    assert update_module.__dict__.get("run_scheduled") is None


def test_messages_are_chinese_functions():
    """更新/匹配/状态文案均由 messages 模块函数产出（GUI 复用）。"""
    from src.fetcher import messages

    assert "更新" in messages.msg_update_ok("2026-11-06")
    assert "风控" in messages.msg_update_aborted(429)
    assert "仅昵称匹配" in messages.msg_partial_notice()
    assert "黄标" in messages.msg_level_note()
    assert "免责" not in messages.DISCLAIMER and "不推断" in messages.DISCLAIMER
