"""Orchestrator 编排服务。

**一条用户输入从 WebSocket 进来，走完全链路，到 TTS 之前的那一步。**

实现方案是**手写 Service 级状态机**，不用框架的流水线原语——流程里有条件分支和
提前 return，顺序流水线不适合；状态机本身也就一百多行，可读性比强行套框架高得多。

四条主分支（状态图）：

    意图 → ┬ PRODUCT_RECOMMENDATION → 澄清判断 → 推荐 → 情感
           ├ CLARIFY_NEEDED         → 澄清（不推荐）
           ├ PRODUCT_COMPARE        → 换锚点重推
           ├ ORDER_CONFIRM          → 订单
           ├ CHITCHAT               → 情感闲聊模式
           └ OUT_OF_SCOPE           → 固定话术拒绝

最后统一过一遍合规兜底。
"""

import logging
import time
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

from app.compliance.compliance_checker import ComplianceChecker
from app.memory.short_term_memory import ShortTermMemory, Turn
from app.memory.turn_summarizer import TurnSummarizer
from app.models.session import SessionState
from app.repositories.product_repository import ProductRepository
from app.schemas.emotion import EmotionResult
from app.schemas.intent import IntentResult, IntentType
from app.schemas.recommend import TONE_CHITCHAT, RecommendResult
from app.schemas.stream import StreamChunk
from app.services import chitchat_templates
from app.services.clarify_service import ClarifyService
from app.services.emotion_service import EmotionService, fallback
from app.services.emotion_streaming_service import EmotionStreamingService
from app.services.intent_service import IntentService
from app.services.order_intent_rules import Confirmation, classify_confirmation
from app.services.order_reference_resolver import OrderReferenceResolver
from app.services.order_service import OrderError, OrderService
from app.services.perspective_hub_service import PerspectiveHubService
from app.services.recommend_orchestrator import RecommendOrchestrator
from app.services.scope_filter import resolve_scope
from app.services.session_scope_cache import SessionScopeCache
from app.services.session_service import SessionService
from app.services.session_state_service import SessionStateService
from app.voice.cached_tts_phrases import CachedTtsPhrases
from app.voice.sentence_aggregator import SentenceAggregator, split_into_sentences
from app.voice.tts_service import TtsService, single_text_stream

logger = logging.getLogger(__name__)

#: 会话阶段常量（与 ``SessionState.phase`` 的取值一致）
PHASE_INTENT = "INTENT"
PHASE_CLARIFY = "CLARIFY"
PHASE_RECOMMEND = "RECOMMEND"
PHASE_ORDER_CONFIRM = "ORDER_CONFIRM"
PHASE_ENDED = "ENDED"

#: 下单成功后的订单号展示位数。
ORDER_NO_DISPLAY_LEN = 6

#: "便宜点"的价格下调系数：新上限 = 上轮最高价 × 0.8
CHEAPER_FACTOR = Decimal("0.8")

#: "贵点"的价格上调系数：新下限 = 上轮最低价 × 1.2
EXPENSIVE_FACTOR = Decimal("1.2")

#: 槽位里表示"价格方向"的键
PRICE_DIRECTION = "priceDirection"

#: 越权 / 兜底拒绝话术
OUT_OF_SCOPE_SPEECH = (
    "鸡哥现在只负责帮你挑商品，这个问题回头可以找客服处理哈。我们继续聊想买什么？"
)

#: 指代解析失败时的追问话术。
#:
#: **这里刻意不用"第一款"举例，否则会出洋相**：合规兜底把"第一"当绝对化
#: 用语（``SOFTEN_MAP["第一"] = "常用"``），于是用户听到的是
#: "可以说常用款、第二款或者商品名"——**推荐列表里根本没有"常用款"这个东西**。
#: 换个不含绝对化用语的例子，就绕开了整件事（判定见
#: ``tests/test_order_flow.py`` 的"口播话术必须原样通过合规"）。
ORDER_ASK_WHICH_SPEECH = "你想要的是刚才推荐的哪一款？可以说第二款、第三款，或者直接说商品名。"

#: 待确认单已经在手，但用户既没说确认也没说取消
ORDER_ASK_CONFIRM_SPEECH = "那你是确认要这款还是不要？"

#: 用户取消下单
ORDER_CANCELLED_SPEECH = "好的，鸡哥没给你下。想再聊点别的还是换款看看？"

#: 下单失败（库存不足 / 预览单过期）时的话术。**措辞刻意模糊**——
#: "库存不足"和"单子过期了"对用户来说都是"这单没成，重来一次"，
#: 分得太细只会让人困惑。
ORDER_FAILED_SPEECH = "这单没下成，可能库存刚被抢完了，要不咱们换一款看看？"

