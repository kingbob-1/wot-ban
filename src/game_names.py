# -*- coding: utf-8 -*-
"""游戏本地化名映射：车辆代码 → 游戏车名、地图代码 → 中文名（纯标准库）。

数据来源（只读本机游戏安装目录，不联网）
--------------------------------------
- 车辆：``<game_dir>/res/text/lc_messages/{nation}_vehicles.mo``，gettext 键为车辆
  类型代码（如 ``J20_Type_2605``），值为客户端显示名（如 ``五式重战``；苏系
  ``R45_IS-7`` → ``IS-7``，英文名保持英文）。``igr_vehicles.mo`` /
  ``multinational_vehicles.mo`` / 各活动模式 ``*vehicles*.mo`` 作为兜底来源。
- 地图：``arenas.mo`` 的 ``{code}/name``（如 ``02_malinovka/name`` → ``马利诺夫卡``）；
  各模式 ``*arenas*.mo`` 作为兜底来源。

接口
----
``vehicle_name(tank_code, game_dir=None)``
    录像 tank 字段 ``japan:J20_Type_2605`` / 盒子 vehicle_icon slug
    ``japan-j20_type_2605`` / ``japan_J20_Type_2605`` → ``五式重战``。
``map_name(map_code, game_dir=None)``
    地图代码 ``99_poland`` / ``121_lost_paradise_v``（也兼容 ``121-lost-paradise-v``）→ 中文名。
``clear_cache()``
    清空进程内缓存（换游戏目录 / 重新探测后调用）。
``resolve_game_dir()``
    用 ``src.detect`` 的动态探测能力定位本机游戏安装目录（不写死盘符），失败返回 None。

约定
----
- **失败一律返回 None，绝不抛异常**：无游戏目录、.mo 缺失 / 损坏、未知代码都安全降级。
- **import 期不读盘**：首次调用才解析 .mo，并按游戏目录做进程内缓存；模块不依赖网络。
- ``game_dir`` 由调用方传入（``src.detect`` 的探测结果 / ``cfg.game_dir``）；传 None 时
  才回退到 ``resolve_game_dir()`` 自动探测（先复用 ``src.config`` 启动探测缓存，缺失时再
  用 ``src.detect`` 动态探测；界面/服务层建议直接传入以避免重复扫描）。
- 国家前缀 ``uk`` 与 ``gb`` 互为别名（本机客户端文件名是 ``gb_vehicles.mo``，而录像/
  盒子代码用 ``uk:``）。

独立运行
--------
::

    python src/game_names.py [game_dir]
"""
from __future__ import annotations

import os
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------- 常量
MO_MAGIC_LITTLE_ENDIAN = 0x950412DE
MO_MAGIC_BIG_ENDIAN = 0xDE120495
MO_HEADER_SIZE = 20
MO_ENTRY_SIZE = 8
MAX_MO_ENTRIES = 1000000

LC_MESSAGES_SUBDIR = ("res", "text", "lc_messages")
LC_MESSAGES_DIR_NAME = "lc_messages"
ARENAS_MO_NAME = "arenas.mo"
ARENAS_MO_GLOB = "*arenas*.mo"
ARENAS_NAME_KEY_SUFFIX = "/name"
VEHICLE_MO_GLOB = "*vehicles*.mo"
# 国家车辆文件的文件名主干后缀（去掉 .mo 后判断，如 japan_vehicles -> japan）
NATION_VEHICLE_STEM_SUFFIX = "_vehicles"
# 非国家文件：通用车辆名兜底来源（先于其它活动模式文件加载）
EXTRA_VEHICLE_MO_STEMS = ("igr_vehicles", "multinational_vehicles")
# 国家代码别名组：文件名/录像/盒子对同一国家的写法不同，组内共享同一份映射。
NATION_ALIAS_GROUPS = (("uk", "gb"),)
# 已知国家代码（目录里缺对应 .mo 时也能正确拆分 ``nation:code`` 输入）
KNOWN_NATIONS = frozenset({
    "ussr", "usa", "germany", "france", "uk", "gb", "china", "japan",
    "czech", "poland", "sweden", "italy",
})
# 车辆代码与国家的分隔符（录像用 ':'，盒子 slug 用 '-'，另兼容 '_'）
NATION_SEPARATORS = (":", "-", "_")

