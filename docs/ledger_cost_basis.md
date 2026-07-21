# 账本真实盈亏与成本法

## 数据字段

阶段二在 `ledger_transactions_v2` 上增加：

- `account_type`、`channel`
- `spread_cny`、`processing_fee_cny`、`tax_cny`
- `delivery_fee_cny`、`storage_fee_cny`
- `purity`、`batch_id`、`certificate_no`、`image_refs_json`
- `cost_method`、`target_batch_id`
- `cost_basis_cny`、`realized_pnl_cny`

数据库启动时执行 migration 4。历史买入记录会自动获得
`legacy-<transaction_id>` 批次编号。

## 成本计算

所有克重、价格、费用和盈亏使用 `Decimal`。

- `moving_average`：按移动加权平均成本计算卖出成本，并按比例减少各批次剩余量。
- `fifo`：先消耗最早买入批次。
- `specific_batch`：只消耗 `target_batch_id` 指定批次。

买入库存成本包含成交金额和所有费用。卖出净到账金额为成交金额减去所有费用。
已实现盈亏等于净到账金额减去本次卖出的成本基础。

## 确认和原子性

新增交易仍只生成待确认草稿。用户回复 `确认记账` 后才写入。

批量交易会在写入前完整重放校验。任何一笔总持仓超卖、指定批次不足、
费用为负或字段无效，整批交易均不会写入。

## 飞书示例

- `按先进先出算一下我现在黄金赚了多少`
- `查看每一批黄金的剩余克重和盈亏`
- `如果按回收价 550 元卖出 20 克，扣除 30 元费用实际到账多少`
- `从批次 batch-2026-01 卖出 5 克`
- `记录买入 10 克建行金条，每克 500 元，加工费 20 元，纯度 999.9`

卖出估算和批次查询是只读操作，不修改账本。
