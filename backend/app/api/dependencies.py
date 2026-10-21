"""FastAPI 依赖：组装仓储与服务。"""

from functools import lru_cache
from typing import Annotated

from agentscope.agent import ReActAgent
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.agent_factory import AgentBuilder, AgentFactory
from app.agent.agent_memory_guard import AgentMemoryGuard
from app.agent.agent_memory_policy import AgentMemoryPolicy
from app.agent.clarify.agent_builder import ClarifyAgentBuilder
from app.agent.emotion.agent_builder import EmotionAgentBuilder
from app.agent.intent.agent_builder import IntentAgentBuilder
from app.agent.perspective.agent_builder import PerspectiveAgentBuilder as PerspectiveBuilder
from app.agent.prompt_loader import PromptLoader
from app.agent.recommend.agent_builder import RecommendAgentBuilder
from app.compliance.compliance_checker import ComplianceChecker
from app.config import get_settings
from app.events.voice_events import VoiceEventListeners, VoiceEventPublisher
from app.infrastructure.dashscope import (
    get_light_chat_model,
    get_main_chat_model,
    get_multi_agent_chat_model,
)
from app.infrastructure.database import get_db
from app.infrastructure.redis import get_redis
from app.listener.session_expire_listener import SessionExpireListener
from app.memory.short_term_memory import ShortTermMemory
from app.memory.turn_summarizer import TurnSummarizer
from app.repositories.faq_repository import FaqRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.product_repository import ProductRepository
from app.repositories.session_repository import (
    SessionRepository,
    SessionStateRepository,
)
from app.repositories.user_profile_repository import (
    UserProfileDynamicRepository,
    UserProfileStaticRepository,
)
from app.repositories.user_repository import AppUserRepository
from app.services.auth_service import AuthService
from app.services.cache_service import RedisCacheService
from app.services.clarify_rule_service import ClarifyRuleService
from app.services.clarify_service import ClarifyService
from app.services.embedding_cache import CachedEmbeddingService
from app.services.embedding_service import DashScopeEmbeddingService, EmbeddingService
from app.services.emotion_service import EmotionService
from app.services.emotion_streaming_service import EmotionStreamingService
from app.services.faq_vector_service import FaqVectorService
from app.services.index_service import FaqIndexService, ProductIndexService
from app.services.intent_cache import IntentCache
from app.services.intent_service import IntentService
from app.services.long_term_memory_writer import LongTermMemoryWriter
from app.services.orchestrator_service import OrchestratorService
from app.services.order_service import OrderService
from app.services.parallel_recommend_service import ParallelRecommendService
from app.services.pending_order_store import PendingOrderStore
from app.services.perspective_hub_service import PerspectiveHubService
from app.services.product_search_service import (
    PgVectorProductSearchService,
    ProductSearchService,
)
from app.services.profile_reranker import ProfileReranker
from app.services.recommend_candidates_service import RecommendCandidatesService
from app.services.recommend_orchestrator import RecommendOrchestrator
from app.services.session_mood_detector import SessionMoodDetector
from app.services.session_scope_cache import SessionScopeCache
from app.services.session_scope_service import SessionScopeService
from app.services.session_service import SessionService
from app.services.session_state_service import SessionStateService
from app.services.user_behavior_sink import UserBehaviorSink
from app.services.user_profile_service import UserProfileService
from app.voice.asr_service import AsrService
from app.voice.cached_tts_phrases import CachedTtsPhrases
from app.voice.tts_service import TtsService


