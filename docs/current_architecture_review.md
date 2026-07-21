# Gold Agent 当前架构审计

审计日期：2026-06-15  
审计范围：阶段 0，仅审计现状，不实现后续功能。

## 1. 结论摘要

当前项目已经从单文件拆分为 Python 包，具备可运行的黄金行情、市场检索、量化分析、账本、截图识别、提醒、定时汇报、Word 报告和用户记忆能力。启动、配置、基础设施、领域功能和飞书集成已有初步分层。

现有结构适合继续演进，但还不是稳定的投资助理领域架构。主要问题如下：

1. 行情数据用松散 `dict` 表达，没有统一价格模型、币种/单位语义和跨源校验。
2. 金额、价格和克重使用 `float` 与 SQLite `REAL`，不满足后续财务计算精度要求。
3. `ledger/service.py` 同时包含交易账本、订阅、提醒和后台调度，职责过重。
4. `integrations/feishu.py` 同时包含 SDK 适配、截图识别、业务路由和工作流编排。
5. 自然语言记账和 LLM 工具记账可直接写库，只有截图记账经过确认。
6. 数据库没有 schema version 和统一迁移框架，只有 `alerts` 表做了局部列迁移。
7. 报告质检代码存在，但未接入实际 Word 生成主链路。
8. 自动任务是进程内永久线程，没有租约、任务锁、失败重试记录和可观测性。
9. 测试只覆盖配置与部分账本逻辑，行情、飞书、报告、记忆和调度基本无测试。
10. 多个模块保留了大量无关 import，部分中文文本存在历史编码显示异常，维护成本较高。

## 2. 当前项目结构

```text
gold_bot.py                         # 兼容启动入口
gold_agent/
├── __main__.py                     # python -m gold_agent
├── app.py                          # 应用启动、后台线程、飞书长连接
├── bootstrap.py                    # 数据库表初始化
├── config.py                       # 环境变量配置
├── agent/
│   ├── core.py                     # 系统提示词、路由、LLM 工具循环
│   └── tools.py                    # Function Calling 定义与分发
├── infra/
│   ├── database.py                 # SQLite 全局连接、游标和锁
│   └── http.py                     # HTTP、代理和通用解析函数
├── integrations/
│   └── feishu.py                   # 飞书收发、图片下载、OCR、消息工作流
├── ledger/
│   └── service.py                  # 账本、提醒、订阅、消息去重、定时任务
├── market/
│   ├── prices.py                   # 实时/静态价格入口和格式化
│   ├── search.py                   # 搜索、正文抓取、舆情分析
│   └── quant.py                    # 历史行情与技术指标
├── memory/
│   └── service.py                  # 用户偏好、摘要、交互记录
└── reports/
    └── service.py                  # 报告质检、修复、Word 生成
tests/
├── test_config.py
└── test_ledger.py
```

### 模块位置对照

| 能力 | 当前位置 | 说明 |
|---|---|---|
| 行情模块 | `gold_agent/market/prices.py` | 国内外金银、品牌、银行、回收价格 |
| 账本模块 | `gold_agent/ledger/service.py` | 交易、成本、盈亏、确认草稿 |
| 飞书消息入口 | `gold_agent/integrations/feishu.py` | `on_p2_im_message_receive_v1` |
| 定时任务 | `gold_agent/ledger/service.py` | `monitor_alerts`、`monitor_scheduled_reports` |
| SQLite 连接 | `gold_agent/infra/database.py` | 单全局连接、游标和 `RLock` |
| SQLite 建表 | `bootstrap.py`、`ledger/service.py`、`memory/service.py` | 启动时执行 |
| 报告生成 | `gold_agent/reports/service.py` | Word 排版、规则质检、自动补爬 |
| 用户记忆 | `gold_agent/memory/service.py` | JSON 偏好和交互日志 |
| 配置 | `gold_agent/config.py`、`.env.example` | 环境变量读取 |
| Agent 编排 | `gold_agent/agent/core.py`、`agent/tools.py` | 直达路由与 DeepSeek Function Calling |

