# CrossBorder Ops Radar

CrossBorder Ops Radar 是一个使用 Python、Pandas 和 Streamlit 构建的跨境电商运营数据分析 Demo。项目计划通过上传 CSV/XLSX 日汇总数据，完成数据质量检查、SKU 指标计算、规则化异常诊断，并生成中文运营报表。

当前仓库处于 Phase 1：仅建立项目结构、依赖清单、数据契约和代表性样例数据，尚未实现加载、校验、指标、诊断、报表或流水线业务逻辑。

## 项目目标

- 接收 CSV 或 XLSX 格式的电商运营数据。
- 检查字段、类型、取值和记录关系等数据质量问题。
- 按 SKU 汇总固定口径的运营指标。
- 使用透明、可配置的规则识别经营异常。
- 在页面展示结果并生成可下载的中文 Excel 运营报表。

V1 不接入任何大模型 API，不使用数据库，也不包含登录或权限系统。

## 输入数据契约

所有字段均为必填列。日期必须使用 `YYYY-MM-DD`；推荐 CSV 使用 UTF-8-SIG 编码。SKU 应作为文本提供，以保留可能存在的前导零。

| 字段 | 含义 | 约束 |
| --- | --- | --- |
| `date` | 数据日期 | `YYYY-MM-DD` |
| `marketplace` | 电商平台/站点 | 非空文本 |
| `country` | 国家或地区标识 | 非空文本 |
| `sku` | SKU 唯一标识 | 非空文本 |
| `product_name` | 商品名称 | 非空文本 |
| `impressions` | 曝光量 | 非负数 |
| `clicks` | 点击量 | 非负数 |
| `orders` | 订单量，允许包含自然订单 | 非负数 |
| `units_sold` | 销售件数 | 非负数 |
| `sales` | 销售额 | 非负数，USD |
| `ad_spend` | 广告花费 | 非负数，USD |
| `refunds` | 退款订单数，不是退款金额 | 非负数 |
| `inventory` | 当日结束时的可售库存快照 | 非负数 |

### 数据粒度与业务键

一行数据表示：

```text
date × marketplace × country × sku 的日汇总数据
```

业务唯一键为：

```text
date + marketplace + country + sku
```

同一业务键原则上只能出现一次。完全重复记录保留第一条用于后续分析，其余副本作为错误行排除；同一业务键内容不一致时视为冲突，冲突记录不应进入指标计算。

### 币种假设

V1 假定所有 `sales` 和 `ad_spend` 在上传前均已转换为 USD。因此跨 marketplace、country 和 SKU 的 GMV、AOV、CPC、CPA、ROAS 才可直接比较和汇总。

### Refunds 定义

`refunds` 表示退款订单数，不表示退款金额。退款可能来自历史订单，因此某一天的 `refunds` 可以大于当天 `orders`；这种情况记录为 Warning，但该行仍参与后续分析。

## 指标口径

| 指标 | 公式 |
| --- | --- |
| CTR | `clicks / impressions` |
| CVR | `orders / clicks` |
| AOV | `sales / orders` |
| CPC | `ad_spend / clicks` |
| CPA | `ad_spend / orders` |
| ROAS | `sales / ad_spend` |
| Refund Rate | `refunds / orders` |
| GMV | `sum(sales)` |

聚合指标必须先汇总分子和分母，再执行除法，不能对行级比例做简单平均。例如，SKU CTR 应为该 SKU 的总点击量除以总曝光量。

### 零分母规则

所有除法必须安全处理分母为 0 或缺失的情况：

- 结果定义为缺失值 `NaN`，不返回无穷大，也不强制写成 0。
- Streamlit 页面计划显示为 `—`。
- Excel 报告计划保持单元格为空。

## 数据质量严重级别

| 规则 | 严重级别 | 是否排除该行 |
| --- | --- | --- |
| 必填列缺失、文件为空或无法解析 | Fatal | 停止分析 |
| 必填值为空 | Error | 是 |
| SKU 为空 | Error | 是 |
| 日期不是 `YYYY-MM-DD` 或无法解析 | Error | 是 |
| 数值字段无法转换 | Error | 是 |
| 数值为负 | Error | 是 |
| `clicks > impressions` | Error | 是 |
| `orders > clicks` | Warning | 否 |
| `refunds > orders` | Warning | 否 |
| 重复或冲突业务键 | Error | 是，按重复策略处理 |

`orders > clicks` 不直接判定为错误，因为订单可能包含自然订单，和广告点击的归因口径不完全一致。

## Demo Default Thresholds

以下阈值仅用于演示规则诊断能力，统称为 **Demo Default Thresholds**。它们不是行业标准，也不代表任何平台的官方建议；后续应根据品类、市场、利润结构和投放目标调整。

| 诊断场景 | Demo 默认规则 |
| --- | --- |
| 高曝光低点击 | `impressions >= 1000` 且 `CTR < 1%` |
| 低转化 | `clicks >= 50` 且 `CVR < 2%` |
| 有点击无订单 | `clicks >= 20` 且 `orders = 0` |
| 广告花费无订单 | `ad_spend > 0`、`clicks >= 20` 且 `orders = 0` |
| 低 ROAS | `ad_spend > 0` 且 `ROAS < 1` |
| 高退款率 | `orders >= 10` 且 `Refund Rate > 10%` |
| 缺货 | 最新有效日期的 `inventory = 0` 且分析期内 `units_sold > 0` |

最小曝光、点击和订单样本量用于减少小样本误报。同一 SKU 可以同时命中多条诊断规则。

## 样例数据

`data/sample_ecommerce_data.csv` 是为后续开发和测试准备的混合样例，故意同时包含正常经营数据、经营表现异常和数据质量错误。它不应被当作全量数据模板或行业基准。

## 计划中的项目结构

```text
CrossBorder Ops Radar/
├── app.py
├── README.md
├── requirements.txt
├── data/
│   └── sample_ecommerce_data.csv
├── src/
│   ├── __init__.py
│   ├── config.py
│   ├── loader.py
│   ├── validator.py
│   ├── metrics.py
│   ├── diagnostics.py
│   ├── report.py
│   └── pipeline.py
└── tests/
```
