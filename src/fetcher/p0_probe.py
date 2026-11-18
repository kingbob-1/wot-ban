# -*- coding: utf-8 -*-
"""P0 可行性复验工具：录像 dbid ↔ 最新一期 wgid 存档求交集（可重复执行）。

用途（计划文档 §8 P0 / §9 #4）
----------------------------
- 解析指定录像目录（默认 ``Config.replay_dir`` 根目录，不递归）的全部 ``*.wotreplay``；
- 统计完整 / partial / 解析失败、参战玩家唯一 dbid 数、partial 占比；
- 与 ``research\\wot-ban\\archive\\wgid\\2026-11-06.json`` 的 100 条 wgid 求交集；
- 已知精确交集（2026-11-18 复验基线）：7053765071 / 7057264568 / 7068999243。

用法
----
    ..\\runtime\\python.exe src\\fetcher\\p0_probe.py
    ... --replay-dir "<录像目录>" --wgid-json <path> --out data\\p0_evidence.json
    ... --files <a.wotreplay> <b.wotreplay>        # 只解析指定文件（快速复验）
    ... --recursive                                # 递归子目录（归档目录一起扫）

默认录像目录取 ``Config.replay_dir``（启动时由 ``src/detect.py`` 动态探测，v0.2 起不写死）；
未探测到则给出中文提示。只读：仅读取录像文件与 wgid 存档，不写数据库、不改任何数据；
证据 JSON 由 ``--out`` 指定。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

try:
    from ..config import load_config
    from ..replay import parser as replay_parser
except ImportError:  # 直接以脚本方式运行时的兜底
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.config import load_config
    from src.replay import parser as replay_parser

KNOWN_INTERSECTIONS = (7053765071, 7057264568, 7068999243)


def load_wgid_archive(path) -> tuple:
    """读取 wgid 存档 JSON，返回 ``(meta, {wgid: item})``。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    items = (payload.get("data") or {}).get("items") or []
    bans = {}
    for item in items:
        wgid = item.get("wgid")
        if wgid is None:
            continue
        bans[int(wgid)] = {
            "wgid": int(wgid), "nickname": item.get("nickname"),
            "reason": item.get("reason"), "duration": item.get("duration"), "day": item.get("day"),
        }
    return payload.get("meta") or {}, bans


def discover_files(replay_dir, recursive: bool) -> list:
    """列出录像文件（默认只看根目录：用户为避免同层过多专门建了归档子目录）。"""
    root = Path(replay_dir)
    if not root.is_dir():
        return []
    pattern = "**/*.wotreplay" if recursive else "*.wotreplay"
    return sorted(root.glob(pattern))


def probe_files(paths: list) -> dict:
    """解析录像文件，返回统计与 dbid 明细（解析异常计入 failed，不中断）。"""
    stats = {"files": len(paths), "complete": 0, "partial": 0, "failed": 0, "players": 0}
    dbids = {}
    records = []
    for path in paths:
        try:
            detail = replay_parser.parse_replay(path)
        except (OSError, ValueError) as exc:
            stats["failed"] += 1
            records.append({"file": Path(path).name, "parse_status": "error", "error": str(exc)})
            continue
        is_complete = (detail.block_count or 0) >= 2 and detail.player_count > 0
        stats["complete" if is_complete else "partial"] += 1
        stats["players"] += detail.player_count
        players = []
        for player in detail.players or []:
            nickname = getattr(player, "nickname", None)
            dbid = getattr(player, "dbid", None)
            players.append({"dbid": dbid, "nickname": nickname,
                            "clan_abbrev": getattr(player, "clan_abbrev", None),
                            "team": getattr(player, "team", None),
                            "tank": getattr(player, "tank", None)})
            if isinstance(dbid, int):
                dbids.setdefault(dbid, {"nicknames": set(), "files": []})
                if nickname:
                    dbids[dbid]["nicknames"].add(nickname)
                if len(dbids[dbid]["files"]) < 5:
                    dbids[dbid]["files"].append(Path(path).name)
        records.append({
            "file": Path(path).name, "file_path": str(path), "block_count": detail.block_count,
            "parse_status": detail.parse_status, "battle_time": detail.battle_time,
            "map_name": detail.map_display_name or detail.map_name,
            "winner_team": detail.winner_team, "duration_s": detail.duration_s,
            "players_count": detail.player_count, "players": players,
        })
    stats["unique_dbids"] = len(dbids)
    if stats["files"]:
        stats["partial_ratio"] = round(stats["partial"] / stats["files"], 4)
    return {"stats": stats, "dbids": dbids, "records": records}


