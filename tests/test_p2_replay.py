# -*- coding: utf-8 -*-
"""P2 录像解析 / 扫描测试：合成录像（可控）+ 真实样本（research 目录存在时）。

覆盖：完整/partial 解析、损坏跳过、增量缓存 (path,size,mtime)、最近 N 天、
progress_cb + cancel、完整优先排序、battle_players 落库、真实 30/30 样本。
"""
import json
import os
import struct
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import models  # noqa: E402
from src.config import Config  # noqa: E402
from src.replay import parser, scanner  # noqa: E402
from src.store import db  # noqa: E402

MAGIC = bytes.fromhex("12323411")
RECORDER_DBID = 7048141181
ARENA_ID = 10973119249944463


# ---------------------------------------------------------------- 合成录像
def encode_replay(meta, results=None):
    """按实测容器格式编码：magic + 块数 + 重复(长度+JSON) + 二进制尾巴。"""
    blocks = [json.dumps(meta, ensure_ascii=False).encode("utf-8")]
    if results is not None:
        blocks.append(json.dumps(results, ensure_ascii=False).encode("utf-8"))
    payload = MAGIC + struct.pack("<I", len(blocks))
    for block in blocks:
        payload += struct.pack("<I", len(block)) + block
    return payload + b"\x00" * 32


def make_meta(date_time="15.11.2026 00:42:32", players=None):
    """构造 meta 块（vehicles 键为战斗内车辆 id 字符串）。"""
    if players is None:
        players = [("玩家甲", 1, "uk:GB134_Gryphon"), ("玩家乙", 2, "germany:G42_Maus")]
    vehicles = {}
    for index, (name, team, vehicle_type) in enumerate(players, start=1):
        vehicles[str(index)] = {
            "name": name, "clanAbbrev": "K-G" if index == 1 else "",
            "team": team, "vehicleType": vehicle_type,
        }
    return {
        "clientVersionFromXml": "坦克世界 v.2.4.1.1 #994",
        "clientVersionFromExe": "2.4.1.0",
        "dateTime": date_time,
        "mapName": "02_malinovka",
        "mapDisplayName": "马利诺夫卡",
        "battleType": 1,
        "regionCode": "CN",
        "serverName": "频道1",
        "playerID": RECORDER_DBID,
        "playerName": "kingbob_",
        "vehicles": vehicles,
    }


def make_results(dbid_names=None, arena_id=ARENA_ID):
    """构造结算块（players dict key=dbid，vehicles 关联车辆 id）。"""
    if dbid_names is None:
        dbid_names = [(RECORDER_DBID, "玩家甲"), (RECORDER_DBID + 1, "玩家乙")]
    players = {}
    vehicles = {}
    for index, (dbid, name) in enumerate(dbid_names, start=1):
        team = 1 if index <= len(dbid_names) / 2 else 2
        players[str(dbid)] = {
            "name": name, "realName": name, "clanAbbrev": "K-G", "clanDBID": 5136, "team": team,
        }
        vehicles[str(index)] = [{"accountDBID": dbid}]
    return [{
        "arenaUniqueID": arena_id,
        "players": players,
        "vehicles": vehicles,
        "common": {"duration": 900, "winnerTeam": 1, "arenaTypeID": 2},
    }]


