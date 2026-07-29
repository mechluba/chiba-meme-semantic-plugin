"""把审核后的语义梗包接入 Maisaka Planner / Replyer 的在线插件。"""

from hashlib import sha256
from pathlib import Path
from time import perf_counter
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

import asyncio
import json
import time

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .meme_runtime import (
    MEME_ACTION_ARG,
    MEME_CARD_ID_ARG,
    MEME_RELEASE_ID_ARG,
    MEME_ROUTE_INDEX_ARG,
    MemeRelease,
    MemeReleaseError,
    MemeToolSelection,
    RetrievedMemeCandidate,
    SemanticSelectorDecision,
    SessionCandidateState,
    augment_reply_tool_definitions,
    build_semantic_query_from_session_messages,
    extract_semantic_query_text,
)


PLUGIN_VERSION = "1.0.2"
DEFAULT_RELEASE_ID = (
    "reviewed-semantic-meme-library-20260729-multiprototype-v1"
)
DEFAULT_UNDERSTAND_ONLY_CARD_IDS = [
    "broad-meme-d1c18154ab138e16",
    "broad-meme-ea3d0eaf19852463",
    "broad-meme-b6b65bba580828f1",
    "broad-meme-5bd5f3edfb5e4f10",
]


@dataclass
class SessionRetrievalState:
    """同一真实对话上下文的短时向量召回缓存。"""

    release_id: str
    semantic_query_text: str
    candidates: Tuple[RetrievedMemeCandidate, ...]
    created_monotonic: float

    def is_fresh(self, ttl_seconds: float) -> bool:
        return time.monotonic() - self.created_monotonic <= ttl_seconds


@dataclass
class ReplyerDecisionState:
    """Replyer 本轮真正收到的梗决策，供回复后效果评估。"""

    selection: MemeToolSelection
    source: str
    created_monotonic: float

    def is_fresh(self, ttl_seconds: float) -> bool:
        return time.monotonic() - self.created_monotonic <= ttl_seconds


class PluginSectionConfig(PluginConfigBase):
    """插件开关与配置版本。"""

    __ui_label__ = "插件"
    __ui_icon__ = "sparkles"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用语义梗插件")
    config_version: str = Field(default=PLUGIN_VERSION, description="插件配置版本")


class ServingConfig(PluginConfigBase):
    """在线召回、作用域与卡片使用策略。"""

    __ui_label__ = "在线服务"
    __ui_icon__ = "route"
    __ui_order__ = 1

    release_id: str = Field(
        default=DEFAULT_RELEASE_ID,
        description="必须与插件内版本化梗包目录和 release_id 一致",
    )
    target_platforms: List[str] = Field(
        default_factory=lambda: ["galpet_app"],
        description="默认仅在指定平台的真实会话中生效",
    )
    target_account_ids: List[str] = Field(
        default_factory=list,
        description="可选账号白名单，空列表表示接受目标平台的全部账号",
    )
    target_scopes: List[str] = Field(
        default_factory=list,
        description="可选路由 scope 白名单，空列表表示接受目标平台的全部 scope",
    )
    target_session_ids: List[str] = Field(
        default_factory=list,
        description="额外允许的真实 session_id；不自行计算会话 ID",
    )
    allow_group_sessions: bool = Field(
        default=False,
        description="是否允许群聊进入梗语义链路",
    )
    top_k: int = Field(default=3, ge=1, le=5, description="送给 Planner 的候选卡数量")
    minimum_similarity: float = Field(
        default=0.22,
        ge=-1.0,
        le=1.0,
        description="route 向量召回的最低余弦相似度",
    )
    history_message_limit: int = Field(
        default=6,
        ge=1,
        le=20,
        description="构造语义查询时使用的最近真实聊天消息数",
    )
    max_query_chars: int = Field(
        default=1800,
        ge=100,
        le=10000,
        description="送入 Embedding 的最近聊天文本字符上限",
    )
    embedding_task_name: str = Field(
        default="embedding",
        description="宿主侧 Embedding 模型任务名",
    )
    embedding_timeout_ms: int = Field(
        default=4500,
        ge=100,
        le=5500,
        description="梗语义查询的独立超时；不能超过 Planner Hook 总超时",
    )
    scope_cache_ttl_seconds: int = Field(
        default=30,
        ge=1,
        le=600,
        description="目标平台会话作用域缓存时间",
    )
    candidate_ttl_seconds: int = Field(
        default=180,
        ge=30,
        le=600,
        description="Planner 候选供随后 Replyer 校验的有效时间",
    )
    retrieval_cache_ttl_seconds: int = Field(
        default=60,
        ge=1,
        le=300,
        description="相同真实聊天上下文复用语义召回的时间，避免多轮工具链重复 Embedding",
    )
    replyer_considers_candidates_on_planner_skip: bool = Field(
        default=True,
        description=(
            "Planner 未选择或选择 SKIP 时，仍把候选作为一次性表达记忆交给 Replyer"
        ),
    )
    semantic_selector_enabled: bool = Field(
        default=True,
        description=(
            "Planner 未授权用梗时，使用独立语义模型对候选做一次安全判定"
        ),
    )
    semantic_selector_task_name: str = Field(
        default="utils",
        description="独立语义裁判使用的宿主模型任务名",
    )
    semantic_selector_timeout_ms: int = Field(
        default=3500,
        ge=500,
        le=4500,
        description="独立语义裁判调用超时",
    )
    semantic_selector_minimum_confidence: float = Field(
        default=0.75,
        ge=0.0,
        le=1.0,
        description="语义裁判授权 USE/UNDERSTAND_ONLY 的最低置信度",
    )
    understand_only_quality_gate_enabled: bool = Field(
        default=True,
        description=(
            "对 UNDERSTAND_ONLY 回复做发送前语义检查，违规时重生成一次"
        ),
    )
    understand_only_quality_gate_timeout_ms: int = Field(
        default=3500,
        ge=500,
        le=4500,
        description="UNDERSTAND_ONLY 发送前语义检查超时",
    )
    understand_only_quality_gate_max_retries: int = Field(
        default=1,
        ge=0,
        le=2,
        description="UNDERSTAND_ONLY 语义违规时最多请求的重生成次数",
    )
    understand_only_card_ids: List[str] = Field(
        default_factory=lambda: list(DEFAULT_UNDERSTAND_ONLY_CARD_IDS),
        description="只允许理解、暂不允许主动复读的卡片 ID",
    )