_UNSET = object()
_INDEX_CACHE: dict = {}
_RESOLVED_GAME_DIR = _UNSET
_CACHE_LOCK = threading.RLock()


# ---------------------------------------------------------------- 基础工具
def _import_detect_module():
    """懒加载 ``src/detect.py``（兼容包内 import 与直接以脚本运行）。"""
    try:
        from . import detect as detect_module
        return detect_module
    except ImportError:  # python src/game_names.py 直接运行
        root = str(Path(__file__).resolve().parent.parent)
        if root not in sys.path:
            sys.path.insert(0, root)
        from src import detect as detect_module
        return detect_module


def _normalize_dir(value) -> Optional[Path]:
    """把 str/Path 归一为 Path；None / 空值返回 None（不做存在性校验）。"""
    if value is None:
        return None
    try:
        text = str(value).strip().strip('"')
        if not text:
            return None
        return Path(text).expanduser()
    except (TypeError, ValueError, OSError):
        return None


def _cache_key(path: Path) -> Optional[str]:
    """游戏目录的缓存键（大小写不敏感，兼容 Windows 路径）。"""
    try:
        return os.path.normcase(os.path.abspath(str(path)))
    except (TypeError, ValueError, OSError):
        return None


def _clean_mo_value(value) -> Optional[str]:
    """清洗 .mo 译文：去空白、取复数形式首段、过滤空值与 ``?empty?`` 占位。"""
    if not isinstance(value, str):
        return None
    text = value.split("\x00", 1)[0].strip()
    if not text:
        return None
    if text.startswith("?") and text.endswith("?"):
        return None
    return text


def _parse_mo(path: Path) -> dict:
    """最小 gettext ``.mo`` 解析器：返回 ``{原始键: 清洗后译文}``。

    仅支持本机客户端实际使用的 little/big-endian 明文格式；文件缺失 / 损坏 /
    表越界时返回空字典（调用方按「无词条」处理），**绝不抛异常**。
    """
    try:
        raw = path.read_bytes()
    except (OSError, TypeError, ValueError):
        return {}
    if len(raw) < MO_HEADER_SIZE:
        return {}
    try:
        magic = struct.unpack_from("<I", raw, 0)[0]
        if magic == MO_MAGIC_LITTLE_ENDIAN:
            endian = "<"
        elif magic == MO_MAGIC_BIG_ENDIAN:
            endian = ">"
        else:
            return {}
        count, orig_offset, trans_offset = struct.unpack_from(endian + "III", raw, 8)
        if count <= 0 or count > MAX_MO_ENTRIES:
            return {}
        if orig_offset > len(raw) or trans_offset > len(raw):
            return {}
        pairs = {}
        for index in range(count):
            key_len, key_offset = struct.unpack_from(
                endian + "II", raw, orig_offset + index * MO_ENTRY_SIZE)
            value_len, value_offset = struct.unpack_from(
                endian + "II", raw, trans_offset + index * MO_ENTRY_SIZE)
            if key_len == 0:
                continue
            if key_offset + key_len > len(raw) or value_offset + value_len > len(raw):
                continue
            try:
                key = raw[key_offset:key_offset + key_len].decode("utf-8")
                value = raw[value_offset:value_offset + value_len].decode("utf-8")
            except UnicodeDecodeError:
                continue
            cleaned = _clean_mo_value(value)
            if cleaned is None:
                continue
            pairs[key] = cleaned
        return pairs
    except (struct.error, ValueError, IndexError):
        return {}


