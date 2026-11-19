# -*- coding: utf-8 -*-
"""game_names 单元测试：合成 gettext .mo（离线必过）+ 真实游戏目录（缺失时 skip）。

覆盖：
- ``.mo`` 解析（little/big-endian、损坏文件降级）；
- 车辆代码四种输入形态（``nation:code`` / ``nation-code`` slug / ``nation_code`` / 裸代码）；
- 国家别名 ``uk`` ↔ ``gb``、``igr_vehicles.mo`` 兜底、``_descr`` / ``?empty?`` 不污染主名；
- 地图代码形态（下划线 / 连字符 / ``_v`` 后缀增删 / 完整 ``code/name`` 键）；
- 无游戏目录 / 坏目录 / 空目录 / 未知代码一律返回 None 且不抛异常；
- 进程内缓存与 ``clear_cache()`` 失效语义；
- 真实 30 人局（arena 10974034077980049）关键车辆与三张地图的真名断言（无游戏时 skip）。
"""
import struct
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import game_names  # noqa: E402

# ---------------------------------------------------------------- 合成 .mo 数据
JAPAN_VEHICLES = {
    "J20_Type_2605": "五式重战",
    "J20_Type_2605_descr": "?empty?",
    "J20_Type_2605_short": "五式重战",
    "Chassis_J20_Type_2605": "五式重战",
    "Turret_1_J20_Type_2605": "五式重战",
}
USSR_VEHICLES = {
    "R45_IS-7": "IS-7",
    "R45_IS-7_descr": "1945年春，IS-7开始研发。",
}
GB_VEHICLES = {"GB100_Manticore": "曼蒂科尔"}
IGR_VEHICLES = {"A120_M48A5_IGR": "M48A5"}
EMPTY_TEST_VEHICLES = {"XXX_Placeholder": "?empty?"}
ARENAS = {
    "02_malinovka/name": "马利诺夫卡",
    "02_malinovka/description": "双方队伍的出生点被一片平坦开阔的地域相隔。",
    "99_poland/name": "斯杜季昂奇",
    "121_lost_paradise_v/name": "牡蛎湾",
}
SYNTHETIC_MO = {
    "japan_vehicles.mo": JAPAN_VEHICLES,
    "ussr_vehicles.mo": USSR_VEHICLES,
    "gb_vehicles.mo": GB_VEHICLES,
    "igr_vehicles.mo": IGR_VEHICLES,
    "multinational_vehicles.mo": {"changeScreen/cancelBtn": "取消"},
    "arenas.mo": ARENAS,
}
EMPTY_TEST_MO = {"japan_vehicles.mo": EMPTY_TEST_VEHICLES, "arenas.mo": ARENAS}


# ---------------------------------------------------------------- 辅助
def _build_mo(pairs: dict, endian: str = "<") -> bytes:
    """把 ``{key: value}`` 编译成最小合法 gettext .mo 字节。"""
    items = sorted(pairs.items())
    count = len(items)
    key_table = []
    key_blob = b""
    for key, _value in items:
        data = key.encode("utf-8")
        key_table.append((len(data), len(key_blob)))
        key_blob += data
    value_table = []
    value_blob = b""
    for _key, value in items:
        data = value.encode("utf-8")
        value_table.append((len(data), len(value_blob)))
        value_blob += data
    header_size = 28
    orig_offset = header_size
    trans_offset = orig_offset + count * 8
    key_data_offset = trans_offset + count * 8
    value_data_offset = key_data_offset + len(key_blob)
    parts = [struct.pack(endian + "IIIIIII", game_names.MO_MAGIC_LITTLE_ENDIAN, 0, count,
                         orig_offset, trans_offset, 0, 0)]
    for length, offset in key_table:
        parts.append(struct.pack(endian + "II", length, key_data_offset + offset))
    for length, offset in value_table:
        parts.append(struct.pack(endian + "II", length, value_data_offset + offset))
    parts.extend([key_blob, value_blob])
    return b"".join(parts)


def _write_lc_dir(game_dir: Path, mo_files: dict) -> Path:
    """在 ``game_dir`` 下写标准本地化布局，返回 ``lc_messages`` 目录。"""
    lc_dir = game_dir / "res" / "text" / "lc_messages"
    lc_dir.mkdir(parents=True, exist_ok=True)
    for filename, pairs in mo_files.items():
        (lc_dir / filename).write_bytes(_build_mo(pairs))
    return lc_dir


