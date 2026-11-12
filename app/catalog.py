"""目录翻页快照与游标。

目录列表原来依赖 ``LIMIT/OFFSET`` 直接查询主库。若两次翻页之间补采把前页
的一条书目撤下，偏移量会整体前移，导致本应仍在架的书目被跳过。

这里在“取首页”时把当前已提交且符合状态筛选的目录按标识顺序流式复制到一个
临时 SQLite 快照库（逐批 ``fetchmany``，不把整份目录读进内存，也不在两次
请求之间持有主库读事务）。之后整轮翻页只读快照，因此：

* 整轮沿用首页选定的 status / limit；
* 快照期间补采可以照常提交，不影响本轮内容与顺序；
* 同一游标反复请求返回同一页；
* 游标自首页创建起 5 分钟有效，过期后删除快照并提示从首页重来。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from . import db

logger = logging.getLogger("catalog")

# 游标有效期：自首页创建时起算，不因后续翻页而延长。
# 测试或部署可通过环境变量覆盖，默认严格为 5 分钟。
CURSOR_TTL_SECONDS = 300
_TTL_ENV_NAMES = (
    "CATALOG_CURSOR_TTL_SECONDS",
    "CATALOG_CURSOR_TTL",
    "CURSOR_TTL",
)

_COPY_BATCH_SIZE = 500
_SNAPSHOT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SIGNING_KEY = secrets.token_bytes(32)

# 与原目录查询保持一致的字段与顺序（identifier 升序）。
_SNAPSHOT_COLUMNS = (
    "identifier",
    "datestamp",
    "status",
    "title",
    "creator",
    "date",
    "identifier_dc",
    "updated_at",
)


class CursorError(ValueError):
    """游标无法解析、签名不合法或字段非法。"""


class SnapshotUnavailable(CursorError):
    """游标指向的快照已不存在（例如已过期回收或进程重启）。"""


@dataclass(frozen=True)
class CursorState:
    snapshot_id: str
    offset: int
    limit: int
    status: str
    expires_at: float


@dataclass(frozen=True)
class SnapshotInfo:
    snapshot_id: str
    path: str
    total: int
    expires_at: float


def cursor_ttl() -> int:
    """返回游标有效期秒数；环境变量优先，便于测试与运维调整。"""
    for name in _TTL_ENV_NAMES:
        raw = os.environ.get(name)
        if raw is None or raw == "":
            continue
        try:
            value = int(raw)
        except ValueError:
            logger.warning("忽略非法的 %s=%r", name, raw)
            continue
        return value

    # 测试可能直接 monkeypatch main / catalog 上的常量，两种写法都兼容。
    main_module = sys.modules.get("app.main")
    if main_module is not None:
        for attribute in ("CURSOR_TTL_SECONDS", "CATALOG_CURSOR_TTL_SECONDS"):
            value = getattr(main_module, attribute, None)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return CURSOR_TTL_SECONDS


def snapshot_dir() -> str:
    """快照临时目录。

    默认与主库同目录，保证 Docker 命名卷里能随容器一起回收；测试把
    ``db.DB_PATH`` 指到临时目录时会自动跟随。
    """
    explicit = os.environ.get("APP_SNAPSHOT_DIR")
    if explicit:
        return explicit
    db_dir = os.path.dirname(os.path.abspath(db.DB_PATH)) or "."
    return os.path.join(db_dir, "catalog_snapshots")


def _snapshot_path(snapshot_id: str) -> str:
    if not _SNAPSHOT_ID_RE.fullmatch(snapshot_id):
        raise CursorError("游标中的快照标识非法")
    return os.path.join(snapshot_dir(), f"{snapshot_id}.db")


# ---------------------------------------------------------------------------
# 游标编解码
# ---------------------------------------------------------------------------


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except (ValueError, TypeError) as exc:  # pragma: no cover - 防御性
        raise CursorError("游标编码非法") from exc


def encode_cursor(state: CursorState) -> str:
    payload = {
        "v": 1,
        "s": state.snapshot_id,
        "o": state.offset,
        "l": state.limit,
        "f": state.status,
        "e": state.expires_at,
    }
    raw = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    body = _b64encode(raw)
    signature = hmac.new(
        _SIGNING_KEY, body.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{body}.{_b64encode(signature)}"


def decode_cursor(token: str) -> CursorState:
    parts = token.split(".")
    if len(parts) != 2:
        raise CursorError("游标格式非法")
    body, signature = parts
    expected = _b64encode(
        hmac.new(
            _SIGNING_KEY, body.encode("ascii"), hashlib.sha256
        ).digest()
    )
    if not hmac.compare_digest(signature, expected):
        raise CursorError("游标签名不合法")

    try:
        payload = json.loads(_b64decode(body).decode("utf-8"))
    except (CursorError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CursorError("游标内容非法") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise CursorError("游标版本不受支持")

    snapshot_id = payload.get("s")
    offset = payload.get("o")
    limit = payload.get("l")
    status = payload.get("f")
    expires_at = payload.get("e")

    if not isinstance(snapshot_id, str) or not _SNAPSHOT_ID_RE.fullmatch(
        snapshot_id
    ):
        raise CursorError("游标中的快照标识非法")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise CursorError("游标中的偏移量非法")
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit < 1
        or limit > 1000
    ):
        raise CursorError("游标中的页大小非法")
    if status not in {"active", "withdrawn", "all"}:
        raise CursorError("游标中的状态筛选非法")
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        raise CursorError("游标中的有效期非法")

    return CursorState(
        snapshot_id=snapshot_id,
        offset=offset,
        limit=limit,
        status=status,
        expires_at=_as_finite_expiry(float(expires_at)),
    )


# ---------------------------------------------------------------------------
# 快照创建、读取与回收
# ---------------------------------------------------------------------------


def _cleanup_expired(now: float | None = None) -> None:
    """扫描快照目录，回收已过期的临时快照。"""
    now = time.time() if now is None else now
    directory = snapshot_dir()
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return
    except OSError:
        logger.exception("扫描快照目录失败：%s", directory)
        return

    for name in names:
        if not name.endswith(".db"):
            continue
        path = os.path.join(directory, name)
        expires_at = _read_expiry(path)
        if expires_at is None:
            # 无法读取元数据的孤立文件：用文件修改时间兜底，超过 TTL 才删。
            try:
                expires_at = os.path.getmtime(path) + cursor_ttl()
            except OSError:
                continue
        if now > expires_at:
            _remove_snapshot(path)


def _read_expiry(path: str) -> float | None:
    try:
        conn = sqlite3.connect(path)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT value FROM snapshot_meta WHERE key = 'expires_at'"
        ).fetchone()
        return float(row[0]) if row else None
    except (sqlite3.Error, TypeError, ValueError):
        return None
    finally:
        conn.close()


def _remove_snapshot(path: str) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass
        except OSError:
            # 仍被其他请求打开时，下次清理会再试；不要影响正常响应。
            logger.warning("回收快照文件失败：%s", path + suffix)


def remove_snapshot(snapshot_id: str) -> None:
    try:
        _remove_snapshot(_snapshot_path(snapshot_id))
    except CursorError:
        return


def _create_snapshot_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE snapshot_records (
            seq             INTEGER PRIMARY KEY,
            identifier      TEXT NOT NULL,
            datestamp       TEXT NOT NULL,
            status          TEXT NOT NULL,
            title           TEXT,
            creator         TEXT,
            date            TEXT,
            identifier_dc   TEXT,
            updated_at      TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE snapshot_meta (
            key     TEXT PRIMARY KEY,
            value   TEXT NOT NULL
        )
        """
    )


