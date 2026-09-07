#!/usr/bin/env python3
"""短时抽样配置中的直播间弹幕，匿名化后归档并写入发现流水线 inbox。"""

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

from meme_discovery.live_sampler import LiveSamplingError, run_live_sampling  # noqa: E402


def _load_config(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("配置根节点必须是 JSON 对象")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=REPO_ROOT / "ops" / "live_sampling.example.json",
        help="直播抽样 JSON 配置",
    )
    args = parser.parse_args()
    config = _load_config(args.config.expanduser().resolve())
    output_root = Path(os.path.expandvars(str(config.get("output_root") or "out/p0-meme-discovery"))).expanduser()
    if not output_root.is_absolute():
        output_root = REPO_ROOT / output_root
    output_root.mkdir(parents=True, exist_ok=True)
    with (output_root / ".live-sampler.lock").open("w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("已有直播间抽样任务正在运行，本次跳过。", file=sys.stderr)
            return 3
        try:
            result = run_live_sampling(config, repo_root=REPO_ROOT)
        except (LiveSamplingError, OSError, ValueError) as exc:
            print(f"直播间抽样失败：{exc}", file=sys.stderr)
            return 4
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
