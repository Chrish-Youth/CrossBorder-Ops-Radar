# CrossBorder Ops Radar

CrossBorder Ops Radar 是一个使用 Python、Pandas 和 Streamlit 构建的跨境电商运营数据分析 Demo。项目计划接收 CSV/XLSX 日汇总数据，完成数据质量检查、SKU 指标计算、规则化异常诊断，并生成中文运营报表。

V1 不接入任何大模型 API，不使用数据库，也不包含登录或权限系统。

## 当前阶段

- Phase 1 completed：项目结构、依赖、数据契约和代表性样例数据已建立。
- Phase 2 completed：CSV/XLSX Loader、Validator、Clean DataFrame 和 Validation Report 已实现。
- Phase 2.2 completed：CSV 结构完整性、Count 精度、Business Key Conflict、XLSX 异常边界和宽日期范围已加固，并已补齐回归测试。
- Phase 3 Metrics Engine completed：Base Measures、Inventory Snapshot 聚合和八项固定指标已实现。
- Phase 3.1 completed：CSV NUL、Loader/Metrics 异常边界、容器型 Extra Column 和关键指标回归测试已加固。
- Phase 4 Diagnostics Engine completed：Demo 默认阈值、最小样本门槛和七条确定性经营诊断规则已实现。
- Phase 5 Pipeline Orchestration completed：Loader、Validator、Metrics 和 Diagnostics 已通过统一入口顺序串联。
- Phase 5.1 Pipeline Contract Hardening completed：阶段返回类型、filename 优先级、file-like 指针语义和 Pipeline 空结果契约已加固。
- Phase 6 Report & Excel Export completed：独立 ReportData、固定四 Sheet 的 Excel bytes 导出和展示格式契约已实现。
- Phase 6.1 Report Integrity Hardening completed：Excel 文本、日期列、快照一致性、Evidence 复杂度、行数和整数精度边界已加固。
- Phase 7 Streamlit Application Layer completed：上传、分析粒度、显式运行、结果展示、结构化错误和 Excel 下载闭环已实现。
- Phase 7.1 Application Reliability Hardening completed：下载文件名 UTF-8 长度、Unexpected Exception 服务端日志和 Session State 回归契约已加固。
- Phase 8 not started：LLM、AI Insight、Root Cause 与自动运营建议均未实现。

当前已实现的数据流为：

```text
User Upload
   ↓
Streamlit Application
   ↓
Raw File Bytes + Filename + Analysis Level
   ↓
Loader
   ↓
Raw DataFrame
   ↓
Validator
   ↓
Clean DataFrame + Validation Report
   ↓
Metrics Engine
   ↓
Aggregated Metrics DataFrame
   ↓
Diagnostics Engine
   ↓
Structured Diagnostics DataFrame
   ↓
Report Model
   ↓
Excel Workbook Bytes
```

## 项目目标

- 接收 CSV 或 XLSX 格式的电商运营数据。
- 检查字段、类型、取值、记录关系和重复冲突等数据质量问题。
- 按 SKU 汇总固定口径的运营指标。
- 使用透明、可配置的规则识别经营异常。
- 在页面展示结果并生成可下载的中文 Excel 运营报表。

SKU 指标聚合已在 Phase 3 实现，确定性规则诊断已在 Phase 4 实现，统一业务入口已在 Phase 5 实现，Report Model 与 Excel 导出已在 Phase 6 实现，并已在 Phase 6.1 完成完整性加固；Phase 7 已提供可直接使用的 Streamlit 单页应用。Phase 8 尚未开始。

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
- CSV 原始内容只要包含 NUL byte（`\x00`），即视为 malformed input，并通过 `DataLoadError(code="MALFORMED_CSV")` 拒绝；不会删除、替换、截断或静默修复。
- 只有 header、没有数据 record 的 CSV 视为空文件。

### XLSX 输入行为

- 多 Sheet 工作簿按工作簿顺序读取第一个包含数据行的非空 Sheet，不合并多个 Sheet；所有 Sheet 都没有数据行时视为空文件。
- XLSX 原生日期单元格由 openpyxl/Pandas 读取为日期或日期时间对象。V1 接受日期值以及时间部分为 `00:00:00` 的日期时间值，并规范化为 Python `datetime.date`；带非零时间或时区的值不符合日粒度契约。
- 文本日期仍必须严格使用 `YYYY-MM-DD`。系统不会根据单元格视觉格式猜测日期。
- XML、ZIP 内部结构、openpyxl 或 Pandas 的底层读取失败统一包装为 `DataLoadError(code="FILE_READ_ERROR")`，不向调用方泄漏第三方异常。

### Loader Error Boundary

Loader 公共接口的文件读取和解析失败统一暴露为 `DataLoadError`，不会向上泄漏普通底层 `Exception`；原异常通过 exception chaining 保留。`KeyboardInterrupt`、`SystemExit` 等 `BaseException` 不会被误吞。V1 Loader 使用以下稳定 Code：

