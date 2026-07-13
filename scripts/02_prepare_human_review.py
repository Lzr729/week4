#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""根据自动章节候选与大模型建议，生成可人工填写的章节复核表。

本脚本不替代人工判断。它只把三类信息排到同一张表中：
1. 规则程序生成的章节候选；
2. 大模型给出的审阅建议；
3. 需要由学生逐页核对后填写的人工结论。
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def read_jsonl(path: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return result
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"JSONL 第 {line_no} 行解析失败：{exc}") from exc
            section_id = str(item.get("section_id", "")).strip()
            if section_id:
                result[section_id] = item
    return result


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成章节人工复核表")
    parser.add_argument("--auto-map", type=Path, required=True, help="自动章节定位 chapter_map.csv")
    parser.add_argument("--model-suggestions", type=Path, required=True, help="大模型建议 JSONL")
    parser.add_argument("--output", type=Path, required=True, help="输出人工复核 CSV")
    args = parser.parse_args()

    auto_rows = read_csv(args.auto_map)
    suggestions = read_jsonl(args.model_suggestions)
    rows: list[dict[str, Any]] = []

    for row in auto_rows:
        sid = row["section_id"]
        suggestion = suggestions.get(sid, {})
        rows.append({
            "section_id": sid,
            "auto_rule_label": row.get("rule_label", ""),
            "auto_title": row.get("title", ""),
            "auto_start_pdf_page": row.get("start_pdf_page", ""),
            "auto_end_pdf_page": row.get("end_pdf_page", ""),
            "auto_start_printed_page": row.get("start_printed_page", ""),
            "auto_end_printed_page": row.get("end_printed_page", ""),
            "auto_confidence": row.get("confidence", ""),
            "auto_review_reasons": row.get("review_reasons", ""),
            "model_recommendation": suggestion.get("recommendation", ""),
            "model_scope": suggestion.get("scope", ""),
            "model_reason": suggestion.get("reason", ""),
            "human_decision": suggestion.get("draft_human_decision", ""),
            "human_final_title": row.get("title", ""),
            "human_start_pdf_page": suggestion.get("suggested_start_pdf_page", row.get("start_pdf_page", "")),
            "human_end_pdf_page": suggestion.get("suggested_end_pdf_page", row.get("end_pdf_page", "")),
            "human_scope": suggestion.get("scope", ""),
            "human_evidence_note": "",
            "human_reason": suggestion.get("draft_human_reason", ""),
            "pdf_checked": "no",
            "student_confirmation": "pending",
            "reviewer": "",
            "review_date": "",
        })

    fields = [
        "section_id", "auto_rule_label", "auto_title",
        "auto_start_pdf_page", "auto_end_pdf_page",
        "auto_start_printed_page", "auto_end_printed_page",
        "auto_confidence", "auto_review_reasons",
        "model_recommendation", "model_scope", "model_reason",
        "human_decision", "human_final_title",
        "human_start_pdf_page", "human_end_pdf_page", "human_scope",
        "human_evidence_note", "human_reason", "pdf_checked",
        "student_confirmation", "reviewer", "review_date",
    ]
    write_csv(args.output, rows, fields)
    print(f"已生成：{args.output}")
    print("请人工逐页核对后填写 pdf_checked、student_confirmation、reviewer、review_date。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
