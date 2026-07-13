#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
301563 云汉芯城招股说明书 - 可审计章节定位脚本

设计目标：
1. 不把“全文关键词命中”直接当成章节定位结果；
2. 先检查 PDF 文本层质量，再进行目录锚点、版式标题和业务证据联合判断；
3. 输出页码、标题、证据、得分、置信度及人工复核队列；
4. 章节定位只负责缩小范围，不直接生成最终股本事件结论；
5. 所有路径均由命令行传入，可复现、可迁移、可在 GitHub 中直接运行。

依赖：PyMuPDF (import fitz)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    import fitz  # PyMuPDF
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "未安装 PyMuPDF。请先运行：pip install -r requirements.txt"
    ) from exc


# -----------------------------
# 数据结构
# -----------------------------


@dataclass(slots=True)
class LineRecord:
    pdf_page: int
    printed_page: str
    line_index: int
    y0: float
    y1: float
    text: str
    normalized_text: str
    max_font_size: float
    is_bold: bool


@dataclass(slots=True)
class PageRecord:
    pdf_page: int
    printed_page: str
    text: str
    normalized_text: str
    text_length: int
    cjk_ratio: float
    replacement_char_count: int
    image_count: int
    quality_status: str
    quality_reasons: list[str] = field(default_factory=list)
    positive_hits: dict[str, int] = field(default_factory=dict)
    negative_hits: dict[str, int] = field(default_factory=dict)
    page_score: float = 0.0


@dataclass(slots=True)
class HeadingRecord:
    heading_id: str
    pdf_page: int
    printed_page: str
    line_index: int
    y0: float
    title: str
    normalized_title: str
    level: int
    max_font_size: float
    is_bold: bool
    parent_chapter: str = ""
    parent_section: str = ""
    end_pdf_page: int = 0
    end_printed_page: str = ""


@dataclass(slots=True)
class TocEntry:
    toc_pdf_page: int
    title: str
    normalized_title: str
    printed_target_page: int


@dataclass(slots=True)
class LocatedSection:
    section_id: str
    rule_id: str
    rule_label: str
    heading_id: str
    title: str
    level: int
    start_pdf_page: int
    end_pdf_page: int
    start_printed_page: str
    end_printed_page: str
    parent_chapter: str
    parent_section: str
    score: float
    confidence: str
    toc_corroborated: bool
    matched_title_pattern: str
    positive_evidence: str
    negative_evidence: str
    text_quality_warning: bool
    review_required: bool
    review_reasons: str


@dataclass(slots=True)
class ReviewItem:
    review_id: str
    item_type: str
    pdf_page_start: int
    pdf_page_end: int
    printed_page_start: str
    printed_page_end: str
    related_section_id: str
    reason_code: str
    reason: str
    suggested_action: str
    status: str = "pending"


# -----------------------------
# 文本、正则与基础工具
# -----------------------------


CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
SECTION_RE = re.compile(r"^第[一二三四五六七八九十百]+节\s*.+$")
LEVEL2_RE = re.compile(r"^[一二三四五六七八九十百]+、\s*.+$")
LEVEL3_RE = re.compile(r"^[（(][一二三四五六七八九十百]+[）)]\s*.+$")
LEVEL4_RE = re.compile(r"^\d+[、.]\s*.+$")
TOC_LINE_RE = re.compile(r"^(?P<title>.+?)\s*\.{2,}\s*(?P<page>\d{1,4})\s*$")
STANDALONE_PAGE_RE = re.compile(r"^\d{1,4}$")


# 这些标题在招股说明书页眉中高频出现，不应当识别为正文标题。
DEFAULT_HEADER_PATTERNS = (
    "招股说明书",
    "云汉芯城（上海）互联网科技股份有限公司",
)


def normalize_text(text: str) -> str:
    """用于匹配的轻度标准化，不改变输出证据原文。"""
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = re.sub(r"\s+", "", text)
    text = text.replace("（", "(").replace("）", ")")
    return text.strip()


def display_clean(text: str) -> str:
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", text).strip()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def count_cjk(text: str) -> int:
    return len(CJK_RE.findall(text))