def _load_mo_names(path: Path) -> dict:
    """解析一个 .mo 并转成「键 casefold → 名称」映射（大小写不敏感查询用）。"""
    names = {}
    for key, value in _parse_mo(path).items():
        folded = key.casefold()
        if not folded:
            continue
        names[folded] = value
    return names


def _lookup_in_names(names: dict, code: str) -> Optional[str]:
    """在一个已 casefold 的名称映射里查车辆代码（含组件键 / _short 兜底）。"""
    if not names or not code:
        return None
    key = str(code).strip().casefold()
    if not key:
        return None
    for candidate in (key, "chassis_" + key, "turret_1_" + key, key + "_short"):
        value = names.get(candidate)
        if value:
            return value
    return None


def _split_nation_prefix(text: str, tokens: tuple):
    """把 ``japan:J20_Type_2605`` / ``japan-j20_type_2605`` 拆成 (国家, 代码)。

    无法识别国家前缀时返回 ``(None, 原文)``。
    """
    lowered = text.casefold()
    for token in tokens:
        for separator in NATION_SEPARATORS:
            prefix = token + separator
            if lowered.startswith(prefix):
                rest = text[len(prefix):].strip()
                if rest:
                    return token, rest
    return None, text


def _vehicle_candidates(index, text: str) -> list:
    """生成车辆代码查询候选：``[(国家或None, 代码), ...]``（按优先级）。"""
    candidates = []
    nation, code = _split_nation_prefix(text, index.known_nation_tokens())
    if nation is not None:
        candidates.append((nation, code))
    elif ":" in text:
        head, _, tail = text.partition(":")
        head = head.strip().casefold()
        tail = tail.strip()
        if head and tail:
            candidates.append((head, tail))
    candidates.append((None, text))
    return candidates


def _map_candidates(text: str) -> list:
    """生成地图代码查询候选（大小写不敏感、兼容 '-' 分隔与 ``_v`` 后缀增删）。"""
    raw = text.strip()
    if raw.casefold().endswith(ARENAS_NAME_KEY_SUFFIX):
        raw = raw[:-len(ARENAS_NAME_KEY_SUFFIX)].strip()
    raw = raw.strip("/").strip()
    bases = []
    for value in (raw, raw.replace("-", "_")):
        value = value.strip()
        if value and value not in bases:
            bases.append(value)
    for base in list(bases):
        if base.casefold().endswith("_v"):
            variant = base[:-2]
        else:
            variant = base + "_v"
        if variant and variant not in bases:
            bases.append(variant)
    return bases


def _find_lc_messages_dir(game_dir: Path) -> Optional[Path]:
    """在游戏安装目录里定位 ``lc_messages`` 目录（也接受直接传入该目录）。"""
    candidates = (
        game_dir.joinpath(*LC_MESSAGES_SUBDIR),
        game_dir / LC_MESSAGES_DIR_NAME,
        game_dir,
    )
    for candidate in candidates:
        try:
            if not candidate.is_dir():
                continue
            if (candidate / ARENAS_MO_NAME).is_file():
                return candidate
            for _pattern in (VEHICLE_MO_GLOB, ARENAS_MO_GLOB):
                if next(candidate.glob(_pattern), None) is not None:
                    return candidate
        except (OSError, TypeError, ValueError):
            continue
    return None


