"""SQLite 存储: 用户 (单管理员场景, 预留多用户扩展) + 面板登录日志"""

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta

from app.core.config import get_settings

_lock = threading.Lock()

# 登录日志保留条数上限 (超出后淘汰最旧记录)
LOGIN_LOG_MAX_ROWS = 100


@contextmanager
def _connect():
    """打开连接并在退出时显式关闭

    注意 sqlite3.Connection 的 `with` 只提交/回滚事务, **不会关闭连接**。
    原实现 `with _lock, _connect() as conn:` 依赖 CPython 引用计数即时回收
    fd, 一旦 GC 延迟就会线性泄漏 (实测关闭 GC 后 500 次查询泄漏 500 个 fd)。
    故此处包一层: 内层 `with conn` 保留事务语义, 外层 finally 保证 close。
    """
    conn = sqlite3.connect(get_settings().db_path)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db():
    with _lock, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS login_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                ip TEXT NOT NULL,
                success INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )


def is_initialized() -> bool:
    """是否已创建管理员账号"""
    with _lock, _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
        return row["c"] > 0


def create_user(username: str, password_hash: str) -> bool:
    with _lock, _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                (username, password_hash),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def create_initial_user(username: str, password_hash: str) -> bool:
    """原子创建首个管理员，确保并发首次设置只能成功一次。

    ``is_initialized`` 与普通 INSERT 分开执行会留出竞态：两个首次设置请求
    可以同时通过检查并使用不同用户名创建两个账号。BEGIN IMMEDIATE 先取得
    SQLite 写锁，再在同一事务内检查和插入；多进程部署下也由 SQLite 保证串行。
    """
    with _lock, _connect() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
            if row is not None:
                return False
            conn.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                (username, password_hash),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def get_password_hash(username: str) -> str | None:
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE username = ?", (username,)
        ).fetchone()
        return row["password_hash"] if row else None


def update_password(username: str, password_hash: str) -> bool:
    with _lock, _connect() as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (password_hash, username),
        )
        return cur.rowcount > 0


def add_login_log(username: str, ip: str, success: bool):
    """记录一次面板登录尝试, 并淘汰超出上限的最旧记录"""
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO login_logs (username, ip, success, created_at) VALUES (?, ?, ?, ?)",
            (username, ip, int(success), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.execute(
            "DELETE FROM login_logs WHERE id NOT IN "
            "(SELECT id FROM login_logs ORDER BY id DESC LIMIT ?)",
            (LOGIN_LOG_MAX_ROWS,),
        )


def get_login_logs(limit: int = 50) -> list[dict]:
    """最近的登录日志 (新的在前)"""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT username, ip, success, created_at FROM login_logs "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "username": r["username"],
                "ip": r["ip"],
                "success": bool(r["success"]),
                "time": r["created_at"],
            }
            for r in rows
        ]


def get_recent_failures(window_seconds: int) -> dict[str, list[float]]:
    """窗口期内登录失败的 IP -> 失败时间戳列表 (epoch 秒)

    供进程重启后恢复限速锁定状态: 记录在 login_logs 表中天然持久,
    恢复真实时间戳可保证锁定剩余时间准确 (而非重新计时)。
    """
    cutoff = (
        datetime.now() - timedelta(seconds=window_seconds)
    ).strftime("%Y-%m-%d %H:%M:%S")
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT ip, created_at FROM login_logs "
            "WHERE success = 0 AND created_at > ? ORDER BY id",
            (cutoff,),
        ).fetchall()
    failures: dict[str, list[float]] = {}
    for r in rows:
        try:
            ts = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            continue
        failures.setdefault(r["ip"], []).append(ts)
    return failures
