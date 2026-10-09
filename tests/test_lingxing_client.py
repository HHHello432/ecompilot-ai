# -*- coding: utf-8 -*-
"""领星 ERP 客户端单元测试（全部 mock，不发起真实网络请求）。"""
from __future__ import annotations

import json
import types
import urllib.request

import pandas as pd
import pytest

import backend.platforms.lingxing as lx
from backend.platforms.lingxing import (
    LingxingClient,
    build_canonical_dataframe,
    build_lingxing_headers,
    fetch_paged,
    send_request,
)
from backend.services.data_cleaner import CANONICAL_COLUMNS


# ---------------------------------------------------------------------------
# Helpers：伪造 urllib.request.urlopen
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload).encode("utf-8")
        self.status = 200

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._payload


def make_urlopen(builder):
    """builder(offset) -> 响应 dict。依据请求体里的 offset 返回分页响应。"""
    def fake_urlopen(req, timeout=None):
        data = req.data
        body = json.loads(data.decode("utf-8")) if data else {}
        return FakeResponse(builder(body.get("offset", 0), body.get("length", lx.PAGE_SIZE)))
    return fake_urlopen


def page_in_data(offset: int, length: int) -> dict:
    total = 250
    if offset >= total:
        return {"code": 1, "data": {"list": [], "total": total}}
    n = min(length, total - offset)
    return {
        "code": 1,
        "data": {"list": [{"id": i} for i in range(offset, offset + n)], "total": total},
    }


def page_top_level(offset: int, length: int) -> dict:
    total = 250
    if offset >= total:
        return {"code": 1, "list": [], "total": total}
    n = min(length, total - offset)
    return {"code": 1, "list": [{"id": i} for i in range(offset, offset + n)], "total": total}


# ---------------------------------------------------------------------------
# 1) build_lingxing_headers：字符串输入 + auth-token 解码 + X-AK 字段
# ---------------------------------------------------------------------------
def test_build_headers_from_string():
    cookie = (
        "auth-token=abc%20def; company_id=12345; env_key=env1; "
        "uid=u1; zid=z1; other=keep"
    )
    headers = build_lingxing_headers(cookie)
    assert headers["auth-token"] == "abc def"          # URL 解码
    assert headers["X-AK-Company-Id"] == "12345"
    assert headers["X-AK-ENV-KEY"] == "env1"
    assert headers["X-AK-Uid"] == "u1"
    assert headers["X-AK-Zid"] == "z1"
    assert headers["X-AK-Version"] == lx.DEFAULT_VERSION
    assert "X-AK-Request-Id" in headers
    assert "auth-token=abc%20def" in headers["Cookie"]


def test_build_headers_from_list():
    cookie = [
        {"Name": "auth-token", "Value": "tok%2F1"},
        {"Name": "company_id", "Value": "99"},
        {"Name": "env_key", "Value": "e"},
        {"Name": "uid", "Value": "u"},
        {"Name": "zid", "Value": "z"},
    ]
    headers = build_lingxing_headers(cookie)
    assert headers["auth-token"] == "tok/1"
    assert headers["X-AK-Company-Id"] == "99"


def test_build_headers_missing_auth_token_raises():
    cookie = "company_id=1; env_key=e; uid=u; zid=z"
    with pytest.raises(ValueError):
        build_lingxing_headers(cookie)


def test_build_headers_version_override():
    cookie = (
        "auth-token=t; company_id=1; env_key=e; uid=u; zid=z"
    )
    headers = build_lingxing_headers(cookie, version="9.9.9")
    assert headers["X-AK-Version"] == "9.9.9"


