"""只读解析 Chiba 的模型任务配置，供离线梗库流水线复用。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import os
import tomllib


class ChibaModelConfigError(RuntimeError):
    """Chiba 模型配置缺失或无法安全解析。"""


def resolve_chiba_task(config_path: str | Path, task_name: str) -> dict[str, Any]:
    """解析任务的首选模型与 Provider；返回值仅应在进程内使用。"""
    path = Path(os.path.expandvars(str(config_path))).expanduser().resolve()
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ChibaModelConfigError(f"无法读取 Chiba 模型配置: {path}") from exc

    task = (document.get("model_task_config") or {}).get(task_name)
    if not isinstance(task, dict):
        raise ChibaModelConfigError(f"Chiba 模型配置中没有任务 {task_name}")
    model_names = task.get("model_list") or []
    if not isinstance(model_names, list) or not model_names:
        raise ChibaModelConfigError(f"Chiba 任务 {task_name} 没有配置模型")
    configured_model_name = str(model_names[0]).strip()

    models = {
        str(item.get("name") or "").strip(): item
        for item in document.get("models") or []
        if isinstance(item, dict)
    }
    model = models.get(configured_model_name)
    if not model:
        raise ChibaModelConfigError(f"Chiba 任务 {task_name} 引用了不存在的模型")
    provider_name = str(model.get("api_provider") or "").strip()
    providers = {
        str(item.get("name") or "").strip(): item
        for item in document.get("api_providers") or []
        if isinstance(item, dict)
    }
    provider = providers.get(provider_name)
    if not provider:
        raise ChibaModelConfigError(f"Chiba 模型 {configured_model_name} 引用了不存在的 Provider")

    api_key_env = str(provider.get("api_key_env") or "").strip()
    api_key = str(os.environ.get(api_key_env) or provider.get("api_key") or "").strip()
    if api_key in {"your-api-key", "..."}:
        api_key = ""
    auth_type = str(provider.get("auth_type") or "bearer").strip()
    if auth_type != "none" and not api_key:
        source = f"环境变量 {api_key_env}" if api_key_env else "Provider 密钥"
        raise ChibaModelConfigError(f"Chiba 任务 {task_name} 缺少可用的{source}")

    return {
        "config_path": str(path),
        "task_name": task_name,
        "configured_model_name": configured_model_name,
        "model": str(model.get("model_identifier") or configured_model_name).strip(),
        "provider_name": provider_name,
        "base_url": str(provider.get("base_url") or "").rstrip("/"),
        "api_key": api_key,
        "auth_type": auth_type,
        "auth_header_name": str(provider.get("auth_header_name") or "Authorization"),
        "auth_header_prefix": str(provider.get("auth_header_prefix") or "Bearer"),
        "default_headers": dict(provider.get("default_headers") or {}),
        "default_query": dict(provider.get("default_query") or {}),
        "extra_params": dict(model.get("extra_params") or {}),
        "temperature": float(task.get("temperature", 0.2)),
        "timeout_seconds": float(task.get("hard_timeout") or provider.get("timeout") or 120),
        "max_retries": int(provider.get("max_retry", 2)),
    }


def public_model_metadata(resolved: dict[str, Any]) -> dict[str, Any]:
    """生成不会泄漏凭据与配置文件路径的运行报告。"""
    return {
        "task": resolved.get("task_name"),
        "configured_model": resolved.get("configured_model_name"),
        "model_identifier": resolved.get("model"),
        "provider": resolved.get("provider_name"),
    }
