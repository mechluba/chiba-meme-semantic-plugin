#!/usr/bin/env python3
"""启动真实插件运行时，并批量执行达妮娅 Planner / Replyer 测试场景。"""

from __future__ import annotations

from argparse import ArgumentParser
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

import asyncio
import json
import sys


PLUGIN_ID = "chiba.meme-semantic-plugin"
DEFAULT_FLOW_RUNNER = (
    Path.home()
    / ".codex"
    / "skills"
    / "denia-online-dialogue"
    / "scripts"
    / "run_denia_flow.py"
)


def _load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"JSON 顶层必须是对象: {path}")
    return payload


def _load_flow_runner(path: Path) -> Any:
    spec = spec_from_file_location("denia_online_dialogue_probe_runner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载达妮娅真实链路 runner: {path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _normalize_cases(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("批量 spec 必须包含非空 cases 数组")
    cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise TypeError("cases 中每一项都必须是对象")
        case = dict(raw_case)
        scene_id = str(case.get("scene_id") or "").strip()
        if not scene_id or scene_id in seen_ids:
            raise ValueError(f"scene_id 为空或重复: {scene_id!r}")
        if str(case.get("mode") or "") != "reply":
            raise ValueError("当前批量 probe 只接受 mode=reply")
        if not str(case.get("user_text") or "").strip():
            raise ValueError(f"场景缺少 user_text: {scene_id}")
        case["variants"] = int(case.get("variants") or 1)
        case["bubble_wait_seconds"] = float(
            case.get("bubble_wait_seconds") or 8
        )
        seen_ids.add(scene_id)
        cases.append(case)
    return cases


async def _run(
    *,
    chiba_root: Path,
    flow_runner_path: Path,
    cases: list[dict[str, Any]],
    observer_wait_seconds: float,
) -> dict[str, Any]:
    if str(chiba_root) not in sys.path:
        sys.path.insert(0, str(chiba_root))
    flow_runner = _load_flow_runner(flow_runner_path)

    from src.plugin_runtime.integration import get_plugin_runtime_manager

    runtime_manager = get_plugin_runtime_manager()
    await runtime_manager.start()
    if not runtime_manager.is_running:
        raise RuntimeError("插件运行时没有成功启动")
    loaded_plugin_ids = sorted(
        {
            plugin_id
            for supervisor in runtime_manager.supervisors
            for plugin_id in supervisor.get_loaded_plugin_ids()
        }
    )
    if PLUGIN_ID not in loaded_plugin_ids:
        await runtime_manager.stop()
        raise RuntimeError(
            f"语义梗插件没有加载，当前插件: {loaded_plugin_ids}"
        )

    results: list[dict[str, Any]] = []
    try:
        for case in cases:
            case_results = await flow_runner._run(case)
            results.append(
                {
                    "scene_id": case["scene_id"],
                    "user_text": case["user_text"],
                    "expectation": case.get("expectation"),
                    "variants": case_results,
                }
            )
        if observer_wait_seconds > 0:
            await asyncio.sleep(observer_wait_seconds)
    finally:
        await runtime_manager.stop()

    return {
        "schema_version": 1,
        "plugin_id": PLUGIN_ID,
        "loaded_plugin_ids": loaded_plugin_ids,
        "cases": results,
    }


def _parse_args() -> Any:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--chiba-root", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--flow-runner", type=Path, default=DEFAULT_FLOW_RUNNER)
    parser.add_argument("--observer-wait-seconds", type=float, default=18)
    parser.add_argument(
        "--scene-id",
        action="append",
        default=[],
        help="只运行指定 scene_id；可重复传入",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    chiba_root = args.chiba_root.resolve()
    if Path.cwd().resolve() != chiba_root:
        raise SystemExit(f"请从 Chiba 根目录运行: {chiba_root}")
    payload = _load_json_object(args.spec)
    cases = _normalize_cases(payload)
    selected_scene_ids = {
        str(scene_id).strip()
        for scene_id in args.scene_id
        if str(scene_id).strip()
    }
    if selected_scene_ids:
        cases = [
            case for case in cases if case["scene_id"] in selected_scene_ids
        ]
        missing = selected_scene_ids - {
            str(case["scene_id"]) for case in cases
        }
        if missing:
            raise SystemExit(f"找不到 scene_id: {sorted(missing)}")
    result = asyncio.run(
        _run(
            chiba_root=chiba_root,
            flow_runner_path=args.flow_runner.resolve(),
            cases=cases,
            observer_wait_seconds=max(0.0, args.observer_wait_seconds),
        )
    )
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