def intersect(dbids: dict, bans: dict) -> list:
    """dbid 与 wgid 存档求精确交集（附录像内昵称与命中文件）。"""
    hits = []
    for dbid in sorted(set(dbids) & set(bans)):
        entry = bans[dbid]
        hits.append({
            "wgid": dbid,
            "ban_nickname": entry["nickname"],
            "ban_day": entry["day"],
            "ban_reason": entry["reason"],
            "replay_nicknames": sorted(dbids[dbid]["nicknames"]),
            "replay_files": dbids[dbid]["files"],
            "nickname_consistent": entry["nickname"] in dbids[dbid]["nicknames"],
        })
    return hits


def build_evidence(replay_dir, wgid_json, recursive=False, paths=None) -> dict:
    """跑一遍复验，返回证据 dict（可直接 json.dumps 落盘）。"""
    started = time.time()
    target_dir = Path(replay_dir) if replay_dir else load_config().replay_dir
    wgid_path = Path(wgid_json)
    meta, bans = load_wgid_archive(wgid_path)
    files = [Path(item) for item in paths] if paths else discover_files(target_dir, recursive)
    probed = probe_files(files)
    hits = intersect(probed["dbids"], bans)
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "replay_dir": str(target_dir),
        "recursive": bool(recursive),
        "wgid_json": str(wgid_path),
        "wgid_meta": {key: meta.get(key) for key in
                      ("date", "fetched_at", "count", "items_returned", "items_with_wgid", "source_url")},
        "stats": probed["stats"],
        "wgid_rows": len(bans),
        "intersections": hits,
        "known_intersections": list(KNOWN_INTERSECTIONS),
        "known_hit_count": len({item["wgid"] for item in hits} & set(KNOWN_INTERSECTIONS)),
        "replays": probed["records"],
        "elapsed_s": round(time.time() - started, 2),
    }


def parse_args(argv: list) -> dict:
    """极简参数解析（保持零依赖，不引 argparse 之外的东西）。"""
    options = {"replay_dir": None, "wgid_json": None, "out": None, "recursive": False,
               "files": [], "quiet": False}
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--recursive":
            options["recursive"] = True
        elif arg == "--quiet":
            options["quiet"] = True
        elif arg in ("--replay-dir", "--wgid-json", "--out") and index + 1 < len(argv):
            key = {"--replay-dir": "replay_dir", "--wgid-json": "wgid_json", "--out": "out"}[arg]
            options[key] = argv[index + 1]
            index += 1
        elif arg == "--files":
            options["files"] = argv[index + 1:]
            break
        index += 1
    return options


def main(argv: list) -> int:
    """CLI 入口：打印复验摘要与交集明细，返回 0 = 复现已知交集。"""
    start = time.time()
    options = parse_args(argv)
    cfg = load_config()
    replay_dir = Path(options["replay_dir"]) if options["replay_dir"] else cfg.replay_dir
    wgid_json = Path(options["wgid_json"]) if options["wgid_json"] else (
        Path(cfg.archive_dir) / "wgid" / "2026-11-06.json")
    out_path = Path(options["out"]) if options["out"] else None
    files = options["files"] or None
    print("[1/2] 复验目标：replay_dir=%s wgid=%s files=%s" %
          (replay_dir or "未检测到", wgid_json, len(files) if files else "全部"))
    if replay_dir is None or not Path(replay_dir).is_dir():
        print("[error] 未检测到录像目录：请在 config.local.json 指定 replay_dir，"
              "或用 --replay-dir 显式传入录像目录")
        return 2
    if not Path(wgid_json).is_file():
        print("[error] 缺少 wgid 存档：%s" % wgid_json)
        return 2
    evidence = build_evidence(replay_dir, wgid_json, options["recursive"], files)
    stats = evidence["stats"]
    if not options["quiet"]:
        print("[2/2] 录像 %d（完整 %d / partial %d / 失败 %d），玩家 %d，唯一 dbid %d，partial 占比 %s"
              % (stats["files"], stats["complete"], stats["partial"], stats["failed"], stats["players"],
                 stats["unique_dbids"], stats.get("partial_ratio")))
        print("      wgid 存档 %d 条，精确交集 %d 个（已知基线命中 %d/%d）"
              % (evidence["wgid_rows"], len(evidence["intersections"]),
                 evidence["known_hit_count"], len(KNOWN_INTERSECTIONS)))
        for item in evidence["intersections"]:
            print("      HIT wgid=%s 名单昵称=%s 录像昵称=%s 文件=%s"
                  % (item["wgid"], item["ban_nickname"], "、".join(item["replay_nicknames"]),
                     "、".join(item["replay_files"])))
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
        print("      证据已写入：%s" % out_path)
    elapsed = int(time.time() - start)
    print("[done] 耗时 %02d:%02d:%02d" % (elapsed // 3600, elapsed % 3600 // 60, elapsed % 60))
    return 0 if evidence["known_hit_count"] > 0 else 1


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
