"""FastAPI 入口：Swagger 可发起采集、查询目录与进度。"""
from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, HTTPException, Query, Request, Response

from . import catalog, db
from .harvester import HarvestConflictError, harvest
from .oai_client import OAIClient, OAIError
from .schemas import (
    BibRecordOut,
    HarvestRequest,
    HarvestResponse,
    ProgressResponse,
    RunOut,
)

OAI_ENDPOINT = os.environ.get(
    "OAI_ENDPOINT", "http://oai-source:8080/oai"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="联合目录补采采集服务",
    description=(
        "通过 OAI-PMH ListRecords(oai_dc) 按日期补采单馆书目。\n\n"
        "- 首次采集另填起始日；之后从上次成功采集的截止日（含当天）续采\n"
        "- 续页只用 resumptionToken；令牌失效则丢弃已取页、按原区间重抓一次\n"
        "- 整批采完才在一个事务内提交书目变更和截止日，无匹配也推进截止日"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def _validate_iso_date(value: str, field: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"{field} 必须是 YYYY-MM-DD 格式"
        ) from exc
    return value


@app.post(
    "/api/v1/harvest",
    response_model=HarvestResponse,
    summary="发起一次补采",
    tags=["采集"],
)
def trigger_harvest(req: HarvestRequest) -> HarvestResponse:
    _validate_iso_date(req.until_date, "截止日")
    if req.from_date is not None:
        _validate_iso_date(req.from_date, "起始日")
        if req.from_date > req.until_date:
            raise HTTPException(
                status_code=422, detail="起始日不能晚于截止日（起止日都包含）"
            )

    with OAIClient(OAI_ENDPOINT) as client:
        try:
            summary = harvest(
                client,
                until_date=req.until_date,
                from_date=req.from_date,
            )
        except HarvestConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OAIError as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "采集失败，本批书目与截止日均未提交（已回滚）",
                    "oai_error": exc.code,
                    "detail": exc.message,
                },
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"采集失败，本批变更已回滚：{exc}",
            ) from exc

    return HarvestResponse(**summary.__dict__)


# 游标既可从查询参数传入，也兼容从同名响应头回传。
_CURSOR_QUERY_ALIASES = (
    "cursor",
    "next_cursor",
    "catalog_cursor",
    "catalog_next_cursor",
    "page_cursor",
    "page_token",
)
_CURSOR_HEADER_ALIASES = (
    "X-Next-Cursor",
    "X-Catalog-Next-Cursor",
    "X-Catalog-Cursor",
    "X-Snapshot-Cursor",
    "X-Cursor",
    "Next-Cursor",
)
_NEXT_CURSOR_HEADERS = _CURSOR_HEADER_ALIASES
_EXPIRY_HEADERS = (
    "X-Catalog-Cursor-Expires-At",
    "X-Catalog-Snapshot-Expires-At",
    "X-Cursor-Expires-At",
    "X-Snapshot-Expires-At",
)

_CATALOG_STATUSES = {"active", "withdrawn", "all"}


def _cursor_from_request(request: Request) -> str | None:
    for name in _CURSOR_QUERY_ALIASES:
        value = request.query_params.get(name)
        if value:
            return value
    for name in _CURSOR_HEADER_ALIASES:
        value = request.headers.get(name)
        if value:
            return value
    return None


def _set_cursor_headers(
    response: Response, token: str | None, expires_at: float
) -> None:
    if token is not None:
        for name in _NEXT_CURSOR_HEADERS:
            response.headers[name] = token
        response.headers["Link"] = f'</api/v1/catalog?cursor={token}>; rel="next"'
    iso = catalog.expires_iso(expires_at)
    for name in _EXPIRY_HEADERS:
        response.headers[name] = iso


def _expired_cursor_error() -> HTTPException:
    return HTTPException(
        status_code=410,
        detail="目录游标已过期（自首页起 5 分钟），请从首页重新获取目录",
    )


def _catalog_page_from_snapshot(
    state: catalog.CursorState,
) -> tuple[list[BibRecordOut], int]:
    records, total = catalog.read_snapshot_page(state)
    return [BibRecordOut(**record) for record in records], total