| Code | 含义 |
| --- | --- |
| `UNSUPPORTED_FILE_TYPE` | 文件扩展名不受支持 |
| `EMPTY_FILE` | 文件或工作簿没有可读取的数据行 |
| `MALFORMED_CSV` | CSV record 结构、引号或 NUL byte 不合法 |
| `FILE_READ_ERROR` | 文件读取、编码或底层解析失败 |

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
- Extra Column 仍参与 Exact Duplicate 的“所有输入字段完全相同”比较。dict、list、set 等容器值不要求可哈希：相同容器内容可构成 Exact Duplicate；内容不同则不是 Exact Duplicate，若 Business Key 相同仍按 Business Key Conflict 处理。

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

## Metrics Engine

Metrics Engine 只接受 Phase 2.2 Validator 输出的 Clean DataFrame，不重新执行日期清洗、数值转换、Error 过滤、去重或 Business Key 校验。

公共接口：

```python
calculate_metrics(dataframe, group_by=None) -> pandas.DataFrame
```

- `group_by=None`、`[]` 或 `()` 表示 Overall；非空输入返回一行整体指标。
- 也可以传入单个字符串或有序字符串序列。
- 正式分组维度仅限 `date`、`marketplace`、`country`、`sku`。
- `product_name` 和其他 Extra Columns 作为附加输入列时会被忽略；若显式传入 `group_by`，则作为非法维度处理。
- 未知维度、重复维度或非字符串维度产生 `INVALID_GROUP_BY`。
- 缺少计算必需字段产生 `MISSING_METRIC_INPUT_COLUMN`。

### 八项固定指标

| 指标 | 输出列 | 公式 | Numerator | Denominator | 聚合分母为 0 |
| --- | --- | --- | --- | --- | --- |
| CTR | `ctr` | `clicks / impressions` | `clicks` | `impressions` | `NaN` |
| CVR | `cvr` | `orders / clicks` | `orders` | `clicks` | `NaN` |
| AOV | `aov` | `sales / orders` | `sales` | `orders` | `NaN` |
| CPC | `cpc` | `ad_spend / clicks` | `ad_spend` | `clicks` | `NaN` |
| CPA | `cpa` | `ad_spend / orders` | `ad_spend` | `orders` | `NaN` |
| ROAS | `roas` | `sales / ad_spend` | `sales` | `ad_spend` | `NaN` |
| Refund Rate | `refund_rate` | `refunds / orders` | `refunds` | `orders` | `NaN` |
| GMV | `gmv` | `sum(sales)` | `sales` | — | 不适用 |

所有 Ratio 必须先聚合 Numerator 和 Denominator，再做除法，即 **Ratio of Sums**。禁止先计算行级比例再求平均。V1 同时输出 Base Measure `sales` 和指标 `gmv`；两者数值相同，这是为了保留样本基础值和指标语义别名。

Derived Metric 的聚合分母为 `0` 时返回 `NaN`，无论聚合 Numerator 是 `0` 还是正数。Required Source Value 的缺失由 Validator 在 Phase 2 排除，不会进入正常 Metrics Engine 输入；这与零分母指标结果是两个不同概念。

### 输出 Schema

输出列顺序固定为：

```text
调用者给定的 Group Dimensions
→ impressions, clicks, orders, units_sold
→ sales, ad_spend, refunds, inventory
→ ctr, cvr, aov, cpc, cpa, roas, refund_rate, gmv
```

| 输出类型 | dtype |
| --- | --- |
| `date` | `object`，元素为 Python `datetime.date` |
| 文本维度 | 继承 Clean DataFrame 的文本/object dtype |
| 六个 Count Base Measures | Pandas nullable `Int64` |
| `sales`、`ad_spend`、`gmv` | Pandas nullable `Float64` |
| 七个 Ratio | NumPy `float64`，零分母使用真正的 `NaN` |

相同输入和相同 `group_by` 会得到稳定的列顺序、按分组维度升序排列的行顺序、`RangeIndex`、值和 dtype。Metrics Engine 不原地修改输入 DataFrame。具有正确 Schema 的空输入返回零行但列和 dtype 稳定的 Metrics DataFrame，不制造虚假的 Overall 全零行。

### Base Measures 与 Inventory Snapshot

以下期间流量字段始终在完整输入范围内求和：

```text
impressions, clicks, orders, units_sold, sales, ad_spend, refunds
```

`inventory` 是日末库存快照，不是期间流量：

- 当 `group_by` 包含 `date` 时，只对同一目标日期组内实际存在的 SKU 库存横向求和，不做 forward-fill。
- 当 `group_by` 不包含 `date` 时，先为每个 `marketplace + country + sku` 库存实体选择当前输入范围内 latest date 的 inventory，再按目标维度汇总。
- Overall 同样使用各库存实体的 latest inventory 之和。不同实体的 latest date 可以不同，因此它是异步最新快照的合计，不代表统一的全局 as-of date。
- latest 选择只作用于 inventory；其他 Base Measures 仍聚合完整分析期。

### 精度、溢出与展示边界

