"""用 Chiba 线上向量模型把新 usage route 与审核后梗卡做近邻校准。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import http.client
import json
import time
import urllib.error
import urllib.request

import numpy as np

from .chiba_model_config import ChibaModelConfigError, public_model_metadata, resolve_chiba_task


class SemanticCalibrationError(RuntimeError):
    """向量校准无法可信完成。"""


def preflight_semantic_calibration(config: dict[str, Any]) -> None:
    if not config.get("enabled", False) or not config.get("required", True):
        return
    _resolve_embedding_config(config)
    _load_release(Path(str(config.get("release_dir") or "")))


def calibrate_candidates(
    candidates: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    embed: Callable[[str], list[float]] | None = None,
) -> dict[str, Any]:
    if not config.get("enabled", False):
        return {"status": "disabled", "calibrated_route_count": 0}
    required = bool(config.get("required", True))
    try:
        resolved = _resolve_embedding_config(config)
        index, vectors = _load_release(Path(str(config.get("release_dir") or "")))
        expected_model = str(index.get("embedding_model") or "")
        if expected_model != resolved["configured_model_name"]:
            raise SemanticCalibrationError(
                f"向量 release 使用 {expected_model}，当前 Chiba 任务使用 {resolved['configured_model_name']}"
            )
        embed_one = embed or _OpenAICompatibleEmbedder(resolved)
        top_k = max(1, int(config.get("top_k", 4)))
        calibrated = 0
        failures: list[dict[str, str]] = []
        for candidate in candidates:
            enrichment = candidate.get("semantic_enrichment") or {}
            draft = candidate.get("draft_card") or {}
            if enrichment.get("status") != "pending_human_review" or draft.get("classification") != "meme_candidate":
                continue
            candidate_matches: list[dict[str, Any]] = []
            for route in draft.get("usage_routes") or []:
                try:
                    query = _route_embedding_text(candidate, route)
                    query_vector = np.asarray(embed_one(query), dtype=np.float32)
                    matches = _nearest_routes(query_vector, vectors, index["items"], top_k=top_k)
                    route["semantic_calibration"] = {
                        "status": "pending_human_review",
                        "nearest_existing_routes": matches,
                        "similarity_band": _similarity_band(matches[0]["similarity"] if matches else 0.0),
                        "auto_merge": False,
                    }
                    candidate_matches.extend(matches)
                    calibrated += 1
                except (SemanticCalibrationError, ValueError, TypeError) as exc:
                    failures.append({"candidate_id": str(candidate.get("candidate_id")), "error": str(exc)})
                    route["semantic_calibration"] = {"status": "error", "error": str(exc), "auto_merge": False}
                    if required:
                        raise
            candidate["semantic_calibration"] = {
                "status": "pending_human_review",
                "nearest_existing_routes": _deduplicate_matches(candidate_matches, top_k),
                "auto_merge": False,
            }
        return {
            "status": "ok" if not failures else "partial",
            "model": public_model_metadata(resolved),
            "release_id": index.get("release_id"),
            "dimension": int(vectors.shape[1]),
            "calibrated_route_count": calibrated,
            "failures": failures,
        }
    except (ChibaModelConfigError, OSError, KeyError, json.JSONDecodeError) as exc:
        if required:
            raise SemanticCalibrationError(str(exc)) from exc
        return {"status": "error", "error": str(exc), "calibrated_route_count": 0}


def _resolve_embedding_config(config: dict[str, Any]) -> dict[str, Any]:
    path = str(config.get("chiba_model_config_path") or "").strip()
    if not path:
        raise SemanticCalibrationError("向量校准缺少 chiba_model_config_path")
    task = str(config.get("chiba_embedding_task") or "embedding").strip()
    try:
        resolved = resolve_chiba_task(path, task)
    except ChibaModelConfigError as exc:
        raise SemanticCalibrationError(str(exc)) from exc
    resolved["minimum_interval_seconds"] = float(config.get("minimum_interval_seconds", 0.2))
    return resolved


def _load_release(release_dir: Path) -> tuple[dict[str, Any], np.ndarray]:
    if not release_dir.is_dir():
        raise SemanticCalibrationError(f"向量 release 目录不存在: {release_dir}")
    index = json.loads((release_dir / "vector_index.json").read_text(encoding="utf-8"))
    vectors = np.load(release_dir / "vectors.npy", allow_pickle=False)
    if vectors.ndim != 2 or vectors.shape[0] != len(index.get("items") or []):
        raise SemanticCalibrationError("向量 release 的索引行数与 vectors.npy 不一致")
    if int(index.get("dimension") or 0) != int(vectors.shape[1]):
        raise SemanticCalibrationError("向量 release 的维度元数据与文件不一致")
    return index, np.asarray(vectors, dtype=np.float32)


class _OpenAICompatibleEmbedder:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.last_request_at = 0.0

    def __call__(self, text: str) -> list[float]:
        extra = dict(self.config.get("extra_params") or {})
        endpoint = str(extra.pop("embedding_endpoint", "/embeddings") or "/embeddings")
        input_format = str(extra.pop("embedding_input_format", "") or "")
        extra.pop("dimensions", None)
        extra.pop("output_dimensionality", None)
        value: Any = (
            [{"type": "text", "text": text}]
            if input_format in {"ark_multimodal_text", "multimodal_text"}
            else text
        )
        body = {"model": self.config["model"], "input": value, **extra}
        wait = self.config["minimum_interval_seconds"] - (time.monotonic() - self.last_request_at)
        if wait > 0:
            time.sleep(wait)
        headers = {"Content-Type": "application/json", **self.config.get("default_headers", {})}
        if self.config.get("auth_type") != "none":
            prefix = str(self.config.get("auth_header_prefix") or "").strip()
            headers[str(self.config.get("auth_header_name") or "Authorization")] = (
                f"{prefix} {self.config['api_key']}".strip()
            )
        request = urllib.request.Request(
            self.config["base_url"] + (endpoint if endpoint.startswith("/") else f"/{endpoint}"),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            self.last_request_at = time.monotonic()
            with urllib.request.urlopen(request, timeout=self.config["timeout_seconds"]) as response:
                payload = json.loads(response.read(8 * 1024 * 1024).decode("utf-8"))
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            TimeoutError,
            OSError,
            json.JSONDecodeError,
        ) as exc:
            raise SemanticCalibrationError(f"向量模型请求失败: {exc}") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict):
            vector = data.get("embedding")
        elif isinstance(data, list) and data and isinstance(data[0], dict):
            vector = data[0].get("embedding")
        else:
            vector = payload.get("embedding") if isinstance(payload, dict) else None
        if not isinstance(vector, list) or not vector:
            raise SemanticCalibrationError("向量模型响应缺少 embedding")
        return [float(item) for item in vector]


def _route_embedding_text(candidate: dict[str, Any], route: dict[str, Any]) -> str:
    return (
        f"表达：{candidate.get('phrase')}\n"
        f"场景：{route.get('when')}\n"
        f"交流意图：{route.get('communicative_intent')}\n"
        f"回应作用：{route.get('response_function')}\n"
        f"语义：{(candidate.get('draft_card') or {}).get('semantic_core')}"
    )


def _nearest_routes(
    query: np.ndarray,
    vectors: np.ndarray,
    items: list[dict[str, Any]],
    *,
    top_k: int,
) -> list[dict[str, Any]]:
    if query.ndim != 1 or query.shape[0] != vectors.shape[1]:
        raise SemanticCalibrationError(f"查询向量维度 {query.shape} 与 release 维度 {vectors.shape[1]} 不匹配")
    norm = float(np.linalg.norm(query))
    if not np.isfinite(norm) or norm <= 0:
        raise SemanticCalibrationError("查询向量不可用")
    scores = vectors @ (query / norm)
    matches: list[dict[str, Any]] = []
    seen_routes: set[tuple[str, int]] = set()
    for row in np.argsort(scores)[::-1].tolist():
        item = items[row]
        key = (str(item.get("card_id")), int(item.get("route_index") or 0))
        if key in seen_routes:
            continue
        seen_routes.add(key)
        matches.append(
            {
                "card_id": key[0],
                "canonical_expression": item.get("canonical_expression"),
                "route_index": key[1],
                "route_tag": item.get("route_tag"),
                "serving_scope": item.get("serving_scope"),
                "similarity": round(float(scores[row]), 4),
                "best_anchor_kind": item.get("anchor_kind"),
                "best_anchor_text": item.get("anchor_text"),
            }
        )
        if len(matches) >= top_k:
            break
    return matches


def _deduplicate_matches(matches: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
    best: dict[tuple[str, int], dict[str, Any]] = {}
    for item in matches:
        key = (str(item["card_id"]), int(item["route_index"]))
        if key not in best or float(item["similarity"]) > float(best[key]["similarity"]):
            best[key] = item
    return sorted(best.values(), key=lambda item: -float(item["similarity"]))[:top_k]


def _similarity_band(score: float) -> str:
    if score >= 0.86:
        return "high_similarity_review_existing_card"
    if score >= 0.72:
        return "medium_similarity_review_overlap"
    return "low_similarity_review_novelty"
