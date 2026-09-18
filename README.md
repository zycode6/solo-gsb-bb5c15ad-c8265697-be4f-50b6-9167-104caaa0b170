# 事件证据链审计台（Event Evidence-Chain Audit Console）

多来源 JSON 事件记录导入、**时钟偏差建模**、可解释时间线重建与冲突定位的审计工作台。
后端 Python · FastAPI · SQLite，前端原生 HTML/CSS/JavaScript（**无任何前端构建步骤、无外部 CDN**）。

---

## 它解决什么问题

事故调查中，日志往往来自时钟并不同步的多台主机、边缘设备或第三方系统。直接按原始时间戳排序会得到
**不可能的因果顺序**（如"支付成功"早于"下单"）。本工具：

1. 为每个来源维护可由审计员调整的**时钟偏差假设**（offset / drift / 不确定半径 / 可信度 / 是否单调）；
2. 为每条事件在统一参考时间轴上计算一个**可信时间区间**而不是单点；
3. 结合 JSON 中显式声明的 `before/after` 因果关系与"同源时钟单调"假设，构造先后约束图；
4. 通过强连通分量（Tarjan）发现**闭环的不可能顺序**，通过拓扑界传播发现**被约束挤压为空的区间冲突**；
5. 审计员修改任何偏差假设后**一键即时重算**，并可导出完整审计结果 JSON 留证。

## 30 秒体验

```bash
docker compose up --build
# 打开 http://localhost:8000
```

1. 点击 **「载入示例」** —— 会看到 1 条闭环冲突 + 若干区间冲突；
2. 在左侧把来源 **srv-db 的时钟偏差改为 `30` 秒**，点「应用并重算」—— 区间冲突消失；
3. 把 **srv-edge 的「假设时钟单调」勾选去掉**再重算 —— 最后一条闭环冲突也消失；
4. 点击任意冲突卡片可在时间线上**定位并高亮冲突链**；
5. 点击 **「导出审计 JSON」** 下载包含偏差假设、派生区间、冲突链与原始记录的完整结果。

## 本地开发运行（不使用 Docker）

```bash
python3 -m pip install -r requirements.txt

# 启动时自动建库（也可显式执行：python -m app.database）
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
python3 -m pytest -q
python3 tests/smoke_api.py     # 端到端 HTTP 冒烟（需要 httpx）
```

## 导入文件格式

支持三种载荷（`.json` / `.ndjson` 均可，也可在导入框直接粘贴）：

**整包对象**（推荐，可同时声明来源时钟假设）：

```json
{
  "sources": [
    {"name": "srv-a", "offset_seconds": 0,  "uncertainty_seconds": 1.0, "trust": 0.9},
    {"name": "srv-b", "offset_seconds": -12.5, "drift_ppm": 20, "uncertainty_seconds": 3.0}
  ],
  "events": [
    {"id": "a-1001", "source": "srv-a", "timestamp": "2026-09-18T10:00:00Z",
     "type": "session.start", "summary": "用户会话建立", "before": ["srv-b/b-55"]},
    {"id": "b-55",   "source": "srv-b", "timestamp": 1789812008000,
     "type": "http.access", "jitter_seconds": 0.5}
  ]
}
```

**事件数组**：`[ {...}, {...} ]`
**NDJSON**：每行一个事件对象（坏行会跳过并在导入结果中告警）

### 事件字段宽松映射

| 语义 | 可识别键名 |
|---|---|
| 事件 ID | `id`, `event_id`, `eid`, `uuid`, `record_id` |
| 来源 | `source`, `source_name`, `src`, `host`, `origin`, `node`, `device`（也支持内联对象） |
| 时间戳 | `timestamp`, `time`, `ts`, `event_time`, `@timestamp`, `occurred_at`, `logged_at` |
| 类型 | `type`, `event_type`, `kind`, `action` |
| 摘要 | `summary`, `message`, `msg`, `title`, `description` |
| 单条抖动 | `jitter_seconds`, `jitter` |
| 后继（本事件先于…） | `before`, `next`, `successor(s)`, `leads_to`, `then` |
| 前驱（本事件后于…） | `after`, `prev`, `predecessor(s)`, `follows`, `caused_by`, `parent` |

- 时间戳支持 ISO-8601（含 `Z`/时区偏移）与 epoch 的 **秒/毫秒/微秒/纳秒** 数值（自动归一）。
- 跨来源引用写 `来源名/事件ID` 或 `来源名:事件ID`；裸 ID 仅在全库唯一时解析，重名按悬空告警而不是猜测。
- 导入模式：**replace**（清空后导入，默认）或 **merge**（按 来源+事件ID 去重覆盖）。

## 计算模型（可解释性）