- Count 使用 Python 精确整数完成分组求和，不经 `Float64`。`9007199254740993` 可以精确保留。
- 任一目标 Group 的 Count 聚合结果超过 `9223372036854775807` 时，整个调用产生 `COUNT_AGGREGATION_OVERFLOW`，不会回绕或返回部分结果。
- Money 继续使用 Float64；有限输入的求和如果溢出则产生 `MONEY_AGGREGATION_OVERFLOW`。
- 非零分母的极端 Ratio 如果产生非有限结果，则产生 `NON_FINITE_METRIC_RESULT`。Metrics DataFrame 不返回 `inf` 或 `-inf`。
- Metrics 不做汇率转换；继续信任 `sales` 和 `ad_spend` 已是 USD。
- `refunds` 继续表示退款订单数，而不是退款金额。
- Warning 数据正常参与计算。CVR 和 Refund Rate 可以大于 1，不做 clipping。
- Metrics 不 Round、不乘以 100，也不返回百分号、货币符号、`—` 或 `N/A`。
- 内部零分母结果保持 `NaN`；未来 UI 计划显示为 `—`，Phase 6 Excel 报告保持为空白单元格。
- 日期保持 Python `datetime.date`，不会重新转换为 `datetime64[ns]`。

Metrics 接口使用 `MetricsCalculationError` 和稳定 Code：

| Code | 含义 |
| --- | --- |
| `INVALID_METRIC_INPUT` | 输入对象不是 DataFrame |
| `MISSING_METRIC_INPUT_COLUMN` | 缺少计算必需字段 |
| `INVALID_GROUP_BY` | 分组维度或分组参数非法 |
| `INVALID_METRIC_INPUT_VALUE` | 输入不符合 Validator Clean DataFrame 接口契约 |
| `COUNT_AGGREGATION_OVERFLOW` | Count 聚合超过 Int64 上限 |
| `MONEY_AGGREGATION_OVERFLOW` | Money 聚合产生溢出或非有限值 |
| `NON_FINITE_METRIC_RESULT` | 非零分母的指标计算产生非有限值 |

`calculate_metrics()` 不重新实现 Validator，但会维护稳定的公共异常边界。手工构造的非法 DataFrame 如果在分组、latest inventory、数值转换或计算阶段触发普通 `TypeError`、`ValueError` 或 `OverflowError`，统一包装为 `INVALID_METRIC_INPUT_VALUE` 并保留 exception chaining。已经明确产生的 `MetricsCalculationError` 原样向上传递，不会被再次包装；非零分母计算得到非有限结果时继续使用独立的 `NON_FINITE_METRIC_RESULT`。

## Diagnostics Engine

Diagnostics Engine 只接受 Phase 3 `calculate_metrics()` 产生的 Metrics DataFrame，不读取原始文件、不调用 Loader/Validator、不重新聚合 Base Measures，也不重新计算 Ratio 或 Inventory Snapshot。

公共接口：

```python
diagnose_metrics(dataframe) -> pandas.DataFrame
```

一条 Diagnostic Issue 对应一行输出。Metrics 中存在的正式 Group Dimensions 按输入列顺序保留，随后固定输出：

```text
group dimensions
→ code, severity, metric, actual_value, threshold, evidence, message
```

- `actual_value` 和 `threshold` 使用 Pandas nullable `Float64`。
- `evidence` 是包含样本门槛依据的 Python dict，dtype 为 `object`。
- Group Dimension dtype 继承输入 Metrics DataFrame。
- 正常 Group 不生成 `NORMAL`、`HEALTHY` 或其他占位 Issue。
- 正确 Schema 的 Empty Metrics DataFrame 返回零行稳定 Schema。
- 输出按原 Metrics 行顺序、再按固定 Rule Order 排列，并使用稳定 `RangeIndex`。
- Diagnostics 不原地修改输入 Metrics DataFrame。

### Demo Default Thresholds 与 V1 Diagnostic Rules

以下阈值只用于演示规则诊断能力，统称为 **Demo Default Thresholds**。它们不是行业标准，也不代表 Amazon 或其他平台的官方建议。

| Code | Condition | Threshold | Minimum Sample / Eligibility | Severity |
| --- | --- | --- | --- | --- |
| `HIGH_IMPRESSIONS_LOW_CTR` | `ctr < 0.01` | CTR `1%` | `impressions >= 1000` | `Warning` |
| `LOW_CVR` | `cvr < 0.02` | CVR `2%` | `clicks >= 50` | `Warning` |
| `CLICKS_WITHOUT_ORDERS` | `orders = 0` | Orders `0` | `clicks >= 20` | `Warning` |
| `SPEND_WITHOUT_ORDERS` | `orders = 0` | Orders `0` | `clicks >= 20` 且 `ad_spend > 0` | `Warning` |
| `LOW_ROAS` | `roas < 1` | ROAS `1` | `ad_spend > 0` | `Warning` |
| `HIGH_REFUND_RATE` | `refund_rate > 0.10` | Refund Rate `10%` | `orders >= 10` | `Warning` |
| `OUT_OF_STOCK` | `inventory = 0` | Inventory `0` | `units_sold >= 1`，等价于分析期内 `units_sold > 0` | `Warning` |

V1 没有冻结多级 Severity 阈值，因此七条规则统一使用稳定的 `Warning`，不自行推断 `Critical`。固定 Rule Order 与上表顺序一致。

规则边界严格保持：