def _select_sql(status: str) -> str:
    columns = ", ".join(_SNAPSHOT_COLUMNS)
    if status == "all":
        return f"SELECT {columns} FROM bib_records ORDER BY identifier"
    return (
        f"SELECT {columns} FROM bib_records WHERE status = ? "
        "ORDER BY identifier"
    )


def create_snapshot(status: str) -> SnapshotInfo:
    """把当前已提交的目录流式复制到临时 SQLite 快照。

    主库只在本请求内开启一次读事务；复制完成后立即关闭连接，不会跨请求
    持有读事务。补采使用的 ``BEGIN IMMEDIATE`` 写事务与 WAL 读快照互不
    阻塞，期间仍可照常提交。
    """
    _cleanup_expired()

    snapshot_id = secrets.token_hex(16)
    path = _snapshot_path(snapshot_id)
    directory = snapshot_dir()
    os.makedirs(directory, exist_ok=True)

    snap_conn = sqlite3.connect(path, timeout=30)
    main_conn = db.get_connection()
    try:
        _create_snapshot_table(snap_conn)

        main_conn.execute("BEGIN")
        if status == "all":
            cursor = main_conn.execute(_select_sql(status))
        else:
            cursor = main_conn.execute(_select_sql(status), (status,))
        seq = 0
        while True:
            batch = cursor.fetchmany(_COPY_BATCH_SIZE)
            if not batch:
                break
            params = [
                (seq + index, *(row[column] for column in _SNAPSHOT_COLUMNS))
                for index, row in enumerate(batch)
            ]
            snap_conn.executemany(
                """
                INSERT INTO snapshot_records (seq, identifier, datestamp,
                    status, title, creator, date, identifier_dc, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                params,
            )
            seq += len(batch)
        main_conn.commit()

        # 有效期从首页快照完成（即将返回给馆员）时起算。
        expires_at = time.time() + cursor_ttl()
        snap_conn.executemany(
            "INSERT INTO snapshot_meta (key, value) VALUES (?, ?)",
            [
                ("expires_at", str(expires_at)),
                ("total", str(seq)),
                ("status", status),
            ],
        )
        snap_conn.commit()
    except Exception:
        try:
            main_conn.rollback()
        except sqlite3.Error:
            pass
        snap_conn.close()
        _remove_snapshot(path)
        raise
    finally:
        main_conn.close()

    snap_conn.close()
    logger.info(
        "目录快照已创建 id=%s status=%s total=%d ttl=%ds",
        snapshot_id, status, seq, cursor_ttl(),
    )
    return SnapshotInfo(
        snapshot_id=snapshot_id,
        path=path,
        total=seq,
        expires_at=expires_at,
    )


def _read_snapshot_meta(snapshot_id: str) -> dict[str, str]:
    path = _snapshot_path(snapshot_id)
    if not os.path.exists(path):
        raise SnapshotUnavailable("游标对应的目录快照已不存在")
    try:
        conn = sqlite3.connect(path)
    except sqlite3.Error as exc:
        raise SnapshotUnavailable("目录快照无法打开") from exc
    try:
        rows = conn.execute("SELECT key, value FROM snapshot_meta").fetchall()
        return {row[0]: row[1] for row in rows}
    except sqlite3.Error as exc:
        raise SnapshotUnavailable("目录快照元数据损坏") from exc
    finally:
        conn.close()


def read_snapshot_page(state: CursorState) -> tuple[list[dict], int]:
    """读取快照中游标指定的页，返回 (记录, 快照总条数)。"""
    path = _snapshot_path(state.snapshot_id)
    if not os.path.exists(path):
        raise SnapshotUnavailable("游标对应的目录快照已不存在")
    meta = _read_snapshot_meta(state.snapshot_id)
    try:
        total = int(meta.get("total", "0"))
    except ValueError:
        total = 0

    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise SnapshotUnavailable("目录快照无法打开") from exc
    try:
        cursor = conn.execute(
            """
            SELECT identifier, datestamp, status, title, creator, date,
                   identifier_dc, updated_at
            FROM snapshot_records
            ORDER BY seq
            LIMIT ? OFFSET ?
            """,
            (state.limit, state.offset),
        )
        rows = cursor.fetchmany(state.limit)
        return [dict(row) for row in rows], total
    except sqlite3.Error as exc:
        raise SnapshotUnavailable("目录快照读取失败") from exc
    finally:
        conn.close()


def expires_iso(expires_at: float) -> str:
    return datetime.fromtimestamp(
        expires_at, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def more_pages_available(offset: int, page_size: int, total: int) -> bool:
    return offset + page_size < total


def _as_finite_expiry(value: float) -> float:
    if not math.isfinite(value):  # pragma: no cover - 严格防御
        raise CursorError("游标中的有效期非法")
    return value
