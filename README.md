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
- Phase 6 not started：报表、Excel 导出和 Streamlit 页面业务逻辑尚未实现。

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
   ↓
Metrics Engine
   ↓
Aggregated Metrics DataFrame
   ↓
Diagnostics Engine
   ↓
Structured Diagnostics DataFrame
```

## 项目目标

- 接收 CSV 或 XLSX 格式的电商运营数据。
- 检查字段、类型、取值、记录关系和重复冲突等数据质量问题。
- 按 SKU 汇总固定口径的运营指标。
- 使用透明、可配置的规则识别经营异常。
- 在页面展示结果并生成可下载的中文 Excel 运营报表。

SKU 指标聚合已在 Phase 3 实现，确定性规则诊断已在 Phase 4 实现，统一业务入口已在 Phase 5 实现；页面、报表和导出属于 Phase 6 及之后的计划，当前尚未实现。

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
- 内部零分母结果保持 `NaN`；未来 UI 计划显示为 `—`，Excel 报告计划保持为空白单元格。
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

- `source` 原样传递给 Loader，支持现有 Loader 接受的 Path、bytes、bytearray 和 file-like object。
- Path 或带 `.name` 的 file-like object 可以由 Loader 推断文件类型；无文件名的 bytes 或 file-like object 应通过 `filename` 指定 `.csv` 或 `.xlsx`。
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

### Validation Continuation 与 Short-circuit

| Validation 结果 | Pipeline 行为 | Status |
| --- | --- | --- |
| Fatal，例如缺少必填列 | 停止；`metrics=None`、`diagnostics=None` | `VALIDATION_FAILED` |
| 普通 Error | 排除错误行，其余 Clean Data 继续执行 | `SUCCESS` |
| Warning | 保留相应数据并继续执行 | `SUCCESS` |
| 无 Fatal 但所有行均被 Error 排除 | 继续生成稳定的 Empty Metrics 和 Empty Diagnostics | `SUCCESS` |

Pipeline 不会把空 Clean DataFrame 制造为虚假的 Overall 全零行。Fatal short-circuit 后不会调用 Metrics 或 Diagnostics；Metrics 失败后也不会调用 Diagnostics。

### Pipeline Exception Boundary

Pipeline 不捕获并统一包装下层已经冻结的结构化异常：

```text
Loader Failure       → DataLoadError 原样传播
Validation Fatal     → PipelineResult(status=VALIDATION_FAILED)
Metrics Failure      → MetricsCalculationError 原样传播
Diagnostics Failure  → DiagnosticsError 原样传播
```

因此 Diagnostics 失败不会返回 `SUCCESS + diagnostics=None` 这样的部分成功结果。相同 source 内容和相同 `group_by` 会产生稳定的 status、Validation、Metrics 和 Diagnostics 结果。

## 样例数据

`data/sample_ecommerce_data.csv` 是为开发和测试准备的混合样例，包含正常经营数据、经营表现异常、Warning、Error、Exact Duplicate 和 Business Key Conflict。它不应被当作全量数据模板或行业基准。

## 当前项目结构

```text
CrossBorder Ops Radar/
├── app.py                       # Phase 6 及之后占位
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
│   ├── report.py                # Phase 6 及之后占位
│   └── pipeline.py              # 顺序编排与结构化 PipelineResult
└── tests/
    ├── test_loader.py
    ├── test_validator.py
    ├── test_metrics.py
    ├── test_diagnostics.py
    └── test_pipeline.py
```

运行测试：

```bash
pytest -q -W error
```