- `<` 和 `>` 不包含阈值本身；例如 `CTR = 1%`、`CVR = 2%`、`ROAS = 1`、`Refund Rate = 10%` 均不触发相应规则。
- `>=` 的最小样本门槛包含边界；例如 `impressions = 1000`、`clicks = 50`、`orders = 10` 已满足对应 Sample Gate。
- Sample Gate 先于 Ratio Threshold 判断。样本不足时，即使 Ratio 很低也不触发。
- Ratio 为 `NaN` 时跳过对应 Ratio Rule，不将 `NaN` 填充或解释为 `0`。
- CVR 和 Refund Rate 可以大于 `1`，不裁剪、不修正，也不重新定义为数据质量 Error。Refund Rate 达到规则条件时仍可产生经营诊断信号。
- 同一 Group 可以独立触发多条 Issue；规则之间没有未声明的互斥或 precedence。
- `OUT_OF_STOCK` 直接使用 Metrics 输出的 latest inventory snapshot，不返回原始数据重新计算。

诊断结果只是基于 Demo 默认阈值生成的确定性经营信号，不代表行业标准，也不能直接证明问题根因。Phase 4 只输出事实化 Observation，不生成 Root Cause、运营建议或任何生成式 AI 内容。

### Diagnostics Error Boundary

Diagnostics 使用 `DiagnosticsError` 和以下稳定 Code：

| Code | 含义 |
| --- | --- |
| `INVALID_DIAGNOSTIC_INPUT` | 输入对象不是 Pandas DataFrame |
| `MISSING_DIAGNOSTIC_INPUT_COLUMN` | 缺少 Phase 3 Metrics 输出字段 |
| `INVALID_DIAGNOSTIC_INPUT_VALUE` | 手工输入包含不符合 Metrics DataFrame 契约的值 |

`diagnose_metrics()` 不建立第二套 Validator，但会将接口内出现的普通 `TypeError`、`ValueError` 或 `OverflowError` 包装为 `INVALID_DIAGNOSTIC_INPUT_VALUE` 并保留 exception chaining；已经明确产生的 `DiagnosticsError` 原样向上传递。

## Pipeline Orchestration

Pipeline 只负责串联已有的确定性模块，不重复实现文件读取、数据校验、指标公式、库存快照或诊断规则。公共入口为：

```python
run_pipeline(
    source,
    *,
    filename=None,
    group_by=None,
) -> PipelineResult
```

- `source` 原样传递给 Loader，支持现有 Loader 接受的 str/Path、bytes、bytearray 和 file-like object。
- 文件类型解析优先级固定为：显式 `filename` > str/Path 自身后缀 > file-like object 的 `.name`。显式 `filename` 与 source 名称冲突时，始终以显式值为准。
- bytes 或没有 `.name` 的 file-like object 必须通过 `filename` 指定 `.csv` 或 `.xlsx`；否则 Loader 返回 `UNSUPPORTED_FILE_TYPE`。
- V1 不根据内容猜测文件格式。例如 CSV bytes 配合 `filename="upload.xlsx"` 时按 XLSX 解析，失败时返回 `FILE_READ_ERROR`，不会回退为 CSV。
- `group_by` 原样传递给 `calculate_metrics()`；Pipeline 不追加维度、不改变顺序，也不自行解释 Overall。`None` 表示 Overall，合法维度仍只有 `date`、`marketplace`、`country`、`sku`。

固定执行顺序为：

```text
load_file(source, filename)
→ validate_dataframe(raw_data)
→ calculate_metrics(validation.clean_data, group_by)
→ diagnose_metrics(metrics)
→ PipelineResult
```

Diagnostics 只接收当前 Pipeline 调用产生的 Metrics DataFrame，不接收 Raw DataFrame 或 Clean DataFrame。Pipeline 不修改任何阶段输出，也不会向下层 DataFrame 增加状态列。

### Pipeline Structural Contract Validation

Pipeline 只检查三个阶段最基本的返回类型：

```text
validate_dataframe(...) → ValidationResult
calculate_metrics(...)  → pandas.DataFrame
diagnose_metrics(...)    → pandas.DataFrame
```

返回类型不符时直接抛出 `PipelineError`，不返回 PipelineResult，也不继续执行后续阶段。Pipeline 不检查 CTR、dtype、诊断 Code、Threshold、Validation 行数或其他业务内容，因此不会形成第二套 Validator、Metrics Validator 或 Diagnostics Validator。

`PipelineError` 仅表示 Pipeline 自身发现的阶段返回契约违规：

```python
PipelineError(
    code="INVALID_STAGE_RESULT",
    stage="validation" | "metrics" | "diagnostics",
    message="...",
)
```

V1 只有一个稳定 Pipeline Error Code：`INVALID_STAGE_RESULT`。`stage` 用于标识违反返回类型契约的阶段。文件损坏、非法 group_by 和 Diagnostics 输入问题继续使用其所属模块的既有异常，不转换为 PipelineError。

### PipelineResult

V1 使用冻结的 `PipelineResult` dataclass：

```python
PipelineResult(
    status: PipelineStatus,
    validation: ValidationResult,
    metrics: pandas.DataFrame | None,
    diagnostics: pandas.DataFrame | None,
)
```

