from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import pandas as pd


class DataValidationError(ValueError):
    """Raised when uploaded commerce data cannot be normalized."""


CANONICAL_COLUMNS = {
    "product_name": "商品名",
    "impressions": "曝光量",
    "clicks": "点击量",
    "visitors": "访客数",
    "orders": "下单数",
    "payment_amount": "支付金额",
    "ad_cost": "广告花费",
    "cost": "成本",
    "refund_amount": "退货金额",
    "stock": "库存",
}

ALIASES: dict[str, set[str]] = {
    "product_name": {"商品名", "商品名称", "产品名", "产品名称", "sku", "SKU", "name", "product", "product_name"},
    "impressions": {"曝光量", "曝光", "展现量", "impressions", "views", "show"},
    "clicks": {"点击量", "点击", "clicks", "click"},
    "visitors": {"访客数", "访客", "UV", "uv", "visitors", "visitor"},
    "orders": {"下单数", "订单数", "成交单数", "orders", "order_count"},
    "payment_amount": {"支付金额", "成交金额", "销售额", "GMV", "gmv", "payment_amount", "revenue"},
    "ad_cost": {"广告花费", "广告消耗", "投放费用", "ad_cost", "ad_spend", "spend"},
    "cost": {"成本", "商品成本", "采购成本", "cost", "cogs"},
    "refund_amount": {"退货金额", "退款金额", "售后金额", "refund_amount", "refund"},
    "stock": {"库存", "库存数", "剩余库存", "stock", "inventory"},
}

NUMERIC_COLUMNS = [
    "impressions",
    "clicks",
    "visitors",
    "orders",
    "payment_amount",
    "ad_cost",
    "cost",
    "refund_amount",
    "stock",
]


@dataclass(frozen=True)
class CleanResult:
    dataframe: pd.DataFrame
    warnings: list[str]


def load_table(file_bytes: bytes, filename: str) -> pd.DataFrame:
    suffix = Path(filename).suffix.lower()
    buffer = io.BytesIO(file_bytes)

    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(buffer)
    if suffix == ".csv":
        return _read_csv_with_fallback(buffer)

    raise DataValidationError("仅支持 CSV、XLS、XLSX 文件。")


def clean_dataframe(raw: pd.DataFrame) -> CleanResult:
    if raw.empty:
        raise DataValidationError("上传文件没有可分析的数据。")

    df = raw.copy()
    df.columns = [str(column).strip() for column in df.columns]
    rename_map = _build_rename_map(df.columns)
    df = df.rename(columns=rename_map)

    missing = [CANONICAL_COLUMNS[col] for col in CANONICAL_COLUMNS if col not in df.columns]
    if missing:
        raise DataValidationError("缺少必要字段：" + "、".join(missing))

    df = df[list(CANONICAL_COLUMNS.keys())].copy()
    df["product_name"] = df["product_name"].astype(str).str.strip()
    df = df[df["product_name"].ne("") & df["product_name"].str.lower().ne("nan")]

    if df.empty:
        raise DataValidationError("商品名为空，无法生成分析。")

    warnings: list[str] = []
    for column in NUMERIC_COLUMNS:
        before_missing = df[column].isna().sum()
        df[column] = (
            df[column]
            .astype(str)
            .str.replace(",", "", regex=False)
            .str.replace("¥", "", regex=False)
            .str.replace("￥", "", regex=False)
            .str.strip()
        )
        df[column] = pd.to_numeric(df[column], errors="coerce").fillna(0)
        df.loc[df[column] < 0, column] = 0
        after_missing = df[column].isna().sum()
        if before_missing or after_missing:
            warnings.append(f"{CANONICAL_COLUMNS[column]}存在空值，已按 0 处理。")

    duplicated = df["product_name"].duplicated().sum()
    if duplicated:
        df = (
            df.groupby("product_name", as_index=False)
            .agg(
                {
                    "impressions": "sum",
                    "clicks": "sum",
                    "visitors": "sum",
                    "orders": "sum",
                    "payment_amount": "sum",
                    "ad_cost": "sum",
                    "cost": "sum",
                    "refund_amount": "sum",
                    "stock": "sum",
                }
            )
        )
        warnings.append(f"发现 {duplicated} 行重复商品，已按商品名汇总。")

    return CleanResult(dataframe=df.reset_index(drop=True), warnings=warnings)


def _build_rename_map(columns: pd.Index) -> dict[str, str]:
    rename_map: dict[str, str] = {}
    lowered = {str(column).strip().lower(): str(column).strip() for column in columns}
    for canonical, names in ALIASES.items():
        for name in names:
            matched = lowered.get(name.lower())
            if matched:
                rename_map[matched] = canonical
                break
    return rename_map


def _read_csv_with_fallback(buffer: io.BytesIO) -> pd.DataFrame:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        buffer.seek(0)
        try:
            return pd.read_csv(buffer, encoding=encoding)
        except UnicodeDecodeError:
            continue
    buffer.seek(0)
    return pd.read_csv(buffer)
