#!/usr/bin/env python3
"""从人工初审结果生成线上兼容语义卡和可编辑二审页面。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import argparse
import copy
import json
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from meme_discovery.reviewed_card_enricher import (  # noqa: E402
    enrich_reviewed_groups,
    prepare_reviewed_groups,
)
from meme_discovery.reviewed_card_review import (  # noqa: E402
    build_pending_library,
    build_serving_patch,
    render_review_html,
    validate_storage_cards,
)
from meme_discovery.web_research import research_reviewed_groups  # noqa: E402


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根节点必须是对象: {path}")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read_evidence(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            evidence_id = str(row.get("evidence_id") or "")
            if evidence_id and evidence_id not in seen:
                seen.add(evidence_id)
                rows.append(row)
    return rows


def prepare(args: argparse.Namespace) -> None:
    evidence_paths = [Path(value) for value in args.evidence]
    document = prepare_reviewed_groups(
        _read_json(Path(args.second_pass)),
        _read_json(Path(args.summary)),
        _read_evidence(evidence_paths),
    )
    _write_json(Path(args.output), document)
    print(json.dumps(document["summary"], ensure_ascii=False))


def enrich(args: argparse.Namespace) -> None:
    document = enrich_reviewed_groups(
        _read_json(Path(args.input)),
        _read_json(Path(args.config)),
        cache_dir=Path(args.cache_dir),
    )
    _write_json(Path(args.output), document)
    print(json.dumps(document["semantic_enrichment_report"], ensure_ascii=False))


def research(args: argparse.Namespace) -> None:
    document = research_reviewed_groups(
        _read_json(Path(args.input)),
        _read_json(Path(args.config)),
        cache_dir=Path(args.cache_dir),
    )
    _write_json(Path(args.output), document)
    print(json.dumps(document["web_research_report"], ensure_ascii=False))


def select_rejected(args: argparse.Namespace) -> None:
    document = _read_json(Path(args.input))
    decisions_document = _read_json(Path(args.decisions))
    decisions = decisions_document.get("decisions") or decisions_document
    rejected_ids = {
        str(card_id)
        for card_id, decision in decisions.items()
        if decision.get("decision") == "reject"
    }
    selected: list[dict[str, Any]] = []
    for source_item in document.get("items") or []:
        card = source_item.get("storage_card") or {}
        decision = decisions.get(card.get("card_id")) or {}
        if decision.get("decision") != "reject":
            continue
        item = copy.deepcopy(source_item)
        item["first_review"] = {
            "decision": "reject",
            "reviewed_at": decisions_document.get("exported_at"),
        }
        item["semantic_status"] = "prepared"
        item["storage_card"] = None
        item.pop("semantic_output", None)
        item.pop("semantic_error", None)
        selected.append(item)
    # 从未修改的输入条目核对，避免审核文件与草稿错配时静默漏项。
    available_rejected_ids = {
        str((item.get("storage_card") or {}).get("card_id") or "")
        for item in document.get("items") or []
        if str((item.get("storage_card") or {}).get("card_id") or "") in rejected_ids
    }
    missing = rejected_ids - available_rejected_ids
    if missing:
        raise ValueError(f"审核记录中的淘汰项不在输入草稿中: {sorted(missing)}")
    document["items"] = selected
    document["report_kind"] = "first_review_rejected_expression_second_review"
    document["review_title"] = "一审淘汰梗二审"
    document["source_first_review"] = {
        "path_name": Path(args.decisions).name,
        "exported_at": decisions_document.get("exported_at"),
        "reject_count": len(selected),
    }
    document["summary"] = {
        "prepared_group_count": len(selected),
        "prepared_expression_count": sum(
            1 + len(item.get("aliases") or []) for item in selected
        ),
        "prior_refine_group_count": sum(
            item.get("prior_serving_policy") == "refine" for item in selected
        ),
        "prior_understand_group_count": sum(
            item.get("prior_serving_policy") == "understand" for item in selected
        ),
    }
    document.pop("semantic_enrichment_report", None)
    document.pop("web_research_report", None)
    _write_json(Path(args.output), document)
    print(json.dumps(document["summary"], ensure_ascii=False))


def render(args: argparse.Namespace) -> None:
    document = _read_json(Path(args.input))
    errors = validate_storage_cards(document)
    if errors:
        raise SystemExit("\n".join(errors))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    library = build_pending_library(document)
    patch = build_serving_patch(document)
    _write_json(output_dir / "semantic-card-drafts.json", document)
    _write_json(output_dir / "pending-online-library.json", library)
    _write_json(output_dir / "pending-understand-only-config-patch.json", patch)
    page = output_dir / "semantic-card-review.html"
    temporary = page.with_suffix(".html.tmp")
    temporary.write_text(render_review_html(document), encoding="utf-8")
    temporary.replace(page)
    print(
        json.dumps(
            {
                "card_count": library["card_count"],
                "review_page": str(page),
                "library": str(output_dir / "pending-online-library.json"),
            },
            ensure_ascii=False,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--second-pass", required=True)
    prepare_parser.add_argument("--summary", required=True)
    prepare_parser.add_argument("--evidence", action="append", default=[])
    prepare_parser.add_argument("--output", required=True)
    prepare_parser.set_defaults(handler=prepare)

    research_parser = subparsers.add_parser("research")
    research_parser.add_argument("--input", required=True)
    research_parser.add_argument("--config", required=True)
    research_parser.add_argument("--cache-dir", required=True)
    research_parser.add_argument("--output", required=True)
    research_parser.set_defaults(handler=research)

    rejected_parser = subparsers.add_parser("select-rejected")
    rejected_parser.add_argument("--input", required=True)
    rejected_parser.add_argument("--decisions", required=True)
    rejected_parser.add_argument("--output", required=True)
    rejected_parser.set_defaults(handler=select_rejected)

    enrich_parser = subparsers.add_parser("enrich")
    enrich_parser.add_argument("--input", required=True)
    enrich_parser.add_argument("--config", required=True)
    enrich_parser.add_argument("--cache-dir", required=True)
    enrich_parser.add_argument("--output", required=True)
    enrich_parser.set_defaults(handler=enrich)

    render_parser = subparsers.add_parser("render")
    render_parser.add_argument("--input", required=True)
    render_parser.add_argument("--output-dir", required=True)
    render_parser.set_defaults(handler=render)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
