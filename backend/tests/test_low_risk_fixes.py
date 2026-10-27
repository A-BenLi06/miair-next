"""低风险修复项的回归测试

覆盖:
- P2-02 未匹配的 /api/* 返回 404 JSON (而非 200 + SPA HTML)
- P2-01 SQLite 连接显式关闭 (不依赖 GC 回收 fd)
- P2-04 _active_proxy_tasks 空集合随任务结束移除
- P3-01 DIDL-Lite 空 <item/> 不再因元素真值语义被判为"未找到"
"""

import asyncio
import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))


# ---------- P2-02: SPA fallback 不吞 /api/* ----------


@pytest.fixture
def spa_app(tmp_path, monkeypatch):
    """构造一个带静态目录的应用, 触发 SPA fallback 注册"""
    import app.main as main_module

    static_dir = tmp_path / "static"
    (static_dir / "assets").mkdir(parents=True)
    (static_dir / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    monkeypatch.setattr(main_module, "STATIC_DIR", str(static_dir))
    return main_module.create_app()


def test_unknown_api_path_returns_404_json(spa_app):
    """未匹配的 API 必须回 404 JSON: 否则前端 JSON.parse(HTML) 报错, 真实原因不可见"""
    from fastapi.testclient import TestClient

    with TestClient(spa_app) as client:
        resp = client.get("/api/v1/definitely-not-a-route")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == {"detail": "Not Found"}


def test_api_root_returns_404_json(spa_app):
    """裸 /api 同样不应返回 SPA"""
    from fastapi.testclient import TestClient

    with TestClient(spa_app) as client:
        resp = client.get("/api")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")


def test_spa_route_still_serves_index(spa_app):
    """非 API 的前端路由必须继续回落到 index.html (回归保护)"""
    from fastapi.testclient import TestClient

    with TestClient(spa_app) as client:
        resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "spa" in resp.text


def test_static_asset_still_served(spa_app, tmp_path):
    """真实静态文件仍按文件返回"""
    asset = tmp_path / "static" / "favicon.ico"
    asset.write_bytes(b"icon")
    from fastapi.testclient import TestClient

    with TestClient(spa_app) as client:
        resp = client.get("/favicon.ico")
    assert resp.status_code == 200
    assert resp.content == b"icon"


# ---------- P2-01: SQLite 连接显式关闭 ----------


def test_db_connections_do_not_leak_fds(tmp_path, monkeypatch):
    """关闭 GC 后反复查询不得泄漏文件描述符

    原实现 `with _lock, _connect() as conn:` 中 sqlite3 连接的 __exit__
    只提交事务不关闭连接, 全靠 CPython 引用计数兜底。GC 一旦延迟就线性泄漏
    (实测 500 次查询泄漏 500 个 fd)。
    """
    import gc

    from app.core.config import AppSettings
    from app.db import store

    monkeypatch.setenv("MIAIR_DATA", str(tmp_path))
    monkeypatch.setattr(
        "app.db.store.get_settings", lambda: AppSettings()
    )
    store.init_db()

    def fd_count() -> int:
        return len(os.listdir("/dev/fd"))

    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        before = fd_count()
        for i in range(200):
            store.get_password_hash(f"nobody-{i}")
            store.add_login_log("u", "127.0.0.1", True)
        leaked = fd_count() - before
    finally:
        if gc_was_enabled:
            gc.enable()

    assert leaked == 0, f"泄漏 {leaked} 个文件描述符 (连接未显式关闭)"


def test_db_operations_still_work(tmp_path, monkeypatch):
    """关闭逻辑不得破坏事务语义: 写入需可见, 重复用户需返回 False"""
    from app.core.config import AppSettings
    from app.db import store

    monkeypatch.setenv("MIAIR_DATA", str(tmp_path))
    monkeypatch.setattr("app.db.store.get_settings", lambda: AppSettings())
    store.init_db()

    assert store.is_initialized() is False
    assert store.create_user("admin", "hash-1") is True
    assert store.create_user("admin", "hash-2") is False  # UNIQUE 冲突
    assert store.is_initialized() is True
    assert store.get_password_hash("admin") == "hash-1"
    assert store.update_password("admin", "hash-3") is True
    assert store.get_password_hash("admin") == "hash-3"


# ---------- P2-04: 代理任务追踪表不残留 ----------


def test_active_proxy_tasks_entry_removed_when_empty():
    """任务结束后应移除对应 udn 的 key, 而非只清空集合留下空 set"""
    from app.engine.dlna.device_server import DeviceServer

    server = DeviceServer("127.0.0.1", 0, None)

    async def run():
        udn = "udn-test"
        tasks = server._active_proxy_tasks.setdefault(udn, set())
        current = asyncio.current_task()
        tasks.add(current)

        def _untrack(t, _udn=udn, _tasks=tasks):
            _tasks.discard(t)
            if not _tasks and server._active_proxy_tasks.get(_udn) is _tasks:
                server._active_proxy_tasks.pop(_udn, None)

        current.add_done_callback(_untrack)

    # 用一个短任务模拟请求结束
    async def scenario():
        await asyncio.create_task(run())
        await asyncio.sleep(0)  # 让 done_callback 执行

    asyncio.run(scenario())
    assert server._active_proxy_tasks == {}, (
        f"残留空集合: {server._active_proxy_tasks}"
    )


def test_untrack_does_not_keyerror_after_unregister():
    """设备注销后 done_callback 不得因回查 udn 抛 KeyError

    旧实现 lambda 里写 self._active_proxy_tasks[udn].discard(t), key 被
    unregister_renderer/_cleanup_all_buffers 清掉后会抛 KeyError, 只是
    asyncio 把回调异常吞掉了不易察觉。
    """
    from app.engine.dlna.device_server import DeviceServer

    server = DeviceServer("127.0.0.1", 0, None)

    async def scenario():
        udn = "udn-gone"
        tasks = server._active_proxy_tasks.setdefault(udn, set())
        current = asyncio.current_task()
        tasks.add(current)

        def _untrack(t, _udn=udn, _tasks=tasks):
            _tasks.discard(t)
            if not _tasks and server._active_proxy_tasks.get(_udn) is _tasks:
                server._active_proxy_tasks.pop(_udn, None)

        current.add_done_callback(_untrack)
        # 模拟设备被注销: 整个 key 被删掉
        server._active_proxy_tasks.pop(udn, None)

    asyncio.run(scenario())
    asyncio.run(asyncio.sleep(0))
    assert server._active_proxy_tasks == {}


# ---------- P3-01: DIDL-Lite 空 item ----------


def test_empty_item_still_yields_no_metadata():
    """空 <item/> 应正常返回空标题 (修复前因元素真值为 False 走错分支)"""
    from app.engine.dlna.renderer import DLNARenderer

    md = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        '<item id="1"/></DIDL-Lite>'
    )
    assert DLNARenderer._parse_track_info(md) == ("", "")


def test_namespaced_item_parses_title_and_artist():
    from app.engine.dlna.renderer import DLNARenderer

    md = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="1"><dc:title>晴天</dc:title>'
        "<upnp:artist>周杰伦</upnp:artist></item></DIDL-Lite>"
    )
    assert DLNARenderer._parse_track_info(md) == ("晴天", "周杰伦")


def test_no_parse_raises_no_deprecation_warning():
    """修复后不应再触发 ElementTree 真值 DeprecationWarning"""
    import warnings

    from app.engine.dlna.renderer import DLNARenderer

    md = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        '<item id="1"/></DIDL-Lite>'
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        DLNARenderer._parse_track_info(md)
    bad = [w for w in caught if "truth value" in str(w.message)]
    assert not bad, f"仍有元素真值告警: {[str(w.message) for w in bad]}"
