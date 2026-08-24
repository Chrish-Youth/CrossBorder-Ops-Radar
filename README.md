# CrossBorder Ops Radar

CrossBorder Ops Radar 是一个使用 Python、Pandas 和 Streamlit 构建的跨境电商运营数据分析 Demo。项目计划接收 CSV/XLSX 日汇总数据，完成数据质量检查、SKU 指标计算、规则化异常诊断，并生成中文运营报表。

V1 不接入任何大模型 API，不使用数据库，也不包含登录或权限系统。

## 当前阶段

- Phase 1 completed：项目结构、依赖、数据契约和代表性样例数据已建立。
- Phase 2 completed：CSV/XLSX Loader、Validator、Clean DataFrame 和 Validation Report 已实现。
- Phase 2.2 completed：CSV 结构完整性、Count 精度、Business Key Conflict、XLSX 异常边界和宽日期范围已加固，并已补齐回归测试。
- Phase 3 not started：指标计算、异常诊断、报表、Pipeline 和 Streamlit 页面业务逻辑尚未实现。

当前已实现的数据流为：

```text
Raw File
   ↓
Loader
   ↓
Raw DataFrame
   ↓
Validator
   ↓
Clean DataFrame + Validation Report
```

## 项目目标

- 接收 CSV 或 XLSX 格式的电商运营数据。
- 检查字段、类型、取值、记录关系和重复冲突等数据质量问题。
- 按 SKU 汇总固定口径的运营指标。
- 使用透明、可配置的规则识别经营异常。
- 在页面展示结果并生成可下载的中文 Excel 运营报表。

后面三项属于 Phase 3 及之后的计划，当前尚未实现。

## 输入数据契约

下表中的 13 个字段均为必填列；允许保留额外列。SKU 应作为文本提供，以保留可能存在的前导零。

| 字段 | 含义 | 约束 |
| --- | --- | --- |
| `date` | 数据日期 | 严格 `YYYY-MM-DD`，且必须是合法 Gregorian calendar date |
| `marketplace` | 电商平台/站点 | 非空文本 |
| `country` | 国家或地区标识 | 非空文本 |
| `sku` | SKU 唯一标识 | 非空文本 |
| `product_name` | 商品名称 | 非空文本 |
| `impressions` | 曝光量 | Count Field |
| `clicks` | 点击量 | Count Field |
| `orders` | 订单量，允许包含自然订单 | Count Field |
| `units_sold` | 销售件数 | Count Field |
| `sales` | 销售额 | 非负数，USD |
| `ad_spend` | 广告花费 | 非负数，USD |
| `refunds` | 退款订单数，不是退款金额 | Count Field |
| `inventory` | 当日结束时的可售库存快照 | Count Field |

### Count Fields

`impressions`、`clicks`、`orders`、`units_sold`、`refunds` 和 `inventory` 必须是 Pandas `Int64` 可表示的非负整数：

```text
0 <= value <= 9223372036854775807
```

Validator 从原始值直接做精确整数校验，不经 `Float64` 中转。超出上限的值产生 `INTEGER_OUT_OF_RANGE`，不会溢出、变成负数或被静默改值。`sales` 和 `ad_spend` 是独立的 Money Fields，继续使用 `Float64` 路径。

### Date

- 文本日期必须严格匹配 `YYYY-MM-DD`，并且是合法日历日期。
- 首尾空格不会被自动移除；例如 `" 2026-08-24"` 和 `"2026-08-24 "` 均不合法。
- 纯空白 token 仍视为必填值缺失。
- Clean DataFrame 使用 Python `datetime.date` 表示日期，Series dtype 为 `object`，不依赖 Pandas `datetime64[ns]` 的日期范围。
- 因此 `1600-01-01`、`2262-04-12` 和 `9999-12-31` 都可作为合法日期处理。

### CSV 输入行为

- 推荐使用 UTF-8-SIG；Loader 也支持 GB18030 回退解码。
- 在构造最终 DataFrame 前，Loader 使用标准 CSV 语义检查每条逻辑 record 的字段数量。每条数据 record 的字段数必须与 header 完全一致。
- quoted comma、quoted field、quoted newline 和 escaped quote 按 CSV 规则解析，不使用简单的逗号切分。
- 字段过多、字段缺失或引号结构损坏时，整个文件被拒绝并产生 `DataLoadError(code="MALFORMED_CSV")`；不会截断字段、吞掉字段或把额外字段变成 index。
- 只有 header、没有数据 record 的 CSV 视为空文件。

### XLSX 输入行为

- 多 Sheet 工作簿按工作簿顺序读取第一个包含数据行的非空 Sheet，不合并多个 Sheet；所有 Sheet 都没有数据行时视为空文件。
- XLSX 原生日期单元格由 openpyxl/Pandas 读取为日期或日期时间对象。V1 接受日期值以及时间部分为 `00:00:00` 的日期时间值，并规范化为 Python `datetime.date`；带非零时间或时区的值不符合日粒度契约。
- 文本日期仍必须严格使用 `YYYY-MM-DD`。系统不会根据单元格视觉格式猜测日期。
- XML、ZIP 内部结构、openpyxl 或 Pandas 的底层读取失败统一包装为 `DataLoadError(code="FILE_READ_ERROR")`，不向调用方泄漏第三方异常。

### 数据粒度与业务键

一行数据表示：

```text
date × marketplace × country × sku 的日汇总数据
```

业务唯一键固定为：