- `PipelineStatus.SUCCESS`：Validation 没有 Fatal，Metrics 和 Diagnostics 均已成功完成。
- `PipelineStatus.VALIDATION_FAILED`：Validation 存在 Fatal，Pipeline 已明确 short-circuit。
- `validation` 保留 ValidationResult，因此 Clean DataFrame 和完整 Validation Report 仍可访问。
- `metrics` 和 `diagnostics` 保存下游实际返回对象，不复制或改造 DataFrame。
- PipelineResult 不额外保存 Raw DataFrame，也不重复保存 `validation.clean_data`，避免产生多个状态来源。
- `frozen=True` 只阻止 PipelineResult 属性被重新赋值，不会使内部 Pandas DataFrame 不可变；Pipeline 不为此深拷贝阶段结果，后续调用者应按约定将其视为只读输入。

`SUCCESS` 现在精确定义为：文件加载成功；Validator 返回合法 ValidationResult 且没有 Fatal；Metrics 返回 DataFrame；Diagnostics 返回 DataFrame。`SUCCESS` 不代表没有 Error rows、Warning 或 Diagnostic Issues。

### Validation Continuation 与 Short-circuit

| Validation 结果 | Pipeline 行为 | Status |
| --- | --- | --- |
| Fatal，例如缺少必填列 | 停止；`metrics=None`、`diagnostics=None` | `VALIDATION_FAILED` |
| 普通 Error | 排除错误行，其余 Clean Data 继续执行 | `SUCCESS` |
| Warning | 保留相应数据并继续执行 | `SUCCESS` |
| 无 Fatal 但所有行均被 Error 排除 | 继续生成稳定的 Empty Metrics 和 Empty Diagnostics | `SUCCESS` |

Pipeline 不会把空 Clean DataFrame 制造为虚假的 Overall 全零行。Fatal short-circuit 后不会调用 Metrics 或 Diagnostics；Metrics 失败后也不会调用 Diagnostics。

`VALIDATION_FAILED` 只用于合法 ValidationResult 中存在 Fatal 的情况，不用于 Loader failure、Metrics failure、Diagnostics failure 或 invalid stage result。

### File-like Pointer Semantics

- Seekable file-like object 在每次读取前都会执行 `seek(0)`，因此同一对象可以连续运行多次 Pipeline。
- 调用完成后指针停留在读取结束位置，不恢复调用前的位置。
- Non-seekable stream 会在读取时被消费，不保证能够用于第二次 Pipeline 调用；已消费的第二次调用可以返回 `EMPTY_FILE`。
- 对象存在 `.seek()` 但 seek 失败时，保持 Loader 契约并返回 `DataLoadError(code="FILE_READ_ERROR")`。
- Pipeline 不缓存 stream、不复制为临时文件，也不额外恢复指针。

### Pipeline Exception Boundary

Pipeline 不捕获并统一包装下层已经冻结的结构化异常：

```text
Loader Failure       → DataLoadError 原样传播
Validation Fatal     → PipelineResult(status=VALIDATION_FAILED)
Metrics Failure      → MetricsCalculationError 原样传播
Diagnostics Failure  → DiagnosticsError 原样传播
Invalid Stage Result → PipelineError(code=INVALID_STAGE_RESULT, stage=...)
```

因此 Diagnostics 失败不会返回 `SUCCESS + diagnostics=None` 这样的部分成功结果。相同 source 内容和相同 `group_by` 会产生稳定的 status、Validation、Metrics 和 Diagnostics 结果。

## Report & Excel Export

Report Layer 只接受已经完成编排的 `PipelineResult`，把现有 Validation、Metrics 和 Diagnostics 结果转换为展示模型与 Excel workbook。它不读取原始文件，不调用 Loader、Validator、Metrics 或 Diagnostics，也不重新计算业务公式、库存快照或诊断阈值。核心原则是：

```text
Compute once, present many
```

公共接口：

```python
build_report_data(
    pipeline_result: PipelineResult,
) -> ReportData

generate_excel_report(
    report_data: ReportData,
) -> bytes
```

- `build_report_data()` 对非 `PipelineResult` 或结构上不一致的 PipelineResult 抛出 `ReportError(code="INVALID_REPORT_INPUT")`。
- `generate_excel_report()` 对非 `ReportData` 或结构上非法的 ReportData 抛出 `ReportError(code="INVALID_REPORT_DATA")`。
- openpyxl 或 workbook 写入的普通异常包装为 `ReportError(code="EXCEL_EXPORT_ERROR")` 并保留 exception chaining；已有 `ReportError` 原样传播。
- Excel 完全在内存中生成并返回 `.xlsx` bytes，不在 Report API 内写入固定本地路径。

Phase 6.1 新增的稳定 Report Error Code 为：

| Code | 含义 |
| --- | --- |
| `EXCEL_CELL_TEXT_TOO_LONG` | 准备写入 Cell 的文本超过 Excel 的 32,767 字符上限 |
| `INCONSISTENT_REPORT_DATA` | Summary 可重建统计与 Detail 快照不一致 |
| `EXCEL_ROW_LIMIT_EXCEEDED` | 表格数据行超过 1,048,575 行（另含一行 Header） |

### ReportData

Phase 6 使用冻结的独立展示模型：