@pytest.fixture(autouse=True)
def _reset_game_names_cache():
    """每个用例前后清空缓存，避免合成目录 / 真实目录互相污染。"""
    game_names.clear_cache()
    yield
    game_names.clear_cache()


@pytest.fixture()
def synthetic_game_dir(tmp_path):
    """带合成 .mo 的假游戏安装目录。"""
    game_dir = tmp_path / "World_of_Tanks_Test"
    _write_lc_dir(game_dir, SYNTHETIC_MO)
    return game_dir


@pytest.fixture(scope="module")
def real_game_dir():
    """真实游戏安装目录（探测不到则整组 skip；带缓存，只探测一次）。"""
    game_dir = game_names.resolve_game_dir()
    if game_dir is None or not Path(game_dir).is_dir():
        pytest.skip("未检测到本机游戏安装目录（无游戏环境时跳过真实数据用例）")
    return Path(game_dir)


# ---------------------------------------------------------------- 合成数据：车辆
@pytest.mark.parametrize("tank_code", [
    "japan:J20_Type_2605",          # 录像 tank 字段
    "japan-J20_Type_2605",          # 盒子 vehicle_icon slug（保留原大小写）
    "japan-j20_type_2605",          # 盒子 slug 全小写
    "JAPAN:j20_TYPE_2605",          # 大小写混合
    "japan_J20_Type_2605",          # 下划线分隔
])
def test_vehicle_code_forms(synthetic_game_dir, tank_code):
    assert game_names.vehicle_name(tank_code, synthetic_game_dir) == "五式重战"


def test_vehicle_bare_code_searches_all_nations(synthetic_game_dir):
    """无国家前缀的裸代码也能解析（盒子部分场景只给 slug 尾段）。"""
    assert game_names.vehicle_name("J20_Type_2605", synthetic_game_dir) == "五式重战"
    assert game_names.vehicle_name("j20_type_2605", synthetic_game_dir) == "五式重战"


def test_vehicle_nation_alias_uk_gb(synthetic_game_dir):
    """录像/盒子用 uk:，客户端文件名是 gb_vehicles.mo → 互为别名。"""
    assert game_names.vehicle_name("uk:GB100_Manticore", synthetic_game_dir) == "曼蒂科尔"
    assert game_names.vehicle_name("gb:GB100_Manticore", synthetic_game_dir) == "曼蒂科尔"
    assert game_names.vehicle_name("uk-gb100_manticore", synthetic_game_dir) == "曼蒂科尔"


def test_vehicle_english_name_kept(synthetic_game_dir):
    """苏系 IS-7 按客户端口径保持英文，不译成中文。"""
    assert game_names.vehicle_name("ussr:R45_IS-7", synthetic_game_dir) == "IS-7"


def test_vehicle_igr_fallback(synthetic_game_dir):
    """国家文件里没有的 IGR 车，从 igr_vehicles.mo 兜底。"""
    assert game_names.vehicle_name("usa:A120_M48A5_IGR", synthetic_game_dir) == "M48A5"
    assert game_names.vehicle_name("usa-a120_m48a5_igr", synthetic_game_dir) == "M48A5"


def test_vehicle_descr_and_empty_values_ignored(synthetic_game_dir, tmp_path):
    """``_descr`` / ``?empty?`` 词条不得返回占位文本（主键仍正常）。"""
    assert game_names.vehicle_name("japan:J20_Type_2605", synthetic_game_dir) == "五式重战"
    assert game_names.vehicle_name("japan:J20_Type_2605_descr", synthetic_game_dir) is None
    game_dir = tmp_path / "Empty_Values_Game"
    _write_lc_dir(game_dir, EMPTY_TEST_MO)
    assert game_names.vehicle_name("japan:XXX_Placeholder", game_dir) is None


