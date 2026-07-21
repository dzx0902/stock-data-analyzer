from gold_agent.router.service import classify_intent, help_text


def test_intent_router_and_help():
    assert classify_intent("帮我设计分批买入计划")["intent"] == "strategy"
    assert classify_intent("查看我的实物黄金清单")["intent"] == "physical"
    assert classify_intent("含糊不清")["needs_clarification"] is True
    assert "账本" in help_text()