## 3. 启动和依赖关系

启动路径：

```text
python -m gold_agent / python gold_bot.py
  -> gold_agent.app.main
  -> validate_feishu
  -> bootstrap.initialize
  -> 启动提醒线程
  -> 启动定时汇报线程
  -> 注册飞书消息事件
  -> 启动飞书 WebSocket 长连接
```

当前主要依赖方向：

```text
config / infra
  -> market / memory / reports / ledger
  -> agent
  -> integrations.feishu
  -> app
```

实际存在反向依赖：`ledger/service.py` 的后台任务在函数内部导入 `integrations.feishu.send_feishu_message`。这是为避免循环导入的权宜方式，说明通知发送接口尚未抽象。

## 4. 已有能力与待扩展能力

| 领域 | 状态 | 当前实现 | 主要缺口 |
|---|---|---|---|
| 国内金价 | 已实现 | 新浪上金所转发，失败回退上金所官方日行情 | 无统一模型、跨源校验、跳变检测 |
| 国际黄金 | 已实现 | AllTick，失败回退新浪 | 产品语义不够严格，缺 FX 换算 |
| 国际白银 | 已实现 | AllTick | 缺稳定 fallback |
| 品牌/银行/回收价 | 部分实现 | 抓取汇率表页面文本 | 单一第三方页面，页面结构变化风险高 |
| 市场检索 | 已实现 | 百度、搜狗、必应，二次抓正文和日期过滤 | 缺抓取契约测试、缓存和来源质量评分 |
| 舆情分析 | 部分实现 | Qwen 基于证据归纳 | 模型输出没有结构化 schema |
| 量化分析 | 已实现 | AllTick/Yahoo/Stooq，多源历史数据 | 指标有限，不是策略回测 |
| 账本 | 部分实现 | 移动平均成本、持仓、已实现/浮动盈亏 | 无 FIFO、批次、真实费用拆分和 Decimal |
| 截图记账 | 已实现 | Qwen VL，多笔草稿、确认后批量提交 | OCR 契约无测试，图片引用未持久化 |
| 价格提醒 | 部分实现 | 高于/低于目标价 | 无策略型条件、无失败状态和通知审计 |
| 定时汇报 | 已实现 | 早/午/晚三个时段 | 进程内轮询，无分布式锁和任务执行表 |
| Word 报告 | 部分实现 | Markdown 转 Word、文件上传 | 质检函数未接入实际生成链路 |
| 用户记忆 | 部分实现 | 偏好 JSON、摘要、最近交互 | 无正式投资档案 schema、规则匹配脆弱 |
| 审计日志 | 未实现 | 无 | 写操作不可追溯 |
| 数据备份 | 未实现 | 无 | SQLite 无自动备份/恢复 |
| 权限控制 | 未实现 | 依赖飞书 user ID | 无角色、租户或管理员边界 |
| 统一意图路由 | 部分实现 | 手工正则、直达路由、LLM 工具 | 路由分散在多个模块 |
| 回测 | 未实现 | 只有静态技术指标 | 无交易模拟和回测指标 |
| 实物黄金 | 未实现 | 仅能在账本 note 中记录 | 无独立档案、证书和估值 |

## 5. SQLite 数据库结构审计

数据库路径由 `GOLD_AGENT_DB` 控制，默认是工作目录下的 `gold_agent.db`。当前仓库中的该文件为 0 字节，实际 schema 由启动时动态创建。

### 5.1 `alerts`

| 字段 | 类型 | 约束/用途 |
|---|---|---|
| id | INTEGER | 主键，自增 |
| user_id | TEXT | 飞书用户标识 |
| condition_type | TEXT | `above` / `below` |
| target_price | REAL | 目标价格 |
| gold_type | TEXT | `sge_spot` / `international` |
| is_triggered | INTEGER | 是否已触发 |
| created_at | INTEGER | Unix 时间 |

