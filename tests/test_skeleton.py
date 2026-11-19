# -*- coding: utf-8 -*-
"""T1 地基冒烟测试：契约模块导入 + SQLite 建表 + bat/run.bat 编码规范。

可直接 pytest 运行，也可单独运行：python tests/test_skeleton.py
"""
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import Config, load_config  # noqa: E402
from src.store import db  # noqa: E402


def test_contract_modules_import():
    """config / models / logger / store.db 均可导入并暴露约定接口。"""
    from src import models

    assert callable(load_config)
    assert callable(db.connect)
    assert callable(db.init_schema)
    assert callable(db.get_meta)
    assert callable(db.set_meta)
    assert models.MATCH_TYPE_ID == "id"
    assert models.LEVEL_RED == "red"
    assert len(db.EXPECTED_TABLES) == 9


def test_config_defaults():
    """默认路径不写死机器路径：archive 相对项目、replay/game 为 None（v0.2 动态探测）。"""
    cfg = load_config(Path(PROJECT_ROOT) / "nonexistent.json", auto_detect=False)
    assert isinstance(cfg, Config)
    assert cfg.db_path == cfg.project_root / "data" / "wotban.db"
    assert cfg.archive_dir == cfg.project_root / "data" / "archive"
    assert cfg.replay_dir is None
    assert cfg.game_dir is None
    assert "common_logger" not in str(cfg.db_path)


def test_init_schema_creates_nine_tables(tmp_path):
    """临时库建表：9 张表 + schema_version + meta 往返。"""
    db_path = tmp_path / "wotban-test.db"
    conn = db.connect(db_path)
    try:
        tables = db.list_tables(conn)
        for name in db.EXPECTED_TABLES:
            assert name in tables, "缺少表: %s" % name
        assert db.get_meta(conn, db.META_SCHEMA_VERSION) == db.SCHEMA_VERSION
        db.set_meta(conn, "days_json", ["2026-11-06", "2026-10-21"])
        assert db.get_meta(conn, "days_json") == ["2026-11-06", "2026-10-21"]
        assert db.get_meta(conn, "missing-key", "fallback") == "fallback"
        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert "ux_bans_dedup" in indexes
        assert "ix_bans_nickname" in indexes
    finally:
        conn.close()


def test_ban_dedup_key(tmp_path):
    """去重键 (batch_day, nickname, reason, duration) 由唯一索引兜底。"""
    conn = db.connect(tmp_path / "dedup.db")
    try:
        sql = (
            "INSERT INTO bans(batch_day, source_type, nickname, reason, duration, ban_times) "
            "VALUES(?, 'csv', ?, ?, ?, 1)"
        )
        conn.execute(sql, ("2026-11-06", "测试玩家", "违规使用脚本", "永久"))
        try:
            conn.execute(sql, ("2026-11-06", "测试玩家", "违规使用脚本", "永久"))
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("重复记录未被唯一索引拦截")
        assert conn.execute("SELECT COUNT(*) AS n FROM bans").fetchone()["n"] == 1
    finally:
        conn.close()


def test_run_bat_encoding():
    """bat/run.bat 必须全 ASCII、CRLF 行尾、用 %~dp0 相对定位 runtime。"""
    bat_path = PROJECT_ROOT / "bat" / "run.bat"
    assert bat_path.is_file(), "缺少 bat/run.bat"
    raw = bat_path.read_bytes()
    raw.decode("ascii")  # 非 ASCII 会抛 UnicodeDecodeError
    assert b"\r\n" in raw
    assert raw.replace(b"\r\n", b"").find(b"\n") == -1, "存在非 CRLF 行尾"
    text = raw.decode("ascii").lower()
    assert "%~dp0" in text
    assert "..\\runtime" in text


def test_pyside6_available():
    """共享 runtime 已安装 PySide6（未安装时跳过，便于紧凑环境）。"""
    try:
        import PySide6
    except ImportError:
        return
    assert PySide6.__version__.count(".") >= 1


def main(argv):
    """独立运行入口：顺序执行全部测试函数并打印结果。"""
    checks = [
        test_contract_modules_import,
        test_config_defaults,
        test_ban_dedup_key,
        test_run_bat_encoding,
        test_pyside6_available,
    ]
    import tempfile

    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for check in checks:
            try:
                if check.__name__ in ("test_init_schema_creates_nine_tables", "test_ban_dedup_key"):
                    check(Path(tmp))
                else:
                    check()
                print("[ok] %s" % check.__name__)
            except Exception as exc:  # noqa: BLE001 - 自检脚本需要汇总全部失败
                failures += 1
                print("[fail] %s: %s" % (check.__name__, exc))
    print("[done] %d 项通过，%d 项失败" % (len(checks) - failures, failures))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
