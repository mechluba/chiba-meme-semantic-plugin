"""审核后梗包的加载、校验、向量召回与 Planner/Reply 载荷构造。"""

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import json
import time

import numpy as np


MEME_ACTION_ARG = "meme_action"
MEME_CARD_ID_ARG = "meme_card_id"
MEME_RELEASE_ID_ARG = "meme_release_id"
MEME_ROUTE_INDEX_ARG = "meme_route_index"
MEME_ALLOWED_ACTIONS = frozenset({"USE", "UNDERSTAND_ONLY", "SKIP"})


class MemeReleaseError(ValueError):
    """梗包内容、索引或文件指纹不合法。"""


@dataclass(frozen=True)
class MemeToolSelection:
    """Planner 通过 reply 工具传递的梗语义决策。"""

    action: str
    release_id: str
    card_id: str
    route_index: int


@dataclass(frozen=True)
class SemanticSelectorDecision:
    """独立语义裁判的结构化判定。"""

    requested_action: str
    selection: MemeToolSelection
    confidence: float
    matched_hard_blocks: Tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class RetrievedMemeCandidate:
    """一次 route 级语义召回结果。"""

    card_id: str
    canonical_expression: str
    semantic_core: str
    culture_scope: str
    serving_scope: str
    route_index: int
    route_tag: str
    when: str
    communicative_intent: str
    allowed_realizations: Tuple[str, ...]
    required_context_signals: Tuple[str, ...]
    hard_blocks: Tuple[str, ...]
    similarity: float

    def to_planner_payload(self, *, understand_only: bool) -> Dict[str, Any]:
        """生成只包含 Planner 做语义判断所需信息的紧凑载荷。"""

        allowed_actions = (
            ["UNDERSTAND_ONLY", "SKIP"]
            if understand_only
            else ["USE", "UNDERSTAND_ONLY", "SKIP"]
        )
        return {
            "card_id": self.card_id,
            "canonical_expression": self.canonical_expression,
            "semantic_core": self.semantic_core,
            "culture_scope": self.culture_scope,
            "serving_scope": self.serving_scope,
            "route_index": self.route_index,
            "route": {
                "route_tag": self.route_tag,
                "when": self.when,
                "communicative_intent": self.communicative_intent,
            },
            "required_context_signals": list(self.required_context_signals),
            "hard_blocks": list(self.hard_blocks),
            "allowed_actions": allowed_actions,
            "similarity": round(self.similarity, 6),
        }