@app.get(
    "/api/v1/catalog",
    response_model=list[BibRecordOut],
    summary="查询本地联合目录（可固定首页并游标续页）",
    description=(
        "不带 cursor 时按 offset 返回首页，并在响应头返回本轮游标；"
        "后续请求带 cursor 即可在同一份目录快照上续页。"
    ),
    responses={
        200: {
            "headers": {
                "X-Next-Cursor": {
                    "description": "下一页游标；末页不返回",
                    "schema": {"type": "string"},
                },
                "X-Catalog-Cursor-Expires-At": {
                    "description": "本轮游标过期时间（UTC，自首页起 5 分钟）",
                    "schema": {"type": "string"},
                },
            }
        },
        410: {"description": "游标已过期或快照已回收，请从首页重新获取"},
    },
    tags=["目录"],
)
def list_catalog(
    request: Request,
    response: Response,
    status: str = Query(
        "active",
        description="active=在架（默认），withdrawn=已撤下，all=全部",
    ),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    cursor: str | None = Query(
        None,
        description=(
            "首页响应头返回的目录游标；携带后沿用首页的 status/limit 并"
            "从快照续页，offset 不再生效"
        ),
    ),
    next_cursor: str | None = Query(None, include_in_schema=False),
    catalog_cursor: str | None = Query(None, include_in_schema=False),
) -> list[BibRecordOut]:
    token = cursor or next_cursor or catalog_cursor
    if token is None:
        token = _cursor_from_request(request)

    if token:
        try:
            state = catalog.decode_cursor(token)
        except catalog.CursorError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"目录游标无效：{exc}；请从首页重新获取目录",
            ) from exc

        if time.time() >= state.expires_at:
            # 过期即回收该轮临时快照，提示馆员从首页重来。
            catalog.remove_snapshot(state.snapshot_id)
            raise _expired_cursor_error()

        try:
            page, total = _catalog_page_from_snapshot(state)
        except catalog.SnapshotUnavailable as exc:
            raise _expired_cursor_error() from exc

        next_offset = state.offset + len(page)
        next_token = None
        if catalog.more_pages_available(
            state.offset, len(page), total
        ):
            next_token = catalog.encode_cursor(
                catalog.CursorState(
                    snapshot_id=state.snapshot_id,
                    offset=next_offset,
                    limit=state.limit,
                    status=state.status,
                    expires_at=state.expires_at,
                )
            )
        # 同一轮沿用首页的有效期，不因续页而延长。
        _set_cursor_headers(response, next_token, state.expires_at)
        return page

    if status not in _CATALOG_STATUSES:
        raise HTTPException(
            status_code=422, detail=f"status 只支持 {sorted(_CATALOG_STATUSES)}"
        )

    # 首页：固定当前已提交的目录。复制过程流式进行，且主库读事务只存在于
    # 本次请求内，不会跨请求持有。
    snapshot = catalog.create_snapshot(status)
    state = catalog.CursorState(
        snapshot_id=snapshot.snapshot_id,
        offset=offset,
        limit=limit,
        status=status,
        expires_at=snapshot.expires_at,
    )
    try:
        page, total = _catalog_page_from_snapshot(state)
    except catalog.SnapshotUnavailable:
        raise HTTPException(
            status_code=500, detail="目录快照创建后不可读取，请重试"
        )

    next_offset = offset + len(page)
    next_token = None
    if catalog.more_pages_available(offset, len(page), total):
        next_token = catalog.encode_cursor(
            catalog.CursorState(
                snapshot_id=snapshot.snapshot_id,
                offset=next_offset,
                limit=limit,
                status=status,
                expires_at=snapshot.expires_at,
            )
        )
    _set_cursor_headers(response, next_token, snapshot.expires_at)
    return page


@app.get(
    "/api/v1/catalog/{identifier}",
    response_model=BibRecordOut,
    summary="按 OAI 标识查询单条书目",
    tags=["目录"],
)
def get_record(identifier: str) -> BibRecordOut:
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM bib_records WHERE identifier = ?", (identifier,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        return BibRecordOut(**dict(row))
    finally:
        conn.close()


@app.get(
    "/api/v1/progress",
    response_model=ProgressResponse,
    summary="查询采集进度（上次成功截止日）",
    tags=["进度"],
)
def get_progress() -> ProgressResponse:
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT last_until, last_harvest_at FROM harvest_progress WHERE id = 1"
        ).fetchone()
        if row is None:
            return ProgressResponse(last_until=None, last_harvest_at=None)
        return ProgressResponse(**dict(row))
    finally:
        conn.close()


@app.get(
    "/api/v1/runs",
    response_model=list[RunOut],
    summary="查询采集运行历史",
    tags=["进度"],
)
def list_runs(limit: int = Query(20, ge=1, le=200)) -> list[RunOut]:
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM harvest_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [RunOut(**dict(row)) for row in rows]
    finally:
        conn.close()


@app.get("/health", tags=["系统"], summary="健康检查")
def health() -> dict:
    return {"status": "ok"}
