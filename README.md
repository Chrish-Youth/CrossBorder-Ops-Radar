# CrossBorder Ops Radar

上传跨境电商每日运营数据，按 SKU、平台或国家/地区查看运营指标、定位异常，并下载 Excel 报表。数据校验、指标计算、诊断和导出都可以在本机完成，无需 API 密钥。

## 启动

已安装依赖的 Windows 用户可以直接双击 `start_demo.cmd`。首次使用时，在项目目录用 PowerShell 运行以下命令（已用 Python 3.11 验证）。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\start_demo.cmd
```

也可以用虚拟环境中的 Python 运行 `python -m streamlit run app.py`。关闭服务时在启动窗口按 Ctrl+C。

## 试一次退款率排查

1. 上传 [正常经营样例](data/demo/interview_success.xlsx)，保留默认的 **SKU** 维度，点击 **运行分析**。
2. 在 **数据校验** 中确认 56 行全部有效。在 **诊断信号** 中找到 `DEMO-HIGH-REFUND-DE`，它的退款率为 **29%**，超过 **10%** 的演示阈值。这是排查退款订单的线索，实际原因需要查看订单明细。
3. 点击 **下载 Excel 报表**。工作簿包含汇总（`Summary`）、校验问题（`Validation Issues`）、指标（`Metrics`）和诊断（`Diagnostics`）四张表。

| 还可以尝试 | 按 SKU 分析时会看到 |
| --- | --- |
| [数据质量问题](data/demo/interview_quality_issues.xlsx) | 4 行有效，8 行排除。校验明细解释了排除原因，有效行继续参与计算。 |
| [缺少国家列](data/demo/interview_fatal_missing_country.xlsx) | 提示缺少必填列 `country`，停止计算指标，仍可下载校验报告。 |

## 使用自己的数据

支持 CSV 和 XLSX。每行代表同一天、同一平台、同一国家/地区、同一 SKU 的汇总。可参照 [CSV 样例](data/sample_ecommerce_data.csv) 的表头。

| 必填列 | 填写口径 |
| --- | --- |
| `date`, `marketplace`, `country`, `sku`, `product_name` | 日期格式为 YYYY-MM-DD。SKU 建议存为文本，以保留前导零。 |
| `impressions`, `clicks`, `orders`, `units_sold` | 曝光量、点击量、订单数、销售件数，使用非负整数。 |
| `sales`, `ad_spend` | 销售额和广告花费，单位为 USD。 |
| `refunds`, `inventory` | 退款订单数和当日结束时的库存，使用非负整数。跨日分析取各商品在对应平台、国家/地区的最新库存。 |

完全重复的记录只保留一条。同一业务键出现冲突时，相关记录均排除，原因会列入校验报告。

## 可选的 AI 洞察

要让 DeepSeek 用中文解读结果，可先关闭应用，在 PowerShell 中设置本次会话的密钥，再从同一窗口启动。输入时密钥会隐藏。

```powershell
$env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'DeepSeek API key' -AsSecureString)).Password
.\start_demo.cmd
```

分析成功后，点击 **生成 AI 洞察** 才会发送分组指标和诊断信号，可能产生 API 费用。每次点击最多发送一次请求，超时后可手动重试。DeepSeek 返回用量时，摘要下方会显示输入和输出 Token 数。重新生成失败会保留同一分析上次成功的结果，切换文件或分析维度会清空旧洞察。

## 运行测试

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

完整离线测试包含历史 Prompt v1 的本地 Token 计数。演示应用只使用 DeepSeek 响应附带的用量。