class MemeRelease:
    """内存中的不可变审核后梗包。"""

    def __init__(
        self,
        *,
        release_dir: Path,
        release_manifest: Dict[str, Any],
        library: Dict[str, Any],
        vector_index: Dict[str, Any],
        vectors: np.ndarray,
        file_hashes: Dict[str, str],
    ) -> None:
        self.release_dir = release_dir
        self.release_manifest = release_manifest
        self.library = library
        self.vector_index = vector_index
        self.release_id = str(release_manifest["release_id"])
        self.embedding_model = str(vector_index["embedding_model"])
        self.dimension = int(vector_index["dimension"])
        self.file_hashes = dict(file_hashes)
        self.cards_by_id: Dict[str, Dict[str, Any]] = {
            str(card["card_id"]): card for card in library["cards"]
        }
        self.route_items: List[Dict[str, Any]] = list(vector_index["items"])
        self.route_count = sum(
            len(card["usage_routes"]) for card in library["cards"]
        )
        self.vector_count = len(self.route_items)
        self.vectors = self._normalize_vectors(vectors)

    @classmethod
    def load(cls, release_dir: Path) -> "MemeRelease":
        """读取并完整校验一个版本化梗包。"""

        resolved_dir = release_dir.resolve()
        manifest = cls._load_json_object(resolved_dir / "release.json")
        release_id = str(manifest.get("release_id") or "").strip()
        if not release_id:
            raise MemeReleaseError("release.json 缺少 release_id")

        raw_files = manifest.get("files")
        if not isinstance(raw_files, dict):
            raise MemeReleaseError("release.json files 必须是对象")

        required_names = {"library", "vector_index", "vectors"}
        if set(raw_files) != required_names:
            raise MemeReleaseError(
                f"release.json files 必须且只能包含 {sorted(required_names)}"
            )

        resolved_files: Dict[str, Path] = {}
        file_hashes: Dict[str, str] = {}
        for logical_name, raw_entry in raw_files.items():
            if not isinstance(raw_entry, dict):
                raise MemeReleaseError(f"files.{logical_name} 必须是对象")
            relative_path = Path(str(raw_entry.get("path") or ""))
            if (
                not str(relative_path)
                or relative_path.is_absolute()
                or ".." in relative_path.parts
            ):
                raise MemeReleaseError(f"files.{logical_name}.path 必须是包内相对路径")
            file_path = (resolved_dir / relative_path).resolve()
            if not file_path.is_relative_to(resolved_dir):
                raise MemeReleaseError(f"files.{logical_name}.path 越出梗包目录")
            if not file_path.is_file():
                raise MemeReleaseError(f"梗包文件不存在: {file_path}")
            actual_hash = cls.sha256_file(file_path)
            expected_hash = str(raw_entry.get("sha256") or "").strip().lower()
            if actual_hash != expected_hash:
                raise MemeReleaseError(
                    f"梗包文件哈希不一致: {logical_name} "
                    f"expected={expected_hash} actual={actual_hash}"
                )
            resolved_files[logical_name] = file_path
            file_hashes[logical_name] = actual_hash

        library = cls._load_json_object(resolved_files["library"])
        vector_index = cls._load_json_object(resolved_files["vector_index"])
        vectors = np.load(resolved_files["vectors"], allow_pickle=False)
        cls._validate_payloads(
            release_id=release_id,
            manifest=manifest,
            library=library,
            vector_index=vector_index,
            vectors=vectors,
        )
        return cls(
            release_dir=resolved_dir,
            release_manifest=manifest,
            library=library,
            vector_index=vector_index,
            vectors=vectors,
            file_hashes=file_hashes,
        )

    @staticmethod
    def _load_json_object(path: Path) -> Dict[str, Any]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise MemeReleaseError(f"{path.name} 必须包含 JSON 对象")
        return payload

    @classmethod
    def _validate_payloads(
        cls,
        *,
        release_id: str,
        manifest: Dict[str, Any],
        library: Dict[str, Any],
        vector_index: Dict[str, Any],
        vectors: np.ndarray,
    ) -> None:
        cards = library.get("cards")
        items = vector_index.get("items")
        if not isinstance(cards, list) or not cards:
            raise MemeReleaseError("library.json cards 不能为空")
        if not isinstance(items, list) or not items:
            raise MemeReleaseError("vector_index.json items 不能为空")
        if str(library.get("library_id") or "") != release_id:
            raise MemeReleaseError("library_id 与 release_id 不一致")
        if int(library.get("card_count") or 0) != len(cards):
            raise MemeReleaseError("library card_count 与 cards 数量不一致")
        if int(manifest.get("card_count") or 0) != len(cards):
            raise MemeReleaseError("release card_count 与 cards 数量不一致")
        if int(vector_index.get("row_count") or 0) != len(items):
            raise MemeReleaseError("vector index row_count 与 items 数量不一致")
        dimension = int(vector_index.get("dimension") or 0)
        if vectors.shape != (len(items), dimension):
            raise MemeReleaseError(
                f"向量形状不一致: expected={(len(items), dimension)} actual={vectors.shape}"
            )
        if not np.isfinite(vectors).all():
            raise MemeReleaseError("向量文件包含 NaN 或 Inf")

        card_ids: List[str] = []
        cards_by_id: Dict[str, Dict[str, Any]] = {}
        expressions: List[str] = []
        route_count = 0
        for card in cards:
            if not isinstance(card, dict):
                raise MemeReleaseError("cards 中存在非对象条目")
            card_id = str(card.get("card_id") or "").strip()
            expression = str(card.get("canonical_expression") or "").strip()
            if not card_id or not expression:
                raise MemeReleaseError("梗卡缺少 card_id 或 canonical_expression")
            if str((card.get("human_review") or {}).get("status") or "") != "approved":
                raise MemeReleaseError(f"梗卡未通过人工审核: {card_id}")
            routes = card.get("usage_routes")
            if not isinstance(routes, list) or not routes:
                raise MemeReleaseError(f"梗卡缺少 usage_routes: {card_id}")
            route_count += len(routes)
            card_ids.append(card_id)
            expressions.append(expression)
            cards_by_id[card_id] = card
        if len(set(card_ids)) != len(card_ids):
            raise MemeReleaseError("梗包包含重复 card_id")
        if len(set(expressions)) != len(expressions):
            raise MemeReleaseError("梗包包含重复 canonical_expression")
        if int(manifest.get("route_count") or 0) != route_count:
            raise MemeReleaseError("release route_count 与语义 route 数量不一致")
        if (
            "route_count" in vector_index
            and int(vector_index.get("route_count") or 0) != route_count
        ):
            raise MemeReleaseError("vector index route_count 与语义 route 数量不一致")
        vector_count = int(
            manifest.get("vector_count", manifest.get("route_count")) or 0
        )
        if vector_count != len(items):
            raise MemeReleaseError("release vector_count 与向量数量不一致")
        if (
            "vector_count" in vector_index
            and int(vector_index.get("vector_count") or 0) != len(items)
        ):
            raise MemeReleaseError("vector index vector_count 与向量数量不一致")

        seen_rows: set[int] = set()
        seen_anchors: set[Tuple[str, int, int]] = set()
        indexed_routes: set[Tuple[str, int]] = set()
        for item in items:
            if not isinstance(item, dict):
                raise MemeReleaseError("vector items 中存在非对象条目")
            row = int(item.get("row", -1))
            card_id = str(item.get("card_id") or "").strip()
            route_index = int(item.get("route_index", -1))
            anchor_index = int(item.get("anchor_index", 0))
            if row < 0 or row >= len(items) or row in seen_rows:
                raise MemeReleaseError(f"向量 row 非法或重复: {row}")
            card = cards_by_id.get(card_id)
            if card is None:
                raise MemeReleaseError(f"向量索引指向未审核卡: {card_id}")
            routes = card["usage_routes"]
            if route_index < 0 or route_index >= len(routes):
                raise MemeReleaseError(
                    f"route_index 越界: card={card_id} route={route_index}"
                )
            if anchor_index < 0:
                raise MemeReleaseError(
                    f"anchor_index 非法: card={card_id} route={route_index}"
                )
            anchor = (card_id, route_index, anchor_index)
            if anchor in seen_anchors:
                raise MemeReleaseError(f"重复语义原型索引: {anchor}")
            seen_rows.add(row)
            seen_anchors.add(anchor)
            indexed_routes.add((card_id, route_index))
        expected_routes = {
            (card_id, route_index)
            for card_id, card in cards_by_id.items()
            for route_index in range(len(card["usage_routes"]))
        }
        if indexed_routes != expected_routes:
            missing = sorted(expected_routes - indexed_routes)
            raise MemeReleaseError(f"存在没有向量原型的语义 route: {missing}")

    @staticmethod
    def _normalize_vectors(vectors: np.ndarray) -> np.ndarray:
        matrix = np.asarray(vectors, dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        if np.any(norms <= 0):
            raise MemeReleaseError("向量文件包含零向量")
        return matrix / norms

    @staticmethod
    def sha256_file(path: Path) -> str:
        digest = sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def fingerprint(self) -> Dict[str, Any]:
        """返回可用于测试/生产环境比对的不可变梗包指纹。"""

        return {
            "release_id": self.release_id,
            "embedding_model": self.embedding_model,
            "dimension": self.dimension,
            "card_count": len(self.cards_by_id),
            "route_count": self.route_count,
            "vector_count": self.vector_count,
            "file_sha256": dict(self.file_hashes),
        }

    def retrieve(
        self,
        query_embedding: Sequence[float],
        *,
        top_k: int,
        minimum_similarity: float,
    ) -> List[RetrievedMemeCandidate]:
        """按 route 向量召回并按 card 去重。"""

        query = np.asarray(query_embedding, dtype=np.float32)
        if query.ndim != 1 or query.shape[0] != self.dimension:
            raise MemeReleaseError(
                f"查询向量维度不一致: expected={self.dimension} actual={query.shape}"
            )
        if not np.isfinite(query).all():
            raise MemeReleaseError("查询向量包含 NaN 或 Inf")
        norm = float(np.linalg.norm(query))
        if norm <= 0:
            raise MemeReleaseError("查询向量是零向量")
        scores = (query / norm) @ self.vectors.T
        ranked_rows = np.argsort(scores)[::-1]

        result: List[RetrievedMemeCandidate] = []
        seen_cards: set[str] = set()
        safe_top_k = max(1, int(top_k))
        for raw_row in ranked_rows:
            row = int(raw_row)
            similarity = float(scores[row])
            if similarity < minimum_similarity:
                break
            item = self.route_items[row]
            card_id = str(item["card_id"])
            if card_id in seen_cards:
                continue
            seen_cards.add(card_id)
            card = self.cards_by_id[card_id]
            route_index = int(item["route_index"])
            route = card["usage_routes"][route_index]
            result.append(
                RetrievedMemeCandidate(
                    card_id=card_id,
                    canonical_expression=str(card["canonical_expression"]),
                    semantic_core=str(card["semantic_core"]),
                    culture_scope=str(card["culture_scope"]),
                    serving_scope=str(card["serving_scope"]),
                    route_index=route_index,
                    route_tag=str(route["route_tag"]),
                    when=str(route["when"]),
                    communicative_intent=str(route["communicative_intent"]),
                    allowed_realizations=tuple(
                        str(value) for value in route["allowed_realizations"]
                    ),
                    required_context_signals=tuple(
                        str(value) for value in card["required_context_signals"]
                    ),
                    hard_blocks=tuple(str(value) for value in card["hard_blocks"]),
                    similarity=similarity,
                )
            )
            if len(result) >= safe_top_k:
                break
        return result

    def get_card(self, card_id: str) -> Optional[Dict[str, Any]]:
        """读取一张已经过加载期校验的梗卡。"""

        return self.cards_by_id.get(card_id)

    def build_planner_resource(
        self,
        candidates: Sequence[RetrievedMemeCandidate],
        *,
        understand_only_card_ids: set[str],
    ) -> str:
        """生成一次性的内部 Planner 梗语义候选资源。"""

        payload = {
            "release_id": self.release_id,
            "decision_contract": {
                "default": "SKIP",
                "USE": (
                    "含义是“授权 Replyer 看见这个表达机会”，不等于最终回复必须用梗；"
                    "usage route 与当前完整语境明确吻合、required_context_signals 满足、"
                    "且没有 hard_blocks 时必须选 USE。原句的夸张感是梗的表达机制，"
                    "Replyer 会负责自然改写或最终不用"
                ),
                "UNDERSTAND_ONLY": (
                    "用户已经说出/引用该梗、只需理解其含义，或候选只允许理解时选；"
                    "不要仅因担心逐字复读而把明确适用的主动表达降级"
                ),
                "SKIP": (
                    "语义不匹配、没有对话价值、required_context_signals 不满足，"
                    "或存在 hard_blocks、严肃/高后果风险"
                ),
            },
            "scope_contract": [
                "culture_scope 和 serving_scope 只说明梗的来源与表达风格，不是硬门槛",
                "不得仅因用户没有主动提及作品名、圈层名或梗名而 SKIP",
                "只有 required_context_signals 和 hard_blocks 才是使用许可的硬约束",
                (
                    "若 usage route 与轻松私聊场景明确吻合，Planner 必须选 USE，"
                    "把表达机会交给 Replyer"
                ),
                (
                    "Planner 不负责最终措辞，不得以原句太夸张、关系阶段不适合或"
                    "担心显得刻意为由 SKIP；这些由 Replyer 根据角色和语气处理"
                ),
            ],
            "candidates": [
                candidate.to_planner_payload(
                    understand_only=candidate.card_id in understand_only_card_ids
                )
                for candidate in candidates
            ],
            "tool_contract": {
                "when_replying": (
                    "只有决定 USE 或 UNDERSTAND_ONLY 时，才在 reply 工具填写 "
                    "meme_action、meme_release_id、meme_card_id、meme_route_index"
                ),
                "optional": True,
                "max_selected": 1,
                "never_expose_internal_resource": True,
            },
        }
        return (
            "【内部梗语义候选】这是系统提供的可选理解/表达资源，不是用户消息，"
            "也不是要求说出的台词。必须根据完整对话语义判断，不得因为向量召回就使用。"
            "Planner 负责决定动作，Replyer 只负责自然表达："
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )

    def build_replyer_resource(
        self,
        selection: MemeToolSelection,
        *,
        effective_action: str,
        decision_source: str = "planner",
    ) -> str:
        """把已校验的语义决策转换成 Replyer 一次性资源。"""

        card = self.cards_by_id[selection.card_id]
        route = card["usage_routes"][selection.route_index]
        if effective_action == "UNDERSTAND_ONLY":
            payload = {
                "release_id": self.release_id,
                "card_id": selection.card_id,
                "semantic_core": card["semantic_core"],
                "culture_scope": card["culture_scope"],
                "selected_usage_route": {
                    "when": route["when"],
                    "communicative_intent": route["communicative_intent"],
                },
                "hard_blocks": card["hard_blocks"],
                "reply_contract": [
                    "这是本轮强制的 UNDERSTAND_ONLY 输出约束，不是表达素材",
                    (
                        "聊天记录和最新推理可能出现用户引用的梗；最终回复不得"
                        "复读或复述、改写、解释、评价其是否贴切，或主动点出该表达"
                    ),
                    (
                        "完全绕开用户引用的梗表达，只回应它所指向的具体人物、"
                        "事件、感受或风险"
                    ),
                    "不得暴露梗卡、向量召回、Planner 决策和内部资源",
                ],
            }
            return (
                "【本轮内部语义理解与禁止复述约束】"
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )
        if effective_action != "USE":
            raise MemeReleaseError(
                f"Replyer 资源不支持的 effective_action: {effective_action}"
            )

        payload = {
            "release_id": self.release_id,
            "card_id": selection.card_id,
            "canonical_expression": card["canonical_expression"],
            "semantic_core": card["semantic_core"],
            "culture_scope": card["culture_scope"],
            "selected_usage_route": {
                "route_tag": route["route_tag"],
                "when": route["when"],
                "communicative_intent": route["communicative_intent"],
                "allowed_realizations": route["allowed_realizations"],
            },
            "hard_blocks": card["hard_blocks"],
            "reply_contract": [
                (
                    "这是已经通过语义许可的 USE 决策；不要求机械逐字复读，"
                    "但最终回复必须让人能辨认出这一个梗的表达或自然变体"
                ),
                (
                    "先自然回应具体事情，再清楚使用 canonical_expression 或"
                    " allowed_realizations 中的一个自然变体"
                ),
                (
                    "普通祝贺、普通感谢或只表达相同情绪不算完成 USE；"
                    "不要把可识别的梗消解成无梗回复"
                ),
                "一条回复最多使用这一个梗",
                "不得解释或暴露梗卡、向量召回、Planner 决策和内部资源",
            ],
        }
        heading = (
            "【本轮内部最终表达决策：覆盖此前对该候选的 SKIP 判断】"
            if decision_source == "semantic_selector"
            else "【本轮内部可选表达资源】"
        )
        return (
            heading
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )

    def build_replyer_candidate_resource(
        self,
        candidates: Sequence[RetrievedMemeCandidate],
        *,
        understand_only_card_ids: set[str],
    ) -> str:
        """让 Replyer 直接语义判断本轮候选，类似一次性记忆召回。"""

        payload_candidates: List[Dict[str, Any]] = []
        for candidate in candidates:
            understand_only = candidate.card_id in understand_only_card_ids
            payload: Dict[str, Any] = {
                "card_id": candidate.card_id,
                "mode": (
                    "UNDERSTAND_ONLY"
                    if understand_only
                    else "EXPRESSION_OPTION"
                ),
                "semantic_core": candidate.semantic_core,
                "culture_scope": candidate.culture_scope,
                "selected_usage_route": {
                    "when": candidate.when,
                    "communicative_intent": candidate.communicative_intent,
                },
                "required_context_signals": list(
                    candidate.required_context_signals
                ),
                "hard_blocks": list(candidate.hard_blocks),
                "similarity": round(candidate.similarity, 6),
            }
            if not understand_only:
                payload["canonical_expression"] = (
                    candidate.canonical_expression
                )
                payload["allowed_realizations"] = list(
                    candidate.allowed_realizations
                )
            payload_candidates.append(payload)

        resource = {
            "release_id": self.release_id,
            "candidates": payload_candidates,
            "reply_contract": [
                "这些是一次性语义表达记忆，不是必须说出的台词",
                "只按完整对话语义判断；不得使用关键词或正则式命中",
                (
                    "EXPRESSION_OPTION 的 usage route 明确吻合且无 hard_blocks 时，"
                    "优先自然使用、改写或隐性呼应其中一个"
                ),
                (
                    "不要仅因梗的来源圈层、原句夸张或 Planner 没有点名就拒绝；"
                    "最终措辞必须符合当前角色与关系"
                ),
                "严肃、高后果、真实伤病或危机场景必须全部不用",
                "UNDERSTAND_ONLY 只帮助理解，不得复读、改写、解释或点出该梗",
                "一条回复最多使用一个梗，也可以一个都不用",
                "不得暴露梗卡、向量召回、Planner 决策和内部资源",
            ],
        }
        return (
            "【本轮内部候选表达记忆】"
            + json.dumps(resource, ensure_ascii=False, separators=(",", ":"))
        )

    def build_semantic_selector_prompt(
        self,
        candidates: Sequence[RetrievedMemeCandidate],
        *,
        semantic_query_text: str,
        understand_only_card_ids: set[str],
    ) -> str:
        """给独立模型构造语义机会判定，不让字面规则承担决策。"""

        payload_candidates: List[Dict[str, Any]] = []
        for candidate in candidates:
            card = self.cards_by_id[candidate.card_id]
            understand_only = candidate.card_id in understand_only_card_ids
            payload_candidates.append(
                {
                    "card_id": candidate.card_id,
                    "canonical_expression": candidate.canonical_expression,
                    "semantic_core": candidate.semantic_core,
                    "culture_scope": candidate.culture_scope,
                    "serving_scope": candidate.serving_scope,
                    "route_index": candidate.route_index,
                    "route": {
                        "route_tag": candidate.route_tag,
                        "when": candidate.when,
                        "communicative_intent": candidate.communicative_intent,
                    },
                    "required_context_signals": list(
                        candidate.required_context_signals
                    ),
                    "hard_blocks": list(candidate.hard_blocks),
                    "reviewed_positive_contexts": list(
                        card.get("positive_contexts") or []
                    ),
                    "reviewed_negative_contexts": list(
                        card.get("negative_contexts") or []
                    ),
                    "allowed_actions": (
                        ["UNDERSTAND_ONLY", "SKIP"]
                        if understand_only
                        else ["USE", "UNDERSTAND_ONLY", "SKIP"]
                    ),
                    "similarity": round(candidate.similarity, 6),
                }
            )

        contract = {
            "release_id": self.release_id,
            "candidates": payload_candidates,
            "output_schema": {
                "action": "USE | UNDERSTAND_ONLY | SKIP",
                "card_id": "USE/UNDERSTAND_ONLY 时填写候选 card_id，否则为空",
                "route_index": "USE/UNDERSTAND_ONLY 时填写候选 route_index",
                "confidence": "0 到 1",
                "matched_hard_blocks": "语义上成立的阻断项数组",
                "reason": "不超过 60 字",
            },
        }
        return (
            "你是对话系统里的“梗语义机会裁判”，不是文案作者。"
            "只按完整对话和候选的整体语义判断；不得使用关键词、正则或字面命中。"
            "你的决定只是是否把一个表达机会授权给 Replyer，不负责最终措辞。\n"
            "判定顺序：\n"
            "A. reviewed_positive_contexts / reviewed_negative_contexts 是人工审核过的"
            "语义边界例子，不是关键词规则；先用它们理解模糊字段，再逐项判断 "
            "hard_blocks。任何 hard block 真正成立都必须 SKIP，不能被 route 匹配覆盖。"
            "普通朋友分享资源、游戏内赠礼或带飞可按正例理解；正式职场、上下级/"
            "师生、金钱回报、现实义务、真实伤病或危机场景应 SKIP。\n"
            "B. 无阻断后，所有 required_context_signals 都必须在整体语境中成立，"
            "否则 SKIP。\n"
            "C. A/B 通过、route 明确吻合且能增加私人轻松对话的社交表达价值时，"
            "积极选择 USE。不得仅因原句夸张、用户没提作品名/圈层名、担心逐字"
            "复读或最终角色措辞而拒绝；Replyer 会自然改写。\n"
            "D. 用户已经引用该梗、只需理解圈层含义，或候选只允许理解时，选 "
            "UNDERSTAND_ONLY。最多选择一个候选。\n"
            "严格只返回一个 JSON 对象，不要 Markdown、代码围栏或额外文字。\n\n"
            "内部候选："
            + json.dumps(contract, ensure_ascii=False, separators=(",", ":"))
            + "\n\n真实可见对话：\n"
            + semantic_query_text
        )

    def parse_semantic_selector_response(
        self,
        raw_response: str,
        *,
        candidates: Sequence[RetrievedMemeCandidate],
        understand_only_card_ids: set[str],
        minimum_confidence: float,
    ) -> SemanticSelectorDecision:
        """严格校验模型判定；不确定或自相矛盾时降级为 SKIP。"""

        payload = json.loads(raw_response)
        if not isinstance(payload, dict):
            raise MemeReleaseError("语义裁判结果必须是 JSON 对象")
        requested_action = str(payload.get("action") or "").strip().upper()
        if requested_action not in MEME_ALLOWED_ACTIONS:
            raise MemeReleaseError(
                f"语义裁判返回未知 action: {requested_action}"
            )
        try:
            confidence = float(payload.get("confidence", 0.0))
        except (TypeError, ValueError) as exc:
            raise MemeReleaseError("语义裁判 confidence 必须是数字") from exc
        if not 0.0 <= confidence <= 1.0:
            raise MemeReleaseError("语义裁判 confidence 必须在 0 到 1 之间")
        raw_blocks = payload.get("matched_hard_blocks")
        if raw_blocks is None:
            raw_blocks = []
        if not isinstance(raw_blocks, list):
            raise MemeReleaseError(
                "语义裁判 matched_hard_blocks 必须是数组"
            )
        matched_hard_blocks = tuple(
            str(value).strip() for value in raw_blocks if str(value).strip()
        )
        reason = str(payload.get("reason") or "").strip()[:200]

        skip_selection = MemeToolSelection(
            action="SKIP",
            release_id=self.release_id,
            card_id="",
            route_index=0,
        )
        if requested_action == "SKIP":
            return SemanticSelectorDecision(
                requested_action=requested_action,
                selection=skip_selection,
                confidence=confidence,
                matched_hard_blocks=matched_hard_blocks,
                reason=reason,
            )

        card_id = str(payload.get("card_id") or "").strip()
        try:
            route_index = int(payload.get("route_index", 0))
        except (TypeError, ValueError) as exc:
            raise MemeReleaseError(
                "语义裁判 route_index 必须是整数"
            ) from exc
        candidate_pairs = {
            (candidate.card_id, candidate.route_index)
            for candidate in candidates
        }
        if (card_id, route_index) not in candidate_pairs:
            raise MemeReleaseError("语义裁判选择了召回候选之外的梗 route")

        effective_action = requested_action
        if matched_hard_blocks or confidence < minimum_confidence:
            effective_action = "SKIP"
        elif (
            effective_action == "USE"
            and card_id in understand_only_card_ids
        ):
            effective_action = "UNDERSTAND_ONLY"

        selection = (
            skip_selection
            if effective_action == "SKIP"
            else MemeToolSelection(
                action=effective_action,
                release_id=self.release_id,
                card_id=card_id,
                route_index=route_index,
            )
        )
        return SemanticSelectorDecision(
            requested_action=requested_action,
            selection=selection,
            confidence=confidence,
            matched_hard_blocks=matched_hard_blocks,
            reason=reason,
        )

    def parse_tool_selection(
        self,
        reply_tool_args: Mapping[str, Any],
    ) -> Optional[MemeToolSelection]:
        """解析并校验 Planner 通过 reply 工具传递的语义决策。"""

        raw_action = reply_tool_args.get(MEME_ACTION_ARG)
        if raw_action is None:
            return None
        action = str(raw_action).strip().upper()
        if action not in MEME_ALLOWED_ACTIONS:
            raise MemeReleaseError(f"未知 meme_action: {action}")
        release_id = str(reply_tool_args.get(MEME_RELEASE_ID_ARG) or "").strip()
        if action == "SKIP":
            if release_id and release_id != self.release_id:
                raise MemeReleaseError(
                    f"meme_release_id 不匹配: expected={self.release_id} actual={release_id}"
                )
            return MemeToolSelection(
                action=action,
                release_id=self.release_id,
                card_id="",
                route_index=0,
            )

        card_id = str(reply_tool_args.get(MEME_CARD_ID_ARG) or "").strip()
        try:
            route_index = int(reply_tool_args.get(MEME_ROUTE_INDEX_ARG, 0))
        except (TypeError, ValueError) as exc:
            raise MemeReleaseError("meme_route_index 必须是整数") from exc

        if release_id != self.release_id:
            raise MemeReleaseError(
                f"meme_release_id 不匹配: expected={self.release_id} actual={release_id}"
            )
        card = self.cards_by_id.get(card_id)
        if card is None:
            raise MemeReleaseError(f"meme_card_id 不存在: {card_id}")
        routes = card["usage_routes"]
        if route_index < 0 or route_index >= len(routes):
            raise MemeReleaseError(
                f"meme_route_index 越界: card={card_id} route={route_index}"
            )
        return MemeToolSelection(
            action=action,
            release_id=release_id,
            card_id=card_id,
            route_index=route_index,
        )


def augment_reply_tool_definitions(
    tool_definitions: Sequence[Mapping[str, Any]],
    *,
    release_id: str,
    candidate_card_ids: Sequence[str],
) -> List[Dict[str, Any]]:
    """给当前轮次的 reply 工具增加可选梗语义决策参数。"""

    updated = deepcopy(list(tool_definitions))
    candidate_ids = list(dict.fromkeys(str(value) for value in candidate_card_ids))
    for tool in updated:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if isinstance(function, dict):
            tool_name = str(function.get("name") or "")
            schema_owner = function
        else:
            tool_name = str(tool.get("name") or "")
            schema_owner = tool
        if tool_name != "reply":
            continue
        parameters = schema_owner.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}}
            schema_owner["parameters"] = parameters
        properties = parameters.get("properties")
        if not isinstance(properties, dict):
            properties = {}
            parameters["properties"] = properties
        properties.update(
            {
                MEME_ACTION_ARG: {
                    "type": "string",
                    "enum": ["USE", "UNDERSTAND_ONLY", "SKIP"],
                    "description": (
                        "本轮内部梗候选的语义决策。只有明确适合时才用 USE；"
                        "只需理解时用 UNDERSTAND_ONLY；否则用 SKIP 或省略全部梗参数。"
                    ),
                },
                MEME_RELEASE_ID_ARG: {
                    "type": "string",
                    "enum": [release_id],
                    "description": "内部梗包版本，必须原样填写。",
                },
                MEME_CARD_ID_ARG: {
                    "type": "string",
                    "enum": candidate_ids,
                    "description": "只能从当前内部候选中选择一张卡。",
                },
                MEME_ROUTE_INDEX_ARG: {
                    "type": "integer",
                    "minimum": 0,
                    "description": "所选候选载荷里的 route_index。",
                },
            }
        )
        return updated
    raise MemeReleaseError("当前 Planner 工具定义中没有 reply 工具")


