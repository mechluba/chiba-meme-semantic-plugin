#!/usr/bin/env python3
"""生成 Chiba 梗插件在某个运行环境中的可比较指纹。"""

from __future__ import annotations

from argparse import ArgumentParser
from datetime import datetime
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Iterable, Mapping

import json
import platform
import subprocess
import sys
import tomllib


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PARENT = PLUGIN_ROOT.parent
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from chiba_meme_semantic_plugin.meme_runtime import MemeRelease  # noqa: E402
from chiba_meme_semantic_plugin.plugin import (  # noqa: E402
    DEFAULT_RELEASE_ID,
    MemeSemanticPlugin,
)


RUNTIME_EXCLUDED_PARTS = frozenset(
    {".git", ".pytest_cache", ".ruff_cache", "__pycache__", "tests", "scripts"}
)
RUNTIME_TOP_LEVEL_FILES = frozenset(
    {"_manifest.json", "meme_runtime.py", "plugin.py"}
)
SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
)
MODEL_TASK_NAMES = ("planner", "replyer")
BOT_KEYS = ("nickname",)
PERSONALITY_KEYS = (
    "personality",
    "reply_style",
    "multiple_reply_style",
    "multiple_probability",
)
CHAT_KEYS = (
    "max_context_size",
    "max_private_context_size",
    "enable_context_optimization",
    "mid_term_memory",
    "mid_term_memory_lenth",
    "enable_reply_quote",
    "private_chat_prompts",
    "chat_prompts",
)
EXPERIMENTAL_KEYS = (
    "enable_fast_reply_path",
    "enable_fast_reply_lightweight_planner_injection",
    "enable_fast_reply_compact_planner_tools",
    "fast_reply_speculative_wait_timeout_ms",
    "fast_reply_speculative_model_task_name",
    "galpet_heroine_id",
)
VISUAL_KEYS = ("planner_mode", "replyer_mode", "max_image_num")


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def _git_state(root: Path) -> dict[str, Any]:
    top_level = _run_git(root, "rev-parse", "--show-toplevel")
    if not top_level or Path(top_level).resolve() != root.resolve():
        return {
            "is_repository": False,
            "commit": "",
            "branch": "",
            "origin": "",
            "dirty": False,
            "status_line_count": 0,
        }
    commit = _run_git(root, "rev-parse", "HEAD")
    status = _run_git(root, "status", "--porcelain")
    return {
        "is_repository": bool(commit),
        "commit": commit,
        "branch": _run_git(root, "branch", "--show-current"),
        "origin": _run_git(root, "remote", "get-url", "origin"),
        "dirty": bool(status),
        "status_line_count": len(status.splitlines()) if status else 0,
    }


