"""应用配置。

把原 ``application.yml`` 的配置项映射为 pydantic-settings 的 ``Settings``，
从环境变量 / ``.env`` 读取。敏感值（数据库密码、JWT Secret、DashScope API Key）
不提供可用于生产的默认值。
"""

from functools import cached_property, lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: 后端根，即 ``backend/``（``app/config.py`` 的上一级的上一级）。
#:
#: 用来把配置里的**相对路径**锚在这里，而不是锚在进程的当前工作目录。
#: 仓库拆成 ``backend/`` + ``frontend/`` 之后，从仓库根起 uvicorn 是很自然的动作，
#: 那样 CWD 就是仓库根，``app/resources/...`` 会解析失败——而下面
#: :attr:`Settings.sensitive_words` 对"文件不存在"的处理是**返回空集合**，
#: 于是合规词表会静默失效、一个字都不报。锚在这里之后，从哪个目录起都一样。
BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _resolve_from_backend_root(value: str) -> Path:
    """相对路径按后端根解析；绝对路径原样返回（环境变量覆盖仍然有效）。"""
    path = Path(value)
    return path if path.is_absolute() else BACKEND_ROOT / path


class Settings(BaseSettings):
    """全局配置。字段名与环境变量一一对应（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 应用 ----
    app_name: str = "voice_agent"
    app_host: str = "0.0.0.0"
    app_port: int = 8080
    app_env: str = "development"
    app_version: str = "0.1.0"

    # ---- PostgreSQL ----
    db_host: str = "127.0.0.1"
    db_port: int = 5432
    db_name: str = "voice_agent"
    db_user: str = "voice_agent"
    db_password: str = ""  # 无生产默认值

    # ---- Redis ----
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_password: str = ""  # 无生产默认值
    redis_database: int = 0

    # ---- 缓存 ----
    cache_ttl_seconds: int = 1800
    cache_null_values: bool = False
    cache_key_prefix: str = "voice_agent:cache:"

    # ---- DashScope ----
    dashscope_api_key: str = ""  # 无生产默认值
    main_model: str = "qwen-max"
    light_model: str = "qwen-flash"
    #: 多 Agent 讨论（MsgHub 点评团）专用模型。
    #: 用中等档位即可——点评团是旁路锦上添花，且一次要跑三次调用。
    multi_agent_model: str = "qwen-plus"

    # ---- Embedding ----
    embedding_model: str = "text-embedding-v3"
    embedding_dim: int = 1024

    # ---- 语音 ASR / TTS ----
    asr_model: str = "paraformer-realtime-v2"
    tts_model: str = "cosyvoice-v3-flash"
    tts_voice: str = "longxiaochun_v3"

    # ---- 会话 ----
    session_ttl_minutes: int = 30
    session_max_history_turns: int = 10

    # ---- JWT ----
    jwt_secret_key: str = ""  # 无生产默认值
    jwt_algorithm: str = "HS256"
    token_expire_seconds: int = 2592000
    #: 是否强制认证。默认关——本地开发不配密钥也能把前面的链路跑起来。
    #: 打开后 WebSocket 与 /api/v1/chat 只认 token，不再信任调用方传的 userId。
    #:
    #: **设计理由**：显式做成开关，是为了让"当前处于不安全模式"这件事在配置里
    #: 可见、在启动日志里可闻，而不是靠"密钥恰好为空"来隐式表达。
    auth_required: bool = False

    # ---- 合规 ----
    sensitive_word_file: str = "app/resources/compliance/sensitive-words.txt"
    absolute_claim_patterns_raw: str = "最好,第一,保证,绝对"

    # ---- 多视角点评团 ----
    #: 默认关闭。点评团是旁路锦上添花，一次要跑三次 LLM 调用，
    #: 灰度时再打开。
    perspective_enabled: bool = False

    # ---- 成本控制 ----
    #: Embedding 缓存有效期。商品 / FAQ 文本基本不变，所以给到 7 天。
    embedding_cache_ttl_seconds: int = 7 * 24 * 3600
    #: Embedding 缓存的 Redis 前缀。键名拆成"前缀 + 指纹"两段，与项目里其它缓存一致。
    embedding_cache_key_prefix: str = "voice_agent:embed:"
    #: Agent 记忆巡检周期（秒）。
    memory_guard_interval_seconds: int = 60
    #: 是否在应用启动时预热 TTS 短语缓存。
    #:
    #: **默认关，这是有意设计。** 无条件预热会真的调 DashScope——
    #: DashScope 抖一下就会抛异常，**整个应用都起不来**。
    #: 一个成本优化项不该有一票否决启动流程的权力。这里改成显式开关 +
    #: 失败只记 warning；要预热就 ``TTS_PHRASE_PRELOAD_ON_STARTUP=true``，
    #: 或者调 ``POST /api/v1/debug/tts-cache/preload`` 手工预热。
    tts_phrase_preload_on_startup: bool = False

    # ============================================================
    # 计算属性
    # ============================================================

    @property
    def database_url(self) -> str:
        """异步 SQLAlchemy Database URL（asyncpg 驱动）。"""
        password = quote_plus(self.db_password) if self.db_password else ""
        auth = f"{self.db_user}:{password}@" if self.db_password else f"{self.db_user}@"
        return (
            f"postgresql+asyncpg://{auth}"
            f"{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def redis_url(self) -> str:
        """Redis URL（含密码与 database 索引）。"""
        auth = f":{quote_plus(self.redis_password)}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_database}"

    @property
    def absolute_claim_patterns(self) -> list[str]:
        """绝对化宣传词列表（逗号分隔）。"""
        return [p for p in (s.strip() for s in self.absolute_claim_patterns_raw.split(",")) if p]

    @cached_property
    def sensitive_words(self) -> set[str]:
        """从合规词文件加载的敏感词集合（忽略空行与 # 注释）。

        路径经 :func:`_resolve_from_backend_root` 锚定，所以不受启动目录影响。
        """
        path = _resolve_from_backend_root(self.sensitive_word_file)
        if not path.exists():
            return set()
        words: set[str] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            word = line.strip()
            if word and not word.startswith("#"):
                words.add(word)
        return words

    @property
    def session_ttl_seconds(self) -> int:
        """会话 TTL（秒）。"""
        return self.session_ttl_minutes * 60

    @model_validator(mode="after")
    def _check_auth_config(self) -> "Settings":
        """打开了认证却没配密钥 → **启动就报错**。

        启动期校验：配置错了不该等到第一个请求进来才发现。
        Python 没有容器，但 pydantic 的 ``model_validator`` 在 ``Settings()`` 构造时
        就会跑，效果一样——进程直接起不来。

        注意**只校验这一个方向**（开了认证必须有密钥）。反方向（有密钥但没开认证）
        是合法且常见的：本地想验证 token 流程，但不想让所有接口都强制登录。
        """
        if self.auth_required and not self.jwt_secret_key:
            raise ValueError(
                "AUTH_REQUIRED=true 时必须配置 JWT_SECRET_KEY，否则任何人都伪造得了 token"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """进程内单例配置。"""
    return Settings()