评价：

- 唯一具有显式列迁移逻辑的表。
- 缺少 `updated_at`、状态、最近检查时间、失败原因、触发价格和通知结果。
- `REAL` 不适合精确金额。
- 没有索引覆盖 `is_triggered` 查询。

### 5.2 `gold_transactions`

| 字段 | 类型 | 约束/用途 |
|---|---|---|
| id | INTEGER | 主键，自增 |
| user_id | TEXT NOT NULL | 用户 |
| side | TEXT NOT NULL | buy/sell |
| quantity_grams | REAL NOT NULL | 克重 |
| unit_price_cny | REAL NOT NULL | 元/克 |
| fee_cny | REAL | 费用 |
| note | TEXT | 备注 |
| trade_time | INTEGER NOT NULL | 交易时间 |
| created_at | INTEGER NOT NULL | 创建时间 |

索引：`idx_gold_transactions_user_time(user_id, trade_time, id)`。

评价：

- 能支持基础移动平均账本。
- 无数据库 `CHECK` 约束，数据完整性依赖 Python。
- 缺少账户类型、渠道、费用拆分、纯度、批次和图片引用。
- 金额、克重使用 `REAL`，不符合财务精度要求。
- 没有修改时间、软删除、版本号和审计关系。
- 同一批截图交易使用同一个秒级 `trade_time`，只能依赖 `id` 排序。

### 5.3 `report_subscriptions`

保存用户是否启用定时汇报、三个固定时段、时区及各时段最后发送日期。

评价：

- 能满足单实例早/午/晚汇报。
- schema 固定为三个时段，不适合任意 cron/频率。
- 没有任务执行日志、失败重试、并发租约。

### 5.4 `processed_messages`

以 `message_id` 为主键做飞书消息幂等，启动初始化时删除 7 天前记录。

评价：

- 基础去重有效。
- 消息在提交线程池之前已标记；后续处理失败时，同一消息重投会被直接忽略。
- 没有 `processing/succeeded/failed` 状态和重试次数。

### 5.5 `pending_ledger_entries`

每个用户只有一条待确认 JSON 草稿，三天过期。

评价：

- 支持截图多笔交易的统一确认。
- 新图片会覆盖同一用户旧草稿。
- 无草稿 ID、操作类型、版本和确认令牌。

### 5.6 `user_profiles`

以 JSON 保存偏好和事实，并保存文本摘要。

评价：

- 扩展方便，适合作为早期实现。
- 无 JSON schema、字段版本、数据校验和结构化投资档案。

### 5.7 `interaction_logs`

保存用户文本、助手文本和时间，每用户最多保留最近 200 条。

评价：

- 可恢复最近上下文。
- 无会话 ID、消息 ID、来源、模型、工具调用、脱敏状态。
- 用户内容可能包含敏感交易信息，目前没有隐私分级。

### 5.8 迁移机制

当前没有 `schema_migrations` 表或版本化 migration 文件。除 `alerts` 外，其余 `CREATE TABLE IF NOT EXISTS` 无法为已有表添加新列。

阶段 1 前应先建立迁移基线，否则阶段 2 的交易字段扩展无法可靠部署。

## 6. 飞书消息处理流程审计

### 6.1 文本消息

```text
飞书事件
 -> 读取 message_id/chat_id/sender_id
 -> processed_messages 去重
 -> 解析消息 JSON
 -> 提交 ThreadPoolExecutor
 -> 检查截图待确认指令
 -> 尝试自然语言账本命令
 -> 尝试定时汇报订阅命令
 -> 尝试价格提醒命令
 -> 进入 Agent
    -> 简单金价直达路由，或
    -> DeepSeek 多轮 Function Calling
 -> 更新用户记忆
 -> 回复文本
 -> 如线程保存了 Word 路径，则上传并发送文件
```

优点：

- 接收线程不执行长耗时任务。
- 有消息去重。
- 简单金价查询绕过 LLM，延迟和误路由更低。
- LLM 工具循环有最大轮数。