```python
@dataclass(frozen=True)
class ReportData:
    summary: pandas.DataFrame
    validation_issues: pandas.DataFrame
    metrics: pandas.DataFrame | None
    diagnostics: pandas.DataFrame | None
```

- `summary` 是机器可读的报告概览。
- `validation_issues` 是 ValidationReport 中 Fatal、Error、Warning 的统一表格。
- `metrics` 和 `diagnostics` 是对应 Pipeline DataFrame 的 presentation copy；`VALIDATION_FAILED` 时为 `None`。
- object dtype 中的可变对象（例如 Diagnostics `evidence` dict）也会复制。修改 ReportData 或生成 Excel 不会反向修改 PipelineResult、ValidationResult、Metrics 或 Diagnostics。

### Workbook Contract

无论 Pipeline 状态如何，Sheet 名称和顺序固定为：

```text
Summary
Validation Issues
Metrics
Diagnostics
```

不导出 Raw Data、Clean Data、Threshold/Config Sheet，不添加动态生成时间、Charts、Dashboard、条件格式、Severity 颜色、Root Cause 或运营建议。表格 Sheet 使用加粗表头、冻结首行、Auto Filter、受上限约束的列宽；长 `message` 和 `evidence` 使用自动换行。

### Report Integrity 与 Excel 边界

- Excel Cell 文本最大允许 32,767 个字符。所有字符串（包括序列化后的 Evidence JSON）均在赋值前检查；32,767 个字符完整导出，32,768 个及以上以 `EXCEL_CELL_TEXT_TOO_LONG` 明确失败，不截断、不拆分、不改写。
- Validation Issues、Metrics、Diagnostics 每张表最多 1,048,575 个数据行，另含一行 Header。行数在创建大量 Cell 前检查，超限以 `EXCEL_ROW_LIMIT_EXCEEDED` 失败，不分页、不截断。
- Evidence 最大嵌套深度固定为 20。深度 20 可以导出，深度 21 或循环引用以 `INVALID_REPORT_DATA` 明确失败，不依赖 Python `RecursionError`。普通 Evidence 继续使用 `ensure_ascii=False`、`sort_keys=True` 的确定性 JSON。
- 任何准备写入 Excel 的字符串都强制保持 text 类型。以 `=`、`+`、`-` 或 `@` 开头的 SKU、message 等业务文本保持原值且不会成为 Excel 公式；Report 不添加前导单引号。
- `NaN`、`pd.NA` 和 `None` 写为空白 Cell；有效数值 `0` 保持 numeric zero。

`SUCCESS` 和 `VALIDATION_FAILED` 的含义保持 Pipeline 契约：

| Pipeline 结果 | Metrics Sheet | Diagnostics Sheet |
| --- | --- | --- |
| `SUCCESS` 且有数据 | 保留原 Metrics 列、行和值 | 保留原 Diagnostics 列、行和值 |
| `SUCCESS` 但所有行被排除 | 稳定 Schema、Header、0 数据行 | 稳定 Schema、Header、0 数据行 |
| `SUCCESS` 且无 Diagnostic Issue | 正常 Metrics 表 | 稳定 Schema、Header、0 数据行 |
| `VALIDATION_FAILED` | `Not generated because validation failed.` | `Not generated because validation failed.` |

### Summary Schema

Summary 固定使用：

```text
Section, Item, Value
```

基础行固定包含 Pipeline Status、Raw Rows、Valid Rows、Excluded Rows、Warning Rows、Fatal Issues、Error Issues、Warning Issues、Metrics Groups 和 Diagnostic Issues。随后按 ValidationReport 与 Diagnostics 的稳定出现顺序追加实际存在的 Validation Code Counts 和 Diagnostic Code Counts；不存在的 Code 不生成零值行。计数保持 Excel numeric integer，状态和 Code 保持 text，不添加时间戳。

`generate_excel_report()` 在导出前重新统计可由当前 Detail 明确定义的快照：Fatal/Error/Warning Issues、Metrics Groups、Diagnostic Issues、Validation Code Counts 和 Diagnostic Code Counts，并按冻结顺序与 Summary 比较。任何漂移均以 `INCONSISTENT_REPORT_DATA` 失败。该检查不重新计算 CTR、CVR、ROAS、GMV、Inventory 或 Diagnostic Rules，也不重建 Raw Rows、Valid Rows、Excluded Rows 和 Warning Rows；后四项继续保留构建 ReportData 时的 Snapshot 语义，尤其不会把 Warning Rows 误定义为 Warning Issues 数量。

### Validation Issues

Validation Issues 固定列顺序为：

```text
level, code, row, field, message
```

Issue 顺序直接沿用 `ValidationReport.issues`，即 Validator 已冻结的 Fatal、Error、Warning 汇总顺序；Report 不重新排序。没有 Issue 时保留 Header 和零数据行，不制造 `NO ISSUES` 记录。缺失的 `row` 或 `field` 在 Excel 中显示为空白。

### Metrics Excel Formatting

Metrics Sheet 直接保留 Phase 3 DataFrame 的列顺序、行顺序和真实数值。Report 不提前 Round、不乘以 100，也不把数值转换成带符号字符串：