def write_replay(path, meta=None, results=None):
    """写一个合成录像文件并返回路径。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(encode_replay(meta or make_meta(), results))
    return target


def touch_mtime(path, days_ago):
    """把文件 mtime 改成 N 天前。"""
    stamp = time.time() - days_ago * 86400.0
    os.utime(str(path), (stamp, stamp))


@pytest.fixture()
def conn():
    """内存 SQLite 连接（9 张表已建）。"""
    connection = db.connect(":memory:")
    yield connection
    connection.close()


def make_config(tmp_path, replay_dir):
    """构造指向临时录像目录的配置。"""
    return Config(project_root=tmp_path, replay_dir=Path(replay_dir),
                  db_path=Path(tmp_path) / "test.db")


# ---------------------------------------------------------------- 解析
def test_parse_full_replay(tmp_path):
    """完整局：ok + 2 名玩家 dbid + 坦克关联 + ISO 战斗时间 + arena id。"""
    path = write_replay(tmp_path / "full.wotreplay", results=make_results())
    detail = parser.parse_replay(path)
    assert detail.parse_status == models.PARSE_STATUS_OK
    assert detail.is_partial is False
    assert detail.block_count == 2
    assert detail.player_count == 2
    assert detail.arena_unique_id == ARENA_ID
    assert detail.recorder_dbid == RECORDER_DBID
    assert detail.recorder_name == "kingbob_"
    assert detail.client_version == "坦克世界 v.2.4.1.1 #994"
    assert detail.battle_time.startswith("2026-11-15T00:42:32")
    assert detail.map_name == "02_malinovka"
    assert detail.duration_s == 900
    dbids = sorted(player.dbid for player in detail.players)
    assert dbids == [RECORDER_DBID, RECORDER_DBID + 1]
    by_dbid = {player.dbid: player for player in detail.players}
    assert by_dbid[RECORDER_DBID].tank == "uk:GB134_Gryphon"
    assert by_dbid[RECORDER_DBID].clan_id == 5136
    assert by_dbid[RECORDER_DBID].battle_vehicle_id == "1"
    payload = parser.detail_to_dict(detail)
    assert json.dumps(payload, ensure_ascii=False)


def test_parse_partial_replay(tmp_path):
    """partial（block_count=1）：partial 标注 + 无 dbid 降级名册 + 无 arena id。"""
    path = write_replay(tmp_path / "partial.wotreplay", results=None)
    detail = parser.parse_replay(path)
    assert detail.parse_status == models.PARSE_STATUS_PARTIAL
    assert detail.is_partial is True
    assert detail.block_count == 1
    assert detail.arena_unique_id is None
    assert detail.player_count == 2
    assert all(player.dbid is None for player in detail.players)
    assert any("partial" in note for note in detail.notes)
    assert parser.read_arena_unique_id(path) is None


def test_parse_corrupt_replay_raises(tmp_path):
    """损坏 / 截断 / 块数越界：抛 ValueError，由 scanner 跳过。"""
    short = tmp_path / "short.wotreplay"
    short.write_bytes(b"abc")
    with pytest.raises(ValueError):
        parser.parse_replay(short)

    raw = encode_replay(make_meta(), make_results())
    truncated = tmp_path / "truncated.wotreplay"
    truncated.write_bytes(raw[:20])
    with pytest.raises(ValueError):
        parser.parse_replay(truncated)

    implausible = tmp_path / "implausible.wotreplay"
    implausible.write_bytes(MAGIC + struct.pack("<I", 999))
    with pytest.raises(ValueError):
        parser.parse_replay(implausible)


def test_read_arena_unique_id(tmp_path):
    """轻量读取 arena id（不构建完整玩家列表）。"""
    path = write_replay(tmp_path / "arena.wotreplay", results=make_results(arena_id=2315991864109994844))
    assert parser.read_arena_unique_id(path) == 2315991864109994844


# ---------------------------------------------------------------- 扫描 / 增量缓存
def test_scan_indexes_and_caches(tmp_path, conn):
    """首扫解析 3 个（含 1 个损坏跳过），二扫全部命中缓存；落库 replays/battle_players。"""
    replay_dir = tmp_path / "replays"
    write_replay(replay_dir / "20261115_0042_complete.wotreplay", results=make_results())
    write_replay(replay_dir / "20261114_0000_complete.wotreplay",
                 make_meta(date_time="14.11.2026 23:00:00"), make_results(arena_id=1))
    write_replay(replay_dir / "20261113_0000_partial.wotreplay",
                 make_meta(date_time="13.11.2026 20:00:00"), None)
    (replay_dir / "20261112_0000_broken.wotreplay").write_bytes(b"broken")
    config = make_config(tmp_path, replay_dir)

    result = scanner.scan_replays(config=config, conn=conn)
    stats = result.stats
    assert stats.total == 4
    assert stats.parsed == 3
    assert stats.failed == 1
    assert stats.cached == 0
    assert stats.complete == 2
    assert stats.partial == 1
    assert len(result.replays) == 3
    assert len(stats.errors) == 1
    # 完整优先：前两个是完整局
    assert [detail.block_count for detail in result.replays[:2]] == [2, 2]
    assert result.replays[2].parse_status == models.PARSE_STATUS_PARTIAL

    rows = conn.execute("SELECT path, block_count, parse_status, player_count FROM replays "
                        "ORDER BY path").fetchall()
    assert len(rows) == 3
    status_by_suffix = {Path(row["path"]).name: row["parse_status"] for row in rows}
    assert status_by_suffix["20261113_0000_partial.wotreplay"] == models.PARSE_STATUS_PARTIAL
    # 2 个完整局 × 2 人 + partial 名册 2 人 = 6 行
    assert conn.execute("SELECT COUNT(*) AS n FROM battle_players").fetchone()["n"] == 6
    null_dbids = conn.execute(
        "SELECT COUNT(*) AS n FROM battle_players WHERE dbid IS NULL").fetchone()["n"]
    assert null_dbids == 2  # partial 名册只有昵称

    second = scanner.scan_replays(config=config, conn=conn)
    assert second.stats.cached == 3
    assert second.stats.parsed == 0
    assert second.stats.failed == 1
    assert len(second.replays) == 3
    assert second.replays[0].parse_status == models.PARSE_STATUS_OK
    assert "cached" in " ".join(second.replays[0].notes)


def test_scan_incremental_reparse_on_change(tmp_path, conn):
    """文件 size/mtime 变化 → 缓存失效重新解析（增量语义）。"""
    replay_dir = tmp_path / "replays"
    target = write_replay(replay_dir / "20261115_0042_complete.wotreplay", results=make_results())
    config = make_config(tmp_path, replay_dir)
    first = scanner.scan_replays(config=config, conn=conn)
    assert first.stats.parsed == 1

    # 内容变化（玩家数变多 → size 变化）
    more = [(RECORDER_DBID + index, "玩家%d" % index) for index in range(4)]
    target.write_bytes(encode_replay(make_meta(), make_results(dbid_names=more)))
    second = scanner.scan_replays(config=config, conn=conn)
    assert second.stats.parsed == 1
    assert second.stats.cached == 0
    assert second.replays[0].player_count == 4
    assert conn.execute("SELECT COUNT(*) AS n FROM battle_players").fetchone()["n"] == 4


def test_scan_recent_days_filter(tmp_path, conn):
    """「最近 N 天」按 mtime 过滤：30 天前的文件不进入 days=7 扫描。"""
    replay_dir = tmp_path / "replays"
    write_replay(replay_dir / "20261001_0000_old.wotreplay", results=make_results())
    touch_mtime(replay_dir / "20261001_0000_old.wotreplay", 30)
    write_replay(replay_dir / "20261115_0042_new.wotreplay", results=make_results(arena_id=2))
    config = make_config(tmp_path, replay_dir)

    recent = scanner.scan_replays(config=config, conn=conn, days=7)
    assert recent.stats.total == 1
    assert recent.replays[0].file_name == "20261115_0042_new.wotreplay"

    everything = scanner.scan_replays(config=config, conn=conn, days=None)
    assert everything.stats.total == 2
    assert everything.stats.cached == 1
    assert everything.stats.parsed == 1


def test_scan_progress_and_cancel(tmp_path, conn):
    """progress_cb 逐文件回调；cancel_event 置位后停止且不报错。"""
    replay_dir = tmp_path / "replays"
    for index in range(3):
        write_replay(replay_dir / ("2026111%d_0000_r%d.wotreplay" % (index, index)),
                     results=make_results(arena_id=index + 1))
    config = make_config(tmp_path, replay_dir)
    import threading

    cancel_event = threading.Event()
    calls = []

    def progress(done, total, path, detail):
        calls.append((done, total, Path(path).name, detail is not None))
        if done == 1:
            cancel_event.set()

    result = scanner.scan_replays(config=config, conn=conn, progress_cb=progress,
                                  cancel_event=cancel_event)
    assert result.stats.cancelled is True
    assert result.stats.total == 3
    assert len(calls) == 1
    assert calls[0][0] == 1 and calls[0][1] == 3
    assert len(result.replays) == 1


def test_scan_persist_false(tmp_path):
    """persist=False：纯解析不碰数据库（预览/测试路径）。"""
    replay_dir = tmp_path / "replays"
    write_replay(replay_dir / "20261115_0042_complete.wotreplay", results=make_results())
    config = make_config(tmp_path, replay_dir)
    result = scanner.scan_replays(config=config, conn=None, persist=False)
    assert result.stats.parsed == 1
    assert result.stats.complete == 1
    assert not config.db_path.exists()


def test_scan_writes_aliases(tmp_path, conn):
    """扫描完整局写 aliases（改名链）：min/max 聚合、幂等、partial 不写。"""
    replay_dir = tmp_path / "replays"
    write_replay(replay_dir / "20261110_0000_a.wotreplay",
                 make_meta(date_time="10.11.2026 20:00:00"),
                 make_results(dbid_names=[(7000000001, "老名字"), (7000000002, "玩家乙")]))
    write_replay(replay_dir / "20261115_0000_b.wotreplay",
                 make_meta(date_time="15.11.2026 20:00:00"),
                 make_results(dbid_names=[(7000000001, "新名字"), (7000000002, "玩家乙")]))
    write_replay(replay_dir / "20261113_0000_partial.wotreplay",
                 make_meta(date_time="13.11.2026 20:00:00"), None)
    config = make_config(tmp_path, replay_dir)

    result = scanner.scan_replays(config=config, conn=conn)
    assert result.stats.aliases_written == 4  # 2 完整局 × 2 名有 dbid 玩家；partial 不写
    rows = conn.execute(
        "SELECT account_id, nickname, first_seen, last_seen, source FROM aliases "
        "ORDER BY account_id, nickname").fetchall()
    assert len(rows) == 3
    by_key = {(row["account_id"], row["nickname"]): row for row in rows}
    assert all(row["source"] == "replay" for row in rows)
    # 同一 dbid 改过名 → 两行（改名链）
    assert by_key[(7000000001, "老名字")]["first_seen"].startswith("2026-11-10T20:00:00")
    assert by_key[(7000000001, "老名字")]["last_seen"].startswith("2026-11-10T20:00:00")
    assert by_key[(7000000001, "新名字")]["last_seen"].startswith("2026-11-15T20:00:00")
    # 同 dbid 同昵称跨两局 → 一行，first=min / last=max
    assert by_key[(7000000002, "玩家乙")]["first_seen"].startswith("2026-11-10T20:00:00")
    assert by_key[(7000000002, "玩家乙")]["last_seen"].startswith("2026-11-15T20:00:00")
    # partial 名册里的「玩家甲」没有 dbid → 不写 aliases
    assert not any(row["nickname"] == "玩家甲" for row in rows)

    # 幂等：再扫一遍（全部缓存命中）不新增行、不改 min/max
    second = scanner.scan_replays(config=config, conn=conn)
    assert second.stats.cached == 3
    assert second.stats.aliases_written == 4
    rows_again = conn.execute(
        "SELECT account_id, nickname, first_seen, last_seen, source FROM aliases "
        "ORDER BY account_id, nickname").fetchall()
    assert [tuple(row) for row in rows_again] == [tuple(row) for row in rows]


def test_find_latest_complete_replay(tmp_path, conn):
    """完整优先：最新的是 partial，也继续往旧找并返回完整局。"""
    replay_dir = tmp_path / "replays"
    complete = write_replay(replay_dir / "20261115_0042_complete.wotreplay",
                            results=make_results())
    write_replay(replay_dir / "20261115_0100_newest_partial.wotreplay", None, None)
    # 让 partial 的 mtime 明确更新（写盘顺序不足以表达「最新」）
    stamp = time.time() - 3600
    os.utime(str(complete), (stamp, stamp))
    config = make_config(tmp_path, replay_dir)
    detail = scanner.find_latest_complete_replay(config=config, conn=conn, days=None)
    assert detail is not None
    assert detail.file_name == "20261115_0042_complete.wotreplay"
    assert detail.block_count == 2
    # 扫过的 partial 与命中的完整局都已落库，可被后续扫描复用
    assert conn.execute("SELECT COUNT(*) AS n FROM replays").fetchone()["n"] == 2


def test_find_latest_complete_fallback_all(tmp_path, conn):
    """最近 7 天没有完整局时退化为全量（默认 fallback_all=True）。"""
    replay_dir = tmp_path / "replays"
    old = write_replay(replay_dir / "20260901_0000_complete.wotreplay", results=make_results())
    touch_mtime(old, 40)
    config = make_config(tmp_path, replay_dir)
    detail = scanner.find_latest_complete_replay(config=config, conn=conn)
    assert detail is not None
    assert detail.file_name == "20260901_0000_complete.wotreplay"


# ---------------------------------------------------------------- 真实样本
REAL_SAMPLES_DIR = PROJECT_ROOT.parent / "research" / "wot-ban" / "replay" / "samples"


@pytest.mark.skipif(not REAL_SAMPLES_DIR.is_dir(), reason="research 真实样本目录不存在")
def test_real_sample_replays():
    """真实国服样本：2 个完整局各 30/30 玩家，1 个 partial 无 dbid。"""
    samples = sorted(REAL_SAMPLES_DIR.glob("*.wotreplay"))
    assert len(samples) >= 3
    details = [parser.parse_replay(path) for path in samples]
    complete = [detail for detail in details if detail.block_count == 2]
    partial = [detail for detail in details if detail.block_count == 1]
    assert len(complete) == 2
    assert len(partial) == 1
    for detail in complete:
        assert detail.parse_status == models.PARSE_STATUS_OK
        assert detail.player_count == 30
        assert all(player.dbid and len(str(player.dbid)) >= 9 for player in detail.players)
        assert detail.arena_unique_id
        assert detail.battle_time
    assert partial[0].parse_status == models.PARSE_STATUS_PARTIAL
    assert partial[0].player_count > 0
    assert all(player.dbid is None for player in partial[0].players)


def main(argv):
    """独立运行入口：顺序执行关键测试（便于手工冒烟）。"""
    import tempfile

    checks = [test_parse_full_replay, test_parse_partial_replay, test_parse_corrupt_replay_raises,
              test_read_arena_unique_id, test_real_sample_replays]
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for check in checks:
            try:
                check(Path(tmp))
                print("[ok] %s" % check.__name__)
            except Exception as exc:  # noqa: BLE001 - 冒烟脚本需汇总全部失败
                failures += 1
                print("[fail] %s: %s" % (check.__name__, exc))
    print("[done] %d 项通过，%d 项失败" % (len(checks) - failures, failures))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
