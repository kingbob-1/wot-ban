# -*- coding: utf-8 -*-
"""wot-ban 便携包构建脚本（Windows x64）。

流程
----
1. 检查源码与构建环境；
2. 解析便携 CPython 3.12 下载地址（python-build-standalone）；
3. 下载并校验 CPython 便携包（tar.gz）；
4. 解压到构建目录；
5. 向便携解释器安装 PySide6（默认清华镜像，可配代理）；
6. 组装便携包目录：复制源码 + 薄启动器 exe/图标/启动器文档 + 生成便携版 run.bat + 许可证/说明；
7. 自检：便携解释器 import PySide6、跑 src/main.py --version；
8. 打包 zip 并输出大小与 sha256。

说明
----
- 便携包 = 薄启动器 exe（不含 Python）+ 内嵌 CPython 3.12（包内 ``runtime\\`` 目录）+ PySide6 动态库 + 源码；
- ``--dry-run`` 只做参数解析、源码检查与步骤预览，不下载、不安装、不写盘；
- 默认不执行任何网络请求；只有实际构建（非 dry-run）或显式 ``--check-url`` 时才联网。

用法
----
    python scripts/build_portable.py --dry-run
    python scripts/build_portable.py
    python scripts/build_portable.py --proxy http://127.0.0.1:7897
    python scripts/build_portable.py --python-url "https://.../cpython-3.12.16%2B20261026-x86_64-pc-windows-msvc-install_only.tar.gz"
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import ssl
import subprocess
import sys
import tarfile
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

TOOL_NAME = "wot-ban portable builder"
TOOL_VERSION = "1.0.0"

# 构建步骤清单（展示与进度用；实际顺序由 execute_build 控制）
PLAN_TITLES = [
    "检查源码与构建环境",
    "解析便携 CPython 3.12 下载地址",
    "下载 CPython 便携包",
    "解压 CPython 便携包",
    "安装 PySide6",
    "组装便携包目录",
    "自检便携环境",
    "打包 zip 并输出校验信息",
]

# 仅用于本地出图/QA 的开发脚本：被 .gitignore 排除且含本机路径，不得进入便携包
LOCAL_QA_SCRIPTS = ("screenshot.py", "screenshot_real.py", "run_qa_guarded.py")
# 复制 src 源码树时的排除规则：Python 缓存/编译产物 + 本地 QA 脚本
SOURCE_IGNORE_PATTERNS = ("__pycache__", "*.pyc", "*.pyd") + LOCAL_QA_SCRIPTS


class BuildError(Exception):
    """构建失败（带中文提示与排障建议）。"""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


@dataclass
class BuildConfig:
    """构建配置（唯一可变配置容器；由 main() 组装后传入各步骤）。"""

    project_root: Path
    python_version: str = "3.12.16"
    python_tag: str = "20261026"
    python_url: str = ""
    python_archive: str = ""
    pyside_version: str = "6.12.0"
    index_url: str = "https://pypi.tuna.tsinghua.edu.cn/simple"
    proxy: str = ""
    output_dir: Path = Path("dist")
    work_dir: Path = Path("build")
    expected_sha256: str = ""
    keep_work: bool = False
    dry_run: bool = False
    check_url: bool = False
    skip_verify: bool = False
    insecure: bool = False

    def resolve_paths(self) -> None:
        """把相对路径统一解析为相对项目根的绝对路径。"""
        root = Path(self.project_root).resolve()
        self.project_root = root
        for name in ("output_dir", "work_dir"):
            value = Path(getattr(self, name)).expanduser()
            if not value.is_absolute():
                value = root / value
            setattr(self, name, value)
        if self.python_archive:
            archive_path = Path(self.python_archive).expanduser()
            if not archive_path.is_absolute():
                archive_path = root / archive_path
            self.python_archive = str(archive_path)


@dataclass
class BuildState:
    """构建过程中的中间状态（由各步骤逐步填充）。"""

    python_url: str = ""
    python_archive: Optional[Path] = None
    stage_dir: Optional[Path] = None
    python_dir: Optional[Path] = None
    zip_path: Optional[Path] = None
    report: dict = field(default_factory=dict)


# ------------------------------------------------------------------ 输出工具
def setup_console() -> None:
    """挂载 UTF-8 控制台输出，避免中文在 GBK 控制台中断。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def format_duration(seconds: float) -> str:
    """秒数格式化为 hh:mm:ss。"""
    total = int(round(seconds))
    return "%02d:%02d:%02d" % (total // 3600, total % 3600 // 60, total % 60)


def format_size(num_bytes: Optional[float]) -> str:
    """字节数格式化为易读文本。"""
    if num_bytes is None:
        return "未知"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%.1f %s" % (size, unit)
        size /= 1024.0
    return "%.1f GB" % size


def print_header(cfg: BuildConfig) -> None:
    """打印构建头信息。"""
    print("=" * 68)
    print("%s v%s" % (TOOL_NAME, TOOL_VERSION))
    print("项目根目录 : %s" % cfg.project_root)
    print("目标平台   : Windows x64（当前 %s %s）" % (platform.system(), platform.machine()))
    print("CPython    : %s（python-build-standalone %s）" % (cfg.python_version, cfg.python_tag))
    print("PySide6    : %s" % cfg.pyside_version)
    print("输出目录   : %s" % cfg.output_dir)
    print("构建目录   : %s" % cfg.work_dir)
    print("模式       : %s" % ("DRY-RUN（只预览，不下载不打包）" if cfg.dry_run else "实际构建"))


def print_step(index: int, total: int, title: str, elapsed: float, status: str) -> None:
    """打印单个步骤的进度与耗时。"""
    print("[%d/%d] %s ... %s（%.2f 秒）" % (index, total, title, status, elapsed))


def print_plan(cfg: BuildConfig, state: BuildState, problems: list) -> None:
    """dry-run 模式：打印构建计划与检查结果。"""
    print("")
    print("-" * 68)
    print("构建计划（dry-run，不执行下载/安装/复制/打包）：")
    for index, title in enumerate(PLAN_TITLES, 1):
        print("  %d. %s" % (index, title))
    print("")
    if cfg.python_archive:
        print("CPython 来源 : 本地文件 %s" % cfg.python_archive)
    else:
        print("计划下载地址 : %s" % state.python_url)
        print("预计下载体积 : 约 %s（CPython 便携包）" % format_size(44900000))
    print("预计成品体积 : 约 250 MB（CPython + PySide6 + 源码，zip 后体积会小一些）")
    print("成品 zip     : %s" % (cfg.output_dir / build_zip_name(cfg)))
    print("便携版入口   : 包内 坦克世界封号查询.exe（双击启动；内嵌运行时位于包内 runtime\\ 目录）")
    print("备用入口     : 包内 run.bat（全 ASCII，直接调用包内 runtime\\pythonw.exe）")
    print("")
    if problems:
        print("源码/环境检查：发现 %d 个问题" % len(problems))
        for item in problems:
            print("  - %s" % item)
    else:
        print("源码/环境检查：通过（必需文件齐全）")
    print("")
    print("下一步：去掉 --dry-run 即开始实际构建（需要网络下载约 45 MB CPython 包 + PySide6 wheel）。")


# ------------------------------------------------------------------ 环境检查
def collect_source_files(cfg: BuildConfig) -> dict:
    """返回需要随便携包分发的源码/说明文件清单。

    注意：``docs/shots/`` 是本地 gitignored 截图目录（含玩家昵称/账号 ID），
    既不是必需品、也不得进入便携包（发行包承诺不含任何界面截图），故不在此列。
    """
    root = cfg.project_root
    return {
        "src/main.py": root / "src" / "main.py",
        "src 目录": root / "src",
        "LICENSE": root / "LICENSE",
        "README.md": root / "README.md",
        "docs/使用说明.md": root / "docs" / "使用说明.md",
        "docs/第三方组件许可.md": root / "docs" / "第三方组件许可.md",
        "docs/P4-启动器.md": root / "docs" / "P4-启动器.md",
        "坦克世界封号查询.exe": root / "坦克世界封号查询.exe",
        "assets/app.ico": root / "assets" / "app.ico",
    }


def check_environment(cfg: BuildConfig) -> list:
    """检查源码文件与构建环境，返回问题列表（空列表表示通过）。"""
    problems = []
    for label, path in collect_source_files(cfg).items():
        if not Path(path).exists():
            problems.append("缺少必需文件/目录：%s（%s）" % (label, path))
    if platform.system() != "Windows":
        problems.append("当前系统不是 Windows：便携包仅面向 Windows x64（dry-run 可继续预览）")
    if platform.machine().lower() not in ("amd64", "x86_64"):
        problems.append("当前 CPU 架构为 %s：便携包目标为 x86_64" % platform.machine())
    if not cfg.python_url and not cfg.python_version and not cfg.python_archive:
        problems.append("未指定 CPython 版本且没有 --python-url / --python-archive")
    if not cfg.pyside_version:
        problems.append("未指定 PySide6 版本")
    if cfg.python_archive and not Path(cfg.python_archive).is_file():
        problems.append("--python-archive 指定的本地文件不存在：%s" % cfg.python_archive)
    return problems


def check_disk_space(cfg: BuildConfig) -> Optional[str]:
    """检查工作目录所在磁盘剩余空间（构建约需 1.5 GB）。"""
    try:
        usage = shutil.disk_usage(str(cfg.project_root))
    except OSError:
        return None
    if usage.free < 1500 * 1024 * 1024:
        return "磁盘剩余空间不足 1.5 GB（当前 %s），建议清理后再构建" % format_size(usage.free)
    return None


# ------------------------------------------------------------------ CPython 下载
def build_python_url(cfg: BuildConfig) -> str:
    """按配置拼出 python-build-standalone 的 Windows x64 install_only 下载地址。"""
    if cfg.python_url:
        return cfg.python_url
    name = "cpython-%s%%2B%s-x86_64-pc-windows-msvc-install_only.tar.gz" % (
        cfg.python_version,
        cfg.python_tag,
    )
    prefix = (
        "https://github.com/astral-sh/python-build-standalone/releases/download/%s"
        % cfg.python_tag
    )
    return "%s/%s" % (prefix, name)


def build_ssl_context(cfg: BuildConfig) -> Optional[ssl.SSLContext]:
    """构造 HTTPS 校验上下文：优先 certifi CA 包，缺失时回退系统默认。"""
    if cfg.insecure:
        print("[警告] 已启用 --insecure：跳过 HTTPS 证书校验，仅建议在受信任的内网代理环境使用")
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    try:
        import certifi  # noqa: F401 - 可选依赖，仅用于补齐根证书
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()
    except (OSError, ssl.SSLError):
        return ssl.create_default_context()


def make_opener(cfg: BuildConfig):
    """按 --proxy / 环境变量构造 urllib opener。"""
    handlers = []
    proxy = cfg.proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    context = build_ssl_context(cfg)
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers)