# ---------------------------------------------------------------------------
# 2) fetch_paged：跨 2 页拼接（total 在 data 内 / 在顶层）
# ---------------------------------------------------------------------------
def test_fetch_paged_two_pages_in_data(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", make_urlopen(page_in_data))
    headers = build_lingxing_headers("auth-token=t; company_id=1; env_key=e; uid=u; zid=z")
    records, total = fetch_paged(
        "https://gw.lingxingerp.com/x",
        {"offset": 0, "length": lx.PAGE_SIZE},
        headers, ("data", "list"), ("data", "total"),
        desc="test", page_size=lx.PAGE_SIZE,
    )
    assert total == 250
    assert len(records) == 250
    # 第 1 页 200 条 + 第 2 页 50 条，不应有重复/缺失
    assert {r["id"] for r in records} == set(range(250))


def test_fetch_paged_two_pages_top_level(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", make_urlopen(page_top_level))
    headers = build_lingxing_headers("auth-token=t; company_id=1; env_key=e; uid=u; zid=z")
    records, total = fetch_paged(
        "https://maique.lingxing.com/x",
        {"offset": 0, "length": lx.PAGE_SIZE},
        headers, ("list",), ("total",),
        desc="test", page_size=lx.PAGE_SIZE,
    )
    assert total == 250
    assert len(records) == 250
    assert {r["id"] for r in records} == set(range(250))


def test_fetch_paged_code_not_1_stops(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", make_urlopen(lambda o, l: {"code": 0, "msg": "err"}))
    headers = build_lingxing_headers("auth-token=t; company_id=1; env_key=e; uid=u; zid=z")
    records, total = fetch_paged(
        "u", {"offset": 0, "length": 10}, headers, ("data", "list"), ("data", "total"),
        desc="test", page_size=10,
    )
    assert records == []
    assert total == 0


# ---------------------------------------------------------------------------
# 3) send_request：成功返回 dict；重试后仍失败返回 None
# ---------------------------------------------------------------------------
def test_send_request_success(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", make_urlopen(lambda o, l: {"code": 1, "ok": True}))
    headers = build_lingxing_headers("auth-token=t; company_id=1; env_key=e; uid=u; zid=z")
    result = send_request("https://x/y", {"a": 1}, headers, "ok")
    assert result == {"code": 1, "ok": True}


def test_send_request_retry_then_none(monkeypatch):
    calls = {"n": 0}

    def boom(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    headers = build_lingxing_headers("auth-token=t; company_id=1; env_key=e; uid=u; zid=z")
    result = send_request("https://x/y", {"a": 1}, headers, "fail", retries=3)
    assert result is None
    assert calls["n"] == 3  # 重试 3 次


# ---------------------------------------------------------------------------
# 4) fetch_* 方法使用正确端点（mock urlopen，只核对 list 结构可取）
# ---------------------------------------------------------------------------
def test_client_fetch_product_performance(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", make_urlopen(page_in_data))
    client = LingxingClient("auth-token=t; company_id=1; env_key=e; uid=u; zid=z")
    items = client.fetch_product_performance("2026-01-01", "2026-01-07")
    assert len(items) == 250


def test_client_fetch_fba_inventory_top_level(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", make_urlopen(page_top_level))
    client = LingxingClient("auth-token=t; company_id=1; env_key=e; uid=u; zid=z")
    records = client.fetch_fba_inventory()
    assert len(records) == 250


def test_client_fetch_ads_report_returns_none():
    client = LingxingClient("auth-token=t; company_id=1; env_key=e; uid=u; zid=z")
    # 无 profile_id -> 最小可用实现返回 None，不抛异常
    assert client.fetch_ads_report() is None
    assert client.fetch_ads_report("some_profile") is None


# ---------------------------------------------------------------------------
# 5) build_canonical_dataframe：字段映射 + 列顺序 == CANONICAL_COLUMNS
# ---------------------------------------------------------------------------
class FakeClient:
    def fetch_product_performance(self, start_date, end_date):
        return [
            {
                "item_name": "SKU-A", "asin": "B0AAAA", "sku": "SKU-A",
                "volume": 10, "amount": 200.5, "spend": 15.0, "clicks": 100,
                "afn_fulfillable_quantity": 5,
            },
            {
                "item_name": "SKU-B", "asin": "B0BBBB", "sku": "SKU-B",
                "volume": 3, "amount": 50.0, "spend": 2.0, "clicks": 20,
            },
        ]

    def fetch_fba_inventory(self):
        return [
            {"sku": "SKU-A", "afn_fulfillable_quantity": 5},
            {"sku": "SKU-B", "afn_fulfillable_quantity": 8},
        ]

    def fetch_stock(self):
        return []

    def fetch_ads_report(self, profile_id=None):
        return None


def test_build_canonical_dataframe_columns_and_values():
    df = build_canonical_dataframe(FakeClient(), "2026-01-01", "2026-01-07")
    assert list(df.columns) == list(CANONICAL_COLUMNS.keys())
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 2

    row_a = df[df["product_name"] == "SKU-A"].iloc[0]
    assert row_a["orders"] == 10
    assert row_a["payment_amount"] == 200.5
    assert row_a["ad_cost"] == 15.0
    assert row_a["clicks"] == 100
    assert row_a["stock"] == 5
    # 无来源的列置 0
    assert row_a["impressions"] == 0
    assert row_a["visitors"] == 0
    assert row_a["cost"] == 0
    assert row_a["refund_amount"] == 0

    row_b = df[df["product_name"] == "SKU-B"].iloc[0]
    assert row_b["stock"] == 8


def test_build_canonical_dataframe_skips_missing_name():
    class NoName(FakeClient):
        def fetch_product_performance(self, start_date, end_date):
            return [{"volume": 1, "amount": 1.0, "spend": 0, "clicks": 0}]

    df = build_canonical_dataframe(NoName(), "2026-01-01", "2026-01-07")
    # 无商品名的行被跳过 -> 空表
    assert len(df) == 0


def test_build_canonical_dataframe_stock_from_guangzhou():
    class GzOnly:
        def fetch_product_performance(self, sd, ed):
            return [{"item_name": "G1", "sku": "G1", "volume": 1, "amount": 1.0,
                     "spend": 0, "clicks": 1}]

        def fetch_fba_inventory(self):
            return []

        def fetch_stock(self):
            return [{"sku": "G1", "good_num": 42, "total": 99}]

        def fetch_ads_report(self, profile_id=None):
            return None

    df = build_canonical_dataframe(GzOnly(), "2026-01-01", "2026-01-07")
    assert df.iloc[0]["stock"] == 42  # 优先 good_num