风险：

- 命令路由分散在飞书适配器、Agent 直达路由和 LLM 工具三处。
- 自然语言账本命令在飞书工作线程中直接写库，没有确认。
- `manage_gold_ledger` 工具同样直接写库。
- 消息先标记已处理，再异步执行；工作线程失败后无法自然重试。
- 线程池无队列上限、超时和拒绝策略。
- 回复重试只覆盖 `reply_message`，主动发送和文件发送重试不一致。

### 6.2 图片消息

```text
飞书图片事件
 -> 下载 message_resource
 -> Qwen VL 识别 transactions 数组
 -> 对每笔校验方向、克重、单价/总额、置信度
 -> 保存 pending_ledger_entries
 -> 回复待确认摘要
 -> 用户回复“确认记账”
 -> 批量校验全部记录
 -> executemany + 单次 commit
 -> 清除草稿
```

优点：

- 支持一图多笔。
- 有置信度和字段完整性门槛。
- 批量写入前先整体校验，避免部分提交。
- 兼容旧的单笔模型返回结构。

风险：

- OCR schema 只靠提示词约束，没有 JSON Schema/Pydantic 校验。
- OCR 和草稿构建位于飞书适配器，无法独立复用和测试。
- 图片本身没有持久化引用，事后无法核对原始凭证。
- 允许只写入完整识别的部分记录，其余记录被拒绝；用户看到数量提示，但缺少逐笔编辑机制。
- 待确认记录按 user_id 覆盖，无法并行处理多张图。

## 7. 行情数据源审计

| 数据类型 | 主数据源 | fallback | 缓存 | 评价 |
|---|---|---|---|---|
| 上金所 Au99.99 实时 | 新浪 `gds_AU9999` 转发 | 上金所 `Dailyhq` 日行情 | 10 秒/60 秒 | 有时效标记，但未跨源校验 |
| 国际黄金 | AllTick GOLD | 新浪国际黄金 | 短缓存 | 主备存在，symbol 语义需标准化 |
| 国际白银 | AllTick SILVER | 无明确备用源 | 短缓存 | 单点依赖 |
| 品牌金价 | huilvbiao 页面 | 无 | 60 秒 | HTML 文本正则，脆弱 |
| 银行金条 | huilvbiao 页面 | 无 | 60 秒 | 不是银行官方接口 |
| 回收价 | huilvbiao 页面 | 无 | 60 秒 | 来源单一 |
| 历史行情 | AllTick | Yahoo、Stooq | 无统一持久缓存 | 有多源 fallback |

当前报价结构由 `normalize_quote` 返回：

```text
status, source, symbol, price, unit, timestamp, confidence, extra
```

与目标 `PriceQuote` 相比缺少：

- `asset`
- 独立的 `currency` 和 `unit`
- 标准化 `quote_type`
- 明确 `is_realtime`
- 一致的 `raw_payload`
- 统一 `confidence_score`
- 可解析的时区时间对象

异常检测目前只有固定合理区间：

- CNY/g：200 到 2000
- USD/oz：1000 到 10000

尚未检测：

- 时间戳过期
- 单次跳变
- 多源价差
- 单位不明
- 汇率缺失
- 数据源连续失败

## 8. 账本模块审计

### 已实现

- 买入、卖出记录。
- 防止卖出超过当前持仓。
- 移动加权平均成本。
- 买入费用计入平均成本。
- 卖出费用计入已实现盈亏。
- 以 Au99.99 估算浮动盈亏。
- 撤销最近一笔。
- 截图多笔批量原子写入。

### 计算口径

买入后平均成本：

```text
(原持仓 * 原均价 + 买入克重 * 买入单价 + 买入费用) / 新持仓
```

卖出已实现盈亏：

```text
卖出克重 * (卖出单价 - 当前移动平均成本) - 卖出费用
```

### 主要缺口