def probe_url(cfg: BuildConfig, url: str) -> tuple:
    """HEAD 探测下载地址是否可用，返回 (ok, 说明)。"""
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "wot-ban-build"})
    try:
        opener = make_opener(cfg)
        with opener.open(request, timeout=30) as response:
            length = response.headers.get("Content-Length")
            return True, "HTTP %s，大小 %s" % (response.status, format_size(length))
    except Exception as exc:  # noqa: BLE001 - 网络探测失败仅提示，不抛出
        return False, "无法访问（%s）" % exc


def download_file(cfg: BuildConfig, url: str, target: Path) -> Path:
    """下载文件到 target，边下边显示进度百分比。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "wot-ban-build"})
    opener = make_opener(cfg)
    temporary = target.with_suffix(target.suffix + ".part")
    downloaded = 0
    try:
        with opener.open(request, timeout=120) as response:
            total = int(response.headers.get("Content-Length") or 0)
            with temporary.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        percent = downloaded * 100 // total
                        if percent % 10 == 0:
                            print("      已下载 %3d%%（%s / %s）" % (percent, format_size(downloaded), format_size(total)))
        temporary.replace(target)
    except Exception as exc:  # noqa: BLE001 - 转中文提示
        if temporary.exists():
            temporary.unlink(missing_ok=True)
        raise BuildError(
            "下载 CPython 便携包失败：%s" % exc,
            "检查网络/代理（可用 --proxy http://127.0.0.1:7897），"
            "或手动下载后用 --python-url 指向本地或可用镜像地址",
        ) from exc
    return target


def sha256_file(path: Path) -> str:
    """计算文件 sha256。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_archive_sha256(cfg: BuildConfig, archive: Path) -> None:
    """如指定了 --sha256，则校验下载文件。"""
    if not cfg.expected_sha256:
        return
    actual = sha256_file(archive)
    if actual.lower() != cfg.expected_sha256.lower():
        raise BuildError(
            "CPython 便携包 sha256 校验失败（期望 %s，实际 %s）" % (cfg.expected_sha256, actual),
            "删除构建目录后重试，或更换镜像地址",
        )


