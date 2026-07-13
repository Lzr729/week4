#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行人工复核工具。

逐条展示自动定位结果和大模型建议，要求人工选择保留、调整、仅作参考或排除，
并填写 PDF 证据备注。复核结果写回新的 CSV，不覆盖原模板。
"""
from __future__ import annotations

import argparse
import csv
from datetime import date
from pathlib import Path


def read_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader), list(reader.fieldnames or [])


def write_rows(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def ask_choice(prompt: str, choices: set[str], default: str) -> str:
    while True:
        value = input(f"{prompt} [{default}]：").strip().lower() or default
        if value in choices:
            return value
        print("可选值：" + ", ".join(sorted(choices)))


def main() -> int:
    parser = argparse.ArgumentParser(description="逐条完成人工章节复核")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reviewer", required=True, help="复核人姓名或GitHub用户名")
    args = parser.parse_args()

    rows, fields = read_rows(args.input)
    print(f"共 {len(rows)} 条。请同时打开原 PDF，逐页核对后作答。输入 q 可保存退出。")

    for index, row in enumerate(rows, start=1):
        if row.get("student_confirmation", "").lower() == "confirmed":
            continue
        print("\n" + "=" * 72)
        print(f"[{index}/{len(rows)}] {row['section_id']} {row['auto_title']}")
        print(f"自动范围：PDF {row['auto_start_pdf_page']}-{row['auto_end_pdf_page']}")
        print(f"自动置信度：{row['auto_confidence']}")
        print(f"程序复核原因：{row['auto_review_reasons'] or '无'}")
        print(f"大模型建议：{row['model_recommendation']} / {row['model_scope']}")
        print(f"建议理由：{row['model_reason']}")
        command = input("回车继续复核；输入 q 保存退出：").strip().lower()
        if command == "q":
            break

        decision = ask_choice(
            "人工决定 keep/adjust/reference_only/reject",
            {"keep", "adjust", "reference_only", "reject"},
            row.get("human_decision", "keep") or "keep",
        )
        scope_default = "exclude" if decision == "reject" else ("reference" if decision == "reference_only" else "core")
        scope = ask_choice("人工范围 core/reference/exclude", {"core", "reference", "exclude"}, scope_default)
        start = input(f"人工起始PDF页 [{row['human_start_pdf_page']}]：").strip() or row["human_start_pdf_page"]
        end = input(f"人工结束PDF页 [{row['human_end_pdf_page']}]：").strip() or row["human_end_pdf_page"]
        evidence = input("请写一条你在PDF中看到的证据或页面特征：").strip()
        reason = input(f"人工判断理由 [{row['human_reason']}]：").strip() or row["human_reason"]

        row.update({
            "human_decision": decision,
            "human_start_pdf_page": start,
            "human_end_pdf_page": end,
            "human_scope": scope,
            "human_evidence_note": evidence,
            "human_reason": reason,
            "pdf_checked": "yes",
            "student_confirmation": "confirmed",
            "reviewer": args.reviewer,
            "review_date": date.today().isoformat(),
        })
        write_rows(args.output, rows, fields)
        print("已即时保存。")

    write_rows(args.output, rows, fields)
    confirmed = sum(r.get("student_confirmation", "").lower() == "confirmed" for r in rows)
    print(f"已保存到 {args.output}；已确认 {confirmed}/{len(rows)} 条。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