```text
date + marketplace + country + sku
```

重复与冲突遵循以下规则：

- Exact Duplicate：所有输入字段的原始内容完全相同。记录 `EXACT_DUPLICATE` Warning，保留首次出现的代表记录并删除后续副本。即使记录自身已有其他 Error，重复事实仍会被识别；“保留首次出现”不覆盖该记录本身的其他排除规则。
- Business Key Conflict：可可靠构建的 Business Key 相同，但存在内容不同的记录。不同内容的代表记录产生 `BUSINESS_KEY_CONFLICT` Error，该 Business Key 的所有记录全部排除，不自动求和、平均、修复或择一保留。后续 Exact Duplicate 副本按去重规则排除。
- 非 Business Key 字段上的其他 Error 不会阻止 Conflict 检测。Business Key 本身缺失或日期无效时，不构造伪 Key，也不产生无意义的二次 Conflict。

### 币种假设

V1 假定上传前已将所有 `sales` 和 `ad_spend` 转换为 USD。因此跨 marketplace、country 和 SKU 的 GMV、AOV、CPC、CPA、ROAS 才可直接比较和汇总。

### Refunds 定义

`refunds` 表示退款订单数，不表示退款金额。退款可能来自历史订单，因此某一天的 `refunds` 可以大于当天 `orders`；这种情况记录为 Warning，但该行仍保留。

## 数据质量规则

文件读取错误由 Loader 直接返回 `DataLoadError`。成功形成 Raw DataFrame 后，Validator 按以下严重级别处理：

| 规则 | 严重级别 / Code | 是否排除 |
| --- | --- | --- |
| 必填列缺失 | Fatal / `MISSING_REQUIRED_COLUMN` | 整个 DataFrame 无有效行 |
| 必填值为空 | Error / `MISSING_REQUIRED_VALUE` | 是 |
| 日期格式或日历日期非法 | Error / `INVALID_DATE_FORMAT` | 是 |
| Count 或 Money 无法转换 | Error / `INVALID_NUMERIC_VALUE` | 是 |
| Count 超出 Int64 上限 | Error / `INTEGER_OUT_OF_RANGE` | 是 |
| Count 或 Money 为负 | Error / `NEGATIVE_VALUE` | 是 |
| `clicks > impressions` | Error / `CLICKS_GT_IMPRESSIONS` | 是 |
| `orders > clicks` | Warning / `ORDERS_GT_CLICKS` | 否 |
| `refunds > orders` | Warning / `REFUNDS_GT_ORDERS` | 否 |
| Exact Duplicate | Warning / `EXACT_DUPLICATE` | 后续副本去重 |
| Business Key Conflict | Error / `BUSINESS_KEY_CONFLICT` | 冲突 Key 的全部记录排除 |

`orders > clicks` 不直接判定为错误，因为订单可能包含自然订单，与广告点击的归因口径不完全一致。

### Validation Report 契约

- `total_rows = valid_rows + excluded_rows` 始终成立。
- 发生 Fatal（例如缺少必填列）时：`valid_rows = 0`，`excluded_rows = total_rows`，Clean DataFrame 为空。
- `warning_rows` 是至少包含一个 Warning 的不同逻辑数据行数；即使该行同时有 Error 或因 Exact Duplicate 被删除，仍计入 `warning_rows`。
- 同一行可以同时产生多个 Issue，例如 `INVALID_NUMERIC_VALUE + BUSINESS_KEY_CONFLICT`，或 `CLICKS_GT_IMPRESSIONS + EXACT_DUPLICATE`。因此 Issue 数量可以大于涉及的行数。
- Validation Issue 的 `row` 表示解析后的逻辑数据行位置，并包含概念上的 header 行，所以第一条数据记录为 `row = 2`。对于包含空白物理行或 quoted multiline field 的 CSV，该编号不保证等于原始文件的物理文本行号。所有 Validation Rule 使用同一套逻辑行编号。

### Clean DataFrame 类型

| 字段类型 | Clean DataFrame 表示 |
| --- | --- |
| `date` | Python `datetime.date`，Series dtype 为 `object` |
| 六个 Count Fields | Pandas nullable `Int64` |
| `sales`、`ad_spend` | Pandas nullable `Float64` |
| 文本和额外字段 | 保留为对象/文本值 |

## 指标口径（Phase 3 计划）

以下口径已冻结，但尚未实现业务计算：

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

## Demo Default Thresholds（Phase 3 计划）

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

`data/sample_ecommerce_data.csv` 是为开发和测试准备的混合样例，包含正常经营数据、经营表现异常、Warning、Error、Exact Duplicate 和 Business Key Conflict。它不应被当作全量数据模板或行业基准。

## 当前项目结构

```text
CrossBorder Ops Radar/
├── app.py                       # Phase 3 前占位
├── README.md
├── requirements.txt
├── data/
│   └── sample_ecommerce_data.csv
├── src/
│   ├── __init__.py
│   ├── config.py                # V1 数据契约常量
│   ├── loader.py                # CSV/XLSX 加载与稳定读取错误
│   ├── validator.py             # 数据校验、清洗结果与结构化报告
│   ├── metrics.py               # Phase 3 前占位
│   ├── diagnostics.py           # Phase 3 前占位
│   ├── report.py                # Phase 3 前占位
│   └── pipeline.py              # Phase 3 前占位
└── tests/
    ├── test_loader.py
    └── test_validator.py
```

运行测试：

```bash
pytest -q
```