def _iter_runtime_files(plugin_root: Path) -> Iterable[Path]:
    for path in sorted(plugin_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(plugin_root)
        if any(part in RUNTIME_EXCLUDED_PARTS for part in relative.parts):
            continue
        if (
            len(relative.parts) == 1
            and relative.name not in RUNTIME_TOP_LEVEL_FILES
        ):
            continue
        if len(relative.parts) > 1 and relative.parts[0] != "resources":
            continue
        if relative.name == "config.toml":
            continue
        yield path


def _runtime_tree(plugin_root: Path) -> dict[str, Any]:
    files: dict[str, str] = {}
    digest = sha256()
    for path in _iter_runtime_files(plugin_root):
        relative = path.relative_to(plugin_root).as_posix()
        file_hash = _sha256_file(path)
        files[relative] = file_hash
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
    return {
        "sha256": digest.hexdigest(),
        "file_count": len(files),
        "files": files,
    }


def _safe_mapping(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_item in value.items():
            key = str(raw_key)
            lowered = key.lower()
            if any(part in lowered for part in SENSITIVE_KEY_PARTS):
                continue
            result[key] = _safe_mapping(raw_item)
        return result
    if isinstance(value, list):
        return [_safe_mapping(item) for item in value]
    return value


def _pick(mapping: Mapping[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    return {
        key: _safe_mapping(mapping[key])
        for key in keys
        if key in mapping
    }


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with path.open("rb") as file:
        payload = tomllib.load(file)
    if not isinstance(payload, dict):
        raise TypeError(f"TOML 顶层必须是对象: {path}")
    return payload


def _plugin_config(plugin_root: Path) -> dict[str, Any]:
    config_path = plugin_root / "config.toml"
    raw = _load_toml(config_path) if config_path.is_file() else {}
    plugin = MemeSemanticPlugin()
    normalized, _ = plugin.normalize_plugin_config(raw)
    return {
        "source": "config.toml" if config_path.is_file() else "schema_defaults",
        "value": _safe_mapping(normalized),
    }


def _model_routing(
    model_config: Mapping[str, Any],
    plugin_config: Mapping[str, Any],
) -> dict[str, Any]:
    task_config = model_config.get("model_task_config")
    if not isinstance(task_config, Mapping):
        raise ValueError("model_config.toml 缺少 model_task_config")

    serving = plugin_config.get("serving")
    observability = plugin_config.get("observability")
    embedding_task = (
        str(serving.get("embedding_task_name") or "embedding")
        if isinstance(serving, Mapping)
        else "embedding"
    )
    selector_task = (
        str(serving.get("semantic_selector_task_name") or "utils")
        if isinstance(serving, Mapping)
        else "utils"
    )
    evaluator_task = (
        str(observability.get("evaluator_task_name") or "utils")
        if isinstance(observability, Mapping)
        else "utils"
    )
    wanted_tasks = tuple(
        dict.fromkeys(
            (*MODEL_TASK_NAMES, embedding_task, selector_task, evaluator_task)
        )
    )
    tasks: dict[str, Any] = {}
    referenced_model_names: set[str] = set()
    for task_name in wanted_tasks:
        task = task_config.get(task_name)
        if not isinstance(task, Mapping):
            raise ValueError(f"model_config.toml 缺少模型任务: {task_name}")
        safe_task = _safe_mapping(task)
        tasks[task_name] = safe_task
        model_list = task.get("model_list")
        if isinstance(model_list, list):
            referenced_model_names.update(str(item) for item in model_list)

    models: dict[str, Any] = {}
    for raw_model in model_config.get("models", []):
        if not isinstance(raw_model, Mapping):
            continue
        model_name = str(raw_model.get("name") or "")
        if model_name in referenced_model_names:
            models[model_name] = _safe_mapping(raw_model)
    missing_models = sorted(referenced_model_names - set(models))
    if missing_models:
        raise ValueError(f"模型任务引用了未定义模型: {missing_models}")

    referenced_providers = {
        str(model.get("api_provider") or "")
        for model in models.values()
        if isinstance(model, Mapping)
    }
    providers: dict[str, Any] = {}
    for raw_provider in model_config.get("api_providers", []):
        if not isinstance(raw_provider, Mapping):
            continue
        provider_name = str(raw_provider.get("name") or "")
        if provider_name in referenced_providers:
            providers[provider_name] = _safe_mapping(raw_provider)

    return {
        "tasks": tasks,
        "models": models,
        "providers": providers,
    }


def _bot_behavior(bot_config: Mapping[str, Any]) -> dict[str, Any]:
    bot = bot_config.get("bot")
    personality = bot_config.get("personality")
    chat = bot_config.get("chat")
    experimental = bot_config.get("experimental")
    visual = bot_config.get("visual")
    return {
        "bot": _pick(bot, BOT_KEYS) if isinstance(bot, Mapping) else {},
        "personality": (
            _pick(personality, PERSONALITY_KEYS)
            if isinstance(personality, Mapping)
            else {}
        ),
        "chat": _pick(chat, CHAT_KEYS) if isinstance(chat, Mapping) else {},
        "experimental": (
            _pick(experimental, EXPERIMENTAL_KEYS)
            if isinstance(experimental, Mapping)
            else {}
        ),
        "visual": _pick(visual, VISUAL_KEYS) if isinstance(visual, Mapping) else {},
    }


def _package_version(package_name: str) -> str:
    try:
        return version(package_name)
    except PackageNotFoundError:
        return ""


def build_fingerprint(
    *,
    chiba_root: Path,
    plugin_root: Path,
    environment: str,
    release_id: str,
) -> dict[str, Any]:
    resolved_chiba = chiba_root.resolve()
    resolved_plugin = plugin_root.resolve()
    plugin_config_record = _plugin_config(resolved_plugin)
    plugin_config = plugin_config_record["value"]
    release = MemeRelease.load(
        resolved_plugin / "resources" / "releases" / release_id
    )
    manifest = json.loads(
        (resolved_plugin / "_manifest.json").read_text(encoding="utf-8")
    )
    model_config = _load_toml(resolved_chiba / "config" / "model_config.toml")
    bot_config = _load_toml(resolved_chiba / "config" / "bot_config.toml")
    chiba_git = _git_state(resolved_chiba)
    plugin_git = _git_state(resolved_plugin)

    compatibility = {
        "chiba": {
            "commit": chiba_git["commit"],
            "origin": chiba_git["origin"],
        },
        "plugin": {
            "id": manifest.get("id"),
            "version": manifest.get("version"),
            "runtime_tree": _runtime_tree(resolved_plugin),
            "config": plugin_config,
        },
        "release": release.fingerprint(),
        "model_routing": _model_routing(model_config, plugin_config),
        "bot_behavior": _bot_behavior(bot_config),
        "runtime": {
            "python": platform.python_version(),
            "maibot_sdk": _package_version("maibot-sdk"),
            "numpy": _package_version("numpy"),
        },
    }
    comparable_json = json.dumps(
        compatibility,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "schema_version": 1,
        "environment": environment,
        "captured_at": datetime.now().astimezone().isoformat(),
        "compatibility_sha256": sha256(
            comparable_json.encode("utf-8")
        ).hexdigest(),
        "compatibility": compatibility,
        "diagnostics": {
            "paths": {
                "chiba_root": str(resolved_chiba),
                "plugin_root": str(resolved_plugin),
            },
            "chiba_git": chiba_git,
            "plugin_git": plugin_git,
            "plugin_config_source": plugin_config_record["source"],
        },
    }


def _parse_args() -> Any:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--chiba-root", type=Path, required=True)
    parser.add_argument("--plugin-root", type=Path, default=PLUGIN_ROOT)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--release-id", default=DEFAULT_RELEASE_ID)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--require-clean",
        action="store_true",
        help="Chiba 和已初始化的插件仓库都必须干净",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    payload = build_fingerprint(
        chiba_root=args.chiba_root,
        plugin_root=args.plugin_root,
        environment=str(args.environment),
        release_id=str(args.release_id),
    )
    if args.require_clean:
        diagnostics = payload["diagnostics"]
        chiba_git = diagnostics["chiba_git"]
        plugin_git = diagnostics["plugin_git"]
        clean = (
            chiba_git["is_repository"]
            and not chiba_git["dirty"]
            and (
                not plugin_git["is_repository"]
                or not plugin_git["dirty"]
            )
        )
        if not clean:
            print(
                json.dumps(
                    {
                        "success": False,
                        "reason": "运行源代码不是干净仓库状态",
                        "chiba_git": chiba_git,
                        "plugin_git": plugin_git,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                file=sys.stderr,
            )
            return 2

    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "success": True,
                    "output": str(output),
                    "environment": payload["environment"],
                    "compatibility_sha256": payload["compatibility_sha256"],
                },
                ensure_ascii=False,
            )
        )
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
