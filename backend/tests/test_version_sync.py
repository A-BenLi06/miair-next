"""版本号一致性校验

docs/development.md 要求「发版时必须同步修改两处」:
    backend/app/engine/const.py  的 VERSION
    backend/pyproject.toml       的 version

这条约定此前只靠人工记忆, 漏改会导致:
- 面板「检查更新」拿 pyproject 版本与 GitHub Release 比较, 误报/漏报新版本
- 镜像内 `python -c "import app; print(app.__version__)"` 与包元数据不一致

本测试把约定固化为 CI 关卡。其余版本号 (前端 package.json、fnOS、iStore)
是各自渠道独立的发版号, 不要求与后端一致, 仅在此处记录现状以便追溯。
"""

import re
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent

sys.path.insert(0, str(BACKEND_DIR))

from app import __version__  # noqa: E402


def _pyproject_version() -> str:
    text = (BACKEND_DIR / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    assert match, "pyproject.toml 中未找到 version 字段"
    return match.group(1)


def _const_version() -> str:
    text = (BACKEND_DIR / "app" / "engine" / "const.py").read_text(encoding="utf-8")
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.M)
    assert match, "const.py 中未找到 VERSION"
    return match.group(1)


def test_package_version_exported():
    """app.__version__ 必须从 const.VERSION 导出, 而非另行硬编码"""
    assert __version__ == _const_version()


def test_pyproject_matches_const():
    """发版必改的两处必须一致 (docs/development.md 第 1 步)"""
    assert _pyproject_version() == _const_version(), (
        "版本号不一致: pyproject.toml="
        f"{_pyproject_version()} vs const.py={_const_version()}; "
        "发版时需同步修改这两处"
    )


def test_version_is_semver():
    """版本号须为 MAJOR.MINOR.PATCH, 否则 GitHub tag 与镜像标签策略会错乱"""
    version = _const_version()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"版本号不符合语义化格式: {version}"


@pytest.mark.parametrize(
    "relative_path,pattern,label",
    [
        ("frontend/package.json", r'"version"\s*:\s*"([^"]+)"', "前端"),
        ("fnos/manifest", r"^version\s*=\s*([\d.]+)", "fnOS"),
        ("istore/app-meta-miair-next/Makefile", r"^PKG_VERSION:=([\d.]+)", "iStore"),
    ],
)
def test_channel_versions_present_and_parseable(relative_path, pattern, label):
    """各发行渠道的版本号必须存在且可解析

    这些渠道有独立的发版节奏 (iStore 的 PKG_RELEASE 还是日期编号), 因此
    只校验「存在且格式正确」, 不强制与后端一致 —— 但要能机械读取, 避免
    渠道包用着早已废弃的版本号却无人发现。

    文件缺失按失败处理 (不用 pytest.skip): 否则在只 checkout 了 backend/
    的环境里会静默跳过, 让这道关卡形同虚设。
    """
    path = REPO_ROOT / relative_path
    assert path.is_file(), (
        f"{label} 配置文件缺失: {relative_path}; "
        "若确已移除该渠道, 请同步删除本参数化用例"
    )
    match = re.search(pattern, path.read_text(encoding="utf-8"), re.M)
    assert match, f"{label} 版本号无法从 {relative_path} 解析"
    assert re.fullmatch(r"\d+\.\d+(\.\d+)?", match.group(1)), (
        f"{label} 版本号格式异常: {match.group(1)}"
    )
