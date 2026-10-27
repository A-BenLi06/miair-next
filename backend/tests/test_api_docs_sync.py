"""接口文档与 OpenAPI schema 的同步校验

背景: docs/api.md 曾与实现长期脱节 —— 文档里写着已在下线提交 (9e0ed7e
「移除无用暂停停止接口」) 中被删除的 `/speakers/{did}/pause`, 同时又漏记了
11 个真实存在的路由 (账号多步登录、日志下载/等级、通知测试等)。
靠人工维护必然再次漂移, 故用测试把两者锚定:

- 代码有、文档无  -> 失败 (新增路由必须补文档)
- 文档有、代码无  -> 失败 (删除路由必须同步删文档)

文档表格的解析只看 `| METHOD | `/path` |` 形式, 因此正文里用反引号提到
某个路径不会造成误判。
"""

import re
from pathlib import Path

import pytest

from app.main import create_app

DOC_PATH = Path(__file__).resolve().parents[2] / "docs" / "api.md"

# 表格行: | GET | `/api/v1/xxx` | 说明 |
_ROW_RE = re.compile(r"^\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|\s*`([^`]+)`\s*\|", re.M)

# 这些路径属于框架自带文档端点, 不属于业务 API 清单
_IGNORED_PREFIXES = ("/api/docs", "/api/openapi.json", "/redoc", "/docs")


def _documented_routes() -> set[tuple[str, str]]:
    text = DOC_PATH.read_text(encoding="utf-8")
    routes = set()
    for method, path in _ROW_RE.findall(text):
        # 去掉查询串与锚点, 只留路径
        clean = path.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        routes.add((method.upper(), clean))
    return routes


def _actual_routes() -> set[tuple[str, str]]:
    app = create_app()
    routes = set()
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith("/api/v1"):
            continue
        if path.startswith(_IGNORED_PREFIXES):
            continue
        for method in operations:
            routes.add((method.upper(), path.rstrip("/")))
    return routes


@pytest.fixture(scope="module")
def route_sets():
    assert DOC_PATH.is_file(), f"接口文档不存在: {DOC_PATH}"
    return _documented_routes(), _actual_routes()


def test_docs_is_not_empty(route_sets):
    """防止解析正则失效导致两侧都为空而"假通过\""""
    documented, actual = route_sets
    assert len(documented) >= 30, f"文档仅解析到 {len(documented)} 条路由, 疑似解析失效"
    assert len(actual) >= 30, f"OpenAPI 仅解析到 {len(actual)} 条路由, 疑似解析失效"


def test_no_undocumented_routes(route_sets):
    """代码有、文档没有 -> 新增接口必须补进 docs/api.md"""
    documented, actual = route_sets
    missing = sorted(actual - documented)
    assert not missing, "以下接口已实现但未写入 docs/api.md:\n" + "\n".join(
        f"  {m:7} {p}" for m, p in missing
    )


def test_no_stale_documented_routes(route_sets):
    """文档有、代码没有 -> 已删除的接口必须从文档移除"""
    documented, actual = route_sets
    stale = sorted(documented - actual)
    assert not stale, "以下接口在 docs/api.md 中有记录但代码中不存在 (文档漂移):\n" + "\n".join(
        f"  {m:7} {p}" for m, p in stale
    )


def test_pause_route_is_intentionally_absent(route_sets):
    """回归锚点: Web 面板的 pause 接口已在下线提交中移除, 不得被误加回文档

    提交 9e0ed7e「移除无用暂停停止接口及相关前端调用」删除了该路由, 但文档
    长期未同步, 导致「文档说能暂停、实际 404」。若将来重新提供该能力, 请同时
    删除本测试并补文档, 而不是只改一边。
    """
    documented, actual = route_sets
    pause = ("POST", "/api/v1/speakers/{did}/pause")
    assert pause not in actual, "pause 路由被重新加入, 请同步更新文档与本测试"
    assert pause not in documented, "pause 路由已不存在, 文档仍在记录它"
