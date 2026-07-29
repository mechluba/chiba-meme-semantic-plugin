#!/usr/bin/env python3
"""从审核后梗包生成多语义原型向量 release。"""

from __future__ import annotations

from argparse import ArgumentParser
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping

import asyncio
import json
import os
import sys

import numpy as np


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CHIBA_ROOT = PLUGIN_ROOT.parents[1]
if str(CHIBA_ROOT) not in sys.path:
    sys.path.insert(0, str(CHIBA_ROOT))

from src.services.embedding_service import EmbeddingServiceClient  # noqa: E402


def _load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"JSON 顶层必须是对象: {path}")
    return payload


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _anchor_texts(
    *,
    card: Mapping[str, Any],
    route: Mapping[str, Any],
) -> list[tuple[str, str]]:
    semantic_core = str(card["semantic_core"])
    when = str(route["when"])
    intent = str(route["communicative_intent"])
    facets = "；".join(
        str(value) for value in (card.get("retrieval_facets") or [])
    )
    anchors = [
        ("when", f"当前对话场景：{when}"),
        ("intent", f"当前适合的回应意图：{intent}"),
        ("semantic", f"当前对话的语义：{semantic_core}"),
        (
            "compact",
            f"场景：{when}\n回应意图：{intent}\n语义：{semantic_core}",
        ),
    ]
    if facets:
        anchors.append(("facets", facets))
    return anchors


async def _build(
    *,
    source_dir: Path,
    target_dir: Path,
    target_release_id: str,
) -> dict[str, Any]:
    if target_dir.exists():
        raise FileExistsError(f"目标 release 已存在: {target_dir}")

    source_library = _load_json_object(source_dir / "library.json")
    source_release = _load_json_object(source_dir / "release.json")
    cards = source_library.get("cards")
    if not isinstance(cards, list) or not cards:
        raise ValueError("源梗包没有 cards")

    documents: list[str] = []
    items: list[dict[str, Any]] = []
    route_count = 0
    for card in cards:
        if not isinstance(card, Mapping):
            raise TypeError("cards 中存在非对象条目")
        routes = card.get("usage_routes")
        if not isinstance(routes, list) or not routes:
            raise ValueError(f"梗卡缺少 usage_routes: {card.get('card_id')}")
        for route_index, route in enumerate(routes):
            if not isinstance(route, Mapping):
                raise TypeError("usage_routes 中存在非对象条目")
            route_count += 1
            for anchor_index, (anchor_kind, anchor_text) in enumerate(
                _anchor_texts(card=card, route=route)
            ):
                documents.append(anchor_text)
                items.append(
                    {
                        "row": len(items),
                        "card_id": str(card["card_id"]),
                        "canonical_expression": str(
                            card["canonical_expression"]
                        ),
                        "route_index": route_index,
                        "route_tag": str(route["route_tag"]),
                        "serving_scope": str(card["serving_scope"]),
                        "anchor_index": anchor_index,
                        "anchor_kind": anchor_kind,
                        "anchor_text": anchor_text,
                    }
                )

    client = EmbeddingServiceClient(
        task_name="embedding",
        request_type="meme.semantic_multiprototype_release",
    )
    embedded = await client.embed_texts(documents, max_concurrent=10)
    if len(embedded) != len(documents):
        raise RuntimeError("Embedding 返回数量与语义原型数量不一致")
    models = {str(result.model_name) for result in embedded}
    if len(models) != 1:
        raise RuntimeError(f"Embedding 模型不一致: {sorted(models)}")
    vectors = np.asarray(
        [result.embedding for result in embedded],
        dtype=np.float32,
    )
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if np.any(norms <= 0):
        raise ValueError("Embedding 服务返回零向量")
    vectors /= norms

    built_at = datetime.now().astimezone().isoformat()
    library = deepcopy(source_library)
    library["library_id"] = target_release_id
    library["built_at"] = built_at
    library["derived_from_release_id"] = str(source_release["release_id"])
    design = library.setdefault("design", {})
    if not isinstance(design, dict):
        raise TypeError("library.design 必须是对象")
    design["retrieval_unit"] = (
        "card_x_usage_route_x_semantic_prototype"
    )
    library["vector_index"] = {
        "path": "vector_index.json",
        "vectors_path": "vectors.npy",
        "strategy": "semantic_multi_prototype_v1",
        "embedding_model": next(iter(models)),
        "dimension": int(vectors.shape[1]),
        "route_count": route_count,
        "vector_count": len(items),
    }
    vector_index = {
        "schema_version": 2,
        "release_id": target_release_id,
        "strategy": "semantic_multi_prototype_v1",
        "embedding_model": next(iter(models)),
        "dimension": int(vectors.shape[1]),
        "route_count": route_count,
        "vector_count": len(items),
        "row_count": len(items),
        "items": items,
    }

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(
        prefix=f".{target_release_id}-",
        dir=target_dir.parent,
    ) as temporary:
        temporary_dir = Path(temporary)
        library_path = temporary_dir / "library.json"
        index_path = temporary_dir / "vector_index.json"
        vectors_path = temporary_dir / "vectors.npy"
        library_path.write_text(
            json.dumps(library, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        index_path.write_text(
            json.dumps(vector_index, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        np.save(vectors_path, vectors, allow_pickle=False)
        release = {
            "schema_version": 2,
            "release_id": target_release_id,
            "built_at": built_at,
            "derived_from_release_id": str(source_release["release_id"]),
            "review_status": "cards_human_reviewed_vectors_semantically_derived",
            "index_strategy": "semantic_multi_prototype_v1",
            "card_count": len(cards),
            "route_count": route_count,
            "vector_count": len(items),
            "files": {
                "library": {
                    "path": "library.json",
                    "sha256": _sha256_file(library_path),
                },
                "vector_index": {
                    "path": "vector_index.json",
                    "sha256": _sha256_file(index_path),
                },
                "vectors": {
                    "path": "vectors.npy",
                    "sha256": _sha256_file(vectors_path),
                },
            },
        }
        (temporary_dir / "release.json").write_text(
            json.dumps(release, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_dir, target_dir)

    return {
        "target": str(target_dir),
        "release_id": target_release_id,
        "embedding_model": next(iter(models)),
        "dimension": int(vectors.shape[1]),
        "card_count": len(cards),
        "route_count": route_count,
        "vector_count": len(items),
    }


def _parse_args() -> Any:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--source-release-id", required=True)
    parser.add_argument("--target-release-id", required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    releases_root = PLUGIN_ROOT / "resources" / "releases"
    source_dir = (releases_root / args.source_release_id).resolve()
    target_dir = (releases_root / args.target_release_id).resolve()
    if not source_dir.is_relative_to(releases_root.resolve()):
        raise SystemExit("源 release 越出 releases 目录")
    if not target_dir.is_relative_to(releases_root.resolve()):
        raise SystemExit("目标 release 越出 releases 目录")
    if not source_dir.is_dir():
        raise SystemExit(f"源 release 不存在: {source_dir}")
    result = asyncio.run(
        _build(
            source_dir=source_dir,
            target_dir=target_dir,
            target_release_id=str(args.target_release_id),
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
