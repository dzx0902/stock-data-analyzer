from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env", override=False)


def _positive_int(name: str, default: int, maximum: int) -> int:
    try:
        return max(1, min(int(os.getenv(name, str(default))), maximum))
    except ValueError:
        return default


def _nonnegative_int(name: str, default: int, maximum: int) -> int:
    try:
        return max(0, min(int(os.getenv(name, str(default))), maximum))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    app_id: str = field(default_factory=lambda: os.getenv("FEISHU_APP_ID", "").strip())
    app_secret: str = field(default_factory=lambda: os.getenv("FEISHU_APP_SECRET", "").strip())
    deepseek_api_key: str = field(default_factory=lambda: os.getenv("DEEPSEEK_API_KEY", "").strip())
    deepseek_base_url: str = field(
        default_factory=lambda: os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
    )
    deepseek_model: str = field(default_factory=lambda: os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip())
    qwen_api_key: str = field(default_factory=lambda: os.getenv("QWEN_API_KEY", "").strip())
    qwen_base_url: str = field(
        default_factory=lambda: os.getenv(
            "QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ).strip()
    )
    qwen_model: str = field(default_factory=lambda: os.getenv("QWEN_MODEL", "qwen-plus").strip())
    qwen_vision_model: str = field(
        default_factory=lambda: os.getenv("QWEN_VISION_MODEL", "qwen-vl-plus").strip()
    )
    alltick_api_key: str = field(default_factory=lambda: os.getenv("ALLTICK_API_KEY", "").strip())
    alltick_http_base: str = field(
        default_factory=lambda: os.getenv("ALLTICK_HTTP_BASE", "").strip().rstrip("/")
    )
    alltick_market: str = field(default_factory=lambda: os.getenv("ALLTICK_MARKET", "").strip())
    alltick_gold_symbol: str = field(default_factory=lambda: os.getenv("ALLTICK_GOLD_SYMBOL", "GOLD").strip())
    alltick_silver_symbol: str = field(
        default_factory=lambda: os.getenv("ALLTICK_SILVER_SYMBOL", "SILVER").strip()
    )
    alltick_usd_cny_symbol: str = field(
        default_factory=lambda: os.getenv("ALLTICK_USD_CNY_SYMBOL", "USDCNY").strip()
    )
    timezone: str = field(
        default_factory=lambda: os.getenv("GOLD_AGENT_TIMEZONE", "Asia/Shanghai").strip() or "Asia/Shanghai"
    )
    search_proxy: str = field(default_factory=lambda: os.getenv("SEARCH_PROXY", "").strip())
    usd_cny_fallback_rate: str = field(
        default_factory=lambda: os.getenv("USD_CNY_FALLBACK_RATE", "").strip()
    )
    usd_cny_provider_url: str = field(
        default_factory=lambda: os.getenv("USD_CNY_PROVIDER_URL", "").strip()
    )
    usd_cny_cache_seconds: int = field(
        default_factory=lambda: _positive_int("USD_CNY_CACHE_SECONDS", 300, 3600)
    )
    max_workers: int = field(default_factory=lambda: _positive_int("GOLD_AGENT_MAX_WORKERS", 8, 32))
    sqlite_wal: bool = field(default_factory=lambda: os.getenv("GOLD_AGENT_SQLITE_WAL", "0") == "1")
    keep_global_proxy: bool = field(default_factory=lambda: os.getenv("KEEP_GLOBAL_PROXY", "0") == "1")
    db_path: Path = field(
        default_factory=lambda: Path(os.getenv("GOLD_AGENT_DB", "gold_agent.db")).expanduser()
    )
    backup_dir: Path = field(
        default_factory=lambda: Path(os.getenv("GOLD_AGENT_BACKUP_DIR", "backups")).expanduser()
    )
    backup_interval_hours: int = field(
        default_factory=lambda: _nonnegative_int("GOLD_AGENT_BACKUP_INTERVAL_HOURS", 24, 720)
    )
    run_local_schedulers: bool = field(
        default_factory=lambda: os.getenv("GOLD_AGENT_RUN_LOCAL_SCHEDULERS", "1") == "1"
    )
    run_feishu: bool = field(default_factory=lambda: os.getenv("GOLD_AGENT_RUN_FEISHU", "1") == "1")

    def resolved_db_path(self) -> Path | str:
        path = self.db_path
        if str(path) == ":memory:":
            return ":memory:"
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve()

    def validate_feishu(self) -> None:
        missing = [
            name
            for name, value in (
                ("FEISHU_APP_ID", self.app_id),
                ("FEISHU_APP_SECRET", self.app_secret),
            )
            if not value
        ]
        if missing:
            raise RuntimeError("缺少飞书环境变量: " + ", ".join(missing))


settings = Settings()

APP_ID = settings.app_id
APP_SECRET = settings.app_secret
DEEPSEEK_API_KEY = settings.deepseek_api_key
BASE_URL_DEEPSEEK = settings.deepseek_base_url
LLM_MODEL_DEEPSEEK = settings.deepseek_model
QWEN_API_KEY = settings.qwen_api_key
BASE_URL_QWEN = settings.qwen_base_url
LLM_MODEL_QWEN = settings.qwen_model
LLM_MODEL_QWEN_VISION = settings.qwen_vision_model
ALLTICK_API_KEY = settings.alltick_api_key
ALLTICK_HTTP_BASE = settings.alltick_http_base
ALLTICK_MARKET = settings.alltick_market
ALLTICK_GOLD_SYMBOL = settings.alltick_gold_symbol
ALLTICK_SILVER_SYMBOL = settings.alltick_silver_symbol
ALLTICK_USD_CNY_SYMBOL = settings.alltick_usd_cny_symbol
APP_TIMEZONE = settings.timezone
SEARCH_PROXY = settings.search_proxy
MAX_WORKERS = settings.max_workers
USD_CNY_FALLBACK_RATE = settings.usd_cny_fallback_rate
USD_CNY_PROVIDER_URL = settings.usd_cny_provider_url
USD_CNY_CACHE_SECONDS = settings.usd_cny_cache_seconds
DB_PATH = str(settings.resolved_db_path())

if not settings.keep_global_proxy:
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        os.environ.pop(key, None)
