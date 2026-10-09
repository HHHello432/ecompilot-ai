from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from backend.services.ai_diagnosis import generate_diagnosis
from backend.services.data_cleaner import CleanResult, clean_dataframe, load_table
from backend.services.metrics import compute_metrics, summarize
from backend.services.product_classifier import classify_products, segment_counts


@dataclass
class AnalysisBundle:
    file_id: str
    products: pd.DataFrame
    summary: dict[str, Any]
    diagnosis: dict[str, Any]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "summary": self.summary,
            "diagnosis": self.diagnosis,
            "warnings": self.warnings,
            "products": self.products.to_dict(orient="records"),
        }


def analyze_upload(file_id: str, file_bytes: bytes, filename: str) -> AnalysisBundle:
    raw = load_table(file_bytes, filename)
    return analyze_dataframe(file_id, raw)


def analyze_dataframe(file_id: str, dataframe: pd.DataFrame) -> AnalysisBundle:
    clean: CleanResult = clean_dataframe(dataframe)
    metrics = compute_metrics(clean.dataframe)
    classified = classify_products(metrics)
    summary = summarize(classified)
    summary["segment_counts"] = segment_counts(classified)
    diagnosis = generate_diagnosis(summary, classified)
    return AnalysisBundle(
        file_id=file_id,
        products=classified,
        summary=summary,
        diagnosis=diagnosis,
        warnings=clean.warnings,
    )


def bundle_from_dict(payload: dict[str, Any]) -> AnalysisBundle:
    products = pd.DataFrame(payload["products"])
    return AnalysisBundle(
        file_id=payload["file_id"],
        products=products,
        summary=payload["summary"],
        diagnosis=payload["diagnosis"],
        warnings=payload.get("warnings", []),
    )