class ObservabilityConfig(PluginConfigBase):
    """线上语义效果观察配置。"""

    __ui_label__ = "效果观察"
    __ui_icon__ = "activity"
    __ui_order__ = 2

    enabled: bool = Field(default=True, description="是否异步评估被选梗的实际回复效果")
    sample_rate: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="USE 回复进入异步语义评估的稳定采样比例",
    )
    evaluator_task_name: str = Field(
        default="utils",
        description="异步效果评估使用的宿主模型任务名",
    )
    evaluator_timeout_ms: int = Field(
        default=15000,
        ge=1000,
        le=60000,
        description="异步效果评估超时",
    )
    include_response_preview_in_logs: bool = Field(
        default=True,
        description="结构化效果日志是否包含最多 300 字的可见回复预览",
    )


class MemeSemanticPluginConfig(PluginConfigBase):
    """语义梗插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    serving: ServingConfig = Field(default_factory=ServingConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)


class MemeSemanticPlugin(MaiBotPlugin):
    """在目标前台默认启用的语义梗 Planner/Reply 插件。"""

    config_model = MemeSemanticPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._release: Optional[MemeRelease] = None
        self._session_scope_cache: Dict[str, Dict[str, Any]] = {}
        self._scope_cache_expires_monotonic = 0.0
        self._candidate_states: Dict[str, SessionCandidateState] = {}
        self._retrieval_states: Dict[str, SessionRetrievalState] = {}
        self._replyer_decision_states: Dict[str, ReplyerDecisionState] = {}

    async def on_load(self) -> None:
        """加载不可变梗包并输出环境比对所需指纹。"""

        self._load_release()
        release = self._require_release()
        self.ctx.logger.info(
            "meme_semantic_release_loaded %s",
            json.dumps(
                {
                    "event": "meme_semantic_release_loaded",
                    "plugin_version": PLUGIN_VERSION,
                    "release": release.fingerprint(),
                    "target_platforms": list(self.config.serving.target_platforms),
                    "target_account_ids": list(self.config.serving.target_account_ids),
                    "target_scopes": list(self.config.serving.target_scopes),
                    "understand_only_card_ids": list(
                        self.config.serving.understand_only_card_ids
                    ),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    async def on_unload(self) -> None:
        """释放运行期会话候选与作用域缓存。"""

        self._candidate_states.clear()
        self._retrieval_states.clear()
        self._replyer_decision_states.clear()
        self._session_scope_cache.clear()
        self._release = None

    async def on_config_update(
        self,
        scope: str,
        config_data: Dict[str, Any],
        version: str,
    ) -> None:
        """插件配置热更新后重新校验梗包和作用域缓存。"""

        del version
        if scope != "self":
            return
        self.set_plugin_config(config_data)
        self._load_release()
        self._candidate_states.clear()
        self._retrieval_states.clear()
        self._replyer_decision_states.clear()
        self._session_scope_cache.clear()
        self._scope_cache_expires_monotonic = 0.0

    def _load_release(self) -> None:
        release_id = self.config.serving.release_id.strip()
        if not release_id:
            raise MemeReleaseError("serving.release_id 不能为空")
        releases_root = Path(__file__).resolve().parent / "resources" / "releases"
        release_dir = (releases_root / release_id).resolve()
        if not release_dir.is_relative_to(releases_root.resolve()):
            raise MemeReleaseError("serving.release_id 越出版本化梗包目录")
        self._release = MemeRelease.load(release_dir)

    def _require_release(self) -> MemeRelease:
        if self._release is None:
            raise RuntimeError("语义梗包尚未完成加载")
        return self._release

    async def _refresh_session_scope_cache(self) -> None:
        target_platforms = [
            platform.strip()
            for platform in self.config.serving.target_platforms
            if platform.strip()
        ]
        if not target_platforms:
            raise ValueError("serving.target_platforms 不能为空")

        refreshed: Dict[str, Dict[str, Any]] = {}
        for platform in target_platforms:
            streams = await self.ctx.chat.get_all_streams(platform=platform)
            if not isinstance(streams, list):
                raise RuntimeError(
                    f"读取目标平台会话失败: platform={platform} result_type={type(streams).__name__}"
                )
            for stream in streams:
                if not isinstance(stream, dict):
                    continue
                session_id = str(
                    stream.get("session_id") or stream.get("stream_id") or ""
                ).strip()
                if session_id:
                    refreshed[session_id] = dict(stream)
        self._session_scope_cache = refreshed
        self._scope_cache_expires_monotonic = (
            time.monotonic() + self.config.serving.scope_cache_ttl_seconds
        )

    async def _resolve_scoped_stream(
        self,
        session_id: str,
    ) -> Optional[Dict[str, Any]]:
        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            return None

        explicit_session_ids = {
            value.strip()
            for value in self.config.serving.target_session_ids
            if value.strip()
        }
        if normalized_session_id in explicit_session_ids:
            return {
                "session_id": normalized_session_id,
                "platform": "explicit_session",
                "account_id": "",
                "scope": "",
                "is_group_session": False,
            }

        now = time.monotonic()
        if now >= self._scope_cache_expires_monotonic:
            await self._refresh_session_scope_cache()
        stream = self._session_scope_cache.get(normalized_session_id)
        if stream is None:
            await self._refresh_session_scope_cache()
            stream = self._session_scope_cache.get(normalized_session_id)
        if stream is None:
            return None

        if bool(stream.get("is_group_session")) and not self.config.serving.allow_group_sessions:
            return None
        target_account_ids = {
            value.strip()
            for value in self.config.serving.target_account_ids
            if value.strip()
        }
        if target_account_ids and str(stream.get("account_id") or "").strip() not in target_account_ids:
            return None
        target_scopes = {
            value.strip()
            for value in self.config.serving.target_scopes
            if value.strip()
        }
        if target_scopes and str(stream.get("scope") or "").strip() not in target_scopes:
            return None
        return stream

    @staticmethod
    def _session_log_id(session_id: str) -> str:
        return sha256(session_id.encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _stable_sample(
        *,
        session_id: str,
        reply_message_id: str,
        sample_rate: float,
    ) -> bool:
        if sample_rate <= 0:
            return False
        if sample_rate >= 1:
            return True
        digest = sha256(
            f"{session_id}:{reply_message_id}".encode("utf-8")
        ).digest()
        value = int.from_bytes(digest[:8], byteorder="big") / float(2**64 - 1)
        return value < sample_rate

    async def _build_semantic_query_text(
        self,
        *,
        session_id: str,
        scoped_stream: Mapping[str, Any],
        planner_messages: List[Any],
        selected_history_count: int,
    ) -> str:
        """优先读取真实会话消息；能力不可用时回退到本轮已选历史。"""

        recent_messages = await self.ctx.message.get_recent(
            chat_id=session_id,
            limit=self.config.serving.history_message_limit,
        )
        if isinstance(recent_messages, list) and recent_messages:
            query_text = build_semantic_query_from_session_messages(
                recent_messages,
                bot_account_id=str(scoped_stream.get("account_id") or ""),
                message_limit=self.config.serving.history_message_limit,
                max_chars=self.config.serving.max_query_chars,
            )
            if query_text:
                return query_text
        return extract_semantic_query_text(
            planner_messages,
            selected_history_count=selected_history_count,
            message_limit=self.config.serving.history_message_limit,
            max_chars=self.config.serving.max_query_chars,
        )

    def _log_planner_decision(
        self,
        *,
        session_id: str,
        release_id: str,
        requested_action: str,
        effective_action: str,
        card_id: str,
        route_index: int,
    ) -> None:
        self.ctx.logger.info(
            "meme_semantic_planner_decision %s",
            json.dumps(
                {
                    "event": "meme_semantic_planner_decision",
                    "session": self._session_log_id(session_id),
                    "release_id": release_id,
                    "requested_action": requested_action,
                    "effective_action": effective_action,
                    "card_id": card_id,
                    "route_index": route_index,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    @HookHandler(
        "maisaka.planner.before_request",
        name="meme_semantic_planner_candidates",
        description="对真实聊天历史做向量召回，并把审核后梗卡交给 Planner 做语义决策。",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=5500,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_planner_before_request(self, **kwargs: Any) -> Dict[str, Any]:
        """召回梗卡、注入内部候选，并扩展本轮 reply 工具参数。"""

        session_id = str(kwargs.get("session_id") or "").strip()
        scoped_stream = await self._resolve_scoped_stream(session_id)
        if not scoped_stream:
            return {"action": "continue"}

        raw_messages = kwargs.get("messages")
        raw_tools = kwargs.get("tool_definitions")
        if not isinstance(raw_messages, list) or not isinstance(raw_tools, list):
            raise TypeError("Planner Hook 缺少 messages 或 tool_definitions")
        query_text = await self._build_semantic_query_text(
            session_id=session_id,
            scoped_stream=scoped_stream,
            planner_messages=raw_messages,
            selected_history_count=int(kwargs.get("selected_history_count") or 0),
        )
        if not query_text:
            self._candidate_states.pop(session_id, None)
            return {"action": "continue"}

        release = self._require_release()
        started = perf_counter()
        query_fingerprint = sha256(query_text.encode("utf-8")).hexdigest()[:12]
        retrieval_state = self._retrieval_states.get(session_id)
        cache_hit = bool(
            retrieval_state
            and retrieval_state.release_id == release.release_id
            and retrieval_state.semantic_query_text == query_text
            and retrieval_state.is_fresh(
                self.config.serving.retrieval_cache_ttl_seconds
            )
        )
        embedding_ms = 0.0
        if cache_hit and retrieval_state is not None:
            embedding_model = release.embedding_model
            candidates = list(retrieval_state.candidates)
        else:
            embedding_started = perf_counter()
            embedding_result = await asyncio.wait_for(
                self.ctx.llm.embed(
                    text=query_text,
                    task_name=self.config.serving.embedding_task_name,
                ),
                timeout=self.config.serving.embedding_timeout_ms / 1000,
            )
            embedding_ms = round((perf_counter() - embedding_started) * 1000, 2)
            if (
                not isinstance(embedding_result, dict)
                or not embedding_result.get("success")
            ):
                raise RuntimeError(f"Embedding 调用失败: {embedding_result}")
            embedding_model = str(
                embedding_result.get("model_name") or ""
            ).strip()
            if embedding_model != release.embedding_model:
                raise MemeReleaseError(
                    f"Embedding 模型与梗包不一致: "
                    f"expected={release.embedding_model} actual={embedding_model}"
                )
            embedding = embedding_result.get("embedding")
            if not isinstance(embedding, list):
                raise TypeError("Embedding 返回缺少 embedding 数组")
            candidates = release.retrieve(
                embedding,
                top_k=self.config.serving.top_k,
                minimum_similarity=self.config.serving.minimum_similarity,
            )
            self._retrieval_states[session_id] = SessionRetrievalState(
                release_id=release.release_id,
                semantic_query_text=query_text,
                candidates=tuple(candidates),
                created_monotonic=time.monotonic(),
            )
        if not candidates:
            self._candidate_states.pop(session_id, None)
            self.ctx.logger.info(
                "meme_semantic_retrieval %s",
                json.dumps(
                    {
                        "event": "meme_semantic_retrieval",
                        "session": self._session_log_id(session_id),
                        "release_id": release.release_id,
                        "candidate_count": 0,
                        "query_fingerprint": query_fingerprint,
                        "cache_hit": cache_hit,
                        "embedding_ms": embedding_ms,
                        "elapsed_ms": round((perf_counter() - started) * 1000, 2),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            return {"action": "continue"}

        understand_only_ids = set(self.config.serving.understand_only_card_ids)
        planner_resource = release.build_planner_resource(
            candidates,
            understand_only_card_ids=understand_only_ids,
        )
        updated_messages = [*raw_messages, {"role": "user", "content": planner_resource}]
        updated_tools = augment_reply_tool_definitions(
            raw_tools,
            release_id=release.release_id,
            candidate_card_ids=[candidate.card_id for candidate in candidates],
        )
        self._candidate_states[session_id] = SessionCandidateState.create(
            release_id=release.release_id,
            candidate_card_ids=[candidate.card_id for candidate in candidates],
            semantic_query_text=query_text,
        )
        self.ctx.logger.info(
            "meme_semantic_retrieval %s",
            json.dumps(
                {
                    "event": "meme_semantic_retrieval",
                    "session": self._session_log_id(session_id),
                    "release_id": release.release_id,
                    "embedding_model": embedding_model,
                    "query_fingerprint": query_fingerprint,
                    "cache_hit": cache_hit,
                    "embedding_ms": embedding_ms,
                    "candidates": [
                        {
                            "card_id": candidate.card_id,
                            "expression": candidate.canonical_expression,
                            "similarity": round(candidate.similarity, 6),
                            "route_index": candidate.route_index,
                        }
                        for candidate in candidates
                    ],
                    "elapsed_ms": round((perf_counter() - started) * 1000, 2),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        modified_kwargs = dict(kwargs)
        modified_kwargs["messages"] = updated_messages
        modified_kwargs["tool_definitions"] = updated_tools
        return {
            "action": "continue",
            "modified_kwargs": modified_kwargs,
        }

    def _validate_selection_for_session(
        self,
        *,
        session_id: str,
        selection: MemeToolSelection,
    ) -> SessionCandidateState:
        state = self._candidate_states.get(session_id)
        if state is None:
            raise MemeReleaseError("Replyer 收到梗决策，但当前会话没有 Planner 候选")
        if not state.is_fresh(self.config.serving.candidate_ttl_seconds):
            self._candidate_states.pop(session_id, None)
            raise MemeReleaseError("Planner 梗候选已过期")
        if state.release_id != selection.release_id:
            raise MemeReleaseError("Planner/Replyer 梗包版本不一致")
        if (
            selection.action != "SKIP"
            and selection.card_id not in state.candidate_card_ids
        ):
            raise MemeReleaseError("Planner 选择了当前候选集合之外的梗卡")
        return state

    def _session_candidates(
        self,
        *,
        session_id: str,
    ) -> Tuple[RetrievedMemeCandidate, ...]:
        state = self._candidate_states.get(session_id)
        if state is None:
            return ()
        if not state.is_fresh(self.config.serving.candidate_ttl_seconds):
            self._candidate_states.pop(session_id, None)
            self._retrieval_states.pop(session_id, None)
            return ()
        retrieval_state = self._retrieval_states.get(session_id)
        if (
            retrieval_state is None
            or retrieval_state.release_id != state.release_id
            or retrieval_state.semantic_query_text
            != state.semantic_query_text
        ):
            return ()
        allowed_ids = set(state.candidate_card_ids)
        return tuple(
            candidate
            for candidate in retrieval_state.candidates
            if candidate.card_id in allowed_ids
        )

    @staticmethod
    def _with_extra_prompt(
        kwargs: Mapping[str, Any],
        resource: str,
        *,
        reply_tool_args: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        existing_extra_prompt = str(
            kwargs.get("extra_prompt") or ""
        ).strip()
        modified_kwargs = dict(kwargs)
        modified_kwargs["extra_prompt"] = (
            f"{existing_extra_prompt}\n\n{resource}"
            if existing_extra_prompt
            else resource
        )
        if reply_tool_args is not None:
            modified_kwargs["reply_tool_args"] = dict(reply_tool_args)
        return {
            "action": "continue",
            "modified_kwargs": modified_kwargs,
        }

    def _remember_replyer_decision(
        self,
        *,
        session_id: str,
        selection: MemeToolSelection,
        source: str,
    ) -> None:
        self._replyer_decision_states[session_id] = ReplyerDecisionState(
            selection=selection,
            source=source,
            created_monotonic=time.monotonic(),
        )

    async def _run_semantic_selector(
        self,
        *,
        session_id: str,
        candidates: Tuple[RetrievedMemeCandidate, ...],
    ) -> Optional[SemanticSelectorDecision]:
        release = self._require_release()
        state = self._candidate_states.get(session_id)
        if state is None:
            return None
        prompt = release.build_semantic_selector_prompt(
            candidates,
            semantic_query_text=state.semantic_query_text,
            understand_only_card_ids=set(
                self.config.serving.understand_only_card_ids
            ),
        )
        started = perf_counter()
        try:
            result = await asyncio.wait_for(
                self.ctx.llm.generate(
                    prompt,
                    model=self.config.serving.semantic_selector_task_name,
                    temperature=0.0,
                    max_tokens=260,
                ),
                timeout=(
                    self.config.serving.semantic_selector_timeout_ms / 1000
                ),
            )
            if not isinstance(result, dict) or not result.get("success"):
                raise RuntimeError(f"语义裁判调用失败: {result}")
            raw_response = str(result.get("response") or "").strip()
            decision = release.parse_semantic_selector_response(
                raw_response,
                candidates=candidates,
                understand_only_card_ids=set(
                    self.config.serving.understand_only_card_ids
                ),
                minimum_confidence=(
                    self.config.serving
                    .semantic_selector_minimum_confidence
                ),
            )
        except Exception as exc:
            self.ctx.logger.warning(
                "meme_semantic_selector_error %s",
                json.dumps(
                    {
                        "event": "meme_semantic_selector_error",
                        "session": self._session_log_id(session_id),
                        "release_id": release.release_id,
                        "error_type": type(exc).__name__,
                        "elapsed_ms": round(
                            (perf_counter() - started) * 1000,
                            2,
                        ),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            return None

        self.ctx.logger.info(
            "meme_semantic_selector_decision %s",
            json.dumps(
                {
                    "event": "meme_semantic_selector_decision",
                    "session": self._session_log_id(session_id),
                    "release_id": release.release_id,
                    "model": str(result.get("model_name") or ""),
                    "requested_action": decision.requested_action,
                    "effective_action": decision.selection.action,
                    "card_id": decision.selection.card_id,
                    "route_index": decision.selection.route_index,
                    "confidence": round(decision.confidence, 4),
                    "matched_hard_blocks": list(
                        decision.matched_hard_blocks
                    ),
                    "reason": decision.reason,
                    "elapsed_ms": round(
                        (perf_counter() - started) * 1000,
                        2,
                    ),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        return decision

    async def _replyer_candidate_fallback(
        self,
        *,
        session_id: str,
        kwargs: Mapping[str, Any],
        source: str,
    ) -> Dict[str, Any]:
        if (
            not self.config.serving
            .replyer_considers_candidates_on_planner_skip
        ):
            return {"action": "continue"}
        candidates = self._session_candidates(session_id=session_id)
        if not candidates:
            return {"action": "continue"}
        release = self._require_release()
        if self.config.serving.semantic_selector_enabled:
            decision = await self._run_semantic_selector(
                session_id=session_id,
                candidates=candidates,
            )
            if decision is None or decision.selection.action == "SKIP":
                return {"action": "continue"}
            self._validate_selection_for_session(
                session_id=session_id,
                selection=decision.selection,
            )
            self._remember_replyer_decision(
                session_id=session_id,
                selection=decision.selection,
                source="semantic_selector",
            )
            resource = release.build_replyer_resource(
                decision.selection,
                effective_action=decision.selection.action,
                decision_source="semantic_selector",
            )
            effective_reply_tool_args = dict(
                kwargs.get("reply_tool_args") or {}
            )
            effective_reply_tool_args.update(
                {
                    MEME_ACTION_ARG: decision.selection.action,
                    MEME_RELEASE_ID_ARG: decision.selection.release_id,
                    MEME_CARD_ID_ARG: decision.selection.card_id,
                    MEME_ROUTE_INDEX_ARG: decision.selection.route_index,
                }
            )
            return self._with_extra_prompt(
                kwargs,
                resource,
                reply_tool_args=effective_reply_tool_args,
            )

        resource = release.build_replyer_candidate_resource(
            candidates,
            understand_only_card_ids=set(
                self.config.serving.understand_only_card_ids
            ),
        )
        self.ctx.logger.info(
            "meme_semantic_replyer_options %s",
            json.dumps(
                {
                    "event": "meme_semantic_replyer_options",
                    "session": self._session_log_id(session_id),
                    "release_id": release.release_id,
                    "source": source,
                    "candidate_card_ids": [
                        candidate.card_id for candidate in candidates
                    ],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        return self._with_extra_prompt(kwargs, resource)

    @HookHandler(
        "maisaka.replyer.before_request",
        name="meme_semantic_replyer_resource",
        description="验证 Planner 梗决策，并把 USE 卡作为 Replyer 一次性可选表达资源。",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=4500,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_replyer_before_request(self, **kwargs: Any) -> Dict[str, Any]:
        """只对经过本轮 Planner 校验的 USE 决策注入 Replyer。"""

        raw_reply_tool_args = kwargs.get("reply_tool_args")
        session_id = str(kwargs.get("session_id") or "").strip()
        if not await self._resolve_scoped_stream(session_id):
            return {"action": "continue"}
        self._replyer_decision_states.pop(session_id, None)
        if not isinstance(raw_reply_tool_args, Mapping):
            return await self._replyer_candidate_fallback(
                session_id=session_id,
                kwargs=kwargs,
                source="planner_args_missing",
            )
        if MEME_ACTION_ARG not in raw_reply_tool_args:
            return await self._replyer_candidate_fallback(
                session_id=session_id,
                kwargs=kwargs,
                source="planner_action_omitted",
            )
        release = self._require_release()
        selection = release.parse_tool_selection(raw_reply_tool_args)
        if selection is None:
            return {"action": "continue"}
        if selection.action == "SKIP":
            self._log_planner_decision(
                session_id=session_id,
                release_id=release.release_id,
                requested_action="SKIP",
                effective_action="SKIP",
                card_id="",
                route_index=0,
            )
            return await self._replyer_candidate_fallback(
                session_id=session_id,
                kwargs=kwargs,
                source="planner_skip",
            )
        self._validate_selection_for_session(
            session_id=session_id,
            selection=selection,
        )
        effective_action = selection.action
        if (
            effective_action == "USE"
            and selection.card_id in set(self.config.serving.understand_only_card_ids)
        ):
            effective_action = "UNDERSTAND_ONLY"

        self._log_planner_decision(
            session_id=session_id,
            release_id=release.release_id,
            requested_action=selection.action,
            effective_action=effective_action,
            card_id=selection.card_id,
            route_index=selection.route_index,
        )
        if effective_action == "SKIP":
            return {"action": "continue"}

        effective_selection = MemeToolSelection(
            action=effective_action,
            release_id=selection.release_id,
            card_id=selection.card_id,
            route_index=selection.route_index,
        )
        self._remember_replyer_decision(
            session_id=session_id,
            selection=effective_selection,
            source="planner",
        )
        meme_resource = release.build_replyer_resource(
            effective_selection,
            effective_action=effective_action,
        )
        return self._with_extra_prompt(kwargs, meme_resource)

    @HookHandler(
        "maisaka.replyer.after_response",
        name="meme_semantic_understand_only_quality_gate",
        description=(
            "用语义模型检查 UNDERSTAND_ONLY 回复是否复读、改写或解释了梗，"
            "违规时请求 Replyer 重生成。"
        ),
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=4500,
        error_policy=ErrorPolicy.SKIP,
    )
    async def gate_understand_only_response(
        self,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """仅为少量 UNDERSTAND_ONLY 卡执行发送前语义质量闸门。"""

        if not self.config.serving.understand_only_quality_gate_enabled:
            return {"action": "continue"}
        session_id = str(kwargs.get("session_id") or "").strip()
        decision_state = self._replyer_decision_states.get(session_id)
        if (
            decision_state is None
            or not decision_state.is_fresh(
                self.config.serving.candidate_ttl_seconds
            )
            or decision_state.selection.action != "UNDERSTAND_ONLY"
        ):
            return {"action": "continue"}
        response = str(kwargs.get("response") or "").strip()
        if not response:
            return {"action": "continue"}

        release = self._require_release()
        selection = decision_state.selection
        card = release.get_card(selection.card_id)
        if card is None:
            return {"action": "continue"}
        candidate_state = self._candidate_states.get(session_id)
        semantic_query_text = (
            candidate_state.semantic_query_text
            if candidate_state is not None
            else ""
        )
        prompt = (
            "你是梗语义输出质量闸门。只做整体语义判断，不使用关键词、"
            "正则或字面包含规则。本轮动作是 UNDERSTAND_ONLY：模型可以理解"
            "用户引用的梗，但最终回复不得复读、改写、解释、评价该梗是否贴切，"
            "也不得主动点出它；只能回应梗所指向的具体事件、人物、感受或风险。"
            "判断最终回复是否违反这一约束。严格只返回 JSON："
            '{"violation":true,"reason":"不超过60字"}。\n\n'
            "语义资源："
            + json.dumps(
                {
                    "canonical_expression": card["canonical_expression"],
                    "aliases": card.get("aliases") or [],
                    "semantic_core": card["semantic_core"],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + f"\n\n真实对话：\n{semantic_query_text}"
            + f"\n\n最终候选回复：\n{response}"
        )
        started = perf_counter()
        try:
            result = await asyncio.wait_for(
                self.ctx.llm.generate(
                    prompt,
                    model=self.config.serving.semantic_selector_task_name,
                    temperature=0.0,
                    max_tokens=180,
                ),
                timeout=(
                    self.config.serving
                    .understand_only_quality_gate_timeout_ms
                    / 1000
                ),
            )
            if not isinstance(result, dict) or not result.get("success"):
                raise RuntimeError(
                    f"UNDERSTAND_ONLY 语义检查失败: {result}"
                )
            evaluation = json.loads(
                str(result.get("response") or "").strip()
            )
            if not isinstance(evaluation, dict):
                raise TypeError("UNDERSTAND_ONLY 语义检查必须返回 JSON 对象")
            violation = bool(evaluation.get("violation"))
            reason = str(evaluation.get("reason") or "").strip()[:200]
        except Exception as exc:
            self.ctx.logger.warning(
                "meme_semantic_understand_only_gate_error %s",
                json.dumps(
                    {
                        "event": "meme_semantic_understand_only_gate_error",
                        "session": self._session_log_id(session_id),
                        "release_id": release.release_id,
                        "card_id": selection.card_id,
                        "error_type": type(exc).__name__,
                        "elapsed_ms": round(
                            (perf_counter() - started) * 1000,
                            2,
                        ),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            return {"action": "continue"}

        retry_count = int(kwargs.get("retry_count") or 0)
        retry_allowed = (
            violation
            and retry_count
            < self.config.serving.understand_only_quality_gate_max_retries
        )
        self.ctx.logger.info(
            "meme_semantic_understand_only_gate %s",
            json.dumps(
                {
                    "event": "meme_semantic_understand_only_gate",
                    "session": self._session_log_id(session_id),
                    "release_id": release.release_id,
                    "card_id": selection.card_id,
                    "violation": violation,
                    "retry_requested": retry_allowed,
                    "retry_count": retry_count,
                    "reason": reason,
                    "elapsed_ms": round(
                        (perf_counter() - started) * 1000,
                        2,
                    ),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        if not retry_allowed:
            return {"action": "continue"}
        return {
            "action": "continue",
            "modified_kwargs": {
                "retry": True,
                "retry_reason": (
                    "本轮是 UNDERSTAND_ONLY。不要复读、改写、解释或评价"
                    "用户引用的梗，也不要用近义说法呼应；只回应它所指向的"
                    "具体小猫、动作和用户的担心。"
                ),
            },
        }

    @HookHandler(
        "maisaka.replyer.after_response",
        name="meme_semantic_effect_observer",
        description="异步评估 USE 决策最终回复是否自然使用、是否生硬或误伤严肃语境。",
        mode=HookMode.OBSERVE,
        order=HookOrder.LATE,
        timeout_ms=20000,
        error_policy=ErrorPolicy.LOG,
    )
    async def observe_replyer_after_response(self, **kwargs: Any) -> Dict[str, Any]:
        """用独立语义评估记录线上真实回复表现，不改写可见回复。"""

        if not self.config.observability.enabled:
            return {"action": "continue"}
        raw_reply_tool_args = kwargs.get("reply_tool_args")
        session_id = str(kwargs.get("session_id") or "").strip()
        reply_message_id = str(kwargs.get("reply_message_id") or "").strip()
        if not self._stable_sample(
            session_id=session_id,
            reply_message_id=reply_message_id,
            sample_rate=self.config.observability.sample_rate,
        ):
            return {"action": "continue"}

        release = self._require_release()
        selection: Optional[MemeToolSelection] = None
        decision_source = ""
        replyer_state = self._replyer_decision_states.get(session_id)
        if (
            replyer_state is not None
            and replyer_state.is_fresh(
                self.config.serving.candidate_ttl_seconds
            )
        ):
            selection = replyer_state.selection
            decision_source = replyer_state.source
        elif (
            isinstance(raw_reply_tool_args, Mapping)
            and MEME_ACTION_ARG in raw_reply_tool_args
        ):
            parsed_selection = release.parse_tool_selection(
                raw_reply_tool_args
            )
            if parsed_selection is not None and parsed_selection.action == "USE":
                selection = parsed_selection
                decision_source = "planner"
        if selection is None or selection.action != "USE":
            return {"action": "continue"}
        state = self._validate_selection_for_session(
            session_id=session_id,
            selection=selection,
        )
        if selection.card_id in set(self.config.serving.understand_only_card_ids):
            return {"action": "continue"}

        response = str(kwargs.get("response") or "").strip()
        if not response:
            return {"action": "continue"}
        card = release.get_card(selection.card_id)
        if card is None:
            raise MemeReleaseError(f"效果评估找不到梗卡: {selection.card_id}")
        route = card["usage_routes"][selection.route_index]
        prompt = (
            "你是线上回复的梗语义效果评估器。只做语义判断，不使用关键词或正则命中。"
            "判断最终回复是否真正使用或自然呼应了候选梗，以及是否生硬、抢话、"
            "误伤严肃语境或暴露内部信息。严格只返回 JSON："
            '{"used":true,"natural":true,"forced":false,'
            '"serious_context_violation":false,"internal_leakage":false,'
            '"reason":"不超过100字"}\n\n'
            f"真实对话上下文：\n{state.semantic_query_text}\n\n"
            "候选梗：\n"
            + json.dumps(
                {
                    "canonical_expression": card["canonical_expression"],
                    "semantic_core": card["semantic_core"],
                    "route": route,
                    "hard_blocks": card["hard_blocks"],
                },
                ensure_ascii=False,
            )
            + f"\n\n最终可见回复：\n{response}"
        )
        started = perf_counter()
        result = await asyncio.wait_for(
            self.ctx.llm.generate(
                prompt,
                model=self.config.observability.evaluator_task_name,
                temperature=0.0,
                max_tokens=300,
            ),
            timeout=self.config.observability.evaluator_timeout_ms / 1000,
        )
        raw_evaluation = (
            str(result.get("response") or "").strip()
            if isinstance(result, dict)
            else ""
        )
        evaluation = json.loads(raw_evaluation)
        if not isinstance(evaluation, dict):
            raise TypeError("梗效果评估结果必须是 JSON 对象")

        event: Dict[str, Any] = {
            "event": "meme_semantic_effect",
            "session": self._session_log_id(session_id),
            "reply_message_id": reply_message_id,
            "release_id": release.release_id,
            "card_id": selection.card_id,
            "expression": card["canonical_expression"],
            "route_index": selection.route_index,
            "decision_source": decision_source,
            "used": bool(evaluation.get("used")),
            "natural": bool(evaluation.get("natural")),
            "forced": bool(evaluation.get("forced")),
            "serious_context_violation": bool(
                evaluation.get("serious_context_violation")
            ),
            "internal_leakage": bool(evaluation.get("internal_leakage")),
            "reason": str(evaluation.get("reason") or "")[:200],
            "evaluation_ms": round((perf_counter() - started) * 1000, 2),
        }
        if self.config.observability.include_response_preview_in_logs:
            event["response_preview"] = response[:300]
        self.ctx.logger.info(
            "meme_semantic_effect %s",
            json.dumps(event, ensure_ascii=False, separators=(",", ":")),
        )
        return {"action": "continue"}


def create_plugin() -> MemeSemanticPlugin:
    """插件加载器入口。"""

    return MemeSemanticPlugin()
