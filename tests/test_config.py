from pathlib import Path

from gold_agent.config import Settings

RUNTIME_DIR = Path(__file__).parent / ".runtime"


def test_secrets_have_no_hardcoded_defaults(monkeypatch):
    for name in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "DEEPSEEK_API_KEY", "QWEN_API_KEY", "ALLTICK_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings()

    assert settings.app_id == ""
    assert settings.app_secret == ""
    assert settings.deepseek_api_key == ""
    assert settings.qwen_api_key == ""
    assert settings.alltick_api_key == ""


def test_relative_database_path_resolves_from_working_directory(monkeypatch):
    RUNTIME_DIR.mkdir(exist_ok=True)
    monkeypatch.chdir(RUNTIME_DIR)
    monkeypatch.setenv("GOLD_AGENT_DB", "data/test.db")

    settings = Settings()

    assert settings.resolved_db_path() == Path(RUNTIME_DIR, "data/test.db").resolve()


def test_exchange_rate_runtime_defaults(monkeypatch):
    monkeypatch.delenv("USD_CNY_PROVIDER_URL", raising=False)
    monkeypatch.delenv("USD_CNY_CACHE_SECONDS", raising=False)

    settings = Settings()

    assert settings.usd_cny_provider_url == ""
    assert settings.usd_cny_cache_seconds == 300