| 类别 | 字段 | Excel number format |
| --- | --- | --- |
| Count | `impressions`, `clicks`, `orders`, `units_sold`, `refunds`, `inventory` | `#,##0` |
| USD | `sales`, `ad_spend`, `gmv`, `aov`, `cpc`, `cpa` | `$#,##0.00` |
| Percentage | `ctr`, `cvr`, `refund_rate` | `0.00%` |
| Multiple | `roas` | `0.00x` |
| Date | `date` | `yyyy-mm-dd` |

`NaN`、`pd.NA` 和 `None` 写为空白 Cell；有效业务值 `0` 仍写为 numeric zero，并按对应格式显示为 `0`、`$0.00` 或 `0.00%`。

Excel numeric cell 不能精确保存所有整数。对任意整数字段按值保护：`abs(integer) <= 9007199254740991`（即 `2**53 - 1`）时保持 numeric；超出该范围时使用完整十进制 text 回退。该规则不限于 Count 字段，可避免例如 `9007199254740993` 被静默改成 `9007199254740992`。Pipeline 与 ReportData 中的值保持不变，只有 Excel presentation 使用该无损回退；重新打开 Workbook 后回退值仍为字符串，不会被保存成科学计数或发生舍入。

日期导出采用整列一致模式。如果同一 `date` 列的全部日期均在 `1900-01-01` 至 `9999-12-31`，整列写为 native Excel Date，并使用 `yyyy-mm-dd` number format；只要该列包含一个需要 fallback 的日期（例如 `1600-01-01`），整列全部写为 ISO `YYYY-MM-DD` text，包括原本安全的 `2026-08-27` 与 `9999-12-31`。同一日期列不会混用 native date 与 text。该选择只发生在 Excel presentation，不修改 Pipeline 或 ReportData 中的 Python `datetime.date`。

### Diagnostics Excel Formatting

Diagnostics Sheet 保留 Phase 4 的 Group Dimensions 与固定 Issue Columns：

```text
group dimensions
→ code, severity, metric, actual_value, threshold, evidence, message
```

`actual_value` 和 `threshold` 根据同一行的 `metric` 设置格式：CTR/CVR/Refund Rate 使用百分比，ROAS 使用倍数，Orders/Inventory 等 Count 使用整数。真实 numeric value 不变。`evidence` 使用 `json.dumps(..., ensure_ascii=False, sort_keys=True)` 的确定性 JSON 表示，并把其中的 Python date、NumPy/Pandas scalar、`pd.NA` 和 NaN 安全转换为 JSON date/text、原生 scalar 或 `null`；原 dict 不被修改。`None` evidence 写为空白。`message` 直接保留 Phase 4 原文，不改写、不追加 Root Cause 或运营建议。

## Streamlit Application

安装依赖后，在项目根目录启动：

```bash
streamlit run app.py
```

Phase 7 是单页面应用，固定完成以下流程：

```text
Upload CSV/XLSX
→ Select Analysis Level
→ Run Analysis
→ Validation / Metrics / Diagnostic Signals
→ Download Excel Report
```

应用只负责上传、参数映射、调用、展示、Session State 和错误呈现。上传内容以原始 bytes 和显式 filename 传给 `run_pipeline()`；UI 不使用 Pandas 自行读取上传文件、不猜测文件格式，也不重新实现 Validation、Metrics、Inventory、Diagnostic Threshold 或 Excel 数据构建。

### Analysis Level

默认分析粒度为 `SKU`。V1 使用固定选项，不提供自由维度编辑：

| UI Option | `group_by` |
| --- | --- |
| `Overall` | `None` |
| `SKU` | `["sku"]` |
| `Marketplace` | `["marketplace"]` |
| `Country` | `["country"]` |
| `Marketplace + Country` | `["marketplace", "country"]` |
| `Marketplace + Country + SKU` | `["marketplace", "country", "sku"]` |
| `Date + Marketplace + Country + SKU` | `["date", "marketplace", "country", "sku"]` |

上传文件后不会自动执行分析。只有点击 `Run Analysis` 才调用 Pipeline、构建 ReportData 并生成一次 Excel bytes。当前 session 保存 analysis signature、PipelineResult、ReportData、Excel bytes、ReportError 和安全下载文件名；signature 包含 filename、文件 bytes SHA-256 和 `group_by`。文件内容、文件名或分析粒度变化时，旧结果立即失效，必须重新点击运行。应用不使用数据库、磁盘缓存或跨 Session 历史记录。

每次新的 `Run Analysis` 在调用 Pipeline 前都会清理上一轮的 execution state，包括 PipelineResult、ReportData、Excel bytes、AnalysisError、ReportError 和下载文件名。上传内容、文件名或 Analysis Level 变化产生的 rerun 也会在结果渲染前使旧状态失效；因此旧 Metrics、Diagnostics 或 Workbook 不会短暂成为新输入的当前结果。Report 失败只隔离 Excel 下载，不清除同一轮已经成功产生的 Validation、Metrics 和 Diagnostics；下一次成功运行会清除旧错误。Download Button 始终只使用当前 analysis signature 对应的 Excel bytes。

### Validation、Metrics 与 Diagnostic Signals