def extract_semantic_query_text(
    messages: Sequence[Mapping[str, Any]],
    *,
    selected_history_count: int,
    message_limit: int,
    max_chars: int,
) -> str:
    """只从 Planner 选中的真实聊天历史构造语义查询文本。"""

    safe_history_count = max(0, int(selected_history_count))
    history_messages = list(messages)[1 : 1 + safe_history_count]
    selected_messages = history_messages[-max(1, int(message_limit)) :]
    lines: List[str] = []
    for message in selected_messages:
        if not isinstance(message, Mapping):
            continue
        role = str(message.get("role") or "").strip()
        content = _content_to_text(message.get("content"))
        if not content:
            continue
        lines.append(f"{role}: {content}" if role else content)
    joined = "\n".join(lines).strip()
    safe_max_chars = max(1, int(max_chars))
    return joined[-safe_max_chars:]


def build_semantic_query_from_session_messages(
    messages: Sequence[Mapping[str, Any]],
    *,
    bot_account_id: str,
    message_limit: int,
    max_chars: int,
) -> str:
    """从消息服务返回的真实收发消息构造语义查询。"""

    ordered_messages = sorted(
        enumerate(messages),
        key=lambda item: (_message_timestamp(item[1]), item[0]),
    )
    selected_messages = ordered_messages[-max(1, int(message_limit)) :]
    lines: List[str] = []
    normalized_bot_account_id = bot_account_id.strip()
    for _, message in selected_messages:
        if not isinstance(message, Mapping):
            continue
        if bool(message.get("is_command")) or bool(message.get("is_notify")):
            continue
        content = _content_to_text(message.get("processed_plain_text"))
        if not content:
            content = _content_to_text(message.get("raw_message"))
        if not content:
            continue
        message_info = message.get("message_info")
        user_info = (
            message_info.get("user_info")
            if isinstance(message_info, Mapping)
            else None
        )
        user_id = (
            str(user_info.get("user_id") or "").strip()
            if isinstance(user_info, Mapping)
            else ""
        )
        role = (
            "assistant"
            if normalized_bot_account_id and user_id == normalized_bot_account_id
            else "user"
        )
        lines.append(f"{role}: {content}")
    joined = "\n".join(lines).strip()
    safe_max_chars = max(1, int(max_chars))
    return joined[-safe_max_chars:]


