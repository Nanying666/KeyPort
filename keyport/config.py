"""配置加载：默认值 + config.json 覆盖 + 环境变量覆盖。

优先级（低 → 高）：内置默认 < config.json < 环境变量 KP_*。
配置对象在进程内单例，管理台热改通过 reload() 生效。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from .upstream import constants as _C

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.json"


def config_path_from_env() -> Path:
    """配置文件路径：KP_CONFIG_FILE 优先（容器里指向持久化卷，避免绑定挂载踩坑）。"""
    raw = os.environ.get("KP_CONFIG_FILE", "").strip()
    if not raw:
        return DEFAULT_CONFIG_PATH
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


@dataclass
class UpstreamConfig:
    """上游出站请求相关配置。"""

    timeout_seconds: float = 120.0
    header_timeout_seconds: float = 120.0
    idle_timeout_seconds: float = 300.0
    # 空 = 使用内置默认（按 realm 拼装官方桌面端三段式 UA）
    user_agent: str = ""
    client_version: str = ""      # WorkBuddy 桌面端版本段
    cli_version: str = ""         # 内置 CLI 版本段
    client_name: str = ""         # 空 = 伪造官方 WorkBuddy 桌面端归属头组
    device_token: str = ""
    device_token_file: str = ""
    passthrough_ip: bool = False


@dataclass
class CaptchaConfig:
    """zcode.z.ai 人机验证参数（verifyParam）供给配置。

    verifyParam 是**一次性**的（实测复用第二次即 3007），
    故由后台农场持续产出、随取随用，不做长时间缓存。
    """

    enabled: bool = True
    headless: bool = True           # 本环境实测可用；被风控判 F001/F011 时改 False
    pool_size: int = 3              # 目标库存：队列达到该值即停止求解
    per_attempt_ms: int = 15000
    gap_ms: int = 300
    idle_sleep_ms: int = 1000       # 库存已满时的轮询间隔
    max_wait_seconds: float = 20.0
    fail_backoff_ms: int = 5000         # 单次求解失败后的退避起始
    fail_backoff_max_ms: int = 120000   # 连续失败退避封顶
    restart_backoff_ms: int = 5000      # 农场异常退出后的重启退避
    restart_backoff_max_ms: int = 300000
    restart_max_consecutive: int = 20   # 连续重启上限（防重启风暴）
    chrome_path: str = ""           # 空 = 自动探测本机 Chrome / Edge
    scene_id: str = ""              # 空 = 取上游 /api/v1/client/configs 下发值
    prefix: str = ""
    region: str = ""


@dataclass
class PoolConfig:
    """账号池调度参数。"""

    max_in_flight: int = 3
    max_in_flight_global: int = 2
    breaker_threshold: int = 3
    breaker_cooldown_seconds: int = 1800
    breaker_cooldown_max_seconds: int = 21600
    degrade_threshold: int = 5
    degrade_cooldown_seconds: int = 600
    degrade_cooldown_max_seconds: int = 7200
    idle_weight_per_hour: float = 0.5
    idle_weight_max: float = 5.0
    expiring_soon_seconds: int = 168 * 3600
    cost_explore_interval_seconds: int = 1800
    soft_rate_seconds: int = 600        # 429 软冷却起始
    soft_rate_max_seconds: int = 7200   # 429 软冷却封顶
    not_found_seconds: int = 60         # 404 固定短冷却
    top_k: int = 5                      # 短名单长度
    pick_gap_ms: int = 100              # 防惊群窗口
    # 无域前缀的请求按「模型归属域」自动路由：模型只在某一个域提供时，
    # 限定到该域选号。否则余额权重会把请求全导向高余额域（例如 z.ai 的
    # token 额度远大于 workbuddy 积分），而该域可能并不提供这个模型。
    route_by_model: bool = True

    # ---- 429 频控：原地等待重试（不冷却账号）----
    retry_429_times: int = _C.RETRY_429_TIMES      # 429 原地重试次数
    retry_429_wait: int = _C.RETRY_429_WAIT        # 429 重试等待（上游 Retry-After 优先）
    retry_429_wait_max: int = _C.RETRY_429_WAIT_MAX  # Retry-After 采信上限（防吊死客户端）
    # ---- 5xx 等一般错误：重试耗尽后冷却换号 ----
    retry_5xx_times: int = _C.RETRY_5XX_TIMES
    retry_5xx_wait: int = _C.RETRY_5XX_WAIT
    cooling_seconds: int = _C.COOLING_SECONDS      # 5xx 重试耗尽 / 连接失败后的冷却
    # ---- 风控（3012 / unusual activity）----
    risk_cooldown_seconds: int = _C.RISK_COOLDOWN_BASE      # 首次冷却（指数退避基数）
    risk_cooldown_max_seconds: int = _C.RISK_COOLDOWN_MAX   # 冷却封顶（24h）
    risk_ban_strikes: int = _C.RISK_BAN_STRIKES             # 累计命中达到即禁用
    risk_strike_decay_seconds: int = _C.RISK_STRIKE_DECAY_SECONDS  # strikes 衰减窗口
    # 风控升级禁用时自动换发设备指纹（新 SKU + 新 device_mid）并补跑安装序
    risk_auto_rotate: bool = True


@dataclass
class SessionStickyConfig:
    enabled: bool = True
    ttl_seconds: int = 1800
    gc_interval_seconds: int = 300
    redis_url: str = ""          # 非空则镜像会话绑定到 Redis


@dataclass
class ClaimConfig:
    """限时套餐领取（ZCode Coding Plan 活动套餐）。

    双轨机制：
      - 自动：入池即领取全部可领套餐，并有周期轮（`round_interval_seconds`）。
      - 手动：前端调起阿里云滑块验证码后，把 verifyParam 转发给上游领取。

    `1005 名额用完`按上游下发的名额恢复时间退避：等待期不重试领取、不耗验证码。
    """

    enabled: bool = True
    # 自动领取轮间隔（秒，0 = 关闭；运行期可在管理台热改）
    round_interval_seconds: int = _C.CLAIM_ROUND_INTERVAL
    # 入池即自动领取
    auto_on_add: bool = True
    # 单账号领取失败后的退避（秒）——非 1005 的服务端窗口未知时的保守退避
    cooldown_seconds: int = 600
    # 每轮最多处理账号数（防一轮打太多 billing 触发风控）
    max_accounts_per_round: int = 20


@dataclass
class InstallConfig:
    """官方首启安装序仿真（client/configs + 激活事件上报）。"""

    enabled: bool = True
    # 入池 / 指纹轮换后自动补跑安装序
    auto_on_add: bool = True
    timeout_seconds: int = 15


@dataclass
class ScheduleConfig:
    """定时任务排程（小时为本地时区整点）。"""

    checkin_hours: list[int] = field(default_factory=lambda: [9, 21])
    travel_hours: list[int] = field(default_factory=lambda: [9, 21])
    activity_hours: list[int] = field(default_factory=lambda: [10])
    keepalive_hours: list[int] = field(default_factory=lambda: [22])
    cat_hours: list[int] = field(default_factory=lambda: [1])

    checkin_enabled: bool = True
    travel_enabled: bool = True
    activity_enabled: bool = True
    keepalive_enabled: bool = True
    cat_enabled: bool = True


@dataclass
class GuardConfig:
    """账号级限额护栏（四条，全部默认关闭 = 0）。

    上游只告诉你「余额还剩多少」，不会阻止你把余额用尽；这四条护栏在本地
    拦住会把账号烧穿的请求。语义与后果见 `keyport/pool/guards.py`。

    `realm_overrides`：`{"cn": {"daily_token_limit": 1000000}}` 形式的分域覆盖；
    某键缺省或显式为 null 即继承全局值。
    """

    # 余额低于/等于该值时账号整体不再接单（防余额用尽触发上游提醒）
    reserve_credits: int = 0
    # 账号当天 token 消耗达到该值后整体不再接单（本地 0 点解封）
    daily_token_limit: int = 0
    # 账号当天积分消耗达到该值后**只服务免费模型**（本地 0 点解封）
    daily_credit_limit: int = 0
    # 账号在单个模型上当天 token 达到该值时，仅该模型不再接单
    model_daily_token_limit: int = 0
    # 分域覆盖（留空 / null = 继承全局）
    realm_overrides: dict[str, dict[str, int]] = field(default_factory=dict)


@dataclass
class ModelsConfig:
    """模型别名映射：把下游在用的名字映到实际模型。

    `{"gpt-4o": "hy4-preview-f", "claude-3-5-sonnet": "glm-5.3"}` —— 客户端
    硬编码的模型名不必改，网关在选号前换掉即可。别名键大小写不敏感；
    映射结果仍可带域前缀（如 `"fast": "zai:glm-5.3-flash"`）。

    只做**一层**映射：别名指向别名时不再二次解析，避免环与歧义。
    """

    aliases: dict[str, str] = field(default_factory=dict)

    def resolve(self, model: str) -> str:
        """解析别名；未命中原样返回。"""
        if not model or not self.aliases:
            return model
        lookup = {str(k).strip().lower(): str(v).strip()
                  for k, v in self.aliases.items()
                  if str(k).strip() and str(v).strip()}
        return lookup.get(model.strip().lower(), model)


@dataclass
class PromptConfig:
    """系统提示词替换策略。"""

    # custom = 用网关自有提示词替换客户端 system；passthrough = 原样透传（拦截时降级重试）
    mode: str = "custom"
    file: str = ""
    default_text: str = "You are a helpful assistant."


@dataclass
class FeaturesConfig:
    sanitize_blacklist_fingerprints: bool = True
    compress_long_context: bool = True
    reasoning_injection: bool = True


@dataclass
class SecurityConfig:
    """面板与 IP 管控。"""

    admin_password_hash: str = ""   # PBKDF2-SHA256 摘要
    admin_username: str = "admin"
    session_ttl_seconds: int = 12 * 3600
    global_allowlist: list[str] = field(default_factory=list)
    global_blocklist: list[str] = field(default_factory=list)
    trust_proxy_headers: bool = False
    audit_enabled: bool = True


@dataclass
class RealmConfig:
    """单个账号域（国内版 / 国际版 / z.ai）配置。"""

    enabled: bool = True
    chat_base: str = ""
    billing_base: str = ""
    models_base: str = ""
    # 仅暴露免费（0 倍率）模型。用于把 WorkBuddy 国际版的大模型列表
    # 收敛到 0 积分模型，避免下游看到一堆用不了/高消耗的付费模型。
    models_free_only: bool = False


def _default_realms() -> dict[str, RealmConfig]:
    """内置三域默认端点（数据直连上游，不编造）。"""
    return {
        # 腾讯 CodeBuddy 国内版
        "cn": RealmConfig(
            enabled=True,
            chat_base="https://copilot.tencent.com",
            billing_base="https://www.codebuddy.cn",
        ),
        # 国际版 WorkBuddy
        "global": RealmConfig(
            enabled=True,
            chat_base="https://www.workbuddy.ai",
            billing_base="https://www.workbuddy.ai",
            # 只暴露 0 积分（免费）模型：上游模型目录含大量付费模型，
            # 下游按需只看到免费档。
            models_free_only=True,
        ),
        # ZCode / z.ai（GLM Coding Plan）
        "zai": RealmConfig(
            enabled=True,
            chat_base="https://api.z.ai/api/coding/paas/v4",
            billing_base="https://api.z.ai/api",
            models_base="https://api.z.ai/api",
        ),
    }


@dataclass
class Config:
    listen_host: str = "0.0.0.0"
    listen_port: int = 8790
    data_dir: str = "./data"
    auth_dir: str = "./auths"
    db_path: str = ""            # 空 = <data_dir>/keyport.db
    static_dir: str = ""         # 空 = <ROOT>/frontend/out
    public_base_url: str = ""    # 管理台/网关对外基址（OAuth 回跳用）

    upstream: UpstreamConfig = field(default_factory=UpstreamConfig)
    pool: PoolConfig = field(default_factory=PoolConfig)
    captcha: CaptchaConfig = field(default_factory=CaptchaConfig)
    session_sticky: SessionStickyConfig = field(default_factory=SessionStickyConfig)
    claim: ClaimConfig = field(default_factory=ClaimConfig)
    install: InstallConfig = field(default_factory=InstallConfig)
    guards: GuardConfig = field(default_factory=GuardConfig)
    models: ModelsConfig = field(default_factory=ModelsConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    prompt: PromptConfig = field(default_factory=PromptConfig)
    features: FeaturesConfig = field(default_factory=FeaturesConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)

    # 双域 + z.ai：cn / global / zai 严格隔离
    realms: dict[str, RealmConfig] = field(default_factory=_default_realms)

    # 来源文件路径（不参与序列化）：save_config 无显式路径时写回此处，
    # 保证「谁加载的配置就存回谁的位置」，避免测试/多实例互相覆盖。
    source_path: Path | None = field(default=None, repr=False, compare=False)

    # ---- 派生路径 ----

    def data_path(self) -> Path:
        p = Path(self.data_dir)
        return p if p.is_absolute() else ROOT / p

    def auth_path(self) -> Path:
        p = Path(self.auth_dir)
        return p if p.is_absolute() else ROOT / p

    def resolved_db_path(self) -> Path:
        if self.db_path:
            p = Path(self.db_path)
            return p if p.is_absolute() else ROOT / p
        return self.data_path() / "keyport.db"

    def resolved_static_dir(self) -> Path:
        if self.static_dir:
            p = Path(self.static_dir)
            return p if p.is_absolute() else ROOT / p
        return ROOT / "frontend" / "out"

    def realm(self, name: str) -> RealmConfig | None:
        return self.realms.get(name)

    def enabled_realms(self) -> list[str]:
        return [name for name, rc in self.realms.items() if rc.enabled]


# ---- 加载与合并 ----

_ENV_PREFIX = "KP_"
_lock = threading.RLock()
_current: Config | None = None


def _apply_env(cfg: Config) -> None:
    """KP_SECTION__FIELD=value 形式覆盖（双下划线分隔层级）。"""
    for env_key, raw in os.environ.items():
        if not env_key.startswith(_ENV_PREFIX):
            continue
        path = env_key[len(_ENV_PREFIX):].lower().split("__")
        if not path:
            continue
        obj: Any = cfg
        for seg in path[:-1]:
            if not hasattr(obj, seg):
                obj = None
                break
            obj = getattr(obj, seg)
        if obj is None or not hasattr(obj, path[-1]):
            continue
        target = getattr(obj, path[-1])
        try:
            if isinstance(target, bool):
                setattr(obj, path[-1], raw.strip().lower() in ("1", "true", "yes", "on"))
            elif isinstance(target, int):
                setattr(obj, path[-1], int(raw))
            elif isinstance(target, float):
                setattr(obj, path[-1], float(raw))
            elif isinstance(target, (list, dict)):
                setattr(obj, path[-1], json.loads(raw))
            else:
                setattr(obj, path[-1], raw)
        except (ValueError, json.JSONDecodeError):
            continue  # 单个环境变量格式错误不阻断启动


def _build_section(cls: type, data: Any) -> Any:
    """按 dataclass 字段白名单构建实例：未知键忽略，缺失键取默认。"""
    if not is_dataclass(cls) or not isinstance(data, dict):
        return data
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        val = data[f.name]
        nested = f.type if isinstance(f.type, type) else globals().get(str(f.type))
        if nested is not None and is_dataclass(nested) and isinstance(val, dict):
            kwargs[f.name] = _build_section(nested, val)
        else:
            kwargs[f.name] = val
    return cls(**kwargs)


def load_config(path: str | Path | None = None) -> Config:
    """从磁盘加载配置；文件缺失/损坏时回落默认（不崩溃）。"""
    cfg_path = Path(path) if path else config_path_from_env()
    raw: dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except (OSError, json.JSONDecodeError):
            raw = {}

    cfg = Config()
    for f in fields(Config):
        if f.name not in raw or f.name == "source_path":
            continue
        val = raw[f.name]
        nested = f.type if isinstance(f.type, type) else globals().get(str(f.type))
        if nested is not None and is_dataclass(nested) and isinstance(val, dict):
            setattr(cfg, f.name, _build_section(nested, val))
        elif f.name == "realms" and isinstance(val, dict):
            merged = _default_realms()
            for rname, rval in val.items():
                base = merged.get(rname, RealmConfig())
                if isinstance(rval, dict):
                    merged[rname] = _build_section(RealmConfig, {**asdict(base), **rval})
            cfg.realms = merged
        else:
            setattr(cfg, f.name, val)

    _apply_env(cfg)
    cfg.source_path = cfg_path
    return cfg


def get_config() -> Config:
    global _current
    with _lock:
        if _current is None:
            _current = load_config()
        return _current


def reload_config(path: str | Path | None = None) -> Config:
    global _current
    with _lock:
        _current = load_config(path)
        return _current


def config_to_dict(cfg: Config) -> dict[str, Any]:
    """导出可序列化配置（排除 source_path 等运行时字段）。"""
    data = asdict(cfg)
    data.pop("source_path", None)
    return data


def save_config(cfg: Config, path: str | Path | None = None) -> None:
    """把当前配置写回磁盘（热改落盘）。

    路径解析顺序：显式 path > cfg.source_path（加载来源）> 环境变量/默认路径。

    `source_path` 由 load_config 设置，因此正常启动流程总能解析到路径。
    若既无显式 path 又无 source_path（例如临时构造的 Config），说明调用方
    没有指定落盘位置——此时**拒绝静默写全局默认文件**，否则测试/多实例会
    互相覆盖生产配置。请显式传 path，或改用 load_config 加载。
    """
    if path is not None:
        cfg_path = Path(path)
    elif cfg.source_path is not None:
        cfg_path = Path(cfg.source_path)
    else:
        raise ValueError(
            "save_config 拒绝写入：配置对象无 source_path 且未显式指定 path。"
            "请传入 path，或用 load_config() 加载配置。"
        )
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(config_to_dict(cfg), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