# ---------------------------------------------------------------- 名称索引
class _GameNameIndex:
    """某个游戏安装目录的本地化名称索引（一次解析，进程内复用）。"""

    def __init__(self, game_dir: Path):
        self.game_dir = game_dir
        self.lc_messages_dir = _find_lc_messages_dir(game_dir)
        self.vehicle_maps = {}
        self.vehicle_extra = {}
        self.map_names = {}
        if self.lc_messages_dir is not None:
            self._load_vehicle_sources()
            self._load_map_sources()
        tokens = set(self.vehicle_maps) | set(KNOWN_NATIONS)
        for group in NATION_ALIAS_GROUPS:
            tokens.update(group)
        self._known_nation_tokens = tuple(
            sorted(tokens, key=lambda item: (-len(item), item)))

    def known_nation_tokens(self) -> tuple:
        """已知国家前缀（按长度降序，便于最长匹配）。"""
        return self._known_nation_tokens

    def _load_vehicle_sources(self) -> None:
        """解析所有 ``*vehicles*.mo``：国家文件按国家建表，其余合并为兜底表。"""
        lc_dir = self.lc_messages_dir
        try:
            paths = sorted(lc_dir.glob(VEHICLE_MO_GLOB))
        except OSError:
            return
        nation_paths = {}
        extra_paths = []
        for path in paths:
            name = path.name
            stem = name[:-3] if name.endswith(".mo") else name
            if stem in EXTRA_VEHICLE_MO_STEMS:
                extra_paths.append(path)
                continue
            if stem.endswith(NATION_VEHICLE_STEM_SUFFIX):
                base = stem[:-len(NATION_VEHICLE_STEM_SUFFIX)].lower()
                if base and "." not in base and base not in nation_paths:
                    nation_paths[base] = path
                    continue
            extra_paths.append(path)
        for group in NATION_ALIAS_GROUPS:
            present = [token for token in group if token in nation_paths]
            if not present:
                continue
            for token in group:
                if token not in nation_paths:
                    nation_paths[token] = nation_paths[present[0]]
        parsed = {}
        for token in sorted(nation_paths):
            path = nation_paths[token]
            file_key = str(path)
            names = parsed.get(file_key)
            if names is None:
                names = _load_mo_names(path)
                parsed[file_key] = names
            self.vehicle_maps[token] = names
        ordered = []
        for stem in EXTRA_VEHICLE_MO_STEMS:
            ordered.extend(path for path in extra_paths
                           if path.name[:-3] == stem)
        ordered.extend(path for path in extra_paths if path not in ordered)
        merged = {}
        for path in ordered:
            for key, value in _load_mo_names(path).items():
                merged.setdefault(key, value)
        self.vehicle_extra = merged

    def _load_map_sources(self) -> None:
        """解析 ``arenas.mo``（及模式扩展 ``*arenas*.mo``）的 ``{code}/name``。"""
        lc_dir = self.lc_messages_dir
        try:
            paths = sorted(lc_dir.glob(ARENAS_MO_GLOB))
        except OSError:
            return
        primary = lc_dir / ARENAS_MO_NAME
        ordered = []
        try:
            if primary.is_file():
                ordered.append(primary)
        except OSError:
            pass
        ordered.extend(path for path in paths if path.name != ARENAS_MO_NAME)
        merged = {}
        for path in ordered:
            for key, value in _parse_mo(path).items():
                if not key.endswith(ARENAS_NAME_KEY_SUFFIX):
                    continue
                code = key[:-len(ARENAS_NAME_KEY_SUFFIX)]
                if not code:
                    continue
                merged.setdefault(code.casefold(), value)
        self.map_names = merged

    def lookup_vehicle(self, nation: Optional[str], code: str) -> Optional[str]:
        """查车辆名：先国家表、再兜底表；无国家前缀时按国家文件顺序全局搜索。"""
        if not code:
            return None
        if nation is not None:
            names = self.vehicle_maps.get(nation)
            if names:
                value = _lookup_in_names(names, code)
                if value:
                    return value
            return _lookup_in_names(self.vehicle_extra, code)
        for token in sorted(self.vehicle_maps):
            value = _lookup_in_names(self.vehicle_maps[token], code)
            if value:
                return value
        return _lookup_in_names(self.vehicle_extra, code)

    def lookup_map(self, code: str) -> Optional[str]:
        """查地图中文名（先精确、再归一化候选）。"""
        if not self.map_names:
            return None
        for candidate in _map_candidates(code):
            value = self.map_names.get(candidate.casefold())
            if value:
                return value
        return None

    def stats(self) -> dict:
        """索引统计（自检 / 日志用）。"""
        return {
            "game_dir": str(self.game_dir),
            "lc_messages_dir": str(self.lc_messages_dir) if self.lc_messages_dir else "",
            "vehicle_sources": len(self.vehicle_maps),
            "vehicle_entries": sum(len(names) for names in self.vehicle_maps.values()),
            "vehicle_extra_entries": len(self.vehicle_extra),
            "map_entries": len(self.map_names),
        }


