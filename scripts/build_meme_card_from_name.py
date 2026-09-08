#!/usr/bin/env python3
"""读取人工输入的梗名，联网检索后用 LLM 生成本地待审核梗卡。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from meme_discovery.manual_intake import run_manual_intake  # noqa: E402
from meme_discovery.semantic_enricher import SemanticEnrichmentError  # noqa: E402


def _load_config(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("配置根节点必须是 JSON 对象")
    if value.get("schema_version") != 1:
        raise ValueError("只支持 schema_version=1 的发现配置")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--name",
        action="append",
        required=True,
        help="梗名；可重复传入多次。每次输入独立生成候选，不去重或合并别名",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=REPO_ROOT / "ops" / "p0_discovery.example.json",
        help="复用定时发现任务的联网检索和语义模型配置",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        help="覆盖本地输出根目录；实际结果写入其 manual-runs/<UTC时间>/",
    )
    args = parser.parse_args()

    try:
        config = _load_config(args.config.expanduser().resolve())
        output_root = args.output_root.expanduser().resolve() if args.output_root else None
        result = run_manual_intake(
            args.name,
            config,
            repo_root=REPO_ROOT,
            output_root=output_root,
        )
    except (OSError, TypeError, ValueError, SemanticEnrichmentError) as exc:
        print(f"人工梗名接入失败：{exc}", file=sys.stderr)
        return 4
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
