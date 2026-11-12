"""目录游标翻页：首页固定目录，续页不受补采撤下影响。"""
from __future__ import annotations

import os

from app import catalog


def _harvest(api, payload):
    resp = api.post("/api/v1/harvest", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _get(api, **params):
    resp = api.get("/api/v1/catalog", params=params)
    return resp


def _ids(resp):
    assert resp.status_code == 200, resp.text
    return [record["identifier"] for record in resp.json()]


def test_cursor_pins_catalog_against_withdrawal_between_pages(client):
    api, _db, oai_mod = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-27"})

    assert len(_ids(_get(api, status="active"))) == 26

    # 取首页，固定本轮目录（26 条在架，limit=10 -> 3 页）。
    first = _get(api, status="active", limit=10, offset=0)
    cursor = first.headers["x-next-cursor"]
    assert len(_ids(first)) == 10
    assert cursor

    # 记录原始第二页内容，作为快照应返回的基准。
    second_before = _ids(_get(api, status="active", limit=10, offset=10))
    assert second_before[0] == "oai:sample.lib:rec-011"

    # 两页之间补采撤下首页中的 rec-005。主库在架数变为 25，原来的
    # offset=10 现在会从 rec-012 开始，从而跳过 rec-011。
    assert oai_mod.set_record_deleted("oai:sample.lib:rec-005", True)
    withdrawn = _harvest(api, {"from": "2026-10-05", "until": "2026-10-05"})
    assert withdrawn["withdrawn"] == 1

    live_page_two = _ids(_get(api, status="active", limit=10, offset=10))
    assert live_page_two[0] == "oai:sample.lib:rec-012"

    # 用首页返回的游标续页：仍是首页时刻的第二页，不跳过 rec-011。
    pinned = _get(api, cursor=cursor)
    assert _ids(pinned) == second_before

    # 同一游标重试仍返回同一页。
    assert _ids(_get(api, cursor=cursor)) == second_before

    # 新的首页反映补采结果：rec-005 已撤下，rec-011 补进首屏。
    new_first = _ids(_get(api, status="active", limit=10, offset=0))
    assert "oai:sample.lib:rec-005" not in new_first
    assert "oai:sample.lib:rec-011" in new_first


def test_last_page_has_no_next_cursor(client):
    api, _db, _oai = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-27"})

    resp = _get(api, status="all", limit=10, offset=0)
    cursor = resp.headers["x-next-cursor"]
    seen = _ids(resp)
    pages = 1
    while cursor:
        resp = _get(api, cursor=cursor)
        seen.extend(_ids(resp))
        pages += 1
        cursor = resp.headers.get("x-next-cursor")
        if pages == 3:
            assert cursor is None

    assert pages == 3
    assert len(seen) == 27


def test_cursor_keeps_first_page_status_and_limit(client):
    api, _db, _oai = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-27"})

    first = _get(api, status="all", limit=7, offset=0)
    assert len(_ids(first)) == 7
    cursor = first.headers["x-next-cursor"]

    # 续页请求里传入冲突的 status/limit 也必须沿用首页选择。
    resp = _get(api, cursor=cursor, status="active", limit=3, offset=99)
    assert len(_ids(resp)) == 7
    assert resp.json()[0]["status"] in {"active", "withdrawn"}
    assert _ids(resp) == [
        "oai:sample.lib:rec-008",
        "oai:sample.lib:rec-009",
        "oai:sample.lib:rec-010",
        "oai:sample.lib:rec-011",
        "oai:sample.lib:rec-012",
        "oai:sample.lib:rec-013",
        "oai:sample.lib:rec-014",
    ]


def test_original_offset_call_still_works(client):
    api, _db, _oai = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-27"})

    first = _get(api, status="all", limit=5, offset=20)
    ids = _ids(first)
    assert ids == [f"oai:sample.lib:rec-{i:03d}" for i in range(21, 26)]
    cursor = first.headers["x-next-cursor"]

    last = _get(api, cursor=cursor)
    assert _ids(last) == ["oai:sample.lib:rec-026", "oai:sample.lib:rec-027"]
    assert "x-next-cursor" not in last.headers


def test_expired_cursor_410_and_snapshot_reclaimed(client, monkeypatch, tmp_path):
    api, _db, _oai = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})

    # 让游标创建即过期；有效期始终自首页创建时起算。
    monkeypatch.setattr(catalog, "CURSOR_TTL_SECONDS", -1)
    first = _get(api, status="active", limit=10, offset=0)
    cursor = first.headers["x-next-cursor"]
    assert cursor

    snapshot_dir = catalog.snapshot_dir()
    files_before = [n for n in os.listdir(snapshot_dir) if n.endswith(".db")]
    assert len(files_before) == 1

    resp = _get(api, cursor=cursor)
    assert resp.status_code == 410
    detail = resp.json()["detail"]
    assert "过期" in detail and "首页" in detail

    files_after = [n for n in os.listdir(snapshot_dir) if n.endswith(".db")]
    assert files_after == []


def test_malformed_cursor_is_rejected_from_scratch(client):
    api, _db, _oai = client
    resp = _get(api, cursor="not-a-valid-cursor")
    assert resp.status_code == 400
    assert "首页" in resp.json()["detail"]