def get_embedding_service() -> EmbeddingService:
    """Embedding 服务：DashScope 实现外面套一层 Redis 缓存。

    **query 路径不受影响**——:class:`CachedEmbeddingService` 只缓存 :meth:`embed`，
    :meth:`embed_query` 直接穿透。商品检索 / FAQ 检索用的都是后者。

    Note:
        缓存前缀与 TTL 单独一套（``voice_agent:embed:`` / 7 天），不复用业务缓存的
        ``voice_agent:cache:`` / 30 分钟——两者生命周期差 300 倍，混在一个命名空间里
        既难排查（``KEYS voice_agent:cache:*`` 会混进几千条向量），也没法单独清理。
    """
    settings = get_settings()
    return CachedEmbeddingService(
        delegate=DashScopeEmbeddingService(),
        cache=RedisCacheService(
            redis=get_redis(),
            key_prefix=settings.embedding_cache_key_prefix,
            default_ttl_seconds=settings.embedding_cache_ttl_seconds,
        ),
        ttl_seconds=settings.embedding_cache_ttl_seconds,
    )


def get_cache_service() -> RedisCacheService:
    """Redis JSON 缓存。"""
    settings = get_settings()
    return RedisCacheService(
        redis=get_redis(),
        key_prefix=settings.cache_key_prefix,
        default_ttl_seconds=settings.cache_ttl_seconds,
    )


def get_short_term_memory() -> ShortTermMemory:
    """会话短期记忆。"""
    return ShortTermMemory(redis=get_redis())


def get_session_scope_cache() -> SessionScopeCache:
    """会话商家范围缓存。

    Note:
        定义位置比较靠前，是被 ``Depends(...)`` 的求值时机逼的——
        它在 ``def`` 语句执行时就求值，那三个 provider
        都要引用它，放后面会直接 NameError。**这里的注释比位置更重要**：
        改动本文件时新增 provider 一律往后加，只有被更早引用的才往上提。
    """
    return SessionScopeCache(get_redis())


def get_pending_order_store() -> PendingOrderStore:
    """待确认订单（预览单）存储。

    Note:
        与 :func:`get_session_scope_cache` 同理——``get_order_service`` 要引用它，
        而它必须排在更前面。**``Depends()`` 在函数定义时就求值**，放后面就是
        ``NameError``。规则见文件末尾"装配根"那一段。
    """
    return PendingOrderStore(get_redis())


