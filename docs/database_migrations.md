# Database Migrations

数据库迁移由 `gold_agent.infra.migrations.run_migrations()` 在应用启动时执行。

## 当前版本

| 版本 | 名称 | 内容 |
|---|---|---|
| 1 | `baseline_audit` | 建立迁移版本表和审计日志 |
| 2 | `decimal_ledger` | 建立 Decimal 文本账本表并迁移旧交易 |
| 3 | `operations_and_message_state` | 建立通用待确认操作和消息处理状态 |

## Decimal 存储

`ledger_transactions_v2` 使用 TEXT 保存十进制数：

- `quantity_grams`：4 位小数
- `unit_price_cny`：4 位小数
- `fee_cny`：2 位小数

读取后统一转换为 `Decimal`。旧 `gold_transactions` 仅作为迁移来源，不再用于新写入。

## 验证

```powershell
$env:GOLD_AGENT_DB=":memory:"
python -c "from gold_agent.bootstrap import initialize; from gold_agent.infra.migrations import migration_status; initialize(); print(migration_status())"
```

部署升级前应备份现有数据库。迁移完成后执行：

```sql
PRAGMA integrity_check;
```
# Migration 5: investment_profiles

新增结构化用户投资档案，保存投资目标、风险等级、目标仓位、单笔上限、
回撤容忍、渠道偏好、报告风格、默认成本法、建议权限和总资产。

比例和金额均以十进制字符串保存，避免 SQLite `REAL` 精度问题。

# Migration 6: remaining_features

新增：

- `strategy_alerts`：策略型提醒
- `macro_events`：宏观事件日历
- `physical_gold_items`：实物黄金档案
- `generated_reviews`：周报、月报和复盘留档

策略参数和扩展字段使用 JSON；金额和克重仍使用十进制字符串。