#: 澄清分支里"其实信息够了"时的应答
CLARIFY_READY_SPEECH = "好，我这就帮你挑。"

#: 短期记忆里一轮对话的角色标记（只写这一种）。
#: 写成常量是为了让"哪些地方在写记忆"可搜索。
TURN_ROLE = "TURN"


class OrchestratorService:
    """多 Agent 协作的状态机。

    Args:
        intent_service: 意图识别。
        clarify_service: 澄清判断。
        recommend_service: 推荐链路（顺序版或并行版都可以，见
            :class:`~app.services.recommend_orchestrator.RecommendOrchestrator`
            与 :class:`~app.services.parallel_recommend_service.ParallelRecommendService`）。
        emotion_service: 情感包装。
        session_service: 会话主表（首轮幂等开启）。
        state_service: 会话状态机读写。
        memory: 短期记忆。
        event_publisher: 用户讲话事件发布器。
        compliance: 合规兜底。
        product_repo: 商品仓储（比较分支查上轮推荐的实际价格）。
        perspective_hub: 多视角点评团（旁路，默认关闭）。
        perspective_enabled: 是否启用点评团旁路。
        tts: TTS 服务。**只有 :meth:`stream_handle` 用得到**——非流式的
            :meth:`handle` 只吐文本，音频由调用方（``/api/v1/chat`` 不合成、
            WebSocket 走流式）自己安排。缺省 ``None`` 时流式口播只有字幕没有声音。
        emotion_streaming: 情感 Agent 的流式版本。缺省 ``None`` 时
            :meth:`_stream_speech` 退回整段包装——**降级方向是"慢一点"，不是"坏掉"**。
        tts_cache: TTS 过渡语预合成缓存。缺省 ``None`` 时
            每一句都走真实合成——**只是没省到钱，业务行为完全一样**。
    """

    def __init__(
        self,
        intent_service: IntentService,
        clarify_service: ClarifyService,
        recommend_service: RecommendOrchestrator,
        emotion_service: EmotionService,
        session_service: SessionService,
        state_service: SessionStateService,
        memory: ShortTermMemory,
        event_publisher: Any,
        compliance: ComplianceChecker,
        product_repo: ProductRepository,
        perspective_hub: PerspectiveHubService | None = None,
        perspective_enabled: bool = False,
        summarizer: TurnSummarizer | None = None,
        order_service: OrderService | None = None,
        scope_cache: SessionScopeCache | None = None,
        reference_resolver: OrderReferenceResolver | None = None,
        tts: TtsService | None = None,
        emotion_streaming: EmotionStreamingService | None = None,
        tts_cache: CachedTtsPhrases | None = None,
    ) -> None:
        self._intent = intent_service
        self._clarify = clarify_service
        self._recommend = recommend_service
        self._emotion = emotion_service
        self._session = session_service
        self._state = state_service
        self._memory = memory
        self._events = event_publisher
        self._compliance = compliance
        self._products = product_repo
        self._perspective = perspective_hub
        self._perspective_enabled = perspective_enabled
        self._summarizer = summarizer or TurnSummarizer()
        self._orders = order_service
        self._scope_cache = scope_cache
        self._resolver = reference_resolver or OrderReferenceResolver()
        self._tts = tts
        self._emotion_stream = emotion_streaming
        self._tts_cache = tts_cache

    async def handle(
        self, session_id: str, user_id: int, utterance: str
    ) -> EmotionResult:
        """处理一次用户输入的主入口（**非流式**，给 ``/api/v1/chat`` 文本调试用）。

        Returns:
            封装好的结果（``speech_text`` + ``display_blocks``），交给 TTS 下发。
        """
        state = await self._begin_turn(session_id, user_id, utterance)

        if self._is_order_short_circuit(state):
            result = await self._handle_order_confirm(session_id, user_id, state, utterance)
            result = self._compliance.ensure_compliant(session_id, user_id, result)
            await self._state.save(state)
            return result

        intent = await self._classify(session_id, state, utterance)
        result = await self._dispatch(session_id, user_id, state, utterance, intent)

        # 合规兜底：TTS 前的最后一道闸门，不降级（原则三）
        result = self._compliance.ensure_compliant(session_id, user_id, result)
        await self._remember(session_id, utterance, intent, result.speech_text)
        await self._state.save(state)
        return result

    async def stream_handle(
        self, session_id: str, user_id: int, utterance: str
    ) -> AsyncIterator[StreamChunk]:
        """处理一次用户输入，**逐片下发**。

        这是语音链路的正式入口——用户不用等整段话生成完，第一句一好就先出声。

        分片顺序：

        ===========================  ============================================
        分支                          下发顺序
        ===========================  ============================================
        推荐                          商品卡片 → （第一句文字 → 第一句音频）→ …
        其余五条分支                   整段文字 → 整段音频（逐句合成）
        ===========================  ============================================

        .. important::

           **两个入口的簿记必须完全一致。** 推荐分支是最常走的路径，同样要开会话
           主表、读写状态、写短期记忆、过合规兜底。任何一项漏掉都会造成实际故障：

           - 首轮会话主表没建，``session_state`` 的外键直接违约；
           - 合规兜底形同虚设，绝对化用语原样念给用户；
           - 多轮上下文断裂（记忆里没有推荐轮），订单状态机也存不下来；
           - 意图矫正 ``revise_intent_by_context`` 不生效，"便宜点"永远踩着坑走。

           为此两个入口的簿记收敛成 :meth:`_begin_turn` / :meth:`_classify` /
           :meth:`_remember` 三个共用方法，**结构上就没法漂移**；意图识别一轮只调一次。
        """
        state = await self._begin_turn(session_id, user_id, utterance)

        if self._is_order_short_circuit(state):
            result = await self._handle_order_confirm(session_id, user_id, state, utterance)
            async for chunk in self._emit_result(
                session_id, user_id, state, utterance, None, result
            ):
                yield chunk
            return

        intent = await self._classify(session_id, state, utterance)

        if intent.intent is IntentType.PRODUCT_RECOMMENDATION:
            async for chunk in self._stream_recommendation(
                session_id, user_id, state, utterance, intent
            ):
                yield chunk
            return

        result = await self._dispatch(session_id, user_id, state, utterance, intent)
        async for chunk in self._emit_result(
            session_id, user_id, state, utterance, intent, result
        ):
            yield chunk

    # ========== 两个入口共用的簿记 ==========

    async def _begin_turn(
        self, session_id: str, user_id: int, utterance: str
    ) -> SessionState:
        """一轮对话开始时的固定动作：开会话、发事件、读状态。"""
        # 幂等开启 session 主表（session_state 外键依赖，首轮必须先创建）
        await self._session.open_if_absent(session_id, user_id)

        # 旁路：发布"用户讲话"事件（画像预热 + 审计），不阻塞主流程
        self._publish_user_spoken(session_id, user_id, utterance)

        # 注意：这里**不再**单独记一条 USER 原话。
        # 如果既写 USER 原话又写 TURN 摘要，同一轮等于存了两份，下一轮意图 Agent
        # 拿"最近 3 轮"时一半配额被原话吃掉，还会看到重复内容。只留 TURN 摘要。
        return await self._state.load(session_id)

    async def _classify(
        self, session_id: str, state: SessionState, utterance: str
    ) -> IntentResult:
        """意图识别 + 上下文矫正。**一轮只调一次**。"""
        intent = await self._intent.classify(session_id, utterance)
        logger.info(
            "[Orc] sessionId=%s intent=%s slots=%s", session_id, intent.intent, intent.slots
        )

        # 意图兜底矫正：LLM 对"便宜点/换一款"这类价格方向表达经常判成
        # CLARIFY_NEEDED，但只要上一轮已经推了商品 + 本轮抽到了 priceDirection，
        # 就强制按 PRODUCT_COMPARE 处理
        return revise_intent_by_context(state, intent)

    async def _remember(
        self, session_id: str, utterance: str, intent: IntentResult, speech_text: str
    ) -> None:
        """一条 TURN 摘要代替原来的 user + assistant 两条。"""
        summary = self._summarizer.summarize(utterance, intent.intent.value, speech_text)
        await self._memory.append(
            session_id,
            Turn(role=TURN_ROLE, agent=intent.intent.value, text=summary, ts=_now_ms()),
        )

    def _is_order_short_circuit(self, state: SessionState) -> bool:
        """是否要短路进订单分支。

        这个判定抽成一个有名字的方法——**它是有副作用的判断**
        （决定了这一轮跑不跑 IntentAgent），名字得能说明白这件事。

        为什么必须短路：这一轮用户能说的话只有三种——确认 / 取消 / 没听懂，
        本地规则完全够用。而 LLM 拿到"再想想"很可能分类成 CHITCHAT 或
        OUT_OF_SCOPE，于是走错分支：**用户说"再想想"，系统回一句闲聊，
        那张预览单还挂在 Redis 里等着被确认**。

        代价是这一轮不做意图识别，短期记忆里记的 intent 沿用上一轮——可以接受，
        因为这一轮的语义本来就不需要意图分类。
        """
        return state.phase == PHASE_ORDER_CONFIRM and self._orders is not None

    async def _dispatch(
        self,
        session_id: str,
        user_id: int,
        state: SessionState,
        utterance: str,
        intent: IntentResult,
    ) -> EmotionResult:
        """按意图路由到六条分支之一。"""
        match intent.intent:
            case IntentType.PRODUCT_RECOMMENDATION:
                return await self._handle_recommendation(
                    session_id, user_id, state, utterance, intent
                )
            case IntentType.CLARIFY_NEEDED:
                return await self._handle_clarify(session_id, state, utterance, intent)
            case IntentType.PRODUCT_COMPARE:
                return await self._handle_compare(
                    session_id, user_id, state, utterance, intent
                )
            case IntentType.ORDER_CONFIRM:
                return await self._handle_order_confirm(session_id, user_id, state, utterance)
            case IntentType.CHITCHAT:
                return await self._handle_chitchat(session_id, user_id, utterance)
            case _:
                return self._handle_out_of_scope(session_id, utterance)

    def _publish_user_spoken(self, session_id: str, user_id: int, utterance: str) -> None:
        """发布用户讲话事件。发布器缺失或未装监听者时静默跳过。"""
        if self._events is None:
            return
        try:
            self._events.publish_user_spoken(session_id, user_id, utterance, _now_ms() / 1000)
        except Exception:  # noqa: BLE001 — 旁路事件绝不能拖垮主链路
            logger.warning("发布用户讲话事件失败 sessionId=%s", session_id, exc_info=True)

    # ========== 流式下发：把"已经生成好的结果"包成片 ==========

    async def _emit_result(
        self,
        session_id: str,
        user_id: int,
        state: SessionState,
        utterance: str,
        intent: IntentResult | None,
        result: EmotionResult,
    ) -> AsyncIterator[StreamChunk]:
        """非推荐分支的下发：合规 → 记记忆 → 存状态 → 发片。

        ``intent`` 为 ``None`` 表示走的是订单短路分支——那条路**不写短期记忆**
        （与 :meth:`handle` 保持一致），所以这里也得跳过，不然两个入口的行为又分叉了。
        """
        result = self._compliance.ensure_compliant(session_id, user_id, result)
        if intent is not None:
            await self._remember(session_id, utterance, intent, result.speech_text)
        await self._state.save(state)

        # 卡片先发（非推荐分支的 display_blocks 目前恒为空，留着是为了让"先卡后话"
        # 这条时序规则在任何分支上都成立）
        if result.display_blocks:
            yield StreamChunk.products_chunk(result.display_blocks)
        if result.speech_text:
            yield StreamChunk.text_chunk(result.speech_text)
            async for frame in self._speak(session_id, result.speech_text):
                yield StreamChunk.audio_chunk(frame)

    async def _speak(self, session_id: str, text: str) -> AsyncIterator[bytes]:
        """按句切分、逐句合成，产出 PCM 帧。

        TTS 挂掉时**静默收尾**而不是往上抛：用户已经收到了文字帧，前端字幕是对的；
        这时候把异常抛到 WebSocket 层，只会让连接以一条无意义的堆栈结束。
        """
        if self._tts is None or not text:
            return
        for sentence in split_into_sentences(text):
            # 先查预合成缓存。命中的是"鸡哥给你挑了三款。"这类
            # 高频过渡语——省一次字符计费，也省一次 300-500ms 的网络往返。
            #
            # **命中路径不发文字帧、不碰合规**：那些事在调用方已经做完了，
            # 这里只负责"把这一句的声音拿出来"。
            cached = self._tts_cache.get(sentence) if self._tts_cache is not None else None
            if cached is not None:
                logger.debug("[Cost] TTS 命中缓存 sentence=%r bytes=%d", sentence, len(cached))
                yield cached
                continue
            try:
                async for frame in self._tts.synthesize(single_text_stream(sentence)):
                    yield frame
            except Exception:  # noqa: BLE001 — 合成失败只影响"听见"，不影响"看见"
                logger.error(
                    "[Orc] TTS 合成失败 sessionId=%s sentence=%r", session_id, sentence,
                    exc_info=True,
                )
                return

    # ========== 六条分支的具体实现 ==========

    async def _handle_recommendation(
        self,
        session_id: str,
        user_id: int,
        state: SessionState,
        utterance: str,
        intent: IntentResult,
    ) -> EmotionResult:
        """推荐主链路：合并槽位 → 澄清判断 → 推荐 → （旁路点评团）→ 情感包装。"""
        slots = merge_slots(state.slots, intent.slots)
        state.slots = slots
        state.current_intent = IntentType.PRODUCT_RECOMMENDATION.value

        clarify = await self._clarify.decide(session_id, utterance, slots)
        if clarify.is_ask:
            state.phase = PHASE_CLARIFY
            state.pending_ask = clarify.missing_slots[0] if clarify.missing_slots else None
            return EmotionResult(speech_text=clarify.question_to_ask or "", display_blocks=[])

        state.phase = PHASE_RECOMMEND
        rec = await self._recommend.recommend(session_id, user_id, utterance, slots)
        state.last_recommendations = [item.product_id for item in rec.items]

        # 旁路：多视角点评团。失败 / 关闭时降级为原始 utterance
        context_for_emotion = utterance
        if self._perspective_enabled and rec.items and self._perspective is not None:
            digest = await self._perspective.discuss(session_id, utterance, rec.items)
            if digest and digest.strip():
                context_for_emotion = f"{utterance}\n\n【点评团意见】\n{digest}"

        # 槽位拼成需求描述喂给情感 Agent——现在它要自己编理由，光有商品属性不够，
        # 还得知道用户在找什么
        return await self._emotion.wrap(
            session_id, context_for_emotion, format_user_needs(slots), rec
        )

    async def _stream_recommendation(
        self,
        session_id: str,
        user_id: int,
        state: SessionState,
        utterance: str,
        intent: IntentResult,
    ) -> AsyncIterator[StreamChunk]:
        """推荐主链路的流式版：**先出卡片，再边说边合成**。

        与 :meth:`_handle_recommendation` 是同一套逻辑，唯一区别是口播**边生成边下发**。
        澄清判断、点评团旁路、状态流转三部分照旧——流式版同样**不能跳过澄清判断**，
        否则用户信息不全时会直接硬推三款商品。
        """
        slots = merge_slots(state.slots, intent.slots)
        state.slots = slots
        state.current_intent = IntentType.PRODUCT_RECOMMENDATION.value

        clarify = await self._clarify.decide(session_id, utterance, slots)
        if clarify.is_ask:
            state.phase = PHASE_CLARIFY
            state.pending_ask = clarify.missing_slots[0] if clarify.missing_slots else None
            result = EmotionResult(
                speech_text=clarify.question_to_ask or "", display_blocks=[]
            )
            async for chunk in self._emit_result(
                session_id, user_id, state, utterance, intent, result
            ):
                yield chunk
            return

        state.phase = PHASE_RECOMMEND
        rec = await self._recommend.recommend(session_id, user_id, utterance, slots)
        state.last_recommendations = [item.product_id for item in rec.items]

        context_for_emotion = utterance
        if self._perspective_enabled and rec.items and self._perspective is not None:
            digest = await self._perspective.discuss(session_id, utterance, rec.items)
            if digest and digest.strip():
                context_for_emotion = f"{utterance}\n\n【点评团意见】\n{digest}"

        # 商品卡片**先发**：前端立刻能渲染，不必等口播的第一个字
        yield StreamChunk.products_chunk(rec.items)

        spoken: list[str] = []
        async for chunk in self._stream_speech(
            session_id, context_for_emotion, format_user_needs(slots), rec, spoken
        ):
            yield chunk

        full_speech = "".join(spoken)
        if not full_speech:
            # 一个字都没说出去（模型直接挂了 / 吐了个空）——补一句兜底，
            # 否则用户看着三张商品卡，一片安静。
            #
            # **兜底话术也要过合规**：它没走 :meth:`_stream_speech` 那条路，
            # 也就没人替它调 ``clean_text``。而模板里就有"第一款：xxx"这种字样，
            # "第一"在词表里是绝对化用语——不洗的话，兜底反而成了唯一一条
            # 绕开合规的出口（:func:`fallback` 的调用点走过
            # ``ensure_compliant``，这里不能退步）。
            full_speech = self._compliance.clean_text(fallback(rec))
            yield StreamChunk.text_chunk(full_speech)
            async for frame in self._speak(session_id, full_speech):
                yield StreamChunk.audio_chunk(frame)

        await self._remember(session_id, utterance, intent, full_speech)
        await self._state.save(state)

    async def _stream_speech(
        self,
        session_id: str,
        context: str,
        user_needs: str,
        rec: RecommendResult,
        spoken: list[str],
    ) -> AsyncIterator[StreamChunk]:
        """流式口播：Token → 句 → 合规 → 文字帧 + 音频帧。

        ``spoken`` 是**出参**：把已经说出去的文本累积给调用方，用于写短期记忆。
        累计的是**过完合规的文本**——记忆里该存"用户实际听到的那句"。

        .. important::

           **一个字都不能攒。** 每攒够一句就立刻 ``yield`` 出去——攒在列表里
           等整条流跑完再统一发，等于把所有句子压到最后一起出声，"边说边答"
           就退化成了"想完再说"。流式处理必须靠"在 ``async for`` 体内 ``yield``"
           来保证逐句下发的时序。
        """

        async def _emit(unit: str) -> AsyncIterator[StreamChunk]:
            """一句：过合规 → 记进 ``spoken`` → 文字帧 → 该句的音频帧。"""
            clean = self._compliance.clean_text(unit)
            if not clean:
                return
            spoken.append(clean)
            yield StreamChunk.text_chunk(clean)
            async for frame in self._speak(session_id, clean):
                yield StreamChunk.audio_chunk(frame)

        aggregator = SentenceAggregator()

        try:
            if self._emotion_stream is None:
                # 没装配流式服务：退回整段包装。行为与整段包装一致，
                # 只是拿不到"边说边答"的那几百毫秒。
                result = await self._emotion.wrap(session_id, context, user_needs, rec)
                if result.speech_text:
                    async for chunk in _emit(result.speech_text):
                        yield chunk
                return

            async for token in self._emotion_stream.stream_wrap(
                session_id, context, user_needs, rec
            ):
                for unit in aggregator.push(token):
                    async for chunk in _emit(unit):
                        yield chunk
            # 模型最后一句常常没标点，flush 兜住
            for unit in aggregator.flush():
                async for chunk in _emit(unit):
                    yield chunk
        except Exception:  # noqa: BLE001 — 流式失败必须降级，不能把连接带崩
            # 直接收尾，**不在这里补兜底话术**：
            # - 一句话都还没出（``spoken`` 为空）→ 调用方看到空文本，会补 ``fallback``；
            # - 已经出过声 → 就停在半句。用户听到 "…第一款是 Asics，鸡哥这边网络
            #   有点卡" 比直接停住更让人困惑：停在半句是坏体验但可理解，接一句
            #   驴唇不对马嘴的兜底才是真故障感。
            logger.error(
                "[Orc] 情感 Agent 流式生成失败 sessionId=%s 已说出 %d 字",
                session_id, len("".join(spoken)), exc_info=True,
            )
            return

    async def _handle_clarify(
        self,
        session_id: str,
        state: SessionState,
        utterance: str,
        intent: IntentResult,
    ) -> EmotionResult:
        """用户第一次就说得很模糊（"最近想买点东西"），用意图里抽到的 slots 开始。"""
        slots = dict(intent.slots or {})
        state.slots = slots
        state.phase = PHASE_CLARIFY

        clarify = await self._clarify.decide(session_id, utterance, slots)
        speech = (
            (clarify.question_to_ask or "") if clarify.is_ask else CLARIFY_READY_SPEECH
        )
        return EmotionResult(speech_text=speech, display_blocks=[])

    async def _handle_compare(
        self,
        session_id: str,
        user_id: int,
        state: SessionState,
        utterance: str,
        intent: IntentResult,
    ) -> EmotionResult:
        """对比类 = 在上一次推荐结果基础上，根据当前诉求重新排序 / 过滤。"""
        slots = merge_slots(state.slots, intent.slots)

        # "便宜点/贵点"要以**上一轮推荐的实际价格**为锚，不能直接用 budget——
        # budget 只是用户给的上限，上一轮推出来的商品可能远低于 budget，
        # 若直接按 budget * 0.8 下调，新上限可能还比上一轮最高价高，起不到"便宜"的效果。
        price_direction = slots.get(PRICE_DIRECTION)
        last_ids = list(state.last_recommendations or [])
        if price_direction is not None and last_ids:
            prices = [
                product.price
                for product in await self._products.find_by_ids(last_ids)
                if product.price is not None
            ]
            if prices:
                cheapest = min(prices)
                dearest = max(prices)
                if price_direction == "cheaper":
                    # 新上限 = 上轮最高价 * 0.8，保证比上一轮任何一款都便宜
                    slots["budget"] = int(dearest * CHEAPER_FACTOR)
                elif price_direction == "expensive":
                    # 新下限 = 上轮最低价 * 1.2（推荐层读 priceMin 字段）
                    slots["priceMin"] = int(cheapest * EXPENSIVE_FACTOR)
                # 排除上轮已推过的，避免重复推同一款
                slots["excludeProductIds"] = last_ids

        rec = await self._recommend.recommend(session_id, user_id, utterance, slots)
        return await self._emotion.wrap(
            session_id, utterance, format_user_needs(slots), rec
        )

    async def _handle_order_confirm(
        self, session_id: str, user_id: int, state: SessionState, utterance: str
    ) -> EmotionResult:
        """下单分支：没有预览单就先建一张，有了就判"确认 / 取消 / 没听懂"。

        **判定顺序是这里最关键的一处**：
        先看有没有预览单，再看用户说了什么——因为"确认"和"取消"只在
        **有单可确认**的时候才有意义。
        """
        state.current_intent = IntentType.ORDER_CONFIRM.value

        if self._orders is None:
            # 没装配订单服务 = 装配漏了，**不是"这个场景不需要下单"**。
            # 既不能装作下单成功（骗用户），也不能默默什么都不说（用户以为在下单）。
            # 如实说"没下成"，并把状态退回 RECOMMEND，别让用户卡在订单分支里。
            logger.error("[Orc] 订单服务未装配，下单分支无法处理 sessionId=%s", session_id)
            state.phase = PHASE_RECOMMEND
            return EmotionResult(speech_text=ORDER_FAILED_SPEECH, display_blocks=[])

        pending = await self._orders.peek_pending(session_id)
        if pending is not None:
            return await self._resolve_pending(session_id, user_id, state, utterance)

        return await self._create_pending(session_id, user_id, state, utterance)

    async def _resolve_pending(
        self, session_id: str, user_id: int, state: SessionState, utterance: str
    ) -> EmotionResult:
        """已经有一张待确认的预览单：判确认 / 取消 / 没听懂。"""
        answer = classify_confirmation(utterance)

        if answer is Confirmation.YES:
            try:
                order = await self._orders.confirm(session_id, user_id)
            except OrderError as exc:
                # 库存被抢走 / 预览单过期。**状态要退回 RECOMMEND**，
                # 否则下一轮又短路进订单分支，用户被困在一个下不成的单上。
                logger.warning("[Orc] 下单失败 sessionId=%s: %s", session_id, exc)
                state.phase = PHASE_RECOMMEND
                return EmotionResult(speech_text=ORDER_FAILED_SPEECH, display_blocks=[])

            state.phase = PHASE_ENDED
            tail = order.order_no[-ORDER_NO_DISPLAY_LEN:]
            return EmotionResult(
                speech_text=f"下单成功，订单尾号 {tail}，1-2 天送达。还有想看的吗？",
                display_blocks=[],
            )

        if answer is Confirmation.NO:
            await self._orders.cancel(session_id)
            state.phase = PHASE_RECOMMEND
            return EmotionResult(speech_text=ORDER_CANCELLED_SPEECH, display_blocks=[])

        # 没听懂 —— **保持 ORDER_CONFIRM 状态**，下一轮继续短路进这个分支。
        # 这里绝不能"默认当成确认"：那是替用户做决定，而且做的是花钱的决定。
        state.phase = PHASE_ORDER_CONFIRM
        return EmotionResult(speech_text=ORDER_ASK_CONFIRM_SPEECH, display_blocks=[])

    async def _create_pending(
        self, session_id: str, user_id: int, state: SessionState, utterance: str
    ) -> EmotionResult:
        """还没有预览单：解析指代 → 建一张预览单（**不落库**）。"""
        product_id = self._resolver.resolve(state, utterance)
        if product_id is None:
            # 解析不出来就追问，**不进 ORDER_CONFIRM 状态**——还没有单可确认，
            # 进了那个状态下一轮会短路到这里，用户说"第二款"反而又被问一遍。
            state.phase = PHASE_RECOMMEND
            return EmotionResult(speech_text=ORDER_ASK_WHICH_SPEECH, display_blocks=[])

        scope = await resolve_scope(self._scope_cache, session_id, user_id)
        try:
            pending = await self._orders.preview(session_id, user_id, product_id, 1, scope)
        except OrderError as exc:
            logger.warning("[Orc] 生成预览单失败 sessionId=%s: %s", session_id, exc)
            state.phase = PHASE_RECOMMEND
            return EmotionResult(speech_text=ORDER_FAILED_SPEECH, display_blocks=[])

        state.phase = PHASE_ORDER_CONFIRM
        return EmotionResult(
            speech_text=(
                f"好，帮你准备下单：{pending.product_name}，"
                f"¥{pending.unit_price}，一共 {pending.total_amount} 元。确认下单吗？"
            ),
            display_blocks=[],
        )

    async def _handle_chitchat(
        self, session_id: str, user_id: int, utterance: str
    ) -> EmotionResult:
        """闲聊：**先过模板，未命中才走 EmotionService 的闲聊模式**。

        闲聊用的是 qwen-max，是整条链路上最贵的一次调用。"你是谁""你会不会推荐"
        "今天天气怎么样"这类问题占了闲聊的绝大多数，用一条正则挡掉，
        命中就完全不调 LLM。

        .. note::

           命中路径**仍然要过合规**。模板话术是写死的、当前也确实是干净的，
           但"模板文案不会触发绝对化用语"这件事**不该是一条隐式约定**——
           ``handle`` / ``stream_handle`` 的出口本来就会调 ``ensure_compliant``
           （踩过的坑：流式兜底绕过合规）。这里不再手动洗一遍，
           靠的是出口那道闸门，**所以别把模板命中做成提前 return 出去**。
        """
        template_reply = chitchat_templates.match(utterance)
        if template_reply is not None:
            logger.info("[Cost] 闲聊命中模板 sessionId=%s utterance=%s", session_id, utterance)
            return EmotionResult(speech_text=template_reply, display_blocks=[])

        # 闲聊没有意图槽位，``userNeeds`` 直接传空串。
        return await self._emotion.wrap(
            session_id,
            utterance,
            "",
            RecommendResult(items=[], explanation_tone=TONE_CHITCHAT),
        )

    def _handle_out_of_scope(self, session_id: str, utterance: str) -> EmotionResult:
        """越权 / 无法处理的输入，用固定话术礼貌拒绝。"""
        logger.info("[Orc] 越权拒绝 sessionId=%s utterance=%s", session_id, utterance)
        return EmotionResult(speech_text=OUT_OF_SCOPE_SPEECH, display_blocks=[])


