# Gold Agent 分阶段扩展计划

本计划基于 2026-06-15 的代码审计。当前只完成阶段 0 文档，不执行阶段 1 及后续实现。

## 1. 实施原则

1. 每个阶段独立提交，阶段内保持改动范围可审查。
2. 先补迁移和测试基线，再扩展数据库字段。
3. 所有金额、价格和克重领域计算统一使用 `Decimal`。
4. SQLite 持久化采用十进制定点字符串或缩放整数，禁止新增财务 `REAL` 字段。
5. 所有外部行情保留 `source`、`timestamp`、`raw_payload`。
6. 所有自动账本写入必须先生成草稿并由用户确认。
7. 删除、撤销、批量修改、清空和恢复操作必须二次确认。
8. 外部接口必须有明确错误和可验证 fallback。
9. 投资建议最终输出必须包含风险提示。
10. 新功能必须包含最小单元测试和飞书命令示例。

## 2. 推荐目标架构

保留当前 `gold_agent` 包，不另起顶层 `services`，避免与现有结构并存两套风格。建议逐步演进为：

```text
gold_agent/
├── agent/
│   ├── core.py
│   ├── tools.py
│   └── prompts.py
├── application/
│   ├── command_bus.py
│   ├── confirmation.py
│   └── dto.py
├── infra/
│   ├── database.py
│   ├── migrations.py
│   ├── http.py
│   └── clock.py
├── integrations/
│   ├── feishu/
│   │   ├── client.py
│   │   ├── handlers.py
│   │   └── files.py
│   └── vision/
│       └── qwen.py
├── market/
│   ├── model.py
│   ├── converter.py
│   ├── validator.py
│   ├── compare.py
│   └── providers/
├── ledger/
│   ├── model.py
│   ├── repository.py
│   ├── cost_basis.py
│   ├── pnl.py
│   └── service.py
├── alerts/
├── reports/
├── memory/
├── security/
└── router/
```

该结构是目标方向，不要求一次性搬迁。

## 3. 第一批改造计划

第一批严格按提示词顺序实施：阶段 1、阶段 2、阶段 9。每个阶段开始前先运行现有测试并建立独立提交。

## 4. 阶段 1：行情可信度与统一入口

### 4.1 目标

建立统一 `PriceQuote`、Decimal 换算、行情验证和多源价差分析，保持现有 `get_gold_quote` 与飞书查价命令兼容。

### 4.2 实施步骤

1. 新增不可变 `PriceQuote` 模型。
2. 新增 `Money`/Decimal 解析与序列化约定。
3. 将现有 provider 返回值适配为 `PriceQuote`，暂时保留旧 dict 外观。
4. 新增盎司/克与 USD/CNY 换算工具。
5. 明确汇率数据源和 fallback；缺汇率时禁止静默换算。
6. 新增时效、空值、零值、范围、单位、跳变检测。
7. 新增 Au99.99、国际折算、银行、品牌、回收的价差比较。
8. 新增价格诊断 Agent 工具和飞书意图。
9. 增加 provider fixture 测试，禁止测试依赖实时网络。
10. 更新 README 和命令示例。

### 4.3 建议新增文件

```text
gold_agent/market/model.py
gold_agent/market/converter.py
gold_agent/market/validator.py
gold_agent/market/compare.py
gold_agent/market/providers/base.py
tests/test_price_model.py
tests/test_price_converter.py
tests/test_price_validator.py
tests/test_price_compare.py
tests/fixtures/market/
```

### 4.4 建议修改文件

```text
gold_agent/market/prices.py
gold_agent/agent/tools.py
gold_agent/agent/core.py
gold_agent/integrations/feishu.py
gold_agent/config.py
.env.example
README.md
```

### 4.5 配置补充

建议新增：

```text
PRICE_STALE_SECONDS_REALTIME
PRICE_STALE_SECONDS_DAILY
PRICE_JUMP_THRESHOLD_PCT
PRICE_SPREAD_THRESHOLD_PCT
USD_CNY_PROVIDER
USD_CNY_FALLBACK_RATE
```

`USD_CNY_FALLBACK_RATE` 只能作为显式配置的应急值，返回结果必须标记非实时和低置信度。

### 4.6 数据库影响

