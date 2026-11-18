# -*- coding: utf-8 -*-
"""录像解析（P2）：移植 ``research\\wot-ban\\replay\\probe_parse.py``，返回项目数据模型。

容器格式（实测，详见 ``research\\wot-ban\\replay\\findings.md`` §2）
--------------------------------------------------------------------
::

    [0:4]   4 字节 magic = 12 32 34 11（1,128 个真实文件一致）
    [4:8]   uint32 LE = JSON 块数量 N（2=完整局、1=partial 未打完）
    之后重复 N 次：uint32 LE 块长度 + L 字节 UTF-8 明文 JSON
    最后一个 JSON 块之后 = 二进制战斗段（本项目不读）

- 块 0 = 对局 meta（版本/地图/时间/录制者/vehicles 名册，**无其他玩家 dbid**）；
- 块 1 = 战斗结算（``players`` dict，key 即账号 dbid；``vehicles`` 关联坦克）。

语义约定（与 probe_parse.py 保持一致）
--------------------------------------
- ``read_container`` / ``find_meta`` / ``find_results`` / ``build_player_rows`` 接口保留；
- 完整局返回 ``models.ReplayDetail(parse_status=ok)``，players 含 dbid；
- partial（缺结算块）返回 ``parse_status=partial``，players 为无 dbid 的降级名册
  （昵称/军团/坦克仍可用于「仅昵称匹配」）；
- 损坏 / 截断 / 块长度越界抛 ``ValueError``，读文件失败抛 ``OSError``，由 scanner 捕获跳过。

独立运行
--------
::

    python src/replay/parser.py <文件或目录> [--out 结果.json] [--quiet]
"""
from __future__ import annotations

import json
import struct
import sys
import time
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    from .. import models
except ImportError:  # 直接以脚本方式运行（python src/replay/parser.py）时的兜底
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src import models

HEADER_MAGIC = bytes.fromhex("12323411")
HEADER_SIZE = 8
MAX_BLOCK_COUNT = 8
# 国服录像 meta.dateTime 格式，例如 ``15.11.2026 00:42:32``（日.月.年 时:分:秒）。
BATTLE_TIME_FORMAT = "%d.%m.%Y %H:%M:%S"
REPLAY_SUFFIX = ".wotreplay"


