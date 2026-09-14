#!/usr/bin/env python3
"""追加已获作者批准的公共梗，保留旧卡、索引与向量，生成独立新版梗包。"""

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

import numpy as np


async def build(args):
    sys.path.insert(0, str(args.chiba_root.resolve()))
    plugin_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(plugin_root))
    from meme_runtime import MemeRelease
    from build_multiprototype_release import _anchor_texts
    from src.services.embedding_service import EmbeddingServiceClient

    source = MemeRelease.load(args.source)
    additions = json.loads(args.cards.read_text(encoding="utf-8"))["cards"]
    if args.target.exists():
        raise FileExistsError(args.target)
    known_ids = set(source.cards_by_id)
    known_expressions = {c["canonical_expression"] for c in source.cards_by_id.values()}
    items = deepcopy(source.route_items)
    documents = []
    for card in additions:
        if card["human_review"]["status"] != "approved":
            raise ValueError("只接受明确审核通过的梗卡")
        if card["card_id"] in known_ids or card["canonical_expression"] in known_expressions:
            raise ValueError("新增梗重复")
        known_ids.add(card["card_id"])
        known_expressions.add(card["canonical_expression"])
        for route_index, route in enumerate(card["usage_routes"]):
            for anchor_index, (kind, anchor) in enumerate(_anchor_texts(card=card, route=route)):
                documents.append(anchor)
                items.append(dict(row=len(items), card_id=card["card_id"],
                                  canonical_expression=card["canonical_expression"],
                                  route_index=route_index, route_tag=route["route_tag"],
                                  serving_scope=card["serving_scope"], anchor_index=anchor_index,
                                  anchor_kind=kind, anchor_text=anchor))
    results = await EmbeddingServiceClient(
        task_name="embedding", request_type="meme.user_approved_release"
    ).embed_texts(documents, max_concurrent=3)
    if len(results) != len(documents) or {r.model_name for r in results} != {source.embedding_model}:
        raise ValueError("新增向量数量或模型与线上梗包不一致")
    vectors = np.asarray([r.embedding for r in results], dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if vectors.shape[1] != source.dimension or not np.isfinite(vectors).all() or np.any(norms <= 0):
        raise ValueError("新增向量维度或数值不合法")
    # 使用文件中的原始旧向量，避免对旧行二次归一化产生浮点变化。
    old_vectors = np.load(args.source / source.release_manifest["files"]["vectors"]["path"], allow_pickle=False)
    vectors = np.concatenate([old_vectors, vectors / norms])
    release_id = args.target.name
    built_at = datetime.now(timezone.utc).isoformat()
    library = deepcopy(source.library)
    library["cards"].extend(additions)
    route_count = sum(len(c["usage_routes"]) for c in library["cards"])
    library.update(library_id=release_id, built_at=built_at, derived_from_release_id=source.release_id,
                   card_count=len(library["cards"]))
    library["vector_index"].update(route_count=route_count, vector_count=len(items))
    index = deepcopy(source.vector_index)
    index.update(release_id=release_id, route_count=route_count, vector_count=len(items), row_count=len(items), items=items)
    manifest = deepcopy(source.release_manifest)
    manifest.update(release_id=release_id, built_at=built_at, derived_from_release_id=source.release_id,
                    card_count=len(library["cards"]), route_count=route_count, vector_count=len(items))
    args.target.mkdir(parents=True)
    for filename, value in [("library.json", library), ("vector_index.json", index)]:
        (args.target / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    np.save(args.target / "vectors.npy", vectors, allow_pickle=False)
    for entry in manifest["files"].values():
        entry["sha256"] = sha256((args.target / entry["path"]).read_bytes()).hexdigest()
    (args.target / "release.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result = MemeRelease.load(args.target)
    assert result.library["cards"][:len(source.cards_by_id)] == source.library["cards"]
    assert result.route_items[:source.vector_count] == source.route_items
    assert np.array_equal(np.load(args.target / "vectors.npy")[:source.vector_count], old_vectors)
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("chiba-root", "source", "cards", "target"):
        parser.add_argument("--" + name, type=Path, required=True)
    asyncio.run(build(parser.parse_args()))