def test_vehicle_short_key_fallback(tmp_path):
    """主键缺失时用 ``_short`` 变体兜底（值合法才返回）。"""
    game_dir = tmp_path / "Short_Only_Game"
    _write_lc_dir(game_dir, {"japan_vehicles.mo": {
        "J20_Type_2605_short": "五式重战",
        "J20_Type_2605_descr": "五式重战的历史说明文字。",
    }})
    assert game_names.vehicle_name("japan:J20_Type_2605", game_dir) == "五式重战"


def test_vehicle_unknown_code_returns_none(synthetic_game_dir):
    assert game_names.vehicle_name("japan:XXX_Not_Exist", synthetic_game_dir) is None
    assert game_names.vehicle_name("xxx:J20_Type_2605", synthetic_game_dir) is None


# ---------------------------------------------------------------- 合成数据：地图
@pytest.mark.parametrize("map_code, expected", [
    ("02_malinovka", "马利诺夫卡"),
    ("99_poland", "斯杜季昂奇"),
    ("99_POLAND", "斯杜季昂奇"),
    ("121_lost_paradise_v", "牡蛎湾"),
    ("121-lost-paradise-v", "牡蛎湾"),
    ("121_lost_paradise", "牡蛎湾"),        # 自动补 _v 后缀
    ("99_poland/name", "斯杜季昂奇"),        # 完整 mo 键
])
def test_map_code_forms(synthetic_game_dir, map_code, expected):
    assert game_names.map_name(map_code, synthetic_game_dir) == expected


def test_map_unknown_returns_none(synthetic_game_dir):
    assert game_names.map_name("999_not_exist", synthetic_game_dir) is None
    assert game_names.map_name("", synthetic_game_dir) is None


# ---------------------------------------------------------------- 失败降级
def test_bad_game_dir_returns_none(tmp_path):
    bad_dir = tmp_path / "no_such_game_dir"
    assert game_names.vehicle_name("japan:J20_Type_2605", bad_dir) is None
    assert game_names.map_name("99_poland", bad_dir) is None


def test_empty_game_dir_returns_none(tmp_path):
    empty_dir = tmp_path / "empty_game_dir"
    empty_dir.mkdir()
    assert game_names.vehicle_name("japan:J20_Type_2605", empty_dir) is None
    assert game_names.map_name("99_poland", empty_dir) is None


def test_none_game_dir_safe(monkeypatch):
    """自动探测不到游戏目录时（不存在 .mo）返回 None，不报错。"""
    monkeypatch.setattr(game_names, "resolve_game_dir", lambda: None)
    assert game_names.vehicle_name("japan:J20_Type_2605", None) is None
    assert game_names.map_name("99_poland", None) is None


def test_none_and_empty_inputs(synthetic_game_dir):
    assert game_names.vehicle_name(None, synthetic_game_dir) is None
    assert game_names.vehicle_name("", synthetic_game_dir) is None
    assert game_names.map_name(None, synthetic_game_dir) is None
    assert game_names.map_name("", synthetic_game_dir) is None


def test_corrupted_mo_does_not_raise(tmp_path):
    """损坏 .mo 只降级为 None，绝不抛异常。"""
    game_dir = tmp_path / "Broken_Game"
    lc_dir = game_dir / "res" / "text" / "lc_messages"
    lc_dir.mkdir(parents=True)
    (lc_dir / "japan_vehicles.mo").write_bytes(b"not a real mo file")
    (lc_dir / "arenas.mo").write_bytes(b"\x00\x01\x02")
    assert game_names.vehicle_name("japan:J20_Type_2605", game_dir) is None
    assert game_names.map_name("99_poland", game_dir) is None


def test_truncated_mo_does_not_raise(tmp_path):
    """头部合法但表偏移越界的截断 .mo 不抛异常。"""
    game_dir = tmp_path / "Truncated_Game"
    lc_dir = game_dir / "res" / "text" / "lc_messages"
    lc_dir.mkdir(parents=True)
    payload = _build_mo({"J20_Type_2605": "五式重战"})
    (lc_dir / "japan_vehicles.mo").write_bytes(payload[:24])
    assert game_names.vehicle_name("japan:J20_Type_2605", game_dir) is None


def test_lc_messages_dir_can_be_passed_directly(synthetic_game_dir):
    """game_dir 也接受直接传 ``lc_messages`` 目录。"""
    lc_dir = synthetic_game_dir / "res" / "text" / "lc_messages"
    assert game_names.vehicle_name("japan:J20_Type_2605", lc_dir) == "五式重战"
    assert game_names.map_name("02_malinovka", lc_dir) == "马利诺夫卡"