阶段 1 可先不持久化行情。如需要跳变检测和来源健康度，新增：

```text
price_observations
provider_health
```

在新增任何表前先完成 `schema_migrations` 基线。

### 4.7 测试方式

```powershell
python -m pytest tests/test_price_model.py
python -m pytest tests/test_price_converter.py
python -m pytest tests/test_price_validator.py
python -m pytest tests/test_price_compare.py
python -m pytest
```

关键断言：

- 1 金衡盎司等于 31.1034768 克。
- USD/oz 与 CNY/g 双向换算误差受 Decimal 精度控制。
- 过期、零值、单位缺失和异常跳变均被识别。
- 任一数据源失败不影响明确配置的 fallback。
- 返回始终保留 source、timestamp、raw_payload。

## 5. 阶段 2：真实盈亏与交易成本

### 5.1 前置条件

- `schema_migrations` 已上线。
- Decimal 持久化约定已确定。
- 所有写账路径已统一为“草稿 -> 确认 -> 提交”。

### 5.2 实施步骤

1. 建立交易、费用和批次领域模型。
2. 为旧交易生成默认账户、渠道和批次。
3. 增加移动平均、FIFO、指定批次成本引擎。
4. 拆分买入费用、卖出费用、加工费、税、配送和保管费。
5. 增加批次剩余克重和逐批盈亏。
6. 增加交易修改草稿和二次确认。
7. 撤销改为审计型反向操作或软删除，不直接无痕删除。
8. 增加账户/渠道收益对比。

### 5.3 建议新增文件

```text
gold_agent/ledger/model.py
gold_agent/ledger/repository.py
gold_agent/ledger/cost_basis.py
gold_agent/ledger/pnl.py
gold_agent/ledger/batches.py
tests/test_ledger_cost_basis.py
tests/test_ledger_real_pnl.py
tests/test_ledger_batches.py
```

### 5.4 数据库迁移建议

不建议直接向旧表大量追加 `REAL` 字段。建议：

1. 增加 `schema_migrations`。
2. 新建 v2 交易表，金额和数量用十进制字符串或缩放整数。
3. 编写一次性迁移，把旧 `REAL` 转为规范小数。
4. 校验总持仓和交易数量后再切换读取。
5. 保留旧表只读一段时间，确认后归档。

建议表：

```text
ledger_transactions
ledger_fees
ledger_batches
ledger_batch_allocations
pending_operations
```

### 5.5 测试方式

- 移动平均、FIFO、指定批次使用同一组交易 fixture 比较。
- 买卖双方费用均进入真实盈亏。
- 超卖和超批次卖出失败。
- 修改/删除未确认时数据库不变化。
- 旧数据迁移前后持仓一致。
- 所有 Decimal 结果精确到约定小数位。

## 6. 阶段 9：安全、审计和备份

阶段 9 在第一批中提前执行，因为阶段 2 会增加高价值写操作。

### 6.1 实施步骤

1. 建立统一审计事件模型。
2. 所有账本、提醒、偏好、报告和导出操作写审计日志。
3. 建立通用 pending operation 和确认令牌。
4. 消息去重表增加状态、重试次数和错误摘要。
5. 使用 SQLite backup API 实现在线备份。
6. 增加备份校验和恢复演练命令。
7. 增加图片、报告临时文件生命周期策略。
8. 日志脱敏 API key、图片数据、完整用户消息和证书号码。

### 6.2 建议新增文件

```text
gold_agent/security/audit.py
gold_agent/security/confirmation.py
gold_agent/security/backup.py
gold_agent/security/privacy.py
tests/test_audit_log.py
tests/test_confirmation.py
tests/test_backup.py
```

### 6.3 数据库新增

```text
audit_logs
pending_operations
backup_records
```

### 6.4 测试方式

- 每类写操作生成一条可关联审计记录。
- 未确认、过期确认、错误用户确认均不执行。
- 备份后可在临时数据库恢复并通过完整性检查。
- 日志中不出现 API key、base64 图片和完整敏感字段。

## 7. 第二批计划

### 阶段 3：用户投资档案

建立结构化风险等级、投资目标、目标仓位、默认成本法和建议授权。保留现有 `user_profiles` JSON，先增加 schema version，再逐步迁移。

### 阶段 4：策略型提醒

