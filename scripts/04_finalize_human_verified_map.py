#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""将人工复核表转为最终章节定位表，并输出自动结果与人工结果的差异。

关键约束：
- 默认只接收 student_confirmation=confirmed 且 pdf_checked=yes 的记录；
- 人工可以 keep、adjust、reference_only 或 reject；
- 人工新增章节从 manual_additions.csv 读取；
- 程序保留自动值、人工值及修改原因，避免人工修正成为黑箱。
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VALID_DECISIONS = {"keep", "adjust", "reference_only", "reject"}
VALID_SCOPES = {"core", "reference", "exclude"}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def as_int(value: str, field: str, row_id: str) -> int:
    try:
        result = int(str(value).strip())
    except ValueError as exc:
        raise SystemExit(f"{row_id} 的 {field} 不是整数：{value!r}") from exc
    if result <= 0:
        raise SystemExit(f"{row_id} 的 {field} 必须大于 0")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="生成经人工确认的最终章节定位表")
    parser.add_argument("--review", type=Path, required=True, help="人工复核 CSV")
    parser.add_argument("--manual-additions", type=Path, required=True, help="人工新增章节 CSV")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--allow-pending", action="store_true", help="仅用于预览；允许未确认记录")
    args = parser.parse_args()

    review_rows = read_csv(args.review)
    addition_rows = read_csv(args.manual_additions)
    final_rows: list[dict[str, Any]] = []
    diff_rows: list[dict[str, Any]] = []
    pending: list[str] = []

    for row in review_rows:
        sid = row.get("section_id", "").strip()
        confirmed = row.get("student_confirmation", "").strip().lower() == "confirmed"
        checked = row.get("pdf_checked", "").strip().lower() == "yes"
        if not (confirmed and checked):
            pending.append(sid)
            if not args.allow_pending:
                continue

        decision = row.get("human_decision", "").strip().lower()
        if decision not in VALID_DECISIONS:
            raise SystemExit(f"{sid} 的 human_decision 无效：{decision!r}")
        scope = row.get("human_scope", "").strip().lower()
        if scope not in VALID_SCOPES:
            raise SystemExit(f"{sid} 的 human_scope 无效：{scope!r}")

        auto_start = as_int(row["auto_start_pdf_page"], "auto_start_pdf_page", sid)
        auto_end = as_int(row["auto_end_pdf_page"], "auto_end_pdf_page", sid)
        human_start = as_int(row["human_start_pdf_page"], "human_start_pdf_page", sid)
        human_end = as_int(row["human_end_pdf_page"], "human_end_pdf_page", sid)
        if human_start > human_end:
            raise SystemExit(f"{sid} 人工起始页大于结束页")

        diff_type = "unchanged"
        if decision == "reject" or scope == "exclude":
            diff_type = "rejected_by_human"
        elif auto_start != human_start or auto_end != human_end:
            diff_type = "range_adjusted"
        elif decision == "reference_only":
            diff_type = "scope_changed_to_reference"

        diff_rows.append({
            "section_id": sid,
            "auto_title": row.get("auto_title", ""),
            "auto_pdf_range": f"{auto_start}-{auto_end}",
            "human_pdf_range": f"{human_start}-{human_end}",
            "human_decision": decision,
            "human_scope": scope,
            "diff_type": diff_type,
            "human_reason": row.get("human_reason", ""),
            "human_evidence_note": row.get("human_evidence_note", ""),
            "reviewer": row.get("reviewer", ""),
            "review_date": row.get("review_date", ""),
            "confirmation_status": "confirmed" if confirmed and checked else "preview_pending",
        })

        if decision == "reject" or scope == "exclude":
            continue
        final_rows.append({
            "final_section_id": sid,
            "origin": "auto_candidate_human_verified" if confirmed and checked else "auto_candidate_review_preview",
            "title": row.get("human_final_title", "") or row.get("auto_title", ""),
            "start_pdf_page": human_start,
            "end_pdf_page": human_end,
            "scope": scope,
            "auto_confidence": row.get("auto_confidence", ""),
            "model_recommendation": row.get("model_recommendation", ""),
            "human_decision": decision,
            "human_reason": row.get("human_reason", ""),
            "human_evidence_note": row.get("human_evidence_note", ""),
            "reviewer": row.get("reviewer", ""),
            "review_date": row.get("review_date", ""),
            "confirmation_status": "confirmed" if confirmed and checked else "preview_pending",
        })

    for index, row in enumerate(addition_rows, start=1):
        add_id = row.get("addition_id", "").strip() or f"MAN{index:03d}"
        confirmed = row.get("student_confirmation", "").strip().lower() == "confirmed"
        checked = row.get("pdf_checked", "").strip().lower() == "yes"
        if not (confirmed and checked):
            pending.append(add_id)
            if not args.allow_pending:
                continue
        scope = row.get("human_scope", "").strip().lower()
        if scope not in VALID_SCOPES:
            raise SystemExit(f"{add_id} 的 human_scope 无效：{scope!r}")
        start = as_int(row["start_pdf_page"], "start_pdf_page", add_id)
        end = as_int(row["end_pdf_page"], "end_pdf_page", add_id)
        if scope != "exclude":
            final_rows.append({
                "final_section_id": add_id,
                "origin": "human_added" if confirmed and checked else "human_added_preview",
                "title": row.get("title", ""),
                "start_pdf_page": start,
                "end_pdf_page": end,
                "scope": scope,
                "auto_confidence": "not_applicable",
                "model_recommendation": "not_applicable",
                "human_decision": "add",
                "human_reason": row.get("human_reason", ""),
                "human_evidence_note": row.get("human_evidence_note", ""),
                "reviewer": row.get("reviewer", ""),
                "review_date": row.get("review_date", ""),
                "confirmation_status": "confirmed" if confirmed and checked else "preview_pending",
            })
        diff_rows.append({
            "section_id": add_id,
            "auto_title": "",
            "auto_pdf_range": "",
            "human_pdf_range": f"{start}-{end}",
            "human_decision": "add",
            "human_scope": scope,
            "diff_type": "human_added",
            "human_reason": row.get("human_reason", ""),
            "human_evidence_note": row.get("human_evidence_note", ""),
            "reviewer": row.get("reviewer", ""),
            "review_date": row.get("review_date", ""),
            "confirmation_status": "confirmed" if confirmed and checked else "preview_pending",
        })

    final_rows.sort(key=lambda r: (int(r["start_pdf_page"]), int(r["end_pdf_page"]), r["final_section_id"]))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    final_fields = [
        "final_section_id", "origin", "title", "start_pdf_page", "end_pdf_page",
        "scope", "auto_confidence", "model_recommendation", "human_decision",
        "human_reason", "human_evidence_note", "reviewer", "review_date", "confirmation_status",
    ]
    diff_fields = [
        "section_id", "auto_title", "auto_pdf_range", "human_pdf_range",
        "human_decision", "human_scope", "diff_type", "human_reason",
        "human_evidence_note", "reviewer", "review_date", "confirmation_status",
    ]
    write_csv(args.output_dir / "final_section_map.csv", final_rows, final_fields)
    write_csv(args.output_dir / "auto_vs_human_diff.csv", diff_rows, diff_fields)
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "final_section_count": len(final_rows),
        "diff_count": len(diff_rows),
        "pending_or_unchecked": pending,
        "allow_pending": args.allow_pending,
    }
    with (args.output_dir / "human_review_summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    if pending and not args.allow_pending:
        print(f"注意：{len(pending)} 条未确认记录未进入最终结果：{', '.join(pending)}")
    print(f"已生成：{args.output_dir / 'final_section_map.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
