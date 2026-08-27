#!/usr/bin/env python3
"""运行一次 P0 热梗候选发现，并把结果留在本地待人工审核。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import argparse
import fcntl
import json
import os
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from meme_discovery import run_discovery  # noqa: E402
from meme_discovery.semantic_calibrator import SemanticCalibrationError  # noqa: E402
from meme_discovery.semantic_enricher import SemanticEnrichmentError  # noqa: E402


def _load_config(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("配置根节点必须是 JSON 对象")
    if value.get("schema_version") != 1:
        raise ValueError("只支持 schema_version=1 的发现配置")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=REPO_ROOT / "ops" / "p0_discovery.example.json",
        help="本地 JSON 配置；默认使用仓库中的低频安全示例配置",
    )
    parser.add_argument(
        "--collection-only",
        action="store_true",
        help="只采集、聚合并生成表层待审信号；显式标记语义未运行，不调用在线模型",
    )
    args = parser.parse_args()
    config_path = args.config.expanduser().resolve()
    config = _load_config(config_path)
    if args.collection_only:
        config["semantic_enrichment"] = {"enabled": False, "required": False}
    output_root = Path(os.path.expandvars(str(config.get("output_root") or "out/p0-meme-discovery")))
    if not output_root.is_absolute():
        output_root = REPO_ROOT / output_root
    output_root.mkdir(parents=True, exist_ok=True)
    lock_path = output_root / ".scheduler.lock"
    with lock_path.open("w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("已有热梗发现任务正在运行，本次跳过。", file=sys.stderr)
            return 3
        try:
            result = run_discovery(config, repo_root=REPO_ROOT)
        except (SemanticEnrichmentError, SemanticCalibrationError) as exc:
            print(f"语义流程失败：{exc}", file=sys.stderr)
            return 4
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