将提醒条件建模为规则，支持盈利率、亏损率、回撤、仓位、区间、波动和再平衡。规则计算与通知发送分离。

### 阶段 10：统一命令路由和帮助系统

把当前分散在飞书、Agent 和正则解析器中的路由统一到意图分类层。低置信度路由只追问，不执行写操作。

## 8. 第三批计划

### 阶段 5：宏观因素面板

为每个宏观因子定义来源、更新时间、影响方向和证据。外部数据均走 provider 接口并保留 raw payload。

### 阶段 6：周报、月报和交易复盘

先建立可复现的统计查询，再调用 LLM 做解释。追高、频繁交易等判断必须有透明规则和证据。

## 9. 第四批计划

### 阶段 7：策略回测

使用独立回测引擎，历史数据、信号、成交、手续费和指标分层。禁止把当前 `run_quant_analysis` 直接扩展成复杂回测大函数。

### 阶段 8：实物黄金管理

建立实物物品、证书、发票、图片引用、存放位置和估值模型。图片权限和生命周期纳入阶段 9 的隐私机制。

## 10. 第一批改造前的基线修复

以下工作不属于新增业务能力，但应作为阶段 1 的第一个小提交：

1. 增加 `schema_migrations` 和 migration runner。
2. 修复 `create_word_report` 未调用质检主链的问题。
3. 将文本/LLM 记账统一改为待确认草稿。
4. 为消息去重增加处理状态，允许失败重试。
5. 对当前关键中文文本做 UTF-8 可读性检查。
6. 清理模块中的无关 import。
7. 补充最小测试：
   - 报告质检确实执行。
   - 文本记账确认前不写库。
   - 失败消息可重试。

## 11. 每一步的通用验证方式

### 静态验证

```powershell
python -m compileall -q gold_agent tests
```

### 单元测试

```powershell
python -m pytest
```

### 数据库迁移验证

1. 从空数据库初始化。
2. 从当前旧 schema 升级。
3. 重复执行迁移，确认幂等。
4. 执行 `PRAGMA integrity_check`。
5. 核对迁移前后用户数、交易数和持仓。

### 行情验证

1. 使用固定 fixture 测 provider 解析。
2. 使用 mock 测主源失败和 fallback。
3. 可选运行在线 smoke test，但不作为单元测试通过条件。

### 飞书验证

1. 用构造事件测试文本、图片和重复消息。
2. mock 飞书 SDK，验证回复、主动通知和文件上传。
3. 在测试应用中做一次真实长连接 smoke test。

### 财务验证

所有预期值使用 `Decimal` 常量。覆盖：

- 多次买入。
- 部分卖出。
- 全部卖出。
- 买卖双方费用。
- 超卖。
- 批次选择。
- 退款/撤销。

## 12. 文档交付要求

每完成一个阶段，应同步更新：

```text
README.md
docs/current_architecture_review.md
docs/agent_extension_plan.md
docs/commands.md
docs/database_migrations.md
```

阶段交付说明至少包含：

- 改动摘要。
- 涉及文件。
- 数据库迁移版本。
- 新增环境变量。
- 飞书命令示例。
- 测试命令和结果。
- 已知限制。
- 下一阶段建议。

## 13. 当前停止点

阶段 0 已完成。高风险治理基线和阶段 1 第一版已于 2026-06-15 实施：

- 版本化迁移、审计日志和自动备份；
- Decimal v2 账本；
- 统一待确认写账；
- 消息处理状态与失败重试；
- 报告质检接入主链；
- `PriceQuote`、换算、验证和价差比较。

下一实施阶段为阶段 2：真实盈亏、费用拆分与批次成本。
# 阶段进度（2026-06-15）

阶段二和阶段三已完成。阶段三新增结构化投资档案、自然语言配置、
档案上下文注入、个性化建议和建议权限控制。

阶段 4、5、6、7、8、10 已完成第一版工程落地：
策略提醒、统一路由、宏观面板、事件日历、周月复盘、行为分析、
策略回测和实物黄金管理均已有独立模块、迁移和单元测试。

后续工作进入生产强化，而非继续新增 Prompt.md 阶段：补充真实宏观数据供应商、
端到端飞书事件测试、策略监控运行指标和更丰富的回测成交模型。