def format_user_needs(slots: dict[str, Any] | None) -> str:
    """把意图槽位拼成一句口语化的需求描述，喂给情感 Agent。

    ``{budget: 500}`` → ``"budget=500"``，多个槽位用中文逗号连接。
    **只跳过 ``None``**——``0`` 和空串都是用户真给过的值
    （比如 ``priceDirection`` 抽到了但方向为空），用 ``if value`` 会把它们一起吃掉。

    抽成独立函数是因为三个分支（推荐 / 对比 / 闲聊）都要用，而闲聊传空串。
    """
    if not slots:
        return ""
    return "，".join(
        f"{key}={value}" for key, value in slots.items() if value is not None
    )


def merge_slots(
    history: dict[str, Any] | None, current: dict[str, Any] | None
) -> dict[str, Any]:
    """合并历史槽位 + 本轮新槽位。

    **只覆盖非 ``None`` 的值**，所以本轮没抽到的槽位会保留历史值，这正是多轮对话
    能"记住"品类和预算的关键。
    """
    merged: dict[str, Any] = dict(history or {})
    for key, value in (current or {}).items():
        if value is not None:
            merged[key] = value
    return merged


def revise_intent_by_context(state: SessionState, intent: IntentResult) -> IntentResult:
    """意图后处理：LLM 在两类场景下容易判错，这里做兜底矫正。

    1. **上下文比较**：上一轮已有推荐 + 本轮抽到 ``priceDirection`` →
       ``CLARIFY_NEEDED`` / ``PRODUCT_RECOMMENDATION`` 统一改写成 ``PRODUCT_COMPARE``

    2. **信息已足**：``state.slots`` + 本轮 slots 合并后，``category`` +
       (``budget`` | ``scenario`` | ``brand``) 任一组合齐全，就不该再
       ``CLARIFY_NEEDED``，强制改写为 ``PRODUCT_RECOMMENDATION``
    """
    current_slots = intent.slots or {}

    # ① 上下文价格对比
    has_last_recommend = (
        state.phase == PHASE_RECOMMEND and bool(state.last_recommendations)
    )
    has_price_direction = current_slots.get(PRICE_DIRECTION) is not None
    if (
        has_last_recommend
        and has_price_direction
        and intent.intent
        in (IntentType.CLARIFY_NEEDED, IntentType.PRODUCT_RECOMMENDATION)
    ):
        logger.info(
            "[Orc] 意图矫正 %s -> PRODUCT_COMPARE, priceDirection=%s",
            intent.intent,
            current_slots.get(PRICE_DIRECTION),
        )
        return IntentResult(
            intent=IntentType.PRODUCT_COMPARE,
            slots=current_slots,
            confidence=intent.confidence,
        )

    # ② 信息已足阈值：合并历史 slots + 本轮 slots
    if intent.intent == IntentType.CLARIFY_NEEDED:
        merged = merge_slots(state.slots, current_slots)
        has_category = merged.get("category") is not None
        has_any_anchor = (
            merged.get("budget") is not None
            or merged.get("scenario") is not None
            or merged.get("brand") is not None
        )
        if has_category and has_any_anchor:
            logger.info(
                "[Orc] 意图矫正 CLARIFY_NEEDED -> PRODUCT_RECOMMENDATION, mergedSlots=%s",
                merged,
            )
            return IntentResult(
                intent=IntentType.PRODUCT_RECOMMENDATION,
                slots=merged,
                confidence=intent.confidence,
            )

    return intent


def _now_ms() -> int:
    """当前毫秒时间戳。"""
    return int(time.time() * 1000)