def keyword_hits(text: str, keywords: dict[str, float]) -> dict[str, int]:
    normalized = normalize_text(text)
    result: dict[str, int] = {}
    for keyword in keywords:
        key_norm = normalize_text(keyword)
        count = normalized.count(key_norm)
        if count:
            result[keyword] = count
    return result


def weighted_score(hits: dict[str, int], weights: dict[str, float], cap: float | None = None) -> float:
    score = sum(weights[key] * count for key, count in hits.items())
    if cap is not None:
        return min(score, cap)
    return score


def infer_heading_level(text: str) -> int | None:
    normalized = display_clean(text)
    if SECTION_RE.match(normalized):
        return 1
    if LEVEL2_RE.match(normalized):
        return 2
    if LEVEL3_RE.match(normalized):
        return 3
    if LEVEL4_RE.match(normalized):
        return 4
    return None


def csv_safe(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set)):
        return " | ".join(str(v) for v in value)
    if isinstance(value, dict):
        return " | ".join(f"{k}:{v}" for k, v in value.items())
    return value


def write_csv(path: Path, rows: Sequence[dict[str, Any]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_safe(row.get(key, "")) for key in fieldnames})


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


# -----------------------------
# 配置读取与参数校验
# -----------------------------


def load_config(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            config = json.load(fh)
    except FileNotFoundError as exc:
        raise SystemExit(f"配置文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"配置文件不是有效 JSON：{path}\n{exc}") from exc

    required = ["company", "target_rules", "positive_keywords", "negative_keywords", "quality"]
    missing = [key for key in required if key not in config]
    if missing:
        raise SystemExit(f"配置文件缺少字段：{', '.join(missing)}")
    return config


def compile_patterns(patterns: Iterable[str]) -> list[re.Pattern[str]]:
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            raise SystemExit(f"非法正则表达式：{pattern}\n{exc}") from exc
    return compiled


# -----------------------------
# PDF 页面解析与质量检查
# -----------------------------


def extract_printed_page(page: fitz.Page, lines: list[LineRecord]) -> str:
    """
    优先读取页脚中的独立数字作为招股书印刷页码。
    封面等没有印刷页码的页面返回空字符串。
    """
    height = float(page.rect.height)
    footer_candidates = [
        line for line in lines
        if STANDALONE_PAGE_RE.match(line.text)
        and line.y0 >= height * 0.82
    ]
    if footer_candidates:
        footer_candidates.sort(key=lambda item: item.y0, reverse=True)
        return footer_candidates[0].text
    return ""


def extract_lines(page: fitz.Page, pdf_page: int) -> list[LineRecord]:
    blocks = page.get_text("dict", sort=True).get("blocks", [])
    raw_lines: list[tuple[float, float, str, float, bool]] = []

    for block in blocks:
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = "".join(span.get("text", "") for span in spans)
            cleaned = display_clean(text)
            if not cleaned:
                continue
            max_size = max((float(span.get("size", 0.0)) for span in spans), default=0.0)
            # PyMuPDF font flags: bold 常见为 bit 4 (16) 置位。
            is_bold = any(int(span.get("flags", 0)) & 16 for span in spans)
            bbox = line.get("bbox", (0.0, 0.0, 0.0, 0.0))
            raw_lines.append((float(bbox[1]), float(bbox[3]), cleaned, max_size, is_bold))

    raw_lines.sort(key=lambda item: (item[0], item[1]))
    provisional = [
        LineRecord(
            pdf_page=pdf_page,
            printed_page="",
            line_index=index,
            y0=y0,
            y1=y1,
            text=text,
            normalized_text=normalize_text(text),
            max_font_size=max_size,
            is_bold=is_bold,
        )
        for index, (y0, y1, text, max_size, is_bold) in enumerate(raw_lines, start=1)
    ]

    printed_page = extract_printed_page(page, provisional)
    for line in provisional:
        line.printed_page = printed_page
    return provisional


def assess_text_quality(
    text: str,
    image_count: int,
    quality_cfg: dict[str, Any],
) -> tuple[str, list[str], float, int]:
    reasons: list[str] = []
    text_length = len(text.strip())
    cjk_count = count_cjk(text)
    cjk_ratio = cjk_count / max(text_length, 1)
    replacement_count = text.count("�")

    min_chars = int(quality_cfg.get("min_text_chars", 80))
    min_cjk_ratio = float(quality_cfg.get("min_cjk_ratio", 0.03))
    max_replacement = int(quality_cfg.get("max_replacement_chars", 3))

    if text_length < min_chars:
        reasons.append(f"文本长度过短({text_length}<{min_chars})")
    if text_length >= min_chars and cjk_ratio < min_cjk_ratio:
        reasons.append(f"中文字符比例偏低({cjk_ratio:.3f}<{min_cjk_ratio})")
    if replacement_count > max_replacement:
        reasons.append(f"乱码替代字符过多({replacement_count}>{max_replacement})")
    if text_length < min_chars and image_count > 0:
        reasons.append("页面含图片但文本层不足，可能为图表或扫描页")

    if not reasons:
        return "good", reasons, cjk_ratio, replacement_count
    if text_length == 0 or replacement_count > max_replacement * 3:
        return "poor", reasons, cjk_ratio, replacement_count
    return "review", reasons, cjk_ratio, replacement_count


def parse_pdf(
    doc: fitz.Document,
    config: dict[str, Any],
) -> tuple[list[PageRecord], list[LineRecord]]:
    pages: list[PageRecord] = []
    all_lines: list[LineRecord] = []
    positive_keywords: dict[str, float] = config["positive_keywords"]
    negative_keywords: dict[str, float] = config["negative_keywords"]

    for index in range(doc.page_count):
        page = doc.load_page(index)
        pdf_page = index + 1
        lines = extract_lines(page, pdf_page)
        all_lines.extend(lines)
        text = page.get_text("text", sort=True)
        printed_page = lines[0].printed_page if lines else ""
        images = page.get_images(full=True)
        quality_status, reasons, cjk_ratio, replacement_count = assess_text_quality(
            text=text,
            image_count=len(images),
            quality_cfg=config["quality"],
        )
        pos_hits = keyword_hits(text, positive_keywords)
        neg_hits = keyword_hits(text, negative_keywords)
        positive_score = weighted_score(pos_hits, positive_keywords, cap=16.0)
        negative_score = weighted_score(neg_hits, negative_keywords, cap=14.0)

        # 页面层面仅用于排序和人工审阅，不直接决定最终章节。
        page_score = round(positive_score - negative_score, 3)
        pages.append(
            PageRecord(
                pdf_page=pdf_page,
                printed_page=printed_page,
                text=text,
                normalized_text=normalize_text(text),
                text_length=len(text.strip()),
                cjk_ratio=round(cjk_ratio, 5),
                replacement_char_count=replacement_count,
                image_count=len(images),
                quality_status=quality_status,
                quality_reasons=reasons,
                positive_hits=pos_hits,
                negative_hits=neg_hits,
                page_score=page_score,
            )
        )
    return pages, all_lines


# -----------------------------
# 目录解析、标题识别与层级范围
# -----------------------------


def extract_toc_entries(
    pages: Sequence[PageRecord],
    scan_page_limit: int,
) -> list[TocEntry]:
    entries: list[TocEntry] = []
    for page in pages[:scan_page_limit]:
        # 目录页通常出现“目录”，且包含点线和页码。
        if "目录" not in page.text and not TOC_LINE_RE.search(page.text):
            continue
        for raw_line in page.text.splitlines():
            line = display_clean(raw_line)
            match = TOC_LINE_RE.match(line)
            if not match:
                continue
            title = display_clean(match.group("title"))
            # 排除仅由点线、页码或过短文本构成的行。
            if len(normalize_text(title)) < 3:
                continue
            entries.append(
                TocEntry(
                    toc_pdf_page=page.pdf_page,
                    title=title,
                    normalized_title=normalize_text(title),
                    printed_target_page=int(match.group("page")),
                )
            )
    # 去重：同一标题+页码保留首次出现。
    dedup: dict[tuple[str, int], TocEntry] = {}
    for entry in entries:
        dedup.setdefault((entry.normalized_title, entry.printed_target_page), entry)
    return list(dedup.values())


def is_header_or_footer(line: LineRecord, page_height: float) -> bool:
    if STANDALONE_PAGE_RE.match(line.text) and line.y0 >= page_height * 0.80:
        return True
    if line.y0 <= page_height * 0.10:
        normalized = normalize_text(line.text)
        if any(normalize_text(pattern) in normalized for pattern in DEFAULT_HEADER_PATTERNS):
            return True
    return False


def is_heading_candidate(
    line: LineRecord,
    page_height: float,
    config: dict[str, Any],
) -> bool:
    title = display_clean(line.text)
    if is_header_or_footer(line, page_height):
        return False
    if "..." in title or "……" in title:
        return False
    if len(title) > int(config.get("max_heading_chars", 95)):
        return False

    level = infer_heading_level(title)
    if level is None:
        return False

    min_size = float(config.get("min_heading_font_size", 11.5))
    # 编号结构 + 字号/加粗双重判断，降低正文引用被误判为标题的概率。
    if line.max_font_size < min_size and not line.is_bold:
        return False
    return True


def extract_headings(
    doc: fitz.Document,
    lines: Sequence[LineRecord],
    config: dict[str, Any],
) -> list[HeadingRecord]:
    body_start_pdf_page = int(config.get("body_start_pdf_page", 20))
    page_heights = {index + 1: float(doc.load_page(index).rect.height) for index in range(doc.page_count)}

    candidates: list[HeadingRecord] = []
    current_chapter = ""
    current_section = ""
    counter = 0

    for line in lines:
        if line.pdf_page < body_start_pdf_page:
            continue
        if not is_heading_candidate(line, page_heights[line.pdf_page], config):
            continue
        level = infer_heading_level(line.text)
        if level is None:
            continue

        counter += 1
        heading = HeadingRecord(
            heading_id=f"H{counter:04d}",
            pdf_page=line.pdf_page,
            printed_page=line.printed_page,
            line_index=line.line_index,
            y0=line.y0,
            title=line.text,
            normalized_title=line.normalized_text,
            level=level,
            max_font_size=round(line.max_font_size, 2),
            is_bold=line.is_bold,
            parent_chapter=current_chapter,
            parent_section=current_section,
        )
        if level == 1:
            current_chapter = line.text
            current_section = ""
            heading.parent_chapter = line.text
        elif level == 2:
            current_section = line.text
            heading.parent_chapter = current_chapter
            heading.parent_section = line.text
        else:
            heading.parent_chapter = current_chapter
            heading.parent_section = current_section
        candidates.append(heading)

    # 计算标题范围：到下一条同级或更高级标题之前。
    for index, heading in enumerate(candidates):
        next_boundary: HeadingRecord | None = None
        for next_heading in candidates[index + 1:]:
            if next_heading.level <= heading.level:
                next_boundary = next_heading
                break
        if next_boundary is None:
            end_page = doc.page_count
        elif next_boundary.pdf_page == heading.pdf_page:
            end_page = heading.pdf_page
        else:
            end_page = next_boundary.pdf_page - 1
        heading.end_pdf_page = max(heading.pdf_page, end_page)
        if heading.end_pdf_page <= doc.page_count:
            page_lines = [line for line in lines if line.pdf_page == heading.end_pdf_page]
            heading.end_printed_page = page_lines[0].printed_page if page_lines else ""
    return candidates


def toc_support_for_heading(heading: HeadingRecord, toc_entries: Sequence[TocEntry]) -> tuple[bool, str]:
    title_norm = heading.normalized_title
    for entry in toc_entries:
        toc_norm = entry.normalized_title
        # 标题完全一致或一方包含另一方；同时印刷页码允许 1 页偏差。
        title_similar = title_norm == toc_norm or title_norm in toc_norm or toc_norm in title_norm
        page_similar = (
            heading.printed_page.isdigit()
            and abs(int(heading.printed_page) - entry.printed_target_page) <= 1
        )
        if title_similar and page_similar:
            return True, f"目录页PDF{entry.toc_pdf_page}，目录印刷页{entry.printed_target_page}"
    return False, ""


# -----------------------------
# 目标规则匹配与章节评分
# -----------------------------


def pages_in_range(pages: Sequence[PageRecord], start: int, end: int) -> list[PageRecord]:
    return [page for page in pages if start <= page.pdf_page <= end]


def join_hit_evidence(hits: dict[str, int]) -> str:
    if not hits:
        return ""
    return "；".join(f"{key}×{value}" for key, value in sorted(hits.items()))


def confidence_from_score(score: float, config: dict[str, Any]) -> str:
    thresholds = config.get("confidence_thresholds", {"high": 20, "medium": 12})
    if score >= float(thresholds.get("high", 20)):
        return "high"
    if score >= float(thresholds.get("medium", 12)):
        return "medium"
    return "low"


def match_target_rule(
    heading: HeadingRecord,
    target_rules: Sequence[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    for rule in target_rules:
        patterns = compile_patterns(rule.get("title_patterns", []))
        for raw_pattern, pattern in zip(rule.get("title_patterns", []), patterns):
            if pattern.search(heading.normalized_title):
                return rule, raw_pattern
    return None, ""


def locate_sections(
    pages: Sequence[PageRecord],
    headings: Sequence[HeadingRecord],
    toc_entries: Sequence[TocEntry],
    config: dict[str, Any],
) -> tuple[list[LocatedSection], list[ReviewItem]]:
    located: list[LocatedSection] = []
    reviews: list[ReviewItem] = []
    positive_weights: dict[str, float] = config["positive_keywords"]
    negative_weights: dict[str, float] = config["negative_keywords"]
    target_chapter_patterns = compile_patterns(config.get("target_chapter_patterns", []))
    review_markers: list[str] = config.get(
        "review_markers", ["转下图", "续上图", "如下图", "具体情况如下"]
    )

    section_counter = 0
    review_counter = 0

    for heading in headings:
        rule, matched_pattern = match_target_rule(heading, config["target_rules"])
        if rule is None:
            continue

        section_counter += 1
        section_id = f"SEC{section_counter:03d}"
        section_pages = pages_in_range(pages, heading.pdf_page, heading.end_pdf_page)
        section_text = "\n".join(page.text for page in section_pages)
        title_pos_hits = keyword_hits(heading.title, positive_weights)
        body_pos_hits = keyword_hits(section_text, positive_weights)
        body_neg_hits = keyword_hits(section_text, negative_weights)

        score = float(rule.get("base_score", 12.0))
        score += weighted_score(title_pos_hits, positive_weights, cap=10.0)
        score += weighted_score(body_pos_hits, positive_weights, cap=12.0)
        score -= weighted_score(body_neg_hits, negative_weights, cap=10.0)
        score += 2.0 if heading.is_bold else 0.0
        score += 1.5 if heading.max_font_size >= 12.0 else 0.0

        in_target_chapter = any(
            pattern.search(normalize_text(heading.parent_chapter or heading.title))
            for pattern in target_chapter_patterns
        )
        if in_target_chapter:
            score += float(config.get("target_chapter_bonus", 4.0))

        toc_supported, toc_note = toc_support_for_heading(heading, toc_entries)
        if toc_supported:
            score += float(config.get("toc_bonus", 4.0))

        score = round(score, 3)
        confidence = confidence_from_score(score, config)

        quality_warning_pages = [
            page for page in section_pages if page.quality_status != "good"
        ]
        review_reasons: list[str] = []
        if quality_warning_pages:
            review_reasons.append(
                "存在文本层质量异常页：" + ",".join(str(page.pdf_page) for page in quality_warning_pages)
            )
        if any(marker in section_text for marker in review_markers):
            review_reasons.append("区间含图表/续表提示，应人工检查跨页边界")
        if re.search(r"暨|和第[一二三四五六七八九十]+次|增资和.*转让", heading.title):
            review_reasons.append("单个标题可能同时包含多个业务事件，需要后续事件切分")
        if confidence == "low":
            review_reasons.append("章节综合得分偏低")

        review_required = bool(review_reasons)
        located.append(
            LocatedSection(
                section_id=section_id,
                rule_id=str(rule["rule_id"]),
                rule_label=str(rule["label"]),
                heading_id=heading.heading_id,
                title=heading.title,
                level=heading.level,
                start_pdf_page=heading.pdf_page,
                end_pdf_page=heading.end_pdf_page,
                start_printed_page=heading.printed_page,
                end_printed_page=heading.end_printed_page,
                parent_chapter=heading.parent_chapter,
                parent_section=heading.parent_section,
                score=score,
                confidence=confidence,
                toc_corroborated=toc_supported,
                matched_title_pattern=matched_pattern,
                positive_evidence=(
                    f"标题[{join_hit_evidence(title_pos_hits)}]；"
                    f"区间[{join_hit_evidence(body_pos_hits)}]；{toc_note}"
                ).strip("；"),
                negative_evidence=join_hit_evidence(body_neg_hits),
                text_quality_warning=bool(quality_warning_pages),
                review_required=review_required,
                review_reasons="；".join(review_reasons),
            )
        )

        for page in quality_warning_pages:
            review_counter += 1
            reviews.append(
                ReviewItem(
                    review_id=f"REV{review_counter:03d}",
                    item_type="page_quality",
                    pdf_page_start=page.pdf_page,
                    pdf_page_end=page.pdf_page,
                    printed_page_start=page.printed_page,
                    printed_page_end=page.printed_page,
                    related_section_id=section_id,
                    reason_code="TEXT_LAYER_REVIEW",
                    reason="；".join(page.quality_reasons) or "文本层质量需人工确认",
                    suggested_action="优先查看原 PDF；若为复杂图表可只对该页使用 MinerU；仅在无文本层时考虑 OCR。",
                )
            )

        if any(marker in section_text for marker in review_markers):
            review_counter += 1
            reviews.append(
                ReviewItem(
                    review_id=f"REV{review_counter:03d}",
                    item_type="section_boundary",
                    pdf_page_start=heading.pdf_page,
                    pdf_page_end=heading.end_pdf_page,
                    printed_page_start=heading.printed_page,
                    printed_page_end=heading.end_printed_page,
                    related_section_id=section_id,
                    reason_code="CROSS_PAGE_TABLE_OR_DIAGRAM",
                    reason="章节包含“转下图/续上图/如下图/具体情况如下”等提示，文本抽取可能未覆盖完整图表。",
                    suggested_action="人工核对起止页，并将跨页正文和表格作为同一个候选事件包保存。",
                )
            )

        if re.search(r"暨|和第[一二三四五六七八九十]+次|增资和.*转让", heading.title):
            review_counter += 1
            reviews.append(
                ReviewItem(
                    review_id=f"REV{review_counter:03d}",
                    item_type="event_boundary",
                    pdf_page_start=heading.pdf_page,
                    pdf_page_end=heading.end_pdf_page,
                    printed_page_start=heading.printed_page,
                    printed_page_end=heading.end_printed_page,
                    related_section_id=section_id,
                    reason_code="MULTI_EVENT_HEADING",
                    reason="标题中同时出现增资和转让等多个动作，章节定位正确不等于事件边界已经确定。",
                    suggested_action="在后续候选事件阶段，分别建立增资事件包与股权转让事件包。",
                )
            )

    # 去重：同一标题命中多个规则时，只保留得分最高者。
    best_by_heading: dict[str, LocatedSection] = {}
    for section in located:
        current = best_by_heading.get(section.heading_id)
        if current is None or section.score > current.score:
            best_by_heading[section.heading_id] = section
    located = sorted(
        best_by_heading.values(),
        key=lambda item: int(item.heading_id.removeprefix("H")),
    )

    # 清理去重过程中产生的跳号，并同步人工复核队列中的 section_id。
    section_id_map: dict[str, str] = {}
    for index, section in enumerate(located, start=1):
        old_id = section.section_id
        new_id = f"SEC{index:03d}"
        section.section_id = new_id
        section_id_map[old_id] = new_id

    filtered_reviews: list[ReviewItem] = []
    for item in reviews:
        if item.related_section_id not in section_id_map:
            continue
        item.related_section_id = section_id_map[item.related_section_id]
        filtered_reviews.append(item)
    reviews = filtered_reviews
    return located, reviews


# -----------------------------
# 输出文件
# -----------------------------


def heading_evidence_excerpt(page: PageRecord, heading: HeadingRecord, max_chars: int = 220) -> str:
    lines = [display_clean(line) for line in page.text.splitlines() if display_clean(line)]
    normalized_heading = heading.normalized_title
    for index, line in enumerate(lines):
        if normalized_heading in normalize_text(line) or normalize_text(line) in normalized_heading:
            excerpt = " ".join(lines[index:index + 4])
            return excerpt[:max_chars]
    return " ".join(lines[:4])[:max_chars]


def export_selected_pages_markdown(
    path: Path,
    located: Sequence[LocatedSection],
    pages: Sequence[PageRecord],
    company: dict[str, Any],
) -> None:
    page_by_no = {page.pdf_page: page for page in pages}
    lines: list[str] = [
        f"# {company.get('code', '')} {company.get('short_name', '')} - 章节定位证据文本",
        "",
        "> 本文件是章节定位后的证据包，不是最终事件抽取结果。页码以 PDF 页码为主，同时保留印刷页码。",
        "",
    ]
    for section in located:
        lines.extend(
            [
                f"## {section.section_id} {section.title}",
                "",
                f"- 规则类别：{section.rule_label}",
                f"- PDF 页码：{section.start_pdf_page}-{section.end_pdf_page}",
                f"- 印刷页码：{section.start_printed_page or '无'}-{section.end_printed_page or '无'}",
                f"- 得分/置信度：{section.score} / {section.confidence}",
                f"- 是否需人工复核：{'是' if section.review_required else '否'}",
                f"- 复核原因：{section.review_reasons or '无'}",
                "",
            ]
        )
        for pdf_page in range(section.start_pdf_page, section.end_pdf_page + 1):
            page = page_by_no[pdf_page]
            lines.extend(
                [
                    f"### PDF_PAGE_{pdf_page:04d} / PRINTED_PAGE_{page.printed_page or 'NA'}",
                    "",
                    "```text",
                    page.text.strip(),
                    "```",
                    "",
                ]
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def export_outputs(
    output_dir: Path,
    pdf_path: Path,
    config_path: Path,
    config: dict[str, Any],
    pages: Sequence[PageRecord],
    headings: Sequence[HeadingRecord],
    toc_entries: Sequence[TocEntry],
    located: Sequence[LocatedSection],
    reviews: Sequence[ReviewItem],
    save_selected_text: bool,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    page_rows = [asdict(page) for page in pages]
    write_csv(
        output_dir / "page_scores.csv",
        page_rows,
        [
            "pdf_page", "printed_page", "text_length", "cjk_ratio",
            "replacement_char_count", "image_count", "quality_status",
            "quality_reasons", "positive_hits", "negative_hits", "page_score",
        ],
    )

    heading_rows: list[dict[str, Any]] = []
    page_by_no = {page.pdf_page: page for page in pages}
    for heading in headings:
        row = asdict(heading)
        row["evidence_excerpt"] = heading_evidence_excerpt(page_by_no[heading.pdf_page], heading)
        heading_rows.append(row)
    write_csv(
        output_dir / "heading_candidates.csv",
        heading_rows,
        [
            "heading_id", "pdf_page", "printed_page", "line_index", "y0",
            "title", "level", "max_font_size", "is_bold", "parent_chapter",
            "parent_section", "end_pdf_page", "end_printed_page", "evidence_excerpt",
        ],
    )

    write_csv(
        output_dir / "toc_entries.csv",
        [asdict(item) for item in toc_entries],
        ["toc_pdf_page", "title", "printed_target_page"],
    )

    located_rows = [asdict(section) for section in located]
    write_csv(
        output_dir / "chapter_map.csv",
        located_rows,
        [
            "section_id", "rule_id", "rule_label", "heading_id", "title", "level",
            "start_pdf_page", "end_pdf_page", "start_printed_page", "end_printed_page",
            "parent_chapter", "parent_section", "score", "confidence",
            "toc_corroborated", "matched_title_pattern", "positive_evidence",
            "negative_evidence", "text_quality_warning", "review_required", "review_reasons",
        ],
    )
    write_json(output_dir / "chapter_map.json", located_rows)

    write_csv(
        output_dir / "review_queue.csv",
        [asdict(item) for item in reviews],
        [
            "review_id", "item_type", "pdf_page_start", "pdf_page_end",
            "printed_page_start", "printed_page_end", "related_section_id",
            "reason_code", "reason", "suggested_action", "status",
        ],
    )

    if save_selected_text:
        export_selected_pages_markdown(
            output_dir / "selected_pages_evidence.md",
            located=located,
            pages=pages,
            company=config["company"],
        )

    summary = {
        "run_time_utc": datetime.now(timezone.utc).isoformat(),
        "pdf_path": str(pdf_path.resolve()),
        "pdf_sha256": sha256_file(pdf_path),
        "config_path": str(config_path.resolve()),
        "company": config["company"],
        "page_count": len(pages),
        "toc_entry_count": len(toc_entries),
        "heading_candidate_count": len(headings),
        "located_section_count": len(located),
        "review_item_count": len(reviews),
        "quality_summary": {
            status: sum(1 for page in pages if page.quality_status == status)
            for status in ("good", "review", "poor")
        },
        "method": {
            "text_layer_first": True,
            "toc_anchor": True,
            "typography_heading_detection": True,
            "business_keyword_scoring": True,
            "negative_section_penalty": True,
            "manual_review_queue": True,
            "ocr_default": False,
        },
    }
    write_json(output_dir / "run_summary.json", summary)


# -----------------------------
# 日志与主程序
# -----------------------------


def configure_logging(output_dir: Path, verbose: bool) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(output_dir / "section_locator.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=handlers,
        force=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="对 301563 云汉芯城招股说明书执行可审计章节定位。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--pdf", required=True, type=Path, help="输入 PDF 路径")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "config" / "section_rules.json",
        help="章节定位规则 JSON",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("outputs/section_location"),
        help="输出目录",
    )
    parser.add_argument(
        "--save-selected-text",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否导出命中章节的页码化证据文本",
    )
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    pdf_path: Path = args.pdf
    config_path: Path = args.config
    output_dir: Path = args.output

    if not pdf_path.exists():
        raise SystemExit(f"PDF 不存在：{pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise SystemExit(f"输入文件不是 PDF：{pdf_path}")

    configure_logging(output_dir, args.verbose)
    config = load_config(config_path)

    logging.info("开始章节定位：%s", pdf_path)
    logging.info("读取配置：%s", config_path)

    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        logging.exception("无法打开 PDF")
        raise SystemExit(f"无法打开 PDF：{exc}") from exc

    if doc.is_encrypted:
        raise SystemExit("PDF 已加密，当前脚本无法读取。")

    logging.info("PDF 页数：%s", doc.page_count)
    pages, lines = parse_pdf(doc, config)
    logging.info("页面解析完成；文本层质量：good=%s, review=%s, poor=%s",
                 sum(page.quality_status == "good" for page in pages),
                 sum(page.quality_status == "review" for page in pages),
                 sum(page.quality_status == "poor" for page in pages))

    toc_entries = extract_toc_entries(
        pages,
        scan_page_limit=int(config.get("toc_scan_page_limit", 30)),
    )
    logging.info("目录条目：%s", len(toc_entries))

    headings = extract_headings(doc, lines, config)
    logging.info("正文标题候选：%s", len(headings))

    located, reviews = locate_sections(pages, headings, toc_entries, config)
    logging.info("目标章节：%s；人工复核项：%s", len(located), len(reviews))

    export_outputs(
        output_dir=output_dir,
        pdf_path=pdf_path,
        config_path=config_path,
        config=config,
        pages=pages,
        headings=headings,
        toc_entries=toc_entries,
        located=located,
        reviews=reviews,
        save_selected_text=args.save_selected_text,
    )

    logging.info("完成。核心结果：%s", output_dir / "chapter_map.csv")
    for section in located:
        logging.info(
            "%s | %s | PDF %s-%s | printed %s-%s | %s | score=%s",
            section.section_id,
            section.title,
            section.start_pdf_page,
            section.end_pdf_page,
            section.start_printed_page,
            section.end_printed_page,
            section.confidence,
            section.score,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