- Validation 直接展示 Raw Rows、Valid Rows、Excluded Rows、Warning Rows，以及 Fatal/Error/Warning Issue 数量和完整 `level/code/row/field/message` 表格。
- `VALIDATION_FAILED` 是合法结果：页面显示 Fatal 和 Validation Issues，不显示虚假 Metrics/Diagnostics，但仍尝试生成并提供 Validation Failed Excel。
- Metrics 保持 Engine 的行列顺序，不重新计算、不排序、不修改原 DataFrame。展示副本中 Percentage 使用 `0.00%`、USD 使用 `$#,##0.00`、ROAS 使用 `0.00x`；`NaN` 显示为 `—`，有效零值分别显示为 `0.00%`、`$0.00` 或 `0.00x`。
- Diagnostic Signals 保留全部 Group Context、Code、Severity、Metric、Actual Value、Threshold、Evidence 和 Message；同一 Group 的多条 Issue 不合并。没有 Issue 时显示明确 empty state，不制造 `NORMAL` 记录。
- 页面持续声明 Diagnostic Signals 使用 Demo Default Thresholds，并非行业标准，也不代表 Root Cause 或运营建议。

### Application Error Boundary

UI 结构化区分并展示稳定 Code：

| Exception | UI heading |
| --- | --- |
| `DataLoadError` | `File could not be loaded.` |
| `PipelineError` | `Internal pipeline contract error.`，同时显示 stage |
| `MetricsCalculationError` | `Metrics calculation failed.` |
| `DiagnosticsError` | `Diagnostics failed.` |
| `ReportError` | `Excel report could not be generated.` |
| 其他 `Exception` | `Unexpected application error.`，不展示 traceback 或原始内部文本 |

Pipeline/Diagnostics 失败不会伪装为 SUCCESS。Report 生成失败不会清除已经成功产生的 Validation、Metrics 和 Diagnostics，只隐藏 Download Button 并在 Excel Report 区域显示 Report Error。`KeyboardInterrupt` 和 `SystemExit` 不在普通 Exception boundary 内。

已知的 `DataLoadError`、`PipelineError`、`MetricsCalculationError`、`DiagnosticsError` 和 `ReportError` 按既有结构化路径展示，不记录为 unexpected server failure。其他未预期的 application/report exception 使用 Python 标准 `logger.exception(...)` 在服务器侧记录 exception 与 traceback；UI 仍只显示稳定的通用 Code 和安全消息，不显示原始异常文本、traceback、文件路径或行号。应用不在日志中写入上传 bytes、完整 DataFrame 或业务数据内容，且不在模块 import 时配置全局 logging handler。

### Excel Download 与 V1 Scope

下载 MIME 固定为：

```text
application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
```

下载名使用上传文件 basename 和去扩展名后的安全 stem，例如 `amazon.data.csv` 生成 `amazon.data_crossborder_ops_radar.xlsx`；无法获得安全 stem 时使用 `crossborder_ops_radar_report.xlsx`。固定后缀为 `_crossborder_ops_radar.xlsx`，最终文件名上限固定为 180 UTF-8 bytes。超长名称只对 sanitized stem 做 deterministic UTF-8 字符边界截断，不截断固定后缀，不生成非法 UTF-8，也不添加 timestamp、UUID 或随机值。默认文件名同样满足该 byte limit。Excel bytes 直接来自现有 Report Layer，后续 Streamlit rerun 不重复生成。

Excel 仍受 Phase 6.1 契约约束：单元格文本最多 32,767 字符；表格最多 1,048,575 个数据行加 Header；包含 pre-1900 日期时整个日期列使用 ISO text fallback。达到限制时明确失败，不截断、不分页。

Phase 7 不展示 Raw/Clean Data，不提供 Threshold Editor、Charts、Dashboard Visualization、LLM、AI Insight、Root Cause、自动运营建议、账号、数据库或历史报告。

## 样例数据

`data/sample_ecommerce_data.csv` 是为开发和测试准备的混合样例，包含正常经营数据、经营表现异常、Warning、Error、Exact Duplicate 和 Business Key Conflict。它不应被当作全量数据模板或行业基准。

## 当前项目结构

```text
CrossBorder Ops Radar/
├── app.py                       # Phase 7 Streamlit 单页应用与纯展示 helper
├── README.md
├── requirements.txt
├── data/
│   └── sample_ecommerce_data.csv
├── src/
│   ├── __init__.py
│   ├── config.py                # V1 数据契约常量
│   ├── loader.py                # CSV/XLSX 加载与稳定读取错误
│   ├── validator.py             # 数据校验、清洗结果与结构化报告
│   ├── metrics.py               # Base Measures、库存快照与八项指标
│   ├── diagnostics.py           # Demo 阈值、七条规则与结构化诊断结果
│   ├── report.py                # ReportData 与固定四 Sheet 的 Excel bytes 导出
│   └── pipeline.py              # 顺序编排与结构化 PipelineResult
└── tests/
    ├── test_loader.py
    ├── test_validator.py
    ├── test_metrics.py
    ├── test_diagnostics.py
    ├── test_pipeline.py
    ├── test_report.py
    └── test_app.py
```

运行测试：

```bash
pytest -q -W error
```