def _message_timestamp(message: Mapping[str, Any]) -> float:
    try:
        return float(message.get("timestamp") or 0)
    except (TypeError, ValueError):
        return 0.0


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return " ".join(content.split())
    if isinstance(content, list):
        parts = [_content_to_text(item) for item in content]
        return " ".join(part for part in parts if part)
    if isinstance(content, Mapping):
        for key in ("text", "content", "data"):
            if key in content:
                rendered = _content_to_text(content[key])
                if rendered:
                    return rendered
    return ""


@dataclass
class SessionCandidateState:
    """某个会话最近一次 Planner 梗候选，供 Replyer 防串轮校验。"""

    release_id: str
    candidate_card_ids: Tuple[str, ...]
    semantic_query_text: str
    created_monotonic: float

    @classmethod
    def create(
        cls,
        *,
        release_id: str,
        candidate_card_ids: Sequence[str],
        semantic_query_text: str,
    ) -> "SessionCandidateState":
        return cls(
            release_id=release_id,
            candidate_card_ids=tuple(candidate_card_ids),
            semantic_query_text=semantic_query_text,
            created_monotonic=time.monotonic(),
        )

    def is_fresh(self, ttl_seconds: float) -> bool:
        return time.monotonic() - self.created_monotonic <= ttl_seconds