# ---------------------------------------------------------------- 目录探测 / 缓存
def _probe_game_dir_from_config():
    """优先从 ``src.config`` 取游戏目录（复用应用启动时的探测缓存，避免重复扫描）。

    返回 ``(已尝试, 目录或None)``：``已尝试=False`` 表示 config 模块不可用 / 加载失败，
    调用方应回退到 ``detect`` 直接探测。
    """
    try:
        try:
            from . import config as config_module
        except ImportError:  # python src/game_names.py 直接运行
            root = str(Path(__file__).resolve().parent.parent)
            if root not in sys.path:
                sys.path.insert(0, root)
            from src import config as config_module
        cfg = config_module.load_config(auto_detect=True, verbose=False)
        path = _normalize_dir(getattr(cfg, "game_dir", None))
        if path is not None and path.is_dir():
            return True, path
        return True, None
    except Exception:  # noqa: BLE001 - config 不可用 / 加载失败时回退 detect
        return False, None


def _probe_game_dir() -> Optional[Path]:
    """动态探测游戏安装目录：config（含启动探测缓存）优先，失败回退 ``src.detect``。"""
    attempted, path = _probe_game_dir_from_config()
    if attempted:
        return path
    try:
        detect_module = _import_detect_module()
        environment = detect_module.detect_environment()
        text = getattr(environment, "game_dir", "") or ""
        if not text:
            return None
        path = Path(str(text))
        return path if path.is_dir() else None
    except Exception:  # noqa: BLE001 - 探测失败不得影响调用方
        return None


def resolve_game_dir() -> Optional[Path]:
    """探测本机游戏安装目录（config 缓存优先 → detect 兜底；进程内缓存，失败返回 None）。

    不写死盘符：先读 ``src.config.load_config()``（应用启动时已探测过则命中缓存），
    config 不可用时回退 ``src.detect.detect_environment()``；任何异常都降级为 None。
    """
    global _RESOLVED_GAME_DIR
    with _CACHE_LOCK:
        cached = _RESOLVED_GAME_DIR
    if cached is not _UNSET:
        return cached
    resolved = _probe_game_dir()
    with _CACHE_LOCK:
        if _RESOLVED_GAME_DIR is _UNSET:
            _RESOLVED_GAME_DIR = resolved
        return _RESOLVED_GAME_DIR


def _get_index(game_dir=None) -> Optional[_GameNameIndex]:
    """取（或构建）游戏目录对应的名称索引；无法定位目录时返回 None。"""
    base = game_dir if game_dir is not None else resolve_game_dir()
    path = _normalize_dir(base)
    if path is None:
        return None
    key = _cache_key(path)
    if key is None:
        return None
    with _CACHE_LOCK:
        cached = _INDEX_CACHE.get(key)
    if cached is not None:
        return cached
    index = _GameNameIndex(path)
    with _CACHE_LOCK:
        cached = _INDEX_CACHE.get(key)
        if cached is not None:
            return cached
        _INDEX_CACHE[key] = index
        return index


def clear_cache() -> None:
    """清空进程内缓存（名称索引 + 已探测的游戏目录）。"""
    global _RESOLVED_GAME_DIR
    try:
        with _CACHE_LOCK:
            _INDEX_CACHE.clear()
            _RESOLVED_GAME_DIR = _UNSET
    except Exception:  # noqa: BLE001 - 清缓存失败不影响调用方
        pass


