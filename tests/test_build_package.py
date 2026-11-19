# -*- coding: utf-8 -*-
"""便携包构建口径回归测试（v1.0.2 修复 P1/P2 后）。

覆盖：
- 便携包必需文件清单包含 ``src/main.py``（不再是根 ``main.py``）与 ``docs/images/``
  （README 首图目录；缺失会导致包内 README 首图断链）；
- 必需文件清单不含本地 gitignored 的 ``docs/shots/`` 截图目录（干净克隆也能构建）；
- 便携包组装不复制任何界面截图，仅允许仓库内 1 张打码 README 首图（``docs/images/``）；
- 复制 src 源码树时排除本地 QA 脚本、Python 缓存/编译产物（``__pycache__`` /
  ``*.pyc`` / ``*.pyo``）与 ``.pytest_cache``；
- 自检运行产生的源码缓存必须在压缩前清理（``purge_source_caches`` /
  ``assert_no_source_caches``），且 ``create_zip`` 对 pyc 兜底跳过；
- 运行期依赖清单含 ``PySide6==<版本>`` / ``requests`` / ``openpyxl``，自检 import 清单
  含 ``src.stats.box_client``（P1：缺 requests/openpyxl 会导致盒子战绩失效、xlsx 导入不可用）；
- 组装后的自检能发现本地 QA 脚本误入包；
- 生成的便携版 ``run.bat`` 指向 ``%PROJECT_DIR%src\\main.py``。

禁止真实下载/打包：本测试只 import ``scripts/build_portable.py`` 的常量与函数，
再用临时目录验证 ``shutil`` 复制规则，不触碰网络与 dist/build 目录。
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUILD_SCRIPT = PROJECT_ROOT / "scripts" / "build_portable.py"
MODULE_NAME = "wot_ban_build_portable"
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif")
README_IMAGE_RELATIVE = "docs/images/v1.0-main.jpg"


def load_build_portable():
    """按文件路径加载 scripts/build_portable.py（scripts 非包，避免改 sys.path）。"""
    if MODULE_NAME in sys.modules:
        return sys.modules[MODULE_NAME]
    spec = importlib.util.spec_from_file_location(MODULE_NAME, str(BUILD_SCRIPT))
    if spec is None or spec.loader is None:
        raise AssertionError("无法加载便携包构建脚本：%s" % BUILD_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _make_fake_project(root: Path, with_shots: bool = True) -> None:
    """构造最小假项目树（可选带本地 docs/shots 截图），用于验证组装排除规则。"""
    (root / "src" / "ui").mkdir(parents=True)
    (root / "src" / "main.py").write_text("# main\n", encoding="utf-8")
    (root / "src" / "ui" / "main_window.py").write_text("# ui\n", encoding="utf-8")
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "使用说明.md").write_text("# 使用说明\n", encoding="utf-8")
    (root / "docs" / "第三方组件许可.md").write_text("# 第三方组件许可\n", encoding="utf-8")
    (root / "docs" / "P4-启动器.md").write_text("# 启动器\n", encoding="utf-8")
    # README 首图目录（仓库内唯一允许入包的图片）
    (root / "docs" / "images").mkdir()
    (root / "docs" / "images" / "v1.0-main.jpg").write_bytes(b"\xff\xd8\xff fake jpeg")
    if with_shots:
        (root / "docs" / "shots").mkdir()
        (root / "docs" / "shots" / "real_01_main_hit.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    (root / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    (root / "README.md").write_text("# readme\n\n![界面示意图](docs/images/v1.0-main.jpg)\n",
                                   encoding="utf-8")
    (root / "坦克世界封号查询.exe").write_bytes(b"MZ fake launcher")
    (root / "assets").mkdir()
    (root / "assets" / "app.ico").write_bytes(b"ico fake")


def test_required_files_include_src_main():
    """必需文件口径 = src/main.py（根 main.py 已移入 src）。"""
    module = load_build_portable()
    cfg = module.BuildConfig(project_root=PROJECT_ROOT)
    cfg.resolve_paths()
    files = module.collect_source_files(cfg)
    assert "src/main.py" in files, "必需文件清单缺少 src/main.py：%s" % sorted(files)
    assert files["src/main.py"] == PROJECT_ROOT / "src" / "main.py"
    assert files["src/main.py"].is_file(), "src/main.py 不存在：%s" % files["src/main.py"]
    assert "main.py" not in files, "旧根入口 main.py 不应再出现在必需文件清单"


def test_required_files_include_readme_image_dir():
    """必需文件清单包含 docs/images（README 首图目录），且 README 相对链接目标真实存在。"""
    module = load_build_portable()
    cfg = module.BuildConfig(project_root=PROJECT_ROOT)
    cfg.resolve_paths()
    files = module.collect_source_files(cfg)
    image_labels = [label for label in files if "docs/images" in label]
    assert image_labels, "必需文件清单缺少 docs/images：%s" % sorted(files)
    for label in image_labels:
        assert Path(files[label]).exists(), "%s 不存在：%s" % (label, files[label])
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert README_IMAGE_RELATIVE in readme, "README 未引用首图：%s" % README_IMAGE_RELATIVE
    assert (PROJECT_ROOT / README_IMAGE_RELATIVE).is_file(), "README 首图文件不存在"


def test_required_files_exclude_local_screenshots():
    """必需文件清单不得包含本地 docs/shots 截图目录（干净克隆不应因此构建失败）。"""
    module = load_build_portable()
    cfg = module.BuildConfig(project_root=PROJECT_ROOT)
    cfg.resolve_paths()
    files = module.collect_source_files(cfg)
    assert not any("shots" in label for label in files), "必需文件清单仍含截图目录：%s" % sorted(files)
    shots_path = (PROJECT_ROOT / "docs" / "shots").resolve()
    resolved = {Path(value).resolve() for value in files.values()}
    assert shots_path not in resolved, "必需文件清单仍把 docs/shots 列为必需：%s" % shots_path


def test_environment_check_passes_without_local_screenshots(tmp_path):
    """模拟干净克隆（无 docs/shots）时，源码/环境检查不应报缺少必需文件。"""
    module = load_build_portable()
    root = tmp_path / "clean_clone"
    _make_fake_project(root, with_shots=False)
    cfg = module.BuildConfig(project_root=root)
    cfg.resolve_paths()
    problems = module.check_environment(cfg)
    missing = [item for item in problems if item.startswith("缺少必需文件")]
    assert missing == [], "干净克隆仍缺必需文件：%s" % missing


def test_stage_copy_ships_readme_image_only(tmp_path):
    """组装便携包时复制 docs/images/（README 首图可用），但不含任何 docs/shots 截图。"""
    module = load_build_portable()
    root = tmp_path / "fake_project"
    _make_fake_project(root, with_shots=True)
    cfg = module.BuildConfig(project_root=root)
    cfg.resolve_paths()
    stage = tmp_path / "stage"
    module.copy_source_tree(cfg, stage)

    assert (stage / "src" / "main.py").is_file(), "src 树未复制"
    assert (stage / "docs" / "使用说明.md").is_file(), "使用说明未复制"
    assert (stage / README_IMAGE_RELATIVE).is_file(), "README 首图未随包复制"
    assert (stage / "README.md").is_file(), "README 未复制"
    assert not (stage / "docs" / "shots").exists(), "便携包不应包含 docs/shots 目录"
    image_files = [
        path.relative_to(stage).as_posix()
        for path in stage.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    ]
    assert image_files, "便携包应含 README 首图"
    assert all(
        name.startswith("docs/images/") for name in image_files
    ), "便携包只允许 docs/images 下图片，实际：%s" % image_files


def test_qa_scripts_excluded_from_src_copy(tmp_path):
    """复制 src 树：保留业务源码，排除本地 QA 脚本与缓存/编译产物。"""
    module = load_build_portable()
    source = tmp_path / "src"
    target = tmp_path / "stage_src"
    (source / "ui").mkdir(parents=True)
    (source / "__pycache__").mkdir()
    (source / ".pytest_cache").mkdir()
    (source / "main.py").write_text("# main\n", encoding="utf-8")
    (source / "ui" / "main_window.py").write_text("# ui\n", encoding="utf-8")
    for name in module.LOCAL_QA_SCRIPTS:
        (source / "ui" / name).write_text("# local qa only\n", encoding="utf-8")
    (source / "ui" / "cached.pyc").write_bytes(b"cache")
    (source / "ui" / "cached.pyo").write_bytes(b"cache")
    (source / ".pytest_cache" / "v" / "cache").mkdir(parents=True)
    (source / ".pytest_cache" / "v" / "cache" / "nodeids").write_text("x", encoding="utf-8")
    (source / "__pycache__" / "main.cpython-312.pyc").write_bytes(b"cache")

    shutil.copytree(str(source), str(target), ignore=module.source_ignore_patterns())
    copied = {path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()}

    assert "main.py" in copied
    assert "ui/main_window.py" in copied
    for name in module.LOCAL_QA_SCRIPTS:
        assert "ui/%s" % name not in copied, "本地 QA 脚本不应进包：%s" % name
    assert not any(path.endswith((".pyc", ".pyo")) for path in copied), \
        "编译产物不应进包：%s" % sorted(copied)
    assert not any("__pycache__" in path for path in copied), "缓存目录不应进包：%s" % sorted(copied)
    assert not any(".pytest_cache" in path for path in copied), \
        "pytest 缓存不应进包：%s" % sorted(copied)


def test_purge_source_caches_removes_selfcheck_pyc(tmp_path):
    """自检运行产生的源码缓存：压缩前可清理，清理后自检通过；有泄漏时自检报错。"""
    module = load_build_portable()
    stage = tmp_path / "stage"
    cache_dir = stage / "src" / "ui" / "__pycache__"
    cache_dir.mkdir(parents=True)
    (stage / "src" / "main.py").write_text("# main\n", encoding="utf-8")
    (stage / "src" / "ui" / "main_window.py").write_text("# ui\n", encoding="utf-8")
    (cache_dir / "main_window.cpython-312.pyc").write_bytes(b"cache")
    (stage / "src" / "ui" / "stray.pyc").write_bytes(b"cache")

    assert module.list_source_cache_artifacts(stage), "应能发现源码缓存"
    with pytest.raises(module.BuildError):
        module.assert_no_source_caches(stage)

    removed = module.purge_source_caches(stage)
    assert removed >= 2, "应删除 __pycache__ 与 *.pyc：%d" % removed
    assert module.list_source_cache_artifacts(stage) == []
    module.assert_no_source_caches(stage)  # 不抛异常


def test_zip_skips_pycache_and_pyc(tmp_path):
    """create_zip 兜底跳过 __pycache__ / *.pyc（即使清理遗漏也不得进入 zip）。"""
    module = load_build_portable()
    stage = tmp_path / "stage"
    (stage / "src" / "ui" / "__pycache__").mkdir(parents=True)
    (stage / "src" / "main.py").write_text("# main\n", encoding="utf-8")
    (stage / "src" / "ui" / "main_window.py").write_text("# ui\n", encoding="utf-8")
    (stage / "src" / "ui" / "__pycache__" / "main_window.cpython-312.pyc").write_bytes(b"cache")
    (stage / "src" / "ui" / "stray.pyc").write_bytes(b"cache")
    zip_path = module.create_zip(stage, tmp_path / "out.zip")
    with zipfile.ZipFile(str(zip_path)) as archive_obj:
        names = archive_obj.namelist()
    assert any(name.endswith("src/main.py") for name in names)
    assert not any("__pycache__" in name or name.endswith((".pyc", ".pyo")) for name in names), \
        "zip 混入缓存/编译产物：%s" % names


def test_runtime_dependency_manifest():
    """P1：运行期依赖清单必须含 PySide6/requests/openpyxl，自检 import 含 box_client。"""
    module = load_build_portable()
    cfg = module.BuildConfig(project_root=PROJECT_ROOT)
    packages = module.runtime_package_specs(cfg)
    assert packages[0] == "PySide6==%s" % cfg.pyside_version, packages
    assert "requests" in packages and "openpyxl" in packages, packages
    for module_name in ("PySide6", "requests", "openpyxl", "src.stats.box_client"):
        assert module_name in module.RUNTIME_IMPORT_CHECKS, module.RUNTIME_IMPORT_CHECKS
    assert "src.stats.box_client" in module.RUNTIME_IMPORT_CHECKS
    # 源码真实依赖位置（requests 硬依赖 box_client；openpyxl 供 xlsx 解析）
    assert "import requests" in (PROJECT_ROOT / "src" / "stats" / "box_client.py").read_text(
        encoding="utf-8")
    assert "import openpyxl" in (PROJECT_ROOT / "src" / "fetcher" / "parsers.py").read_text(
        encoding="utf-8")


def test_stage_assertion_detects_qa_script_leak(tmp_path):
    """自检函数：干净包通过，误入 QA 脚本时报 BuildError。"""
    module = load_build_portable()
    stage = tmp_path / "stage"
    (stage / "src" / "ui").mkdir(parents=True)
    (stage / "src" / "main.py").write_text("# main\n", encoding="utf-8")
    module.assert_qa_scripts_excluded(stage)
    (stage / "src" / "ui" / "screenshot.py").write_text("# leak\n", encoding="utf-8")
    with pytest.raises(module.BuildError):
        module.assert_qa_scripts_excluded(stage)


def test_portable_run_bat_points_to_src_main():
    """便携版 run.bat 必须用 %PROJECT_DIR%src\\main.py 启动，且全 ASCII。"""
    module = load_build_portable()
    text = "\r\n".join(module.portable_run_bat_lines())
    assert "%PROJECT_DIR%src\\main.py" in text, "便携版 run.bat 未指向 src\\main.py"
    text.encode("ascii")  # 非 ASCII 会抛 UnicodeEncodeError
