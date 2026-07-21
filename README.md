# Gold Agent

一个基于飞书的黄金投资助手。当前代码已经不是单纯的聊天壳子，而是把行情查询、市场分析、账本、定时报表、投资档案、策略提醒、宏观事件、实体黄金管理、记忆和审计都串起来了。

## 已实现功能

- 黄金行情查询：上金所 Au99.99、国际黄金、白银、品牌金店、银行金条、回收价，以及一键汇总
- 价差与换算：国际金价折算、品牌金店/银行金条/回收价对比、美元兑人民币换算
- 市场分析：网页搜索、正文抓取、时间过滤、舆情分析
- 量化分析：历史行情、收益率、波动率、最大回撤、RSI
- 账本管理：买卖记账、批量记账、移动加权平均成本、已实现盈亏、批次持仓、卖出估算、撤销最近一笔
- 待确认机制：记账和部分危险操作先落草稿，用户回复确认后才真正写库
- 投资档案：目标仓位、风险等级、单笔上限、回撤容忍度、默认成本法、持仓偏好
- 个性化建议：结合账本和投资档案生成持仓建议和风险提示
- 策略提醒：价格阈值、买入区间、仓位偏离、风险触发、分批买入和再平衡
- 宏观面板：因子解释、宏观事件记录与查询
- 周报/月报：交易复盘、行为分析、周期性总结
- 实体黄金：金条、首饰、证书、存放位置、图片引用、估值和回收估值
- 报告生成：Markdown 转 Word，生成前做质量检查，必要时自动补写
- 飞书接入：消息收发、图片识别、Word 文件上传、后台线程处理
- 长期记忆：用户偏好、会话摘要、交互日志
- 审计与备份：关键操作审计日志、SQLite 备份

## 目录结构

```text
gold_agent/
├── app.py              # 进程入口，启动飞书长连和后台线程
├── bootstrap.py        # 数据库迁移与基础表初始化
├── config.py           # 环境变量读取与配置
├── agent/              # LLM 编排、工具定义、路由辅助
├── infra/              # SQLite、HTTP、Decimal、迁移
├── integrations/       # 飞书接入
├── market/             # 行情、价差、搜索、量化、汇率
├── ledger/             # 账本、待确认操作、提醒、报表调度
├── memory/             # 长期记忆和会话摘要
├── user_profile/       # 投资档案模型、解析和存储
├── strategy/           # 策略提醒、买入计划、再平衡
├── macro/              # 宏观因子和事件
├── reports/            # 报告质量检查与 Word 生成
├── physical_gold/      # 实体黄金管理
├── review/             # 周报/月报与交易行为分析
├── advice/             # 个性化建议
├── security/           # 审计与备份
└── router/             # 意图分类和帮助文本
```

## 运行方式

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
python -m gold_agent
```

也可以继续用旧入口：

```powershell
python gold_bot.py
```

## 配置

参考 `.env.example` 配置环境变量。核心项包括：

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `DEEPSEEK_API_KEY`
- `QWEN_API_KEY`
- `ALLTICK_API_KEY`
- `GOLD_AGENT_DB`
- `GOLD_AGENT_BACKUP_DIR`

## 数据库

项目使用 SQLite，启动时会执行迁移。当前已有的主要表包括：

- `audit_logs`
- `ledger_transactions_v2`
- `pending_operations`
- `processed_messages`
- `investment_profiles`
- `strategy_alerts`
- `macro_events`
- `physical_gold_items`
- `generated_reviews`
- `user_profiles`
- `interaction_logs`

## 当前状态说明

`Prompt.md` 仍停留在“阶段 0：项目现状审计”的原始任务描述上，没有跟着代码演进更新。

按现在的代码看，项目已经覆盖了阶段 1 到阶段 9 中相当一部分能力，尤其是：

- 阶段 1：统一行情模型和多源校验，已经有 `PriceQuote` 和行情对比逻辑
- 阶段 2：账本和真实盈亏计算，已经有 Decimal 版账本和迁移
- 阶段 3：投资档案和个性化建议，已经有档案模型、解析、存储和建议生成
- 阶段 4 / 5 / 6 / 8 / 9：策略提醒、宏观面板、报表、实体黄金、审计和备份也都有实装

仍然缺得比较明显的，主要是更完整的产品化能力，例如：

- 更严格的权限和确认流程
- 更完善的测试覆盖
- 更系统的报表和审计工作流
- 更稳健的多源行情校验与错误处理
- 更规范的 schema 演进与回放能力

## 说明

- 现有代码已经实现飞书消息驱动的主流程
- 金额、克重、价格相关计算已逐步切到 Decimal
- 复杂操作会先落草稿，用户确认后才入库
- Word 报告生成前会做质量检查，并尝试自动补写