# ---------------------------------------------------------------- 公开接口
def vehicle_name(tank_code, game_dir=None) -> Optional[str]:
    """车辆代码 → 游戏客户端显示名；无法解析时返回 None（不抛异常）。

    参数
    ----
    tank_code: 录像 tank 字段（``japan:J20_Type_2605``）、盒子 vehicle_icon slug
        （``japan-j20_type_2605``）或裸代码（``J20_Type_2605``）。
    game_dir: 游戏安装目录（Path/str）；None 时用 ``resolve_game_dir()`` 自动探测。
    """
    try:
        if tank_code is None:
            return None
        text = str(tank_code).strip()
        if not text:
            return None
        index = _get_index(game_dir)
        if index is None:
            return None
        for nation, code in _vehicle_candidates(index, text):
            value = index.lookup_vehicle(nation, code)
            if value:
                return value
        return None
    except Exception:  # noqa: BLE001 - 映射失败一律降级为 None
        return None


def map_name(map_code, game_dir=None) -> Optional[str]:
    """地图代码 → 中文名；无法解析时返回 None（不抛异常）。

    参数
    ----
    map_code: 地图代码（``99_poland`` / ``121_lost_paradise_v``），也兼容
        ``121-lost-paradise-v`` 与完整键 ``99_poland/name``。
    game_dir: 游戏安装目录（Path/str）；None 时用 ``resolve_game_dir()`` 自动探测。
    """
    try:
        if map_code is None:
            return None
        text = str(map_code).strip()
        if not text:
            return None
        index = _get_index(game_dir)
        if index is None:
            return None
        return index.lookup_map(text)
    except Exception:  # noqa: BLE001 - 映射失败一律降级为 None
        return None


# ---------------------------------------------------------------- 自检入口
def main(argv: list) -> int:
    """命令行自检：打印解析统计与抽样验证结果（独立运行入口）。"""
    start = time.time()
    base_path = "."
    game_dir_arg = argv[0] if argv else None
    sample_vehicles = ("japan:J20_Type_2605", "ussr:R45_IS-7", "japan-j20_type_2605")
    sample_maps = ("02_malinovka", "99_poland", "121_lost_paradise_v")

    print("[1/3] 加载游戏本地化 .mo（base_path=%r）" % base_path)
    step_start = time.time()
    index = _get_index(game_dir_arg)
    if index is None or index.lc_messages_dir is None:
        print("      未找到游戏本地化文件：请传入游戏安装目录，或确认游戏已安装")
        elapsed = int(time.time() - start)
        print("[done] base_path=%r 耗时 %02d:%02d:%02d"
              % (base_path, elapsed // 3600, elapsed % 3600 // 60, elapsed % 60))
        return 1
    stats = index.stats()
    print("      game_dir      : %s" % stats["game_dir"])
    print("      lc_messages   : %s" % stats["lc_messages_dir"])
    print("      车辆来源/条目 : %d 个国家·文件 / %d 条；兜底 %d 条"
          % (stats["vehicle_sources"], stats["vehicle_entries"],
             stats["vehicle_extra_entries"]))
    print("      地图条目      : %d" % stats["map_entries"])
    print("      本步骤耗时    : %.2f 秒" % (time.time() - step_start))

    print("[2/3] 抽样验证")
    game_dir = Path(stats["game_dir"])
    for code in sample_vehicles:
        print("      车辆 %-24s -> %s" % (code, vehicle_name(code, game_dir) or "未解析"))
    for code in sample_maps:
        print("      地图 %-24s -> %s" % (code, map_name(code, game_dir) or "未解析"))

    print("[3/3] 完成")
    elapsed = int(time.time() - start)
    print("[done] base_path=%r 耗时 %02d:%02d:%02d"
          % (base_path, elapsed // 3600, elapsed % 3600 // 60, elapsed % 60))
    return 0


if __name__ == "__main__":
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