- 所有财务计算使用 `float`。
- 无 FIFO、指定批次和批次剩余数量。
- 无加工费、点差、税、配送、保管等费用拆分。
- 无账户/渠道维度。
- 无交易修改，只有物理删除最近一笔。
- 撤销不需要二次确认。
- 无审计日志。
- 自然语言写入和 LLM 工具写入未统一经过确认。
- 当前浮动盈亏统一使用 Au99.99，不适合品牌首饰、回收、ETF 等不同资产。

## 9. 报告生成模块审计

### 已实现

- Markdown 标题、段落、列表和表格转换。
- Word 页边距、标题和生成时间设置。
- 规则型质量检查。
- 质量失败时尝试重新检索和重写。
- 生成后通过飞书上传。

### 关键问题

`auto_improve_report_content` 和 `review_report_quality` 已实现，但 `create_word_report` 没有调用 `auto_improve_report_content`，也没有设置 `last_report_quality`。因此实际生成路径通常直接把传入正文写入 Word，返回文案中的“已执行自动质检”与真实执行链不一致。

其他风险：

- 文件生成在当前工作目录，缺少专用临时目录。
- 上传成功后删除文件，失败后长期保留，没有清理策略。
- 文件名只有秒级时间戳，并发时可能冲突。
- 报告生成和发送无审计记录。
- 投资建议风险提示主要依赖 prompt，而非最终输出强制校验。

## 10. 用户记忆模块审计

### 已实现

- 默认偏好。
- 显式偏好持久化。
- 基于关键词的确定性偏好学习。
- 每用户保留最近 200 条交互。
- Agent 启动上下文恢复最近 12 组问答。
- 运行时每用户最多约 30 条消息。

### 主要缺口

- 关键词规则和系统提示词耦合，容易误判。
- 偏好 JSON 无 schema 和迁移版本。
- 没有投资目标、风险等级、目标仓位等正式档案。
- 运行时缓存没有失效和跨进程同步。
- 交互日志没有敏感数据脱敏和删除机制。
- 用户偏好写入没有审计日志。

## 11. 风险清单

### P0：进入后续阶段前必须处理

1. **财务精度风险**：金额、价格、克重使用 `float/REAL`。
2. **写入确认不一致**：文本和 LLM 记账直接写入，截图才确认。
3. **迁移风险**：没有版本化 schema migration。
4. **报告质检失效**：质检实现未接入生成主链。
5. **敏感凭证历史风险**：旧单文件曾出现硬编码密钥，相关密钥需要确认已轮换。

### P1：第一批改造中处理

1. 行情返回结构不统一，缺 raw payload 和异常检测。
2. 单一第三方页面承载品牌、银行和回收价。
3. 消息先去重后处理，失败消息不可重试。
4. 自动任务没有任务状态、锁和重试记录。
5. 账本撤销无确认、无审计。
6. 全局 SQLite 连接和游标耦合所有服务。

### P2：持续改进

1. 领域模块存在大量无关 import。
2. 中文文本存在历史编码可读性问题。
3. 测试覆盖不足。
4. README 只说明目录和启动，没有命令与运维手册。
5. 没有结构化日志、指标和健康检查。

## 12. 当前测试基线

现有测试共 6 项：

- 配置密钥无硬编码默认值。
- 相对数据库路径解析。
- 买卖、盈亏和撤销。
- 超持仓卖出保护。
- 多笔批量提交。
- 无效批次不写入。

未覆盖：

- 行情解析、fallback、过期判断。
- 搜索结果解析和日期过滤。
- 量化指标边界。
- 飞书消息路由、去重失败恢复。
- 图片 OCR schema 和草稿构建。
- 提醒触发。
- 定时汇报。
- 报告质检和 Word 生成。
- 用户记忆更新和恢复。
- 数据库迁移。

## 13. 阶段 0 验收

阶段 0 只新增审计和计划文档，不修改业务实现、不修改数据库 schema、不改变飞书命令行为。后续应等待确认后从阶段 1 开始。
