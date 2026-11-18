# -*- coding: utf-8 -*-
"""P0 复验 + P1 ID 精确链路端到端测试（本机有录像/存档时才跑，否则跳过）。

复现基线（计划文档 §8 P0、§9 #4）：
- 根目录 1046 个录像：完整 460 / partial 586（56.0%）/ 解析失败 0，唯一 dbid 11,402；
- 与 wgid 存档 100 条求交集 = 3 个精确命中：7053765071 / 7057264568 / 7068999243。
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import load_config  # noqa: E402
from src.fetcher import p0_probe  # noqa: E402
from src.match import matcher  # noqa: E402
from src.models import LEVEL_YELLOW, MATCH_TYPE_ID  # noqa: E402
from src.store import bans, db  # noqa: E402

CFG = load_config()
WGID_JSON = Path(CFG.archive_dir) / "wgid" / "2026-11-06.json"
KNOWN_FILES = [
    "20261020_0226_germany-G193_Pz_Kpfw_55_04_himmelsdorf.wotreplay",   # 7053765071
    "20261019_1800_germany-G193_Pz_Kpfw_55_60_asia_miao.wotreplay",     # 7057264568
    "20261029_0543_usa-A178_SPHT_31_airfield.wotreplay",                # 7068999243
]


def _require_replays():
    if not WGID_JSON.is_file():
        pytest.skip("缺少 wgid 存档，跳过 P0 复验")
    paths = [Path(CFG.replay_dir) / name for name in KNOWN_FILES]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        pytest.skip("本机缺少 P0 基线录像：%s" % "、".join(missing))
    return paths


def test_p0_known_intersections_reproduced():
    """解析 3 个已知完整录像，与 wgid 存档求交集 = 3 个精确命中且昵称一致。"""
    paths = _require_replays()
    evidence = p0_probe.build_evidence(CFG.replay_dir, WGID_JSON, recursive=False, paths=paths)
    assert evidence["stats"]["files"] == 3
    assert evidence["stats"]["complete"] == 3
    assert evidence["stats"]["failed"] == 0
    assert evidence["known_hit_count"] == 3
    assert {item["wgid"] for item in evidence["intersections"]} == set(p0_probe.KNOWN_INTERSECTIONS)
    for item in evidence["intersections"]:
        assert item["nickname_consistent"] is True
        assert item["replay_files"], "命中项应记录录像文件"


def test_p1_id_match_end_to_end(tmp_path):
    """端到端：wgid 存档 -> 名单库 -> 解析录像 -> matcher 产出「ID 确认」黄标。"""
    paths = _require_replays()
    conn = db.connect(tmp_path / "p0-e2e.db")
    try:
        bans.import_wgid_file(conn, WGID_JSON)
        index = bans.load_ban_index(conn)
        found = {}
        for path in paths:
            detail = __import__("src.replay.parser", fromlist=["parse_replay"]).parse_replay(path)
            assert detail.is_partial is False
            for matched in matcher.match_replay(detail, index):
                if matched.match_type == MATCH_TYPE_ID:
                    found[matched.dbid] = matched
        assert set(found) >= set(p0_probe.KNOWN_INTERSECTIONS)
        for wgid in p0_probe.KNOWN_INTERSECTIONS:
            matched = found[wgid]
            assert matched.level == LEVEL_YELLOW
            assert matched.ban_count == 1
            assert matched.records[0].batch_day == "2026-11-06"
            assert matched.is_id_matched is True
    finally:
        conn.close()