设来源 `s` 的参数为 `offset`、`drift_ppm`、`uncertainty`，事件原始时钟为 `t`、单条抖动 `jitter`，
`t_ref` 为全库最早原始时间戳：

```
参考中点  ref(t) = t + offset + drift_ppm · 1e-6 · (t - t_ref)
可信区间  [ref(t) - jitter - uncertainty,  ref(t) + jitter + uncertainty]
```

先后约束边来自：

- **explicit**：JSON 里的 `before / after / predecessor` 等显式因果声明；
- **monotonic**：来源勾选"时钟单调"时，同源事件按原始时钟顺序串联。

每条约束边给出三态判定：

- `forced`（绿）：上游区间上界仍严格早于下游区间下界，顺序确定；
- `ambiguous`（黄）：两区间重叠，仅凭当前假设无法确定先后；
- `impossible`（红）：边位于冲突环上，或其端点区间已被挤压为空。

冲突分两类：

- **cycle（不可能顺序）**：约束图出现强连通环，输出完整闭环链；
- **interval（区间冲突）**：拓扑前向/后向传播后 `lower > upper`，输出把边界顶到不可能的证据链。

事件可信度：

```
confidence = 0.65 · source.trust + 0.25 · 区间锐度 + 0.10 · 一致性（不在冲突中为 1）
```

所有算法均为 O(V+E) 量级，调参后重算为亚秒级。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state` | 当前全部来源、事件（含派生区间/可信度/序号）、约束边、冲突链与统计 |
| POST | `/api/import` | multipart 上传 `file` 或表单字段 `payload`；`mode=replace|merge` |
| POST | `/api/sample?mode=replace` | 一键载入内置示例 |
| PATCH | `/api/sources/{id}` | 修改 `offset_seconds / drift_ppm / uncertainty_seconds / trust / monotonic / note`，**立即重算**并返回新状态 |
| POST | `/api/recompute` | 基于当前假设强制重算 |
| GET | `/api/export` | 下载完整审计结果 JSON（文件名带 UTC 时间戳） |
| POST | `/api/reset` | 清空工作区 |

导出 JSON 顶层包含：`stats / sources / events / constraints / links / conflicts / raw_records / model`。

## 页面功能

- **文件导入**：点击或拖拽上传、直接粘贴文本、replace/merge 可选；
- **冲突定位**：冲突卡片按类型列出完整证据链，点击即滚动并闪烁高亮时间线上的相关事件条；
- **时间线筛选**：关键字、来源、事件类型、仅看冲突、可信度 < 0.5；按来源分泳道，条长即可信区间，
  并叠加约束箭头（绿=确定 / 黄=存疑 / 红=不可能，实线=显式、虚线样式区分状态）；
- **审计导出**：一键下载 JSON；
- 点击任意事件条可查看参考区间、可信度、前后置关系与原始字段。

## 目录结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由与状态序列化
│   ├── database.py      # SQLite 连接、schema、启动初始化
│   ├── engine.py        # 时钟修正 / 区间 / Tarjan / 拓扑传播 / 冲突与可信度
│   ├── importer.py      # 多形态 JSON/NDJSON 解析、宽松字段映射、去重入库
│   ├── sample_data.py   # 内置示例（含两类可被调参解除的冲突）
│   └── web/             # 原生前端 index.html / styles.css / app.js
├── examples/events.example.json
├── tests/               # 单元测试 + HTTP 冒烟脚本
├── Dockerfile           # 启动即完成依赖安装与建库（entrypoint.sh）
├── docker-compose.yml   # 端口 8000，SQLite 持久化到命名卷 /data
└── requirements.txt
```

## 数据持久化与配置

- 数据库路径由环境变量 `AUDIT_DB` 指定，Compose 中持久化到命名卷 `audit-data`（容器内 `/data/audit.db`）。
- 监听端口由 `PORT` 控制（默认 8000）。
- 启动流程（`entrypoint.sh`）：`python -m app.database` 建库建表 → `uvicorn` 启动；
  FastAPI 的 startup 钩子也会幂等执行建表，二者保证"启动即就绪"。

## 审计方法提示

发现冲突后的典型排查顺序：

1. **区间冲突**优先核对相关来源的 `offset`（时间戳整体错位）与 `uncertainty_seconds`（不确定度是否给得过小）；
2. **闭环冲突**先检查环上的显式 `before/after` 是否为人工误标；若没有显式边，
   说明矛盾来自"同源时钟单调"假设——可能发生了日志乱序/回拨，尝试取消勾选该来源的单调假设；
3. 大量黄色 `ambiguous` 边表示区间普遍重叠，需要提高时钟质量（减小 uncertainty / 引入更精确的时间源），
   而不是强行判定顺序。
