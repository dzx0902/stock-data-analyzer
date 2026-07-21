from __future__ import annotations


INTENTS = {
    "price": ["金价", "黄金价格", "回收价", "国际金"],
    "price_compare": ["价差", "溢价", "对比上金所"],
    "market": ["舆情", "新闻", "为什么上涨", "为什么下跌"],
    "quant": ["量化", "RSI", "均线", "波动率"],
    "ledger": ["记账", "账本", "持仓", "盈亏"],
    "profile": ["投资档案", "风险偏好", "目标仓位"],
    "strategy": ["分批买入", "止盈", "止损", "再平衡"],
    "macro": ["宏观因子", "美联储", "CPI", "非农", "美元指数"],
    "review": ["周报", "月报", "复盘", "交易习惯"],
    "backtest": ["回测", "定投策略", "网格策略"],
    "physical": ["实物黄金", "金条清单", "证书图片"],
    "help": ["你能做什么", "怎么用", "帮助"],
}


def classify_intent(text: str) -> dict[str, object]:
    scores = {
        intent: sum(1 for term in terms if term.lower() in (text or "").lower())
        for intent, terms in INTENTS.items()
    }
    best = max(scores, key=scores.get)
    score = scores[best]
    return {
        "intent": best if score else "unknown",
        "confidence": min(1.0, score / 2),
        "needs_clarification": score == 0,
    }


def help_text(topic: str = "") -> str:
    sections = {
        "ledger": "账本：自然语言记账、图片批量识别、持仓盈亏、FIFO 和指定批次。",
        "strategy": "策略：分批买入计划、止盈止损、仓位偏离和再平衡提醒。",
        "backtest": "回测：定投、均线、RSI、网格和分批买入策略对比。",
        "physical": "实物黄金：记录金条/首饰、证书、图片、存放位置和回收估值。",
    }
    if topic in sections:
        return sections[topic]
    return "支持查价、价差、市场分析、量化、账本、投资档案、策略提醒、宏观面板、周月报、回测和实物黄金管理。"
