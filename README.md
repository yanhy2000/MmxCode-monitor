# mmx-monitor

MiniMax Code 本地用量监控台 —— 终端一条命令启动，浏览器里看 token 消耗、输出速度、缓存命中率。

<p align="center">
  <img src="img/ui.png" width="720" alt="mmx-monitor 界面预览" />
</p>

零第三方依赖（仅 Python 标准库 + 本地化的 ECharts），只读读取运行中的本地数据库，**不影响正在使用中的 MiniMax Code**。

## 功能

- **KPI 总览**：输入 / 输出 / 缓存读取 tokens、缓存命中率、调用次数、加权输出速度、会话数
- **Token 消耗时序**：按数据跨度自动分桶（分钟 → 小时 → 天 → 周），缓存 + 输入堆叠柱 + 输出折线双轴
- **模型筛选**：多选下拉（全选 / 反选 / 单点勾选），筛选实时作用于全部图表与表格
- **按模型拆分视图**：一键把时序图切成分模型的多条输出曲线
- **模型对比**：各模型的调用次数、总输入、输出速度横向对比；点击柱子可快速只看该模型
- **最近调用明细**：时间、模型、会话标题、tokens 明细、耗时、单次输出速度
- **时间范围**：最近 1 小时 / 24 小时 / 7 天 / 30 天 / 全部
- **自动刷新**：5s / 10s / 30s / 手动

## 环境要求

- Python 3.8+
- 无需 pip 安装任何包

## 快速开始

```bash
git clone https://github.com/yanhy2000/mmx-monitor.git
cd mmx-monitor
python monitor.py
```

默认监听 `127.0.0.1:7341` 并自动打开浏览器。

### 命令行参数

| 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `--port` | 监听端口（被占用时自动 +1 重试） | `7341` |
| `--host` | 监听地址 | `127.0.0.1` |
| `--db` | 数据库文件路径 | 自动探测 |
| `--no-browser` | 不自动打开浏览器 | - |

```bash
python monitor.py --port 8000 --no-browser
python monitor.py --db "D:/path/to/runtime-state.sqlite"
```

数据库路径默认按以下顺序探测，也可用环境变量 `MINIMAX_DATA_DIR` 指定数据目录：

```
$MINIMAX_DATA_DIR/v2/sqlite/runtime-state.sqlite
~/.minimax/v2/sqlite/runtime-state.sqlite
```

## 数据来源

所有数据来自 MiniMax Code 的本地运行时数据库：

```
~/.minimax/v2/sqlite/runtime-state.sqlite
```

主要用到两张表：

### `local_runtime_message_rows`

每条消息一行的 JSON 记录，其中 `data_json` 里带有单次模型调用的完整用量信息：

```json
{
  "usage": {
    "total_tokens": 27117,
    "context_window": 512000,
    "input_tokens": 321,
    "output_tokens": 252,
    "cache_read": 26544,
    "request_duration_ms": 1476
  },
  "context_usage_telemetry": { "model": "MiniMax-M3" }
}
```

模型名在 `context_usage_telemetry.model` 字段里。

### `local_runtime_token_usage`

结构化的计费账本表，字段为 `input_tokens` / `output_tokens` / `reasoning_tokens` / `cache_read_tokens` / `cache_write_tokens` / `cost_usd`，适合做跨会话的账单聚合。当前界面用它做交叉核对。

## 指标口径

| 指标 | 计算方式 |
| --- | --- |
| 缓存命中率 | `cache_read / (cache_read + input_tokens)` |
| 输出速度 | `Σ output_tokens / Σ request_duration_ms × 1000`（请求级加权平均） |
| 单次速度 | `output_tokens / request_duration_ms × 1000` |

**关于缓存命中率的数据来源**：`usage.cache_read` 是**服务端下发**的权威值，客户端也会用本地 tokenizer 估算一份（`context_usage_telemetry.localTokens`），两者存在约 3~4% 的稳定偏差，对应字段 `context_usage_telemetry.divergenceRate`。本工具一律采用服务端数值，客户端估算仅作为一致性参考。

## 安全性

- 每次请求都通过 **SQLite 官方 backup API**（`mode=ro` 只读 URI + `src.backup(dst)`）取一份内存快照再查询，正确处理 WAL、不锁库、不阻塞写入方
- 极端锁场景回退到"复制 db / -wal / -shm 三件套到临时目录"方案
- 全程只读，不对 MiniMax Code 的数据做任何写入或修改

## 项目结构

```
mmx-monitor/
├── monitor.py           # 终端程序入口：HTTP 服务 + 只读快照 + JSON API
├── static/
│   ├── index.html       # 单页监控台
│   └── echarts.min.js   # 本地化 ECharts（离线可用）
├── img/
│   └── ui.png           # 界面预览图
├── LICENSE
└── README.md
```

前端通过 `/api/data?range=<范围>&models=<模型列表>` 获取聚合结果，`models` 参数省略时返回全部模型。

## 已知限制

- `cost_usd` 字段在本地库中恒为 0，暂未提供成本估算
- 模型筛选状态不做持久化，刷新后回到"全部模型"
- 时区按运行机器的本地时区（UTC+8）渲染

## 关于 AI 生成

本项目的代码与文档由 **MiniMax Code**（AI 编程助手）生成并迭代，作者负责需求定义、方向决策与验收。仓库内的提交同样包含大量 AI 协作产出，这一点在此如实说明。

## 参考项目

- [zcode-monitor](https://github.com/yiyanwannian/zcode-monitor) — 本项目的思路来源，早先的 ZCode 用量监控面板

## 第三方组件

- [ECharts](https://echarts.apache.org/) — Apache License 2.0，已本地化打包以便离线使用（若加载失败会自动回退到 CDN）

## License

[MIT](LICENSE)