# ---------------------------------------------------------------- 解析器 / 缓存
def test_parse_mo_big_endian(tmp_path):
    """big-endian .mo 也能解析（本机客户端是 LE，但解析器不应写死）。"""
    path = tmp_path / "japan_vehicles.mo"
    path.write_bytes(_build_mo({"J20_Type_2605": "五式重战"}, endian=">"))
    pairs = game_names._parse_mo(path)
    assert pairs.get("J20_Type_2605") == "五式重战"


def test_clear_cache_invalidates_names_index(tmp_path):
    """clear_cache() 后重新读盘：.mo 内容变化能生效。"""
    game_dir = tmp_path / "Cache_Game"
    lc_dir = _write_lc_dir(game_dir, {"japan_vehicles.mo": {"J20_Type_2605": "五式重战"}})
    assert game_names.vehicle_name("japan:J20_Type_2605", game_dir) == "五式重战"
    (lc_dir / "japan_vehicles.mo").write_bytes(
        _build_mo({"J20_Type_2605": "五式重战（新版）"}))
    assert game_names.vehicle_name("japan:J20_Type_2605", game_dir) == "五式重战"
    game_names.clear_cache()
    assert game_names.vehicle_name("japan:J20_Type_2605", game_dir) == "五式重战（新版）"


# ---------------------------------------------------------------- 真实游戏数据
def test_real_vehicle_japan(real_game_dir):
    value = game_names.vehicle_name("japan:J20_Type_2605", real_game_dir)
    if value is None:
        pytest.skip("游戏目录不含 japan 车辆本地化（可能非国服客户端）")
    assert value == "五式重战"


def test_real_vehicle_is7_keeps_english(real_game_dir):
    value = game_names.vehicle_name("ussr:R45_IS-7", real_game_dir)
    if value is None:
        pytest.skip("游戏目录不含 ussr 车辆本地化（可能非国服客户端）")
    assert value == "IS-7"


def test_real_vehicle_icon_slug(real_game_dir):
    """盒子 slug 形态在真实数据上与录像代码得到同一车名。"""
    value = game_names.vehicle_name("japan-j20_type_2605", real_game_dir)
    if value is None:
        pytest.skip("游戏目录不含 japan 车辆本地化（可能非国服客户端）")
    assert value == "五式重战"


@pytest.mark.parametrize("map_code, expected", [
    ("02_malinovka", "马利诺夫卡"),
    ("99_poland", "斯杜季昂奇"),
    ("121_lost_paradise_v", "牡蛎湾"),
])
def test_real_map_names(real_game_dir, map_code, expected):
    value = game_names.map_name(map_code, real_game_dir)
    if value is None:
        pytest.skip("游戏目录不含 arenas 地图本地化（可能非国服客户端）")
    assert value == expected


def test_real_game_replay_tank_codes(real_game_dir):
    """真实录像（arena 10974034077980049，30 人局）抽查多种国家车辆代码。"""
    samples = {
        "germany:G121_Grille_15_L63": None,   # 只要求可解析出非空中文/英文名
        "ussr:R45_IS-7": "IS-7",
        "japan:J20_Type_2605": "五式重战",
        "uk:GB100_Manticore": "曼蒂科尔",
        "china:Ch48_BZ_75": "BZ-75",
    }
    missing = []
    for code in samples:
        value = game_names.vehicle_name(code, real_game_dir)
        if value is None:
            missing.append(code)
    if len(missing) == len(samples):
        pytest.skip("游戏目录不含车辆本地化（可能非国服客户端）")
    assert not missing, "以下真实车辆代码未解析：%s" % missing
    assert game_names.vehicle_name("ussr:R45_IS-7", real_game_dir) == "IS-7"
    assert game_names.vehicle_name("japan:J20_Type_2605", real_game_dir) == "五式重战"
    assert game_names.vehicle_name("uk:GB100_Manticore", real_game_dir) == "曼蒂科尔"
    assert game_names.vehicle_name("china:Ch48_BZ_75", real_game_dir) == "BZ-75"