def get_product_repository(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProductRepository:
    return ProductRepository(db)


def get_faq_repository(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FaqRepository:
    return FaqRepository(db)


def get_product_search_service(
    repository: Annotated[ProductRepository, Depends(get_product_repository)],
    embedding: Annotated[EmbeddingService, Depends(get_embedding_service)],
) -> ProductSearchService:
    return PgVectorProductSearchService(repository, embedding)


def get_product_index_service(
    repository: Annotated[ProductRepository, Depends(get_product_repository)],
    embedding: Annotated[EmbeddingService, Depends(get_embedding_service)],
) -> ProductIndexService:
    return ProductIndexService(repository, embedding)


def get_faq_vector_service(
    repository: Annotated[FaqRepository, Depends(get_faq_repository)],
    embedding: Annotated[EmbeddingService, Depends(get_embedding_service)],
) -> FaqVectorService:
    return FaqVectorService(repository, embedding)


def get_faq_index_service(
    repository: Annotated[FaqRepository, Depends(get_faq_repository)],
    embedding: Annotated[EmbeddingService, Depends(get_embedding_service)],
) -> FaqIndexService:
    return FaqIndexService(repository, embedding)


# ---------------------------------------------------------------- 用户画像与会话


def get_user_profile_static_repository(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserProfileStaticRepository:
    return UserProfileStaticRepository(db)


def get_user_profile_dynamic_repository(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserProfileDynamicRepository:
    return UserProfileDynamicRepository(db)


def get_user_profile_service(
    static_repo: Annotated[
        UserProfileStaticRepository, Depends(get_user_profile_static_repository)
    ],
    dynamic_repo: Annotated[
        UserProfileDynamicRepository, Depends(get_user_profile_dynamic_repository)
    ],
    cache: Annotated[RedisCacheService, Depends(get_cache_service)],
) -> UserProfileService:
    return UserProfileService(static_repo, dynamic_repo, cache=cache)


def get_session_repository(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionRepository:
    return SessionRepository(db)


def get_session_state_repository(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionStateRepository:
    return SessionStateRepository(db)


def get_session_service(
    repo: Annotated[SessionRepository, Depends(get_session_repository)],
) -> SessionService:
    return SessionService(repo)


def get_session_state_service(
    repo: Annotated[SessionStateRepository, Depends(get_session_state_repository)],
) -> SessionStateService:
    return SessionStateService(repo, redis=get_redis())


def get_user_behavior_sink(
    repo: Annotated[
        UserProfileDynamicRepository, Depends(get_user_profile_dynamic_repository)
    ],
) -> UserBehaviorSink:
    return UserBehaviorSink(repo)


# ---------------------------------------------------------------- ASR / TTS


def get_asr_service() -> AsrService:
    """ASR 服务。"""
    return AsrService()


def get_tts_service() -> TtsService:
    """TTS 服务。"""
    return TtsService()


@lru_cache(maxsize=1)
def get_cached_tts_phrases() -> CachedTtsPhrases:
    """TTS 过渡语预合成缓存。

    **必须进程内单例**——预热出来的 PCM 要能被所有会话共享；
    每次请求新建一个的话，预热白做，缓存永远是空的。
    """
    return CachedTtsPhrases(tts=get_tts_service())


# ---------------------------------------------------------------- Prompt / Agent 工厂 / 意图识别


@lru_cache(maxsize=1)
def get_prompt_loader() -> PromptLoader:
    """Prompt 加载器（无状态，进程内复用）。"""
    return PromptLoader()


@lru_cache(maxsize=1)
def get_agent_factory() -> AgentFactory:
    """Agent 工厂（内部持有按会话的 Agent LRU 缓存，必须进程内单例）。

    进程内单例，所有会话共享。
    """
    return AgentFactory(
        intent_builder=IntentAgentBuilder(get_light_chat_model(), get_prompt_loader()).build,
        clarify_builder=ClarifyAgentBuilder(get_light_chat_model(), get_prompt_loader()).build,
        recommend_builder=RecommendAgentBuilder(get_main_chat_model(), get_prompt_loader()).build,
        emotion_builder=EmotionAgentBuilder(get_main_chat_model(), get_prompt_loader()).build,
        perspective_builder=PerspectiveBuilder(
            get_multi_agent_chat_model(), get_prompt_loader()
        ).build,
    )


def _pending_builder(name: str) -> AgentBuilder:
    """尚未实现的 Builder 占位。

    这些 Builder 分别在各自模块中落地，占位做成**调用时才报错**，
    这样工厂本身、意图 Agent 和它的调试接口现在就能完整工作。
    """

    def _build() -> ReActAgent:
        raise NotImplementedError(f"{name} 尚未实现")

    return _build


@lru_cache(maxsize=1)
def get_agent_memory_guard() -> AgentMemoryGuard:
    """Agent 记忆巡检。

    **必须进程内单例**：它持有那个后台巡检 task，每次请求新建一个的话，
    起的是一堆各扫各的循环，而且谁也不会被 ``stop()`` 掉。
    """
    settings = get_settings()
    return AgentMemoryGuard(
        factory=get_agent_factory(),
        interval_seconds=settings.memory_guard_interval_seconds,
    )


def get_intent_cache() -> IntentCache:
    """意图结果缓存。"""
    return IntentCache(get_redis())


@lru_cache(maxsize=1)
def get_agent_memory_policy() -> AgentMemoryPolicy:
    """Agent 内部记忆裁剪策略（无状态，进程内复用）。

    Note:
        必须定义在**第一个使用它的 provider 之前**——``Depends(...)`` 是在
        ``def`` 语句执行时求值的，不是调用时才求值。放后面会直接 NameError。
    """
    return AgentMemoryPolicy()


def get_intent_service(
    factory: Annotated[AgentFactory, Depends(get_agent_factory)],
    memory: Annotated[ShortTermMemory, Depends(get_short_term_memory)],
    cache: Annotated[IntentCache, Depends(get_intent_cache)],
    policy: Annotated[AgentMemoryPolicy, Depends(get_agent_memory_policy)],
) -> IntentService:
    """意图识别 Service。"""
    return IntentService(factory, memory, cache=cache, memory_policy=policy)


# ---------------------------------------------------------------- 需求澄清


@lru_cache(maxsize=1)
def get_clarify_rule_service() -> ClarifyRuleService:
    """澄清规则表（加载一次，进程内复用——改了规则要重启）。"""
    return ClarifyRuleService()


def get_clarify_service(
    factory: Annotated[AgentFactory, Depends(get_agent_factory)],
    rule_service: Annotated[ClarifyRuleService, Depends(get_clarify_rule_service)],
) -> ClarifyService:
    """需求澄清 Service。"""
    return ClarifyService(factory, rule_service)


# ---------------------------------------------------------------- 推荐召回与重排


def get_recommend_candidates_service(
    search: Annotated[ProductSearchService, Depends(get_product_search_service)],
) -> RecommendCandidatesService:
    """候选召回 Service。"""
    return RecommendCandidatesService(search)


def get_profile_reranker() -> ProfileReranker:
    """画像加权重排器（无状态，进程内复用即可）。"""
    return ProfileReranker()


# ``get_recommend_reason_service`` 这个 provider 已经被删掉了：
# ``RecommendReasonService`` 已不在热路径上（理由生成并进了情感 Agent），
# 留一个没有任何依赖者的 provider 只会让人以为它还在用。类本身保留，
# 见 ``app/services/recommend_reason_service.py`` 的说明。


def get_recommend_orchestrator(
    candidates: Annotated[
        RecommendCandidatesService, Depends(get_recommend_candidates_service)
    ],
    reranker: Annotated[ProfileReranker, Depends(get_profile_reranker)],
    profile_service: Annotated[UserProfileService, Depends(get_user_profile_service)],
    scope_cache: Annotated[SessionScopeCache, Depends(get_session_scope_cache)],
) -> RecommendOrchestrator:
    """推荐链路编排器。"""
    return RecommendOrchestrator(
        candidates, reranker, profile_service, scope_cache=scope_cache
    )


# ---------------------------------------------------------------- 情感应答


def get_session_mood_detector(
    memory: Annotated[ShortTermMemory, Depends(get_short_term_memory)],
) -> SessionMoodDetector:
    """会话情绪识别器。"""
    return SessionMoodDetector(memory)


def get_emotion_service(
    factory: Annotated[AgentFactory, Depends(get_agent_factory)],
    mood_detector: Annotated[SessionMoodDetector, Depends(get_session_mood_detector)],
    policy: Annotated[AgentMemoryPolicy, Depends(get_agent_memory_policy)],
) -> EmotionService:
    """情感应答 Service。"""
    return EmotionService(factory, mood_detector, memory_policy=policy)


def get_emotion_streaming_service(
    factory: Annotated[AgentFactory, Depends(get_agent_factory)],
    mood_detector: Annotated[SessionMoodDetector, Depends(get_session_mood_detector)],
    policy: Annotated[AgentMemoryPolicy, Depends(get_agent_memory_policy)],
) -> EmotionStreamingService:
    """情感应答 Service 的流式版。

    与 :func:`get_emotion_service` 并列而不是二选一——非流式的
    ``/api/v1/chat`` 走的是前者，它要把整段话一次性返回给 HTTP 调用方；
    流式那份只服务 WebSocket。两个 provider 共享同一个 ``AgentFactory``，
    所以底下是**同一个 Agent 实例**，只是出口不同。
    """
    return EmotionStreamingService(factory, mood_detector, memory_policy=policy)


# ---------------------------------------------------------------- 多视角点评团 / 并行推荐


def get_perspective_hub_service(
    factory: Annotated[AgentFactory, Depends(get_agent_factory)],
) -> PerspectiveHubService:
    """多视角点评团 Service。"""
    return PerspectiveHubService(factory)


def get_parallel_recommend_service(
    profile_service: Annotated[UserProfileService, Depends(get_user_profile_service)],
    candidates: Annotated[
        RecommendCandidatesService, Depends(get_recommend_candidates_service)
    ],
    reranker: Annotated[ProfileReranker, Depends(get_profile_reranker)],
    scope_cache: Annotated[SessionScopeCache, Depends(get_session_scope_cache)],
) -> ParallelRecommendService:
    """并行版推荐链路（画像 ∥ 召回）。"""
    return ParallelRecommendService(
        profile_service, candidates, reranker, scope_cache=scope_cache
    )


@lru_cache(maxsize=1)
def get_voice_event_publisher() -> VoiceEventPublisher:
    """用户讲话事件发布器（进程内单例——订阅者列表要能累积）。"""
    return VoiceEventPublisher()


def get_voice_event_listeners(
    profile_service: Annotated[UserProfileService, Depends(get_user_profile_service)],
) -> VoiceEventListeners:
    """内置的两个监听者（画像预热 + 审计）。"""
    return VoiceEventListeners(profile_service)


# ---------------------------------------------------------------- 合规与长期记忆回流


@lru_cache(maxsize=1)
def get_compliance_checker() -> ComplianceChecker:
    """合规兜底（词表进程启动时读一次）。"""
    return ComplianceChecker.from_settings()


def get_long_term_memory_writer(
    repo: Annotated[
        UserProfileDynamicRepository, Depends(get_user_profile_dynamic_repository)
    ],
    state_service: Annotated[SessionStateService, Depends(get_session_state_service)],
    memory: Annotated[ShortTermMemory, Depends(get_short_term_memory)],
    profile_service: Annotated[UserProfileService, Depends(get_user_profile_service)],
) -> LongTermMemoryWriter:
    """长期记忆回流器。"""
    return LongTermMemoryWriter(repo, state_service, memory, profile_service)


def get_session_expire_listener(
    writer: Annotated[LongTermMemoryWriter, Depends(get_long_term_memory_writer)],
    session_service: Annotated[SessionService, Depends(get_session_service)],
) -> SessionExpireListener:
    """会话 TTL 过期监听器（第三个回流触发点）。

    Note:
        需要 Redis 开启 keyspace notifications（``CONFIG SET notify-keyspace-events Ex``），
        否则订阅得到的是**永久的沉默**——不报错，只是什么都不来。
    """
    return SessionExpireListener(get_redis(), writer, session_service)


# ---------------------------------------------------------------- 鉴权 / 会话范围 / 订单


def get_app_user_repository(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AppUserRepository:
    return AppUserRepository(db)


def get_order_repository(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OrderRepository:
    return OrderRepository(db)


def get_auth_service(
    repo: Annotated[AppUserRepository, Depends(get_app_user_repository)],
) -> AuthService:
    """登录服务。"""
    return AuthService(repo)


def get_order_service(
    repo: Annotated[OrderRepository, Depends(get_order_repository)],
    products: Annotated[ProductRepository, Depends(get_product_repository)],
    pending: Annotated[PendingOrderStore, Depends(get_pending_order_store)],
    memory_writer: Annotated[
        LongTermMemoryWriter, Depends(get_long_term_memory_writer)
    ],
    behavior_sink: Annotated[UserBehaviorSink, Depends(get_user_behavior_sink)],
) -> OrderService:
    """订单服务（含下单写路径）。

    Note:
        **这五个依赖里有三个是"下单成功之后的回流"**——画像、长期记忆。
        它们在 ``OrderService`` 里是可选的（``None`` 就跳过），但装配时
        必须是真货：否则用户下了单，画像不更新、会话不回流，
        而这一切**不会有任何报错**。
    """
    return OrderService(
        repo,
        products=products,
        pending=pending,
        memory_writer=memory_writer,
        behavior_sink=behavior_sink,
    )


def get_session_scope_service(
    products: Annotated[ProductRepository, Depends(get_product_repository)],
    sessions: Annotated[SessionService, Depends(get_session_service)],
    scope_cache: Annotated[SessionScopeCache, Depends(get_session_scope_cache)],
) -> SessionScopeService:
    """会话启动编排。"""
    return SessionScopeService(products, sessions, scope_cache)


# ---------------------------------------------------------------- 装配根（必须放在最后）
#
# **这个位置不是随便放的，改回去会直接 ImportError。**
#
# FastAPI 的 ``Depends(get_xxx)`` 在 **``def`` 语句执行时**就求值（拿的是函数对象），
# 所以被引用的 provider 必须在**这一行之前**已经定义。
# 这是 Python 模块级执行顺序带来的约束，与依赖图无关。
#
# 早先把 ``get_session_scope_cache`` 往上提过一次（治标）；后来，
# ``get_orchestrator_service`` 要引用 ``get_order_service``，而后者又依赖
# ``get_order_repository`` 和 ``get_long_term_memory_writer``——链条一长，
# "谁该在谁前面"就彻底算不清了。
#
# 所以这里换了个治法：**把唯一的装配根放到文件最后**。规则从此只剩一条——
# 新的 provider 往后加，装配根永远垫底。谁也不必再推演依赖顺序。


def get_orchestrator_service(
    intent_service: Annotated[IntentService, Depends(get_intent_service)],
    clarify_service: Annotated[ClarifyService, Depends(get_clarify_service)],
    recommend_service: Annotated[
        RecommendOrchestrator, Depends(get_recommend_orchestrator)
    ],
    emotion_service: Annotated[EmotionService, Depends(get_emotion_service)],
    emotion_streaming: Annotated[
        EmotionStreamingService, Depends(get_emotion_streaming_service)
    ],
    tts: Annotated[TtsService, Depends(get_tts_service)],
    tts_cache: Annotated[CachedTtsPhrases, Depends(get_cached_tts_phrases)],
    session_service: Annotated[SessionService, Depends(get_session_service)],
    state_service: Annotated[SessionStateService, Depends(get_session_state_service)],
    memory: Annotated[ShortTermMemory, Depends(get_short_term_memory)],
    event_publisher: Annotated[
        VoiceEventPublisher, Depends(get_voice_event_publisher)
    ],
    compliance: Annotated[ComplianceChecker, Depends(get_compliance_checker)],
    product_repo: Annotated[ProductRepository, Depends(get_product_repository)],
    perspective_hub: Annotated[
        PerspectiveHubService, Depends(get_perspective_hub_service)
    ],
    order_service: Annotated[OrderService, Depends(get_order_service)],
    scope_cache: Annotated[SessionScopeCache, Depends(get_session_scope_cache)],
) -> OrchestratorService:
    """Orchestrator 主链路。

    ``RecommendOrchestrator``（顺序版）和 ``ParallelRecommendService``（并行版）
    签名兼容，想换并行实现只改这一处依赖即可。

    ``order_service`` + ``scope_cache`` 是后来接进来的：订单分支需要建预览单、
    落订单，而 ``preview`` 要按会话范围校验商品——所以编排层也得能拿到 scope 缓存。

    ``emotion_streaming`` + ``tts`` 是后来接进来的。**TTS 从 WebSocket 层
    挪到了这里**：只有编排层知道一句口播什么时候算完整，也只有它手上同时握着
    逐 Token 的文字和"已经说出去多少"。留在 WebSocket 层的话，那边要么只能
    等整段话（等于没流式），要么自己再实现一遍句子聚合。
    """
    return OrchestratorService(
        intent_service=intent_service,
        clarify_service=clarify_service,
        recommend_service=recommend_service,
        emotion_service=emotion_service,
        session_service=session_service,
        state_service=state_service,
        memory=memory,
        event_publisher=event_publisher,
        compliance=compliance,
        product_repo=product_repo,
        perspective_hub=perspective_hub,
        perspective_enabled=get_settings().perspective_enabled,
        summarizer=TurnSummarizer(),
        order_service=order_service,
        scope_cache=scope_cache,
        tts=tts,
        emotion_streaming=emotion_streaming,
        tts_cache=tts_cache,
    )
