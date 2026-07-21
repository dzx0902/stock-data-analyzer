import importlib
import json
import threading
from decimal import Decimal


def _load_services(monkeypatch):
    monkeypatch.setenv("GOLD_AGENT_DB", ":memory:")
    import gold_agent.config as config
    import gold_agent.infra.database as database
    import gold_agent.infra.migrations as migrations
    import gold_agent.ledger.service as ledger
    import gold_agent.user_profile.store as store

    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(migrations).run_migrations()
    importlib.reload(ledger)
    return importlib.reload(store), ledger


def test_profile_parser_extracts_stable_preferences():
    from gold_agent.user_profile.parser import parse_profile_updates

    updates = parse_profile_updates(
        "记住我的黄金目标仓位是15%，风险偏好比较低，"
        "以后默认用先进先出，单笔最多5000元，报告简洁一点"
    )

    assert updates["target_gold_allocation"] == Decimal("0.15")
    assert updates["risk_level"] == "low"
    assert updates["default_cost_method"] == "fifo"
    assert updates["max_single_buy_amount_cny"] == Decimal("5000")
    assert updates["report_style"] == "concise"


def test_profile_store_validates_and_persists(monkeypatch):
    store, _ = _load_services(monkeypatch)

    profile = store.update_investment_profile(
        "profile-user",
        {
            "investment_goal": "long_term",
            "risk_level": "low",
            "target_gold_allocation": "0.15",
            "preferred_channels": ["银行积存金"],
            "default_cost_method": "fifo",
            "allow_suggestion": True,
        },
    )

    assert profile.investment_goal == "long_term"
    assert profile.target_gold_allocation == Decimal("0.15")
    assert store.get_investment_profile("profile-user").default_cost_method == "fifo"


def test_personalized_advice_respects_profile(monkeypatch):
    store, ledger = _load_services(monkeypatch)
    store.update_investment_profile(
        "advice-user",
        {
            "investment_goal": "long_term",
            "risk_level": "low",
            "target_gold_allocation": "0.10",
            "total_assets_cny": "100000",
            "max_single_buy_amount_cny": "5000",
        },
    )
    ok, _ = ledger.add_gold_transaction(
        "advice-user", "buy", "10", "500", batch_id="A"
    )
    assert ok

    from gold_agent.advice.personalized import generate_personalized_advice

    result = generate_personalized_advice("advice-user", "600")

    assert result["ledger"]["estimated_allocation"] == Decimal("0.06")
    assert any("低于目标仓位" in item for item in result["suggestions"])
    assert any("分批" in item for item in result["suggestions"])
    assert "风险提示" in result["risk_notice"]


def test_disabling_suggestions_changes_output(monkeypatch):
    store, _ = _load_services(monkeypatch)
    store.update_investment_profile(
        "info-only", {"allow_suggestion": False, "risk_level": "high"}
    )
    from gold_agent.advice.personalized import generate_personalized_advice

    result = generate_personalized_advice("info-only")

    assert result["suggestions"] == [
        "用户档案设置为只提供信息，因此不输出操作倾向。"
    ]


def test_agent_profile_tool_updates_and_shows_profile(monkeypatch):
    _load_services(monkeypatch)
    import gold_agent.agent.tools as agent_tools

    agent_tools = importlib.reload(agent_tools)
    threading.current_thread().current_user_id = "tool-profile"
    updated = json.loads(
        agent_tools.call_tool(
            "manage_investment_profile",
            {
                "action": "parse_text",
                "text": "我的风险偏好比较低，黄金目标仓位是12%，以后默认先进先出",
            },
        )
    )
    shown = json.loads(
        agent_tools.call_tool(
            "manage_investment_profile", {"action": "show"}
        )
    )

    assert updated["status"] == "success"
    assert shown["profile"]["risk_level"] == "low"
    assert shown["profile"]["target_gold_allocation"] == "0.12"
    assert shown["profile"]["default_cost_method"] == "fifo"