# ---------------------------------------------------------------- 容器读取
def decode_block_payload(raw: bytes) -> Any:
    """解码单个块：优先 UTF-8 明文 JSON，失败时回退 zlib + JSON。"""
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        pass
    try:
        decompressed = zlib.decompress(raw)
    except zlib.error:
        return None
    try:
        return json.loads(decompressed.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def read_container(path: Any) -> dict:
    """流式读取容器：8 字节头 + 各 JSON 块，**不读**末尾二进制战斗段。

    返回 dict：``magic_hex / magic_ok / block_count / blocks / json_block_bytes /
    binary_tail_bytes / file_bytes``。格式异常时抛 ``ValueError``。
    """
    file_path = Path(path)
    file_size = file_path.stat().st_size
    with open(file_path, "rb") as handle:
        header = handle.read(HEADER_SIZE)
        if len(header) < HEADER_SIZE:
            raise ValueError("file too short: %d bytes" % len(header))
        magic = header[:4]
        block_count = struct.unpack_from("<I", header, 4)[0]
        if block_count > MAX_BLOCK_COUNT:
            raise ValueError("implausible block count: %d" % block_count)
        offset = HEADER_SIZE
        blocks = []
        for index in range(block_count):
            size_raw = handle.read(4)
            if len(size_raw) < 4:
                raise ValueError("truncated block header at offset %d" % offset)
            size = struct.unpack("<I", size_raw)[0]
            if size <= 0 or size > file_size:
                raise ValueError("block %d size %d exceeds file bounds" % (index, size))
            raw = handle.read(size)
            if len(raw) < size:
                raise ValueError("truncated block %d payload" % index)
            offset += 4 + size
            blocks.append({"index": index, "raw": raw, "json": decode_block_payload(raw)})
    return {
        "magic_hex": magic.hex(),
        "magic_ok": magic == HEADER_MAGIC,
        "block_count": block_count,
        "blocks": blocks,
        "json_block_bytes": sum(len(block["raw"]) for block in blocks),
        "binary_tail_bytes": file_size - offset,
        "file_bytes": file_size,
    }


# ---------------------------------------------------------------- 块定位
def find_meta(blocks: list) -> Optional[dict]:
    """返回 meta JSON 对象（首个含 ``clientVersionFromXml`` 的 dict）。"""
    for block in blocks:
        obj = block["json"]
        if isinstance(obj, dict) and "clientVersionFromXml" in obj:
            return obj
    for block in blocks:
        if isinstance(block["json"], dict):
            return block["json"]
    return None


def find_results(blocks: list) -> Optional[dict]:
    """返回结算对象（含 ``players`` dict 的 dict；块本身可能是数组）。"""
    for block in blocks:
        obj = block["json"]
        candidates = obj if isinstance(obj, list) else [obj]
        for item in candidates:
            if isinstance(item, dict) and isinstance(item.get("players"), dict):
                return item
    return None


# ---------------------------------------------------------------- 字段归一化
def meta_record(meta: Optional[dict]) -> dict:
    """归一化 meta 块（probe_parse.meta_record 语义保持）。"""
    if not isinstance(meta, dict):
        return {}
    return {
        "client_version_xml": meta.get("clientVersionFromXml"),
        "client_version_exe": meta.get("clientVersionFromExe"),
        "date_time": meta.get("dateTime"),
        "map_name": meta.get("mapName"),
        "map_display_name": meta.get("mapDisplayName"),
        "battle_type": meta.get("battleType"),
        "gameplay_id": meta.get("gameplayID"),
        "region_code": meta.get("regionCode"),
        "server_name": meta.get("serverName"),
        "has_mods": meta.get("hasMods"),
        "recorder_dbid": meta.get("playerID"),
        "recorder_name": meta.get("playerName"),
        "recorder_vehicle": meta.get("playerVehicle"),
        "meta_vehicle_count": len(meta.get("vehicles") or {}),
    }


def results_record(results: Optional[dict]) -> dict:
    """归一化结算块（probe_parse.results_record 语义保持）。"""
    if not isinstance(results, dict):
        return {}
    common = results.get("common") or {}
    return {
        "arena_unique_id": results.get("arenaUniqueID"),
        "duration_s": common.get("duration"),
        "winner_team": common.get("winnerTeam"),
        "arena_type_id": common.get("arenaTypeID"),
        "arena_create_time": common.get("arenaCreateTime"),
        "finish_reason": common.get("finishReason"),
        "results_player_count": len(results.get("players") or {}),
        "results_vehicle_count": len(results.get("vehicles") or {}),
    }


def build_player_rows(meta: Optional[dict], results: Optional[dict]) -> list:
    """把 meta.vehicles + results.players/vehicles 关联成 ``models.ReplayPlayer`` 列表。

    dbid / 昵称 / 实名 / 军团 / 阵营来自 results.players；坦克由
    ``results.vehicles[vid][0].accountDBID`` 反查 ``meta.vehicles[vid].vehicleType`` 得到。
    """
    players = (results or {}).get("players") or {}
    result_vehicles = (results or {}).get("vehicles") or {}
    meta_vehicles = (meta or {}).get("vehicles") or {}

    dbid_to_vid = {}
    for vid, entries in result_vehicles.items():
        if isinstance(entries, list) and entries and isinstance(entries[0], dict):
            entry = entries[0]
            dbid = entry.get("accountDBID")
            if dbid:
                dbid_to_vid[str(dbid)] = str(vid)

    rows = []
    for dbid, info in players.items():
        if not isinstance(info, dict):
            continue
        vid = dbid_to_vid.get(str(dbid))
        meta_vehicle = meta_vehicles.get(vid) if vid else None
        rows.append(models.ReplayPlayer(
            dbid=_to_int(dbid),
            nickname=info.get("name"),
            real_name=info.get("realName"),
            clan_abbrev=info.get("clanAbbrev") or (meta_vehicle or {}).get("clanAbbrev"),
            clan_id=_to_int(info.get("clanDBID")),
            team=_to_int(info.get("team")),
            tank=(meta_vehicle or {}).get("vehicleType"),
            battle_vehicle_id=vid,
        ))
    rows.sort(key=lambda row: (str(row.team), str(row.dbid)))
    return rows


def build_roster_without_dbid(meta: Optional[dict]) -> list:
    """partial 录像降级名册：只有昵称/军团/坦克，无账号 dbid。"""
    rows = []
    for vid, info in ((meta or {}).get("vehicles") or {}).items():
        if not isinstance(info, dict):
            continue
        rows.append(models.ReplayPlayer(
            dbid=None,
            nickname=info.get("name"),
            clan_abbrev=info.get("clanAbbrev"),
            team=_to_int(info.get("team")),
            tank=info.get("vehicleType"),
            battle_vehicle_id=str(vid),
        ))
    rows.sort(key=lambda row: (str(row.team), str(row.nickname)))
    return rows


def parse_battle_time(value: Any) -> Optional[str]:
    """把 meta.dateTime（``15.11.2026 00:42:32``）转 ISO 8601；无法解析时原样返回。"""
    if value is None or value == "":
        return None
    text = str(value).strip()
    try:
        return datetime.strptime(text, BATTLE_TIME_FORMAT).astimezone().isoformat(timespec="seconds")
    except ValueError:
        return text


def _to_int(value: Any) -> Optional[int]:
    """安全转 int（数字字符串/浮点/None），失败返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 单文件解析
def parse_replay(path: Any) -> models.ReplayDetail:
    """解析单个录像文件 → ``models.ReplayDetail``。

    - 完整局：``parse_status=ok``，players 含 dbid；
    - partial：``parse_status=partial``，players 为无 dbid 降级名册；
    - 容器可读但块数 >=2 却找不到结算块：``parse_status=error``（保留名册，不中断批量）；
    - 文件损坏 / 截断：抛 ``ValueError``；读文件失败：抛 ``OSError``。
    """
    file_path = Path(path)
    stat = file_path.stat()
    container = read_container(file_path)
    meta = find_meta(container["blocks"])
    results = find_results(container["blocks"])
    meta_info = meta_record(meta)
    results_info = results_record(results)

    detail = models.ReplayDetail(
        file_path=str(file_path.resolve()),
        file_name=file_path.name,
        file_size=stat.st_size,
        mtime=stat.st_mtime,
        battle_time=parse_battle_time(meta_info.get("date_time")),
        map_name=meta_info.get("map_name"),
        map_display_name=meta_info.get("map_display_name"),
        client_version=meta_info.get("client_version_xml") or meta_info.get("client_version_exe"),
        block_count=container["block_count"],
        duration_s=_to_int(results_info.get("duration_s")),
        winner_team=_to_int(results_info.get("winner_team")),
        arena_unique_id=_to_int(results_info.get("arena_unique_id")),
        recorder_dbid=_to_int(meta_info.get("recorder_dbid")),
        recorder_name=meta_info.get("recorder_name"),
    )
    if results is not None:
        detail.players = build_player_rows(meta, results)
        detail.parse_status = models.PARSE_STATUS_OK
        if not detail.players:
            detail.notes.append("results block found but no players parsed")
    else:
        detail.players = build_roster_without_dbid(meta)
        if container["block_count"] < 2:
            detail.parse_status = models.PARSE_STATUS_PARTIAL
            detail.notes.append(
                "results block missing (partial replay): other players' dbid unavailable; "
                "fallback roster has names/clans/tanks only"
            )
        else:
            detail.parse_status = models.PARSE_STATUS_ERROR
            detail.notes.append("block_count>=2 but results block missing or unparsable")
    if not container["magic_ok"]:
        detail.notes.append("unexpected magic bytes: %s" % container["magic_hex"])
    return detail


def read_arena_unique_id(path: Any) -> Optional[int]:
    """轻量读取一局的 arena_unique_id（供战绩接口按需查询；不落缓存）。"""
    container = read_container(path)
    results = find_results(container["blocks"])
    if results is None:
        return None
    return _to_int(results.get("arenaUniqueID"))


# ---------------------------------------------------------------- 证据输出
def detail_to_dict(detail: models.ReplayDetail) -> dict:
    """把解析结果转成可 JSON 序列化的证据字典。"""
    return {
        "file": detail.file_name,
        "file_path": detail.file_path,
        "file_size": detail.file_size,
        "mtime": detail.mtime,
        "battle_time": detail.battle_time,
        "map_name": detail.map_name,
        "map_display_name": detail.map_display_name,
        "client_version": detail.client_version,
        "block_count": detail.block_count,
        "duration_s": detail.duration_s,
        "winner_team": detail.winner_team,
        "arena_unique_id": detail.arena_unique_id,
        "recorder_dbid": detail.recorder_dbid,
        "recorder_name": detail.recorder_name,
        "parse_status": detail.parse_status,
        "is_partial": detail.is_partial,
        "player_count": detail.player_count,
        "notes": list(detail.notes),
        "players": [
            {
                "dbid": player.dbid,
                "nickname": player.nickname,
                "real_name": player.real_name,
                "clan_abbrev": player.clan_abbrev,
                "clan_id": player.clan_id,
                "team": player.team,
                "tank": player.tank,
                "battle_vehicle_id": player.battle_vehicle_id,
            }
            for player in detail.players
        ],
    }


def iter_inputs(args: list, recursive: bool = True) -> list:
    """把文件/目录参数展开成 .wotreplay 路径列表（目录默认递归）。"""
    paths = []
    for arg in args:
        target = Path(arg)
        if target.is_dir():
            pattern = "**/*.wotreplay" if recursive else "*.wotreplay"
            paths.extend(sorted(target.glob(pattern)))
        elif target.is_file():
            paths.append(target)
        else:
            print("[warn] not found: %s" % arg)
    return paths


def save_json(records: list, out_path: Any) -> Path:
    """把证据记录写为 UTF-8 JSON 文件。"""
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    return target


def print_detail(detail: models.ReplayDetail) -> None:
    """打印单个录像的紧凑摘要。"""
    print("-" * 78)
    print("file: %s" % detail.file_name)
    print("  container: block_count=%d status=%s partial=%s" %
          (detail.block_count, detail.parse_status, detail.is_partial))
    print("  client   : %s" % detail.client_version)
    print("  battle   : %s | %s | arena=%s | duration=%s" %
          (detail.battle_time, detail.map_name, detail.arena_unique_id, detail.duration_s))
    print("  recorder : %s (dbid %s)" % (detail.recorder_name, detail.recorder_dbid))
    print("  players  : %d" % detail.player_count)
    for player in detail.players[:4]:
        print("    %s | %s | clan=%s(%s) | team=%s | %s" %
              (player.dbid, player.nickname, player.clan_abbrev,
               player.clan_id, player.team, player.tank))
    if detail.notes:
        print("  notes    : %s" % "; ".join(detail.notes))


def parse_args(argv: list) -> dict:
    """解析命令行参数（--out / --quiet / --no-recursive / --limit）。"""
    options = {"paths": [], "out_path": None, "quiet": False, "recursive": True, "limit": None}
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--out" and index + 1 < len(argv):
            options["out_path"] = argv[index + 1]
            index += 2
            continue
        if arg == "--limit" and index + 1 < len(argv):
            options["limit"] = int(argv[index + 1])
            index += 2
            continue
        if arg == "--quiet":
            options["quiet"] = True
        elif arg == "--no-recursive":
            options["recursive"] = False
        elif not arg.startswith("--"):
            options["paths"].append(arg)
        index += 1
    return options


def main(argv: list) -> int:
    """独立运行入口：解析给定文件/目录，打印汇总并可选落证据 JSON。"""
    start = time.time()
    base_path = "."
    options = parse_args(argv)
    if not options["paths"]:
        print("usage: python src/replay/parser.py <file-or-dir> [more ...] "
              "[--out result.json] [--quiet] [--no-recursive] [--limit N]")
        print("[done] base_path=%r 耗时 %.2f 秒" % (base_path, time.time() - start))
        return 1
    paths = iter_inputs(options["paths"], options["recursive"])
    if options["limit"]:
        paths = paths[: options["limit"]]
    records = []
    failed = 0
    partial = 0
    for path in paths:
        step = time.time()
        try:
            detail = parse_replay(path)
        except (OSError, ValueError) as exc:
            failed += 1
            print("[error] %s: %s" % (path, exc))
            continue
        records.append(detail)
        if detail.parse_status == models.PARSE_STATUS_PARTIAL:
            partial += 1
        if not options["quiet"]:
            print_detail(detail)
            print("  parse time: %.2f ms" % ((time.time() - step) * 1000))
    if records and options["out_path"]:
        target = save_json([detail_to_dict(detail) for detail in records], options["out_path"])
        print("[ok] evidence JSON written: %s (%d records)" % (target, len(records)))
    elapsed = time.time() - start
    print("total: %d file(s), ok=%d partial=%d failed=%d, elapsed %.2f s" %
          (len(paths), len(records) - partial, partial, failed, elapsed))
    print("[done] base_path=%r 耗时 %.2f 秒" % (base_path, elapsed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
