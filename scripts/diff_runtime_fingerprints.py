#!/usr/bin/env python3
"""严格比较两个 Chiba 梗插件运行指纹。"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from typing import Any, Mapping

import json


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"指纹必须是 JSON 对象: {path}")
    return payload


def _diff(left: Any, right: Any, path: str = "compatibility") -> list[dict[str, Any]]:
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        result: list[dict[str, Any]] = []
        all_keys = sorted(set(left) | set(right), key=str)
        for key in all_keys:
            child_path = f"{path}.{key}"
            if key not in left:
                result.append(
                    {"path": child_path, "left": "<missing>", "right": right[key]}
                )
                continue
            if key not in right:
                result.append(
                    {"path": child_path, "left": left[key], "right": "<missing>"}
                )
                continue
            result.extend(_diff(left[key], right[key], child_path))
        return result
    if isinstance(left, list) and isinstance(right, list):
        if left == right:
            return []
        return [{"path": path, "left": left, "right": right}]
    if left != right:
        return [{"path": path, "left": left, "right": right}]
    return []


def _parse_args() -> Any:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    left = _load(args.left)
    right = _load(args.right)
    differences = _diff(left.get("compatibility"), right.get("compatibility"))
    result = {
        "match": not differences,
        "left_environment": left.get("environment"),
        "right_environment": right.get("environment"),
        "left_sha256": left.get("compatibility_sha256"),
        "right_sha256": right.get("compatibility_sha256"),
        "difference_count": len(differences),
        "differences": differences,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not differences else 1


if __name__ == "__main__":
    raise SystemExit(main())