def extract_python_archive(cfg: BuildConfig, archive: Path, target_dir: Path) -> Path:
    """解压 CPython 便携包，返回解释器目录（archive 内顶层 python/ 目录）。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:gz") as archive_obj:
            try:
                archive_obj.extractall(str(target_dir), filter="data")
            except TypeError:  # Python < 3.12 不支持 filter 参数
                archive_obj.extractall(str(target_dir))
    except (tarfile.TarError, OSError) as exc:
        raise BuildError(
            "解压 CPython 便携包失败：%s" % exc,
            "确认下载文件完整（可加 --sha256 校验）后删除构建目录重试",
        ) from exc
    python_dir = target_dir / "python"
    if not (python_dir / "python.exe").is_file():
        raise BuildError(
            "解压后的目录结构异常：未找到 %s" % (python_dir / "python.exe"),
            "该压缩包可能不是 python-build-standalone 的 install_only 构建，请检查 --python-url",
        )
    return python_dir


def bundled_python_exe(python_dir: Path) -> Path:
    """便携解释器路径。"""
    return Path(python_dir) / "python.exe"


def subprocess_env(cfg: BuildConfig) -> dict:
    """构造子进程环境变量（代理、UTF-8 输出、离屏 Qt）。"""
    env = dict(os.environ)
    proxy = cfg.proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    if proxy:
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy
    env["PYTHONIOENCODING"] = "utf-8"
    env["QT_QPA_PLATFORM"] = "offscreen"
    return env


def run_python_command(cfg: BuildConfig, python_dir: Path, args: list, timeout: int = 120) -> subprocess.CompletedProcess:
    """用便携解释器执行命令（统一编码/代理环境；超时转为失败结果）。"""
    command = [str(bundled_python_exe(python_dir))] + list(args)
    try:
        return subprocess.run(
            command,
            cwd=str(cfg.project_root),
            env=subprocess_env(cfg),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 1, "", "命令执行超时（%d 秒）" % timeout)
    except OSError as exc:
        return subprocess.CompletedProcess(command, 1, "", "命令无法启动：%s" % exc)


def ensure_pip(cfg: BuildConfig, python_dir: Path) -> None:
    """确保便携解释器内可用 pip（部分便携构建未自带 pip）。"""
    probe = run_python_command(cfg, python_dir, ["-m", "pip", "--version"], timeout=60)
    if probe.returncode == 0:
        print("      pip 已就绪：%s" % (probe.stdout or "").strip())
        return
    print("      便携解释器未自带 pip，尝试 ensurepip ...")
    ensured = run_python_command(cfg, python_dir, ["-m", "ensurepip", "--upgrade"], timeout=180)
    if ensured.returncode == 0:
        print("      ensurepip 完成")
        return
    print("      ensurepip 不可用，改为下载 get-pip.py ...")
    get_pip_url = "https://bootstrap.pypa.io/get-pip.py"
    target = cfg.work_dir / "downloads" / "get-pip.py"
    download_file(cfg, get_pip_url, target)
    boot = run_python_command(cfg, python_dir, [str(target), "--no-warn-script-location"], timeout=300)
    if boot.returncode != 0:
        raise BuildError(
            "便携解释器无法安装 pip（退出码 %d）" % boot.returncode,
            "检查网络，或改用自带 pip 的 python-build-standalone 版本；详细输出：%s"
            % ((boot.stderr or "")[-800:]),
        )
    print("      get-pip.py 安装完成")


def install_pyside(cfg: BuildConfig, python_dir: Path) -> None:
    """向便携解释器安装 PySide6。"""
    ensure_pip(cfg, python_dir)
    python_exe = str(bundled_python_exe(python_dir))
    command = [
        python_exe,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-warn-script-location",
        "--index-url",
        cfg.index_url,
        "PySide6==%s" % cfg.pyside_version,
    ]
    print("      执行：%s" % " ".join(command))
    result = subprocess.run(
        command,
        cwd=str(cfg.project_root),
        env=subprocess_env(cfg),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        tail = (result.stdout or "")[-1500:] + (result.stderr or "")[-1500:]
        raise BuildError(
            "PySide6 安装失败（pip 退出码 %d）" % result.returncode,
            "检查网络/代理与 --index-url；详细输出：" + tail,
        )


# ------------------------------------------------------------------ 组装便携包
def portable_run_bat_lines() -> list:
    """返回便携版 run.bat 的逐行内容（全 ASCII）。"""
    return [
        "@echo off",
        "rem wot-ban portable ASCII fallback launcher.",
        "rem Normal use: double-click the launcher exe in this folder.",
        "rem This bat is an ASCII-only fallback that runs src/main.py with the bundled runtime.",
        "setlocal",
        'set "PROJECT_DIR=%~dp0"',
        'set "PY=%PROJECT_DIR%runtime\\python.exe"',
        'set "PYW=%PROJECT_DIR%runtime\\pythonw.exe"',
        "",
        'if not exist "%PY%" (',
        "    echo [ERROR] Bundled Python runtime not found:",
        "    echo         %PY%",
        "    echo         Re-extract the portable package.",
        "    pause",
        "    exit /b 1",
        ")",
        "",
        '"%PY%" -c "import PySide6" >nul 2>&1',
        "if errorlevel 1 (",
        "    echo [ERROR] PySide6 is missing from the bundled runtime.",
        "    echo         Re-extract the portable package.",
        "    pause",
        "    exit /b 1",
        ")",
        "",
        'cd /d "%PROJECT_DIR%"',
        'if exist "%PYW%" (',
        '    start "" "%PYW%" "%PROJECT_DIR%src\\main.py" %*',
        "    exit /b 0",
        ")",
        "",
        '"%PY%" "%PROJECT_DIR%src\\main.py" %*',
        'set "EXIT_CODE=%errorlevel%"',
        'if not "%EXIT_CODE%"=="0" pause',
        "exit /b %EXIT_CODE%",
        "",
    ]


def write_portable_run_bat(target: Path) -> None:
    """写入便携版 run.bat（ASCII + CRLF，符合 Windows bat 规范）。"""
    payload = "\r\n".join(portable_run_bat_lines()).encode("ascii")
    Path(target).write_bytes(payload)


def read_app_version(cfg: BuildConfig) -> str:
    """从 src/config.py 读取 app_version（避免版本号写两份）。"""
    config_path = cfg.project_root / "src" / "config.py"
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return "0.0.0"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('app_version: str = "'):
            return stripped.split('"')[1]
    return "0.0.0"


def build_zip_name(cfg: BuildConfig) -> str:
    """便携包 zip 文件名。"""
    return "wot-ban-portable-v%s-win64.zip" % read_app_version(cfg)


def build_stage_name(cfg: BuildConfig) -> str:
    """便携包目录名（解压后可见的根目录）。"""
    return "wot-ban-portable-v%s-win64" % read_app_version(cfg)


def source_ignore_patterns():
    """返回复制 src 源码树时的忽略规则（缓存/编译产物 + 本地 QA 脚本）。"""
    return shutil.ignore_patterns(*SOURCE_IGNORE_PATTERNS)


def assert_qa_scripts_excluded(stage_dir: Path) -> None:
    """自检：本地 QA 脚本不得进入便携包（被 .gitignore 排除且含本机路径）。"""
    leaked = []
    for name in LOCAL_QA_SCRIPTS:
        for path in sorted(Path(stage_dir).rglob(name)):
            if path.is_file():
                leaked.append(path.relative_to(stage_dir).as_posix())
    if leaked:
        raise BuildError(
            "便携包混入本地 QA 脚本：%s" % ", ".join(leaked),
            "检查 scripts/build_portable.py 的 SOURCE_IGNORE_PATTERNS（src 复制排除规则）",
        )


def copy_source_tree(cfg: BuildConfig, stage_dir: Path) -> None:
    """把源码与说明文件复制进便携包目录（入口 src/main.py 随 src 树复制）。"""
    root = cfg.project_root
    stage_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "LICENSE", stage_dir / "LICENSE")
    shutil.copy2(root / "README.md", stage_dir / "README.md")
    shutil.copytree(
        root / "src",
        stage_dir / "src",
        ignore=source_ignore_patterns(),
    )
    docs_target = stage_dir / "docs"
    docs_target.mkdir(parents=True, exist_ok=True)
    # 便携包不带任何界面截图：docs/shots 为本地 gitignored 目录，含玩家昵称/账号 ID，严禁外发
    shutil.copy2(root / "docs" / "使用说明.md", docs_target / "使用说明.md")
    shutil.copy2(root / "docs" / "第三方组件许可.md", docs_target / "第三方组件许可.md")
    launcher_exe = root / "坦克世界封号查询.exe"
    if not launcher_exe.is_file():
        raise BuildError(
            "缺少薄启动器 exe：%s" % launcher_exe,
            "先运行 bat\\build_launcher.bat 生成启动器（便携包 = 启动器 + 内嵌运行时 + 源码）",
        )
    shutil.copy2(launcher_exe, stage_dir / launcher_exe.name)
    icon_source = root / "assets" / "app.ico"
    if icon_source.is_file():
        icon_target = stage_dir / "assets"
        icon_target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(icon_source, icon_target / icon_source.name)
    p4_doc = root / "docs" / "P4-启动器.md"
    if p4_doc.is_file():
        shutil.copy2(p4_doc, docs_target / p4_doc.name)
    write_portable_run_bat(stage_dir / "run.bat")


def verify_stage(cfg: BuildConfig, stage_dir: Path, python_dir: Path) -> dict:
    """自检便携环境：import PySide6 / 跑 src/main.py --version。"""
    python_exe = str(bundled_python_exe(python_dir))
    env = subprocess_env(cfg)
    check_code = (
        "import sys; import PySide6; from PySide6 import QtCore; "
        "print('python=' + sys.version.split()[0]); "
        "print('PySide6=' + PySide6.__version__); "
        "print('Qt=' + QtCore.qVersion())"
    )
    result = subprocess.run(
        [python_exe, "-c", check_code],
        cwd=str(stage_dir),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise BuildError(
            "便携环境自检失败：PySide6 无法导入（%s）" % ((result.stderr or "").strip()[-800:]),
            "检查 PySide6 安装是否完整，或改用其他 PySide6 版本重试",
        )
    version_result = subprocess.run(
        [python_exe, str(stage_dir / "src" / "main.py"), "--version"],
        cwd=str(stage_dir),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if version_result.returncode != 0:
        raise BuildError(
            "便携环境自检失败：src/main.py --version 退出码 %d" % version_result.returncode,
            "检查源码是否完整",
        )
    report = {
        "python_info": (result.stdout or "").strip().splitlines(),
        "app_version_output": (version_result.stdout or "").strip(),
        "launcher_output": "",
    }
    launcher_exe = stage_dir / "坦克世界封号查询.exe"
    if launcher_exe.is_file():
        launcher_result = subprocess.run(
            [str(launcher_exe), "--version"],
            cwd=str(stage_dir),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if launcher_result.returncode != 0:
            raise BuildError(
                "便携包自检失败：启动器 exe --version 退出码 %d" % launcher_result.returncode,
                "确认包内 runtime\\python.exe、src\\main.py 与启动器布局正确；详细输出：%s"
                % ((launcher_result.stderr or "")[-600:]),
            )
        launcher_stdout = (launcher_result.stdout or "").strip()
        report["launcher_output"] = launcher_stdout.splitlines()[-1] if launcher_stdout else ""
    return report


def create_zip(stage_dir: Path, zip_path: Path) -> Path:
    """把便携包目录压缩成 zip。"""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    base_parent = stage_dir.parent
    with zipfile.ZipFile(str(zip_path), "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive_obj:
        for current_dir, _dir_names, file_names in os.walk(str(stage_dir)):
            for file_name in file_names:
                full_path = Path(current_dir) / file_name
                arc_name = full_path.relative_to(base_parent).as_posix()
                archive_obj.write(str(full_path), arc_name)
    return zip_path


# ------------------------------------------------------------------ 构建流程
def execute_build(cfg: BuildConfig, state: BuildState) -> int:
    """执行完整构建（dry-run 时只检查与打印计划）；返回退出码。"""
    total = len(PLAN_TITLES)
    start_all = time.perf_counter()

    step_start = time.perf_counter()
    problems = check_environment(cfg)
    space_problem = check_disk_space(cfg)
    if space_problem:
        problems.append(space_problem)
    for problem in problems:
        print("[警告] %s" % problem)
    print_step(1, total, PLAN_TITLES[0], time.perf_counter() - step_start, "完成")

    step_start = time.perf_counter()
    state.python_url = build_python_url(cfg)
    if cfg.check_url or (not cfg.dry_run and not cfg.python_archive):
        ok, detail = probe_url(cfg, state.python_url)
        print("[检查] CPython 下载地址：%s（%s）" % (detail, "可用" if ok else "当前不可用"))
        if not ok and not cfg.dry_run:
            raise BuildError(
                "CPython 下载地址不可用：%s" % state.python_url,
                "检查网络/代理，或换用 --python-url / --python-archive 指定可用来源"
                "（见 python-build-standalone releases）",
            )
    print_step(2, total, PLAN_TITLES[1], time.perf_counter() - step_start, "完成")

    if cfg.dry_run:
        print_plan(cfg, state, problems)
        hard_errors = [
            item
            for item in problems
            if item.startswith("缺少必需文件") or item.startswith("--python-archive")
        ]
        if hard_errors:
            print("[失败] dry-run 检查未通过：%d 个必需条件不满足（见上）" % len(hard_errors))
            return 1
        print("[完成] dry-run 检查结束，未执行任何下载/安装/打包操作，耗时 %s" % format_duration(time.perf_counter() - start_all))
        return 0

    # ---------------- 实际构建 ----------------
    stage_name = build_stage_name(cfg)
    stage_dir = cfg.work_dir / "stage" / stage_name

    for label in (cfg.output_dir, cfg.work_dir):
        Path(label).mkdir(parents=True, exist_ok=True)
    if stage_dir.exists():
        print("[清理] 删除上次构建的临时目录：%s" % stage_dir)
        shutil.rmtree(stage_dir, ignore_errors=True)

    step_start = time.perf_counter()
    if cfg.python_archive:
        local_archive = Path(cfg.python_archive)
        if not local_archive.is_file():
            raise BuildError(
                "本地 CPython 便携包不存在：%s" % local_archive,
                "检查 --python-archive 路径，或去掉该参数改为联网下载",
            )
        state.python_archive = local_archive
        print("[使用] 本地 CPython 便携包：%s" % local_archive)
    else:
        state.python_archive = cfg.work_dir / "downloads" / urllib.parse.unquote(Path(state.python_url).name)
        if state.python_archive.exists() and state.python_archive.stat().st_size > 1024 * 1024:
            print("[跳过] 已存在下载文件：%s（如需重下请删除后重试）" % state.python_archive)
        else:
            state.python_archive = download_file(cfg, state.python_url, state.python_archive)
    verify_archive_sha256(cfg, state.python_archive)
    print(
        "[校验] CPython 便携包大小 %s，sha256=%s"
        % (format_size(state.python_archive.stat().st_size), sha256_file(state.python_archive))
    )
    print_step(3, total, PLAN_TITLES[2], time.perf_counter() - step_start, "完成")

    step_start = time.perf_counter()
    extract_dir = cfg.work_dir / "extract"
    if extract_dir.exists():
        shutil.rmtree(extract_dir, ignore_errors=True)
    state.python_dir = extract_python_archive(cfg, state.python_archive, extract_dir)
    print_step(4, total, PLAN_TITLES[3], time.perf_counter() - step_start, "完成（%s）" % state.python_dir)

    step_start = time.perf_counter()
    install_pyside(cfg, state.python_dir)
    print_step(5, total, PLAN_TITLES[4], time.perf_counter() - step_start, "完成")

    step_start = time.perf_counter()
    stage_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        state.python_dir,
        stage_dir / "runtime",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    copy_source_tree(cfg, stage_dir)
    assert_qa_scripts_excluded(stage_dir)
    print_step(6, total, PLAN_TITLES[5], time.perf_counter() - step_start, "完成（%s）" % stage_dir)

    step_start = time.perf_counter()
    if cfg.skip_verify:
        print("[跳过] 便携环境自检（--skip-verify）")
    else:
        state.report = verify_stage(cfg, stage_dir, stage_dir / "runtime")
        print("[自检] %s" % "；".join(state.report.get("python_info", [])))
        print("[自检] %s" % state.report.get("app_version_output", ""))
        if state.report.get("launcher_output"):
            print("[自检] 启动器 exe：%s" % state.report["launcher_output"])
    print_step(7, total, PLAN_TITLES[6], time.perf_counter() - step_start, "完成")

    step_start = time.perf_counter()
    state.zip_path = create_zip(stage_dir, cfg.output_dir / build_zip_name(cfg))
    zip_size = state.zip_path.stat().st_size
    zip_sha = sha256_file(state.zip_path)
    print("[产物] %s" % state.zip_path)
    print("[产物] 体积 %s，sha256=%s" % (format_size(zip_size), zip_sha))
    print_step(8, total, PLAN_TITLES[7], time.perf_counter() - step_start, "完成")

    if not cfg.keep_work:
        print("[清理] 删除临时构建目录：%s（加 --keep-work 可保留）" % cfg.work_dir)
        shutil.rmtree(cfg.work_dir, ignore_errors=True)

    print("")
    print("[完成] 便携包构建成功，总耗时 %s" % format_duration(time.perf_counter() - start_all))
    print("       zip : %s" % state.zip_path)
    print("       大小: %s" % format_size(zip_size))
    print("       sha256: %s" % zip_sha)
    print("       发布前请解压 zip 并双击 run.bat 做一次冒烟（无名单数据、无 log/data 目录）。")
    return 0


# ------------------------------------------------------------------ 主函数
def parse_args(argv: list) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        prog="build_portable.py",
        description="构建 wot-ban Windows 便携包（薄启动器 + 内嵌 CPython + PySide6 + 源码）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只做参数解析、源码检查与步骤预览，不下载不打包")
    parser.add_argument("--check-url", action="store_true", help="HEAD 探测 CPython 下载地址是否可用（需联网）")
    parser.add_argument("--python-version", default="3.12.16", help="便携 CPython 版本，默认 3.12.16")
    parser.add_argument("--python-tag", default="20261026", help="python-build-standalone 发布标签，默认 20261026")
    parser.add_argument("--python-url", default="", help="直接指定 CPython 便携包下载地址（覆盖版本/标签）")
    parser.add_argument(
        "--python-archive",
        default="",
        help="使用本地已有的 CPython 便携包 tar.gz（跳过下载；适合离线构建）",
    )
    parser.add_argument("--pyside-version", default="6.12.0", help="PySide6 版本，默认 6.12.0")
    parser.add_argument(
        "--index-url",
        default="https://pypi.tuna.tsinghua.edu.cn/simple",
        help="pip 镜像地址，默认清华镜像",
    )
    parser.add_argument("--proxy", default="", help="下载与 pip 使用的代理，如 http://127.0.0.1:7897")
    parser.add_argument("--output-dir", default="dist", help="zip 输出目录，默认项目内 dist")
    parser.add_argument("--work-dir", default="build", help="构建临时目录，默认项目内 build")
    parser.add_argument("--sha256", default="", help="可选：校验下载的 CPython 便携包 sha256")
    parser.add_argument("--keep-work", action="store_true", help="构建完成后保留临时目录")
    parser.add_argument("--skip-verify", action="store_true", help="跳过便携环境自检（不推荐）")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="跳过 HTTPS 证书校验（仅内网/代理证书异常时临时使用）",
    )
    return parser.parse_args(argv)


def main(argv: list) -> int:
    """主入口：参数解析 -> 配置组装 -> 执行构建。"""
    setup_console()
    args = parse_args(argv)
    base_path = "."
    cfg = BuildConfig(
        project_root=Path(__file__).resolve().parent.parent,
        python_version=args.python_version,
        python_tag=args.python_tag,
        python_url=args.python_url,
        python_archive=args.python_archive,
        pyside_version=args.pyside_version,
        index_url=args.index_url,
        proxy=args.proxy,
        output_dir=Path(args.output_dir),
        work_dir=Path(args.work_dir),
        expected_sha256=args.sha256,
        keep_work=args.keep_work,
        dry_run=args.dry_run,
        check_url=args.check_url,
        skip_verify=args.skip_verify,
        insecure=args.insecure,
    )
    cfg.resolve_paths()
    print_header(cfg)
    print("基路径     : %s" % base_path)
    start_all = time.perf_counter()
    state = BuildState()
    try:
        code = execute_build(cfg, state)
    except BuildError as exc:
        print("")
        print("[失败] %s" % exc.message)
        if exc.hint:
            print("[建议] %s" % exc.hint)
        print("总耗时 %s" % format_duration(time.perf_counter() - start_all))
        return 1
    except KeyboardInterrupt:
        print("")
        print("[中止] 用户中断构建")
        print("总耗时 %s" % format_duration(time.perf_counter() - start_all))
        return 130
    except Exception as exc:  # noqa: BLE001 - 顶层兜底：未预期错误给出中文提示与排障建议
        print("")
        print("[失败] 未预期错误：%s" % exc)
        print("[建议] 加 --keep-work 保留临时目录后重试；若仍失败请附完整输出反馈")
        print("总耗时 %s" % format_duration(time.perf_counter() - start_all))
        return 1
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
