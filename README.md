# 合同审批审查系统

面向企业合同审批场景的**自动审查工具服务**：拉取待审合同 → 下载附件 → 解析文档与扫描件 →
执行规则审查 → 生成风险意见 → 写回审批评论区。

> **定位**：辅助审查，**不代替人工审批**。系统只输出"规则层面的风险提示"与"审批关注点"，
> 不产生通过/驳回的终局决定。金额、日期、比例全部由确定性代码判断，不做语义猜测。

需求基线：项目实战文档第 2.4 节「合同审批审查系统」。
逐条需求映射见 [docs/01-需求追踪矩阵与技术方案.md](docs/01-需求追踪矩阵与技术方案.md)。

## 核心特点

**三层规则体系**（共 43 条），价值从低到高：

| 层 | 条数 | 判断方式 | 人能发现吗 |
| --- | --- | --- | --- |
| 浅层 `seed_rules` | 26 | 单条款内找特征 | **能**，法务扫一眼就知道 |
| 深层 `deep_rules` | 5 | 跨条款交叉比对 | **不能**，需要运算 |
| 陷阱 `trap_rules` | 12 | **真实判例 + 法条依据** | **不能**，需要法律知识 |

第三层是核心价值：每条规则都来自公开的司法案例，命中时告诉你
"按《民法典》第 X 条，这样写会导致 Y 后果"，而不是"系统觉得有问题"。

例如定金超过合同金额 20%：

```
合同写：定金 30%（300 万）
卖方违约，买方主张双倍返还 600 万
法院实际支持：400 万
【法律依据】《民法典》第五百八十六条：超过部分不产生定金的效力
```

**其他工程特点**：

- 金额/日期/比例全部确定性抽取，**算不出来就跳过，不猜**
- 每条命中都带原文片段 + 页码行号，可复核
- 回写幂等由数据库条件更新保证，跨进程有效
- 扫描件走 OCR，识别不足时进入 `blocked` 交人工，不硬猜
- 141 项测试覆盖解析、规则、流程、异常、Web、并发

## 技术栈

Python 3.12 · FastAPI · SQLAlchemy · SQLite · PyMuPDF · RapidOCR · Jinja2

## 快速开始

```bash
uv venv --python 3.12
uv pip install -r requirements.txt

uv run --python 3.12 python scripts/make_samples.py   # 生成示例合同
uv run --python 3.12 python scripts/init_db.py        # 建表 + 43 条规则
uv run --python 3.12 python scripts/serve.py          # 启动，默认 8099
```

打开 <http://127.0.0.1:8099>。

详细步骤见下方各节。

---

## ⚠ 开不了页面？先看这里

> 这一节针对一个特定的本机环境问题（浏览器 Service Worker 劫持端口）。
> 如果你打开页面看到的是**别的应用的界面**，按下面排查。

**症状**：服务启动正常，`curl` 也能拿到正确 HTML，但浏览器打开 `127.0.0.1:8080`
显示的是**别的应用的界面**（一个聊天框写着 "Hello there"），
并报 `Server unavailable`。

**原因**：浏览器里有个 **Service Worker 占着 `127.0.0.1:8080` 这个 origin**。
它接管了该端口下所有页面导航，直接返回它自己缓存的界面，
请求根本到不了本系统。（Service Worker 按 origin 注册且长期驻留，
即使原应用已关闭或卸载，它仍然活着。）

**本系统默认端口已改为 `8099`，直接用默认值即可避开：**

```bash
uv run --python 3.12 python scripts/serve.py
# 打开 http://127.0.0.1:8099
```

如果仍要使用 8080，任选一种方式清掉那个 Service Worker：

| 方式 | 操作 | 说明 |
| --- | --- | --- |
| 注销 SW（推荐） | 打开 `chrome://serviceworker-internals/`，找到 `http://127.0.0.1:8080`，点 **Unregister** | 一劳永逸 |
| 无痕窗口 | `Ctrl+Shift+N` 后访问 | 无痕窗口不继承已注册的 SW |
| 换端口 | `--port 9000` | 最省事 |

> **清浏览器缓存通常无效**——Service Worker 注册独立于 HTTP 缓存。

**30 秒判断是代码问题还是 SW 劫持**：

```bash
curl http://127.0.0.1:8099/ | grep title
```

- 输出 `<title>待办列表 · 合同审批审查系统</title>` → 服务正常，问题在浏览器端（SW 劫持）；
- 输出别的标题或连接失败 → 才是服务本身的问题。

这一条能省掉大量"以为是代码 bug"的无效排查。

---

## 〇、我有一份自己的合同要审，怎么操作？

**系统没有"上传"按钮，这是设计使然**——需求 2.4.3 的定位是"审批系统触发待办拉取"，
不是用户手动上传。真实场景里，合同由业务员在 OA 提交，法务打开待办列表就能看到。

演示环境没有真实 OA，所以提供一个等价动作：**把合同放进 `inbox/` 目录**。

### 三步操作

```bash
# 第 1 步：在项目根目录创建 inbox/，把你的合同拷进去
#   （支持 pdf / docx / png / jpg / bmp / tif）
mkdir inbox
#   然后把合同文件复制到 inbox/ 下

# 第 2 步：注册成待审记录（在项目根目录执行）
uv run --python 3.12 python scripts/import_inbox.py

# 第 3 步：启动并操作
uv run --python 3.12 python scripts/serve.py
# 打开 http://127.0.0.1:8099
# 点「拉取待处理审批单」→ 找到你的合同 → 「查看详情」→「一键处理」
```

### 实测输出（已跑通）

```
发现 1 个文件
  ✓ 新增：测试采购合同.pdf
      审批编号：INBOX-001（列表里按此编号查找）
完成：新增 1 条，跳过 0 条
```

处理完成后，审查结果：

```
任务: INBOX-001  done  success
命中 8 条:
  [high  ] R101_ADVANCE_PAYMENT_HIGH        预付款比例过高
  [high  ] R401_PENALTY_RATIO_HIGH          违约金比例过高
  [high  ] R501_JURISDICTION_COUNTERPARTY   管辖地约定在对方所在地
  [medium] T005                             按日计违约金但未设累计上限
  [medium] T006                             赊销付款但未约定所有权保留
  [medium] T009                             长期合同未约定解除条件
  [medium] R801_NO_CONFIDENTIALITY          保密条款缺失
  [medium] R901_NO_DATA_CLAUSE              数据条款缺失
```

### 关于审批编号

本地导入的合同编号为 `INBOX-001`、`INBOX-002`……**纯 ASCII**。
中文文件名放在「合同标题」字段里，不放进编号。

> 为什么：中文编号放进表单提交会被 URL 再编码一次，服务端收不到原始值，
> 导致查不到记录（实测踩过：`INBOX-测试采购合同` → `INBOX-%25E6%25B5%258B...`）。
> 而且真实审批系统的编号本来就是 ASCII 格式（如 `HT-2024-0001`）。

### 重复导入会怎样

按**文件名**去重，同一份文件重复导入会提示"已导入过，跳过"，
不会在待办列表里堆出重复项。

### 支持的格式与已知限制

| 格式 | 支持情况 |
| --- | --- |
| `.pdf`（文本层） | ✅ 直接提取文字 |
| `.pdf`（扫描件） | ✅ 走 OCR，约 5-7 秒/页 |
| `.docx` | ✅ 含表格内容 |
| `.png` `.jpg` `.jpeg` `.bmp` `.tif` `.tiff` | ✅ 走 OCR |
| 其他格式（如 `.xlsx`） | ❌ 会进入 blocked，提示格式不支持 |

**扫描件识别率取决于图像质量**：手写体、印章遮挡、低分辨率会降低准确率。
识别不足时任务进入 `blocked` 交人工处理，不会猜。

---

## 一、快速开始

环境：Windows / Linux，**Python 3.12**（本机实测 3.12.13），依赖管理用 **uv**。

### 1.1 安装 uv（若尚未安装）

```bash
# Windows（PowerShell）
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

安装后重开终端，确认：`uv --version`

### 1.2 初始化环境与数据

```bash
cd contract_review

# 创建虚拟环境（uv 会自动下载并使用 Python 3.12，无需本机预装）
uv venv --python 3.12

# 安装依赖到该虚拟环境
uv pip install -r requirements.txt

# 生成示例合同（含真实无文本层扫描件）与模拟审批数据
uv run --python 3.12 python scripts/make_samples.py

# 建表并灌入 26 条种子规则
uv run --python 3.12 python scripts/init_db.py
```

### 1.3 运行

```bash
# 完整闭环演示（十步，打印全部验收证据）
uv run --python 3.12 python scripts/demo_flow.py

# 启动调用端页面（推荐：带访问日志过滤）
uv run --python 3.12 python scripts/serve.py
# 浏览器打开 http://127.0.0.1:8099

# 等价的原始命令（日志不过滤，探测请求会刷屏）
# uv run --python 3.12 python -m uvicorn app.main:app --port 8099

# 运行测试（141 项）
uv run --python 3.12 python -m pytest
```

### 1.4 说明

**`uv run` 与 `uv pip` 的分工**：

| 命令 | 作用 | 何时用 |
| --- | --- | --- |
| `uv venv --python 3.12` | 建虚拟环境，并锁定解释器为 3.12 | 首次初始化 |
| `uv pip install -r ...` | 装依赖进 `.venv` | 首次初始化、改了 requirements 之后 |
| `uv run --python 3.12 ...` | 在 `.venv` 里执行命令 | 每次跑脚本/测试/服务 |

`uv run` 会自动找到项目根目录下的 `.venv`，**不需要手动 activate**，
也不会误用系统里其它 Python。指定 `--python 3.12` 是显式约束：
若环境被别人改成了 3.14，uv 会报错而不是静默换版本，避免"我这能跑你那不能跑"。

**为什么不用 `uv sync`**：
`uv sync` 面向 `pyproject.toml` + `uv.lock` 的项目管理模式。
本项目用 `requirements.txt`（课程交付要求"代码文件不带依赖"，保持纯 pip 兼容），
因此走 `uv pip` 这条路径。若后续想切到项目管理模式：

```bash
uv init --python 3.12          # 生成 pyproject.toml
uv add fastapi uvicorn ...     # 逐项加依赖并生成 uv.lock
uv sync                        # 之后用 uv sync 复现环境
```

> **Windows 提示**：若报 `Device or resource busy`（数据库文件被占用），
> 说明有残留的 uvicorn 进程，先结束进程再重试：
> `Get-Process python | Where-Object { $_.Path -like "*contract_review*" } | Stop-Process -Force`

---

## 二、目录结构

```
contract_review/
├── app/
│   ├── config.py            配置（唯一来源，支持运行期覆盖供测试使用）
│   ├── main.py              FastAPI 装配：页面路由 + JSON 工具接口 + 异常映射
│   ├── domain/              纯领域层（不依赖任何外部框架）
│   │   ├── enums.py         11 个状态/分类枚举
│   │   ├── errors.py        8 个业务异常（带 reason 与 retry_target）
│   │   └── schemas.py       解析/审查的数据结构
│   ├── db/
│   │   ├── base.py          引擎、会话、事务上下文
│   │   └── models.py        8 张业务表
│   ├── approval/            外部审批系统适配
│   │   ├── gateway.py       接口契约 + 传输对象
│   │   ├── mock_gateway.py  本地 JSON 模拟 OA（演示用）
│   │   ├── http_gateway.py  真实 OA 的 HTTP 实现
│   │   └── factory.py       按配置选择实现
│   ├── parser/              文档理解
│   │   ├── text_utils.py    归一化、数值抽取、证据截取
│   │   ├── ocr.py           RapidOCR 封装
│   │   ├── document.py      PDF/DOCX/图片读取，按页判定文本层或 OCR
│   │   ├── field_extractor.py  8 项基础信息
│   │   └── clause_extractor.py 8 类条款
│   ├── review/              规则层
│   │   ├── rule_engine.py   三种匹配模式 + 风险汇总 + 关注点
│   │   └── seed_rules.py    26 条种子规则（覆盖 11 类主题）
│   ├── services/            业务编排（事务边界）
│   │   ├── log_service.py       全链路日志
│   │   ├── task_service.py      拉取、去重、状态流转、阻塞、重试
│   │   ├── attachment_service.py 下载、路径安全、SHA256
│   │   ├── parse_service.py     解析编排、合并、blocked 判定
│   │   ├── review_service.py    审查编排、命中入库
│   │   ├── result_service.py    结果入库、回写文本生成
│   │   └── comment_service.py   回写、幂等、并发互斥
│   ├── tools/
│   │   └── contract_tools.py    需求 2.4.10 的 7 个工具 + 编排
│   └── web/                 调用端
│       ├── routes.py        5 个模块页面 + 操作路由
│       ├── templates/       Jinja2 模板
│       └── static/style.css
├── scripts/
│   ├── make_samples.py      生成示例合同与模拟审批数据
│   ├── init_db.py           建表 + 灌种子规则
│   ├── demo_flow.py         十步闭环演示
│   ├── serve.py             启动服务（装配访问日志过滤）
│   ├── import_inbox.py      把 inbox/ 里的合同注册成待审记录 ← 审自己的合同用这个
│   └── access_log_filter.py 探测日志过滤器
├── tests/                   141 项测试
├── docs/
│   ├── 00-开发进度.md
│   ├── 01-需求追踪矩阵与技术方案.md
│   ├── 02-工具调用说明.md
│   └── 03-前端使用说明.md      ← 不会用界面先看这个
├── inbox/                   ★ 把你自己的合同放这里，再跑 import_inbox.py
├── samples/                 示例合同（生成产物）
├── data/                    运行期数据（数据库、附件、模拟 OA）
└── requirements.txt
```

**依赖方向**（禁止反向）：

```
web → tools → services → (parser | review | approval | db) → domain
```

---

## 三、工具调用说明（需求 2.4.10）

7 个工具是本系统对外的唯一能力入口，全部在 `app/tools/contract_tools.py`。
既可被 Python 直接调用，也可通过 HTTP 接口调用（见下节）。

| # | 工具函数 | 入参 | 返回要点 |
| --- | --- | --- | --- |
| 1 | `list_pending_contract_approvals(limit)` | `limit` 1—200 | 审批编号/标题/申请人/申请时间/附件数；`created`/`updated` 计数（去重证据） |
| 2 | `get_contract_approval(instance_id)` | 审批单业务标识 | 审批信息、表单数据、附件信息、当前处理状态 |
| 3 | `download_contract_attachment(instance_id, attachment_id, file_name)` | 外部附件编号 | `document_id`、`local_path`、`file_size`、`checksum`（SHA256） |
| 4 | `parse_contract_document(document_id)` | `document_id` | 8 项基础信息 + 8 类条款，每项含 `value`/`snippet`/`location`/`status`/`detail` |
| 5 | `run_contract_rules(case_id)` | `case_id` | 总风险等级、命中列表（名称/等级/证据/位置/建议）、摘要、关注点 |
| 6 | `save_review_result(case_id, overall_risk_level, summary_text, focus_points_json, comment_text)` | 见左 | `review_id` |
| 7 | `write_approval_comment(instance_id, review_id)` | 见左 | `write_status`、`write_response_text`、`already_written` |

**ID 语义**（避免同一实体多套标识）：

- `instance_id` = 审批单业务唯一标识（`approval_code`）
- `case_id` = 本地任务主键（`approval_tasks.id`）
- `document_id` = 本地附件主键（`approval_attachments.id`）
- `review_id` = 本地结果主键（`review_results.id`）

**典型调用顺序**：

```python
from app.tools import contract_tools

# 1. 拉取
r = contract_tools.list_pending_contract_approvals(limit=20)

# 2. 详情 → 3. 下载
detail = contract_tools.get_contract_approval("HT-2024-0001")
for att in detail["attachments"]:
    dl = contract_tools.download_contract_attachment(
        "HT-2024-0001", att["external_attachment_id"], att["file_name"])

# 4. 解析
parsed = contract_tools.parse_contract_document(dl["document_id"])

# 5. 审查（case_id 从 detail["task_id"] 取）
case_id = detail["task_id"]
review = contract_tools.run_contract_rules(case_id)

# 6. 保存 → 7. 回写
saved = contract_tools.save_review_result(
    case_id, review["overall_risk_level"], review["summary_text"],
    json.dumps(review["focus_points"], ensure_ascii=False), comment_text)
contract_tools.write_approval_comment("HT-2024-0001", saved["review_id"])
```

另有三个便捷编排函数（非需求强制）：`run_full_pipeline(case_id)`、
`get_task_snapshot(case_id)`、`retry_task(case_id)`。

---

## 四、HTTP 接口

启动后访问 `http://127.0.0.1:8099/docs` 可看自动生成的接口文档。

**页面**（需求 2.4.7 的 5 个模块）：

| 路径 | 模块 | 说明 |
| --- | --- | --- |
| `GET /` | 待办调用 | 任务列表、统计卡片、拉取按钮、最近日志 |
| `GET /approvals/{code}` | 详情查看 + 解析结果 + 规则命中 + 结果处理 | 一页承载四个模块，按流程自上而下排列 |
| `GET /rules` | — | 26 条规则列表（便于验收覆盖度） |
| `GET /logs` | — | 运行日志（管理员入口） |

**操作**（表单提交，PRG 模式）：

| 路径 | 作用 |
| --- | --- |
| `POST /actions/pull` | 拉取待办 |
| `POST /actions/run` | 一键处理（下载→解析→审查→保存→回写） |
| `POST /actions/retry` | 重试阻塞任务 |
| `POST /actions/write-comment` | 回写评论（幂等） |

**JSON 工具接口**：

| 路径 | 对应工具 |
| --- | --- |
| `GET /api/tools/list_pending?limit=20` | 1 |
| `GET /api/tools/approval/{instance_id}` | 2 |
| `POST /api/tools/download` | 3 |
| `POST /api/tools/parse` | 4 |
| `POST /api/tools/review` | 5 |
| `POST /api/tools/save-result` | 6 |
| `POST /api/tools/write-comment` | 7 |
| `POST /api/tools/run-pipeline` | 编排 |
| `GET /api/tools/snapshot/{case_id}` | 编排 |
| `GET /health` | 健康检查 |

**异常映射**：`NotFoundError → 404`、`TaskStateError → 409`、其他业务异常 `→ 400`
（响应体含 `error_type` = 异常 `reason`，便于调用方按原因分支）。

### 关于启动日志里的 404（含一次真实返工）

启动后 access log 会出现这类请求：

```
GET  /tools      404
GET  /props      404
GET  /build.json 404
GET  /v1/models  404
GET  /sw.js      404
POST /tools      405
```

**这些不是本系统的路由，也不是配置错误。** 原因是浏览器装了本机服务探测类插件
（常见于本地大模型前端扩展），它会连锁扫描本机端口，判断"端口后面是不是它要连的服务"。

这些路径登记在 `app/main.py` 的 `_PROBE_PATHS` 中，响应设计如下：

| 请求 | 状态码 | body |
| --- | --- | --- |
| `GET /tools` 等 | 404 | **合法 JSON** `{"ok":false,"error_type":"not_supported","message":"..."}` |
| `POST /tools` 等 | 405 | 同上 JSON |
| 真实接口 | 200 | 正常业务数据 |
| 其他不存在的路径 | 404 | FastAPI 默认 |

#### 这个设计返工过两次，过程值得记下来

**第一次：返回 `204 No Content`**

理由听起来很正当——"没有内容就返回无内容状态码，日志也干净"。

结果前端直接报错：

```
Failed to execute 'json' on 'Response': Unexpected end of JSON input
```

**原因**：204 按 HTTP 规范**不允许有响应体**。客户端拿到空 body 去 `res.json()`，
必然抛异常。我为了让服务端日志好看，把前端搞崩了——这个取舍完全本末倒置，
而且当时的验证只用了 `curl` 看状态码，没模拟客户端的解析行为，所以没发现。

**第二次：只修 GET 的 204，漏了 405**

改回 404 + JSON 后，界面不崩了。但实机日志里还有一行：

```
POST /tools HTTP/1.1" 405 Method Not Allowed
```

FastAPI 对"路径存在但方法不允许"自动返回 405，**body 同样为空**——这是同一个坑的
另一个入口。只盯着 204 就会漏掉它。

**最终方案**：GET 返 404 + JSON，非 GET 返 405 + JSON。状态码诚实，body 可解析。

**为什么不用 200 + JSON**：
那会让插件误判"找到了它要的服务"，可能发起更多请求。用错误信息换表面安静，不值。

#### 边界（有测试守着）

| 约束 | 守护测试 |
| --- | --- |
| 探测路径 body 必须可解析 | `test_web.py::test_probe_paths_return_parseable_json` |
| 非 GET 也必须可解析 | `test_web.py::test_probe_paths_non_get_returns_parseable_json` |
| 真实接口不被遮蔽 | `test_web.py::test_real_tool_routes_not_shadowed_by_probe` |
| 其他路径仍 404 | `test_web.py::test_unknown_path_still_404` |

> **改动警告**：若将来要把 `/favicon.ico` 之类做成真实路由，必须先把它从
> `_PROBE_PATHS` 移除。FastAPI 中先注册者优先，覆盖后真实路由会**静默失效且不报错**。

#### 手动验证方法

仓库提供了两个 Node 脚本（用 Node 是为了复刻浏览器 `fetch` + `res.json()` 的真实行为，
`curl` 看不出"解析这一步会不会炸"）：

```bash
# 正验证：所有探测路径与真实接口都能被前端安全解析
node scripts/verify_probe_response.mjs http://127.0.0.1:8099

# 反验证：临时起一个返回 204 空体的服务，确认上面的脚本能检出问题
node scripts/negative_check_204.mjs
```

反验证的意义：证明验证脚本不是"永远打印通过"的空转。它会输出
`✓ 已检出：Unexpected end of JSON input`，与用户界面看到的报错完全一致。

#### 让日志也干净（第二轮修复）

第一轮只解决了"前端崩溃"，**没有解决"日志刷屏"**——这是只做了一半。
用户的实机日志仍然每秒数条 404/405。

于是补了访问日志过滤器 `scripts/access_log_filter.py`，通过
`scripts/serve.py` 装配：

```bash
# 默认：探测日志不记录
uv run --python 3.12 python scripts/serve.py

# 排查用：不静默，但每条探测日志前标 [已静默]，确认判定是否正确
uv run --python 3.12 python scripts/serve.py --verbose-probe

# 查看本次静默了多少条（Ctrl+C 退出时打印）
```

**过滤规则**：路径在白名单内 **且** 状态码是 404/405 → 静默，**不区分 HTTP 方法**。

实测效果（8 条探测 + 3 条真实请求）：

```
INFO:     127.0.0.1:49375 - "GET / HTTP/1.1" 200 OK
INFO:     127.0.0.1:49376 - "GET /approvals/HT-2024-0001 HTTP/1.1" 200 OK
INFO:     127.0.0.1:49377 - "GET /health HTTP/1.1" 200 OK
```

探测请求一条不剩，真实请求一条不少。

**为什么不按方法区分（初版判断错了）**：
初版只静默 GET/HEAD，理由是"POST 可能是真实接口误调用"。
但实机日志里 `POST /tools 405`、`POST /props 405` 仍在刷屏，问题只解决一半。
更关键的是那个理由站不住——真实接口都在 `/api/tools/*` 下，
顶层 `/tools` 不存在任何真实路由，对它发 POST 只可能是探测。
真正需要看见的"接口误调用"（如用 GET 调 `/api/tools/download`）路径不在白名单，仍会记录。

**为什么在日志层过滤，而不是改状态码**：
这两件事必须分开。HTTP 响应要诚实（404 + 合法 JSON），
日志是给人看的（异常可见即可，不必全量留痕）。
为了日志干净去改状态码，就是拿错误信息换安静——那是本末倒置，已经踩过一次。

**实现细节（踩了两个坑，记下来避免重犯）**：

1. **改 `record.msg` 无效**。uvicorn 的 access formatter 是 `AccessFormatter`，
   格式串为 `'%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'`
   —— **完全不用 `%(message)s`**，而是直接读 `record.args`。
   所以改 msg 对 access 日志毫无作用。
2. **占位符与 args 数量必须严格对应**。若 msg 里多加一个 `%s` 而 args 仍是 5 个，
   `getMessage()` 会抛格式化异常，日志反而丢失——比不打标记更糟。

最终做法：只改 `record.args[0]`（客户端地址），不动 msg，格式化始终成立。

**测试保障**（`tests/test_access_log.py`，11 项）：

| 断言 | 意义 |
| --- | --- |
| 探测路径 404/405 被静默（含 POST） | 噪音确实消失 |
| 真实业务请求 200/303 被保留 | **最关键**：过滤不能变成"吞掉真实日志" |
| 白名单路径返回 200/500 时保留 | 将来变成真实路由时自动恢复记录 |
| 非白名单路径的 404 保留 | 路径写错是真实问题，必须可见 |
| 记录结构异常时放行 | uvicorn 改格式时不崩、不吞 |
| 两层路径清单一致 | 防漂移：应用层加了路径，日志层要跟上 |

---

## 五、配置

全部通过环境变量覆盖，前缀 `CR_`：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CR_DATA_DIR` | `./data` | 运行期数据根目录 |
| `CR_DB_URL` | `sqlite:///./data/contract_review.db` | 可切 PostgreSQL |
| `CR_APPROVAL_MODE` | `mock` | `mock`（本地 JSON）/ `http`（真实 OA） |
| `CR_APPROVAL_BASE_URL` | `http://127.0.0.1:8100` | http 模式下的 OA 地址 |
| `CR_APPROVAL_TOKEN` | 空 | http 模式下的访问令牌 |
| `CR_MOCK_OA_STORE` | `./data/mock_oa/approvals.json` | 模拟 OA 数据文件 |
| `CR_APPROVAL_TIMEOUT_SECONDS` | `10.0` | 外部接口超时 |
| `CR_OCR_DPI` | `200` | 扫描页渲染分辨率 |
| `CR_OCR_MIN_CONFIDENCE` | `0.5` | OCR 置信度下限 |
| `CR_SCANNED_PAGE_CHAR_THRESHOLD` | `30` | 低于此字符数判为扫描页 |
| `CR_EMPTY_DOCUMENT_CHAR_THRESHOLD` | `20` | 低于此字符数判为内容为空 |
| `CR_COMMENT_TIMEOUT_SECONDS` | `10.0` | 回写超时 |

---

## 六、数据表（需求 2.4.9 的 8 张）

| 表 | 列数 | 说明 |
| --- | --- | --- |
| `approval_tasks` | 13 | 审批任务主表，`approval_code` 唯一索引保证去重 |
| `approval_attachments` | 11 | 附件记录，含下载状态、SHA256、失败原因 |
| `contract_parses` | 9 | 解析结果，含基础信息/条款 JSON、失败原因、原文行 |
| `review_rules` | 12 | 审查规则，26 条种子数据 |
| `rule_hits` | 11 | 命中记录，**含规则快照**（防规则改名后历史失真） |
| `review_results` | 7 | 审查结果，含回写文本 |
| `comment_logs` | 6 | 回写尝试历史 |
| `task_logs` | 6 | 全链路日志 |

原文未列出但实现中补充的字段，均在 `app/db/models.py` 注释中标「扩展」并说明原因。

---

## 七、任务状态机

```
pending ──→ parsing ──→ reviewing ──→ done
              │             │
              ↓             ↓
           blocked ←────────┘
              │
              └──→ 人工重试 → parsing | reviewing
```

| 阻塞原因（`blocked_reason`） | 触发条件 | 重试目标 |
| --- | --- | --- |
| `attachment_missing` | 附件取不到 / 下载失败 / 本地文件丢失 | `parsing` |
| `ocr_unavailable` | 扫描件识别失败（引擎异常、置信度不足） | `parsing` |
| `document_empty` | 文档有效字符不足（含空白页） | `parsing` |
| `unsupported_document` | 格式不支持（如 .xlsx） | `parsing` |
| `approval_gateway_error` | 审批系统接口失败 | 按阶段 |
| `comment_write_failed` | 回写超时 / 外部拒绝 | `reviewing` |

**回写状态**：`not_written → writing → success | failed`。
幂等由条件更新保证：只有 `not_written`/`failed` 才能抢占 `writing`，
`success` 与 `writing` 状态下的重复请求都不会再次写外部系统。

**已完成任务的重复处理保护**：`done` 状态的任务再次调用解析/流水线时，
默认抛 `TaskStateError` 拒绝。理由：`done` 意味着意见已写入评论区，
静默重跑会让状态退回 `parsing`（审批人会怀疑之前的意见是否作废），
且扫描件 OCR 每页约 5 秒，重跑一次就多花一次。
若确需重审（合同换版），显式传 `allow_reprocess=True`：

```python
contract_tools.run_full_pipeline(case_id, allow_reprocess=True)
contract_tools.parse_service.parse_task_documents(case_id, allow_reprocess=True)
```

---

## 八、实测结果

运行环境：Windows，Python 3.12.13，RapidOCR 1.4.4，PyMuPDF 2.0。

### 测试

```
141 passed in 104.69s
```

| 测试文件 | 数量 | 覆盖 |
| --- | --- | --- |
| `test_parser.py` | 11 | 文本层 PDF、扫描件 OCR、DOCX 表格、空文档、字段/条款抽取、两个真实缺陷回归 |
| `test_rules.py` | 18 | 三种匹配模式、scope 限定、中文数字、风险汇总、关注点去重、种子规则覆盖度 |
| `test_flow.py` | 10 | 完整闭环、多附件合并、OCR 纳入、DOCX 闭环、回写文本结构、状态流转、校验值一致性、已完成任务重处理保护、演示脚本可重复运行 |
| `test_errors.py` | 13 | 去重、状态不被重置、附件缺失、空文档、失败记录、重试、接口失败、格式不支持 |
| `test_comment.py` | 7 | 首次回写、幂等、失败阻塞、重试只重写、归属校验、并发互斥 |
| `test_web.py` | 8 | 页面可访问、健康检查、探测路径返回可解析 JSON、非 GET 也返回 JSON、真实接口不被遮蔽、真实 404 语义、OpenAPI 洁净 |
| `test_access_log.py` | 11 | 探测日志静默（含 POST）、真实日志不被吞、白名单路径返 200 时保留、结构异常放行、两层清单一致 |
| `test_deep_rules.py` | 19 | D001-D005 五条深层规则的正反用例、天数抽取不被日期污染、已过期合同不报警、异常隔离 |
| `test_trap_rules.py` | 44 | T001-T012 十二条真实陷阱的正反用例、法条依据完整性、误报收窄、风险排序 |

> `test_web.py` 的存在理由：FastAPI 中先注册的路由优先匹配。探测路径是循环批量注册的，
> 若将来误把 `/api` 之类加进探测清单，真实接口会静默返回 204 而不是执行——
> 服务不报错、只是"接口没反应"，极难发现。该文件直接用断言守住这条边界。

### 解析实测

| 文档 | 文本层字符 | 判定 | OCR 页 | 解析字符 | 耗时 |
| --- | --- | --- | --- | --- | --- |
| `contract_normal.pdf` | 486 | 文本层 | 无 | 432 | 0.04 s |
| `contract_scanned.pdf` | **0** | **扫描页** | 第1页 | 431 | **6.25 s** |
| `contract.docx` | — | 文本层 | 无 | 377 | 0.10 s |
| `contract_empty.pdf` | 0 | 空白页 | 无 | 0 | → `document_empty` |

扫描件文本层为 **0 字符**，证明 OCR 路径被真实触发，不是 mock。

### Python 版本与 OCR 兼容性

本项目在以下两个环境中都实测通过（141 项测试全绿）：

| 环境 | Python | RapidOCR | 扫描页 OCR 耗时 | 备注 |
| --- | --- | --- | --- | --- |
| 交付基线 | **3.12.13** | 1.4.4 | 6.25 s | 依赖均有 cp312 官方 wheel |
| 早期开发 | 3.14.6 | 1.2.3 | 5.55 s | 仅作对照，非交付环境 |

**版本差异不影响的实现细节**：

RapidOCR 1.2.3 的识别结果里置信度是**字符串**，1.4.4 改成 **float**。
`parser/ocr.py` 对两者都用 `float(item[2])` 转换，因此无需按版本分支。
这一点是有意为之：解析层的容错逻辑不应依赖第三方库的具体返回类型形态，
否则每次升级都要改业务代码。

**依赖版本范围**：`requirements.txt` 全部用 `>=` 下限约束，不锁定精确版本。
好处是能在多个 Python 小版本上装到合适 wheel；代价是可复现性略弱。
若需要严格复现，建议在目标环境执行 `pip freeze > requirements.lock`。

### 闭环演示关键输出

```
[STEP1] 首次拉取 created=4；重复拉取 created=0 updated=4  ✓ 去重生效
[STEP4] 扫描件 OCR 页=[1] 有效字符=431 耗时=6.25s
[STEP5] 执行规则 26 条，命中 5 条，总风险=high
        命中：预付款比例过高(60%>30%)、违约金比例过高(30%>20%)、
              管辖地约定在对方所在地、保密条款缺失、数据条款缺失
[STEP7] 回写前 0 条 → 首次新增 1 条 → 重复新增 0 条  ✓ 幂等生效
        回写日志 1 条（成功记录不重复）
[STEP8] 附件缺失 → blocked(attachment_missing) → 重试 → 再次失败（可复现）
[STEP9] 空文档 → blocked(document_empty)（分类正确，非 ocr_unavailable）
[STEP10] 全链路日志 14 条，完整回放拉取→下载→解析→审查→结果→回写
```

---

## 九、开发中修复的真实缺陷

这些不是设计推演，而是真实验证中发现并修掉的问题（每个都有回归测试）：

| # | 缺陷 | 现象 | 修复 |
| --- | --- | --- | --- |
| 1 | 标签用子串匹配 | "乙方向甲方提供…"被当成"甲方"标签行，签约主体抽成正文句 | 严格模式（标签在行首 + 紧跟分隔符）优先，宽松模式兜底 |
| 2 | 金额科学计数法 | `1,200,000` 显示为 `1.2e+06` | 整数走千分位，非整数两位小数 |
| 3 | 日期只认显式标签 | "有效期至 2025-12-31"抽不到到期时间，会误报风险 | 增加语义回退模式 |
| 4 | 空文档误报 OCR 失败 | 空白 PDF 报 `ocr_unavailable`，排查方向被带偏 | 增加视觉空白页识别，正确报 `document_empty` |
| 5 | 合并记录丢原文 | 多附件时全文检索只能看片段，漏命中 | 合并记录保存全部原文行 |
| 6 | 审查读错记录 | 多附件时只读最后一个附件，误报"条款缺失" | 优先取合并记录（`merged=true`） |
| 7 | **并发回写未互斥** | `_claim_write` 只排除 `success`，`writing` 状态可被覆盖，导致重复写外部系统 | 可写状态限定为 `not_written`/`failed` |
| 8 | 审批单不存在语义错误 | 抛 `ApprovalGatewayError`（400/5xx），调用方会去重试不存在的对象 | 改为 `NotFoundError`（404） |
| 9 | 状态无变化仍打日志 | 多附件解析产生 `reviewing → reviewing` 噪声 | 状态相同时跳过 |
| 10 | 测试配置隔离失败 | 重载 `db.base` 产生新 `Base`，模型仍继承旧 `Base`，"建表成功但表不存在" | 改为就地修改 `settings` 字段，不重载模块 |
| 11 | 演示逐个解析附件 | 多附件时审查只读最后一个附件的记录，日志出现 `reviewing → parsing` 回退 | 演示改用任务级合并解析 |
| 12 | 已完成任务可被静默重跑 | `done` 任务再点处理会退回 `parsing`，白耗 OCR 时间且让人怀疑意见作废 | 增加 `allow_reprocess` 守卫，默认拒绝，需显式放行 |
| 13 | 演示脚本重复运行崩溃 | 加了 12 的守卫后，第二次跑 `demo_flow.py` 在 STEP4 直接抛 `TaskStateError` 中断 | 演示开始前把主任务与两个异常任务复位为 `pending`（新增 `_reset_demo_state` 等三个函数），而不是在 STEP4 绕过守卫 |
| 14 | 回写幂等统计口径错误 | 重复运行时显示"成功记录 3 条（应 1 条）"，误判幂等失效 | 改为按 `review_id` 统计本次审查的记录，并单独说明累计数来自多轮独立审查 |
| 15 | **探测路径返回 204 导致前端崩溃** | 界面报 `Failed to execute 'json' on 'Response': Unexpected end of JSON input`；204 按规范无 body，客户端 `res.json()` 必抛异常 | 改为 404 + 合法 JSON body。**这是本文件最严重的误判**：为日志好看而牺牲客户端可用性，且当时只用 curl 验证状态码，没模拟客户端解析 |
| 16 | 修 204 时漏了 405 | 实机日志仍有 `POST /tools 405`，body 同样为空，客户端一样崩 | 显式接管探测路径的非 GET 方法，返回 405 + 同一份 JSON |
| 17 | **页面载入动画导致整页空白** | headless 截图拿到全白页面；根因是 `animation-fill-mode: both` 让元素在动画开始前就是 `opacity: 0` | 改用**纯 transform 位移、不动 opacity**。opacity 的中间态等于"内容不可见"，是有风险的动画属性 |
| 18 | 子标题装饰块渲染成小方块 | `h2.sub` 是 flex 容器，`::before` 的 `height:1px` 被拉伸成方块 | 子标题取消 before 装饰，改用字号+字色+留白建立层次 |
| 19 | **只修了前端崩溃，没修日志刷屏** | 第 15 轮修完后，前端不崩了，但 access log 仍每秒数条 404/405 | 补 `scripts/access_log_filter.py`，按"路径白名单 + 404/405"静默，不区分方法。**问题只解决一半就交付，是这一轮的主要教训** |
| 20 | 过滤器改 `record.msg` 无效 | verbose 模式的 `[已静默]` 标记完全不出现 | uvicorn 的 `AccessFormatter` 格式串不用 `%(message)s`，直接读 `record.args`。改为修改 `args[0]` |
| 21 | 过滤器按方法区分，POST 仍刷屏 | `POST /tools 405` 依然输出 | 去掉方法判断。真实接口都在 `/api/tools/*` 下，顶层路径发 POST 只可能是探测 |

---

## 九点二、三层规则体系

系统有三层规则，价值从低到高：

| 层 | 模块 | 条数 | 做法 | 人能发现吗 | 价值 |
| --- | --- | --- | --- | --- | --- |
| 一 | `seed_rules.py` | 26 | 单条款内找特征 | **能**，扫一眼就知道 | 低（需求 2.4.6 要求，凑数） |
| 二 | `deep_rules.py` | 5 | 跨条款交叉比对 | **不能**，要运算 | 中 |
| 三 | `trap_rules.py` | 12 | **真实司法案例 + 法条依据** | **不能**，需要法律知识 | **高** |

### 第三层：合同陷阱（来自真实案例）

这一层是系统的核心价值。每条规则都来自**公开的司法案例与法院裁判观点**，
命中时告诉审批人"按哪条法律，这样写会导致什么后果"——不是"系统觉得有问题"。

**资料来源**（均为公开法律实务资料）：

1. 山东德衡律师事务所 郭培明《[供应链合同十大法律陷阱](https://www.deheng.com/dhgc/flgc/25764.html)》(2025-05-21)
2. 浙江法院网《[避雷合同中的这5类"模糊条款"](https://zjsfgkw.gov.cn/art/2025/6/17/art_56_37364.html)》(开化县人民法院, 2025-06-17)

#### T001 争议解决同时约定仲裁与诉讼

```
合同写：「可向仲裁委申请仲裁，或向法院起诉」
当事人以为：两条路都能走，更灵活
实际后果：仲裁协议**无效**，只能走诉讼
```

**法条**：《仲裁法解释》第七条——约定可仲裁也可起诉的，仲裁协议无效。

想用仲裁的保密性和效率，目的完全落空。

#### T002 定金超过合同金额 20%

```
合同写：定金 30%（300 万）
卖方违约，买方主张：双倍返还 600 万
法院实际支持：400 万
```

**法条**：《民法典》第 586 条——超过 20% 的部分不产生定金效力，自动转为普通预付款。

那 100 万**没有双倍罚则保护**。

#### T003 质量条款"符合国家标准"——把标准降到最低线

```
合同写：「面粉符合食品安全标准」
真实案例：砷含量 0.4mg/kg（国标 ≤0.5，买方内控 ≤0.3）
法院判：合同未约定特殊标准时优先适用国标 → 供应商未违约
结果：买方承担全部召回损失
```

**法条**：《民法典》第 511 条——质量不明按国标履行。

"符合国标"看起来是质量保证，实际是**把标准降到最低线**。

#### T004 履约期限写"尽快发货"

```
合同写：「收到订单后尽快发货」
按《民法典》511 条第（四）项：「债务人可以随时履行」
结果：**无法主张逾期违约**——根本没有可计算的逾期起点
```

对方迟延交付，法院会问："合同约定几号交货？"

#### T005 按日计违约金但无累计上限

```
合同写：「每逾期一日按 0.5% 支付违约金」
算一下：0.5% × 200 天 = 100% → 违约金等于合同全额
```

**法条**：《民法典》第 585 条 + 《九民纪要》第 50 条。

虽然可请求调减，但**诉讼成本已经发生**。

#### T006 赊销但未约定所有权保留

```
合同写：「验收合格后 30 日内付款」
买方破产时：
  - 有所有权保留 → 可取回未付款货物（回收率接近 100%）
  - 无所有权保留 → 只能按普通债权清偿（**通常低于 30%**）
```

**法条**：《民法典》第 641、642 条。

#### T007 委托开发但未约定知识产权归属

```
合同写：「乙方受甲方委托开发定制模具」
直觉：我出钱，成果归我
法律默认：专利申请权归研究开发人（即乙方）
```

**法条**：《民法典》第 859 条。

真实案例：供应商把模具图纸卖给竞争对手，法院判专利权归供应商，委托方损失 35% 市场份额。

#### T008 不可抗力条款笼统

```
合同写：「不可抗力按法律规定执行」
真实案例：跨境电商仅写"疫情属不可抗力"，未含政府管控措施
法院判：免除 30% 责任（**不是全部免除**）
```

**法条**：《民法典》180 条、590 条——需"及时通知并提供证明"。

不约定通知时限，对方未通知也难以追责。

#### T009 长期合同未约定解除条件

```
真实案例：新能源企业与锂矿商签长协，锂价暴涨 300%
若无约定解除条件，只能依赖「情势变更」原则
诉讼成本高、周期长
```

**法条**：《民法典》563 条、533 条。

> **误报收窄说明**：初版逻辑是"全文找不到'解除合同'就报"，
> 结果 12 个演示案例**全部命中**——因为它们都只构造了 1-2 个条款。
> 这种"见谁都报"的规则只会让用户关掉系统。
> 收窄为三个条件同时成立：① 无解除条款 ② 条款数 ≥3 ③ 有长期履约特征。

#### T010 违约金与定金并存，未明确择一

```
合同写：既约定「定金 15%」，又约定「违约金 10%」
当事人以为：双重保障，可以同时主张
法律实际：**只能择一适用**，不能叠加
```

**法条**：《民法典》588 条。

#### T011 「以实际结算为准」但无结算依据

```
合同写：「总价款以实际结算为准」
未约定：单价、计量方式、价格依据
后果：法院按「订立合同时履行地的市场价格」认定
```

**法条**：《民法典》511 条第（二）项。

定价权悬空——涨了按涨的算，跌了按跌的算。

#### T012 涉外合同未约定适用法律

```
真实案例：中德设备合同未约定管辖，需在德国起诉
耗时 3 年，**诉讼成本占争议金额 40%**
```

**法条**：《涉外民事关系法律适用法》41 条。

不同法系对违约认定与赔偿范围差异极大，结果不可预期。

### 设计原则

**1. 只实现"能从文本确定性判断"的陷阱**

有些陷阱需要上下游两份合同才能判断（如"违约金比例差超 70% 构成显失公平"），
单份合同无法检测，因此**不实现**——不实现比实现一个会误报的规则更好。

**2. 量化阈值来自法条或判例，不是拍脑袋**

| 阈值 | 来源 |
| --- | --- |
| 定金 ≤ 20% | 《民法典》586 条 |
| 违约金 > 实际损失 30% | 《九民纪要》50 条 |

**3. 每条命中必须带法条依据**

证据文本格式：
```
<命中的事实>
【法律依据】<法条原文>
```

测试 `test_all_traps_have_legal_basis` 强制校验这一点——
没有法条依据的命中等于主观判断，价值大打折扣。

**4. 排序：陷阱 > 深层 > 浅层**

同级风险时，陷阱类命中排最前（带法条依据，最需要看），
浅层排最后（法务自己也能发现）。

### 验证方式

```bash
# 七个真实案例的完整推理过程（含法条依据）
uv run --python 3.12 python scripts/demo_trap_value.py

# 跨条款推理的五个案例
uv run --python 3.12 python scripts/demo_deep_value.py
```

44 项测试（`tests/test_trap_rules.py`），每条规则都配正反用例。

---

## 九点三、两层规则：浅层特征 vs 深层推理

这是本系统**最有价值的部分**，值得单独说明。

### 为什么需要两层

| 层 | 规则数 | 做法 | 人能发现吗 |
| --- | --- | --- | --- |
| **浅层**（`seed_rules.py`） | 26 条 | 在**单条条款**里找一个特征 | **能**。看到 60% 就知道预付款偏高，看不到保密条款就知道缺失 |
| **深层**（`deep_rules.py`） | 5 条 | **跨条款交叉比对** | **不能**。必须把全文拼起来在脑子里算 |

浅层规则满足需求 2.4.6 的验收（覆盖 11 类主题），但**价值有限**——法务自己扫一眼就发现了。
深层规则才是系统存在的理由：**单看每条条款都正常，合起来才有洞**。

### 五条深层规则

#### D001 付款挂钩验收，但验收由对方单方决定

```
付款条款：「验收合格后 30 日内支付 70%」   ← 标准写法
验收条款：「验收标准由甲方确定」            ← 也常见
```

**组合后的陷阱**：甲方可以永远不出验收结论 → 付款义务永不触发。

单看任何一条都没问题，必须对照两条才能发现。

#### D002 违约金封顶额相对合同金额偏低

```
违约条款：「违约金累计不超过合同总金额的 5%」  ← 像在保护双方
合同金额：12,000,000 元
```

**算一下**：`12,000,000 × 5% = 600,000 元`。若对方违约需重新采购，差价可能远超 60 万。

**必须做一次乘法才能看出来**，人通常不会去算。

#### D003 付款账期跨越合同到期日

```
合同到期日：30 天后
付款账期：90 日
验收期限：7 日
```

**算一下**：`90 + 7 = 97 天 > 30 天` → 尾款必然落在合同到期之后。

三个数字分散在不同条款，各自都正常。**要做一次日期减法**，人几乎不会主动去算。

#### D004 扣了质保金，但全文找不到质保期

```
付款条款：「留 5% 作为质保金，质保期满后支付」  ← 标准
全文：找不到"质保期是多久"
```

扣款有依据、退款无期限，实务中极易产生纠纷。
这类「**引用了某个东西但被引用方缺失**」的问题，必须主动追踪才能发现。

#### D005 异地管辖 + 本地履约

```
争议条款：「由乙方所在地法院管辖」    ← 明确，法务会注意到
交付条款：「交付至甲方所在地仓库」    ← 也正常
```

**错配**：证据、标的物、验收记录都在我方，却要跑去对方所在地打官司，诉讼成本显著上升。

要把两个条款对照，才成立「错配」。

### 设计原则：只算不猜

所有深层规则都是**确定性运算**，不用大模型：

| 类型 | 例子 | 算式 |
| --- | --- | --- |
| 交叉比对 | D001 / D005 | 条款A的特征 + 条款B的特征 → 组合判断 |
| 量级校验 | D002 | `金额 × 百分比 = 绝对额`，与阈值比 |
| 时序推演 | D003 | `到期日 - (账期 + 验收期) = 最晚交付日` |
| 引用追踪 | D004 | 付款条款引用质保期 → 全文搜索被引用方 |

**算不出来就跳过，不猜**。比如 D002 拿不到合同金额时不命中，而不是假设一个金额。

### 证据必须可复核

每条深层命中的证据里都写着**完整算式**，人可以直接验算：

```
违约金条款封顶「不超过 5%」，合同总额 12,000,000 元，
换算封顶额 = 12,000,000 × 5% = 600,000 元。
若对方违约需重新采购，损失可能远超该封顶额。
```

不允许出现"系统说有风险但说不出怎么算的"。

### 验证方式

```bash
# 看五个案例的完整推理过程
uv run --python 3.12 python scripts/demo_deep_value.py
```

19 项测试（`tests/test_deep_rules.py`），每个检查都配**正反用例**：

- 正向：组合成立 → 必须命中
- 反向：只差一个条件 → **必须不命中**

反向用例比正向更重要——它防止规则变成"见谁都报"的噪音源。

### 与浅层规则的合并

深层命中与浅层命中放在同一列表，统一按风险降序。
界面用 `rule_code` 前缀区分：`D` 开头为深层推理。

同级时**深层排在前面**——信息量更大、更容易被忽略，值得优先呈现。

### 实现过程中修掉的两个真实缺陷

1. **天数抽取被日期污染**：`"2024 年 12 月 31 日前完成交付"` 中的 `31 日` 被当成 31 天，
   导致验收期限从 7 日变成 31 日，D003 算出错误结论。
   修复：抽天数前先剔除完整日期。
2. **已过期合同仍报警**：对一份 `2025-12-31` 到期的历史合同报"跨期风险"，
   算出"距到期 -296 天"，纯属噪音。修复：`remaining <= 0` 直接跳过。

---

## 九点五、调用端视觉设计

界面不是默认的后台管理模板，而是刻意选了一个方向并贯彻到底。

### 设计方向：「法律文书 / 编辑排版」

**为什么是这个方向**：
这个系统的产物是一份给法务看的**审查意见书**。使用者是律师、法务、审批人，
他们日常面对的是纸质合同、条款编号、朱笔批注。所以界面不该像 SaaS 后台
（蓝白卡片 + 圆角按钮 + 图标），而应该像一份排版精良的法律文书。

**具体落地**：

| 维度 | 选择 | 理由 |
| --- | --- | --- |
| 正文字体 | Noto Serif SC（思源宋体） | 衬线体承载阅读文本，模拟印刷合同质感；本机已有，零网络请求 |
| 界面字体 | 系统无衬线 | 表格/标签信息密度高，宋体小字易糊，用无衬线保证清晰 |
| 数字字体 | 等宽（ui-monospace） | 金额、SHA256、审批编号需要对齐可读 |
| 主色 | 墨黑 `#1a1a1a` | 纯黑刺眼，墨黑更接近油墨印刷 |
| 强调色 | 朱砂 `#a83a2c` | 中国传统批注色，对应纸面上的朱笔圈点；比鲜红沉稳 |
| 底色 | 纸白 `#faf9f6` | 略带暖调，比纯白柔和，配 CSS 噪点纹理模拟纸张 |
| 形状 | 全部方角，无阴影 | 印刷品的语言；圆角和投影是屏幕界面语言，会破坏调性 |
| 章节 | 「壹 · 贰 · 叁」中文序号 | 文书章节感，比 1/2/3 更贴合语境 |
| 详情页 | 「合同审查意见书」文书头 + 审批编号 | 建立正式感，同时让人确认在看哪份合同 |

**克制的动效**：
只有页面载入时的 6px 上浮 + 逐块微错开（总延迟 0.16s 内）。
**不使用 opacity 动画**——它的中间态直接等于"内容不可见"，
在 headless 渲染、后台标签页、打印场景下会变成整页空白（真实踩过，见缺陷 17）。
纯位移的中间态只是"位置偏了一点"，内容始终可见。

**无障碍**：
- 键盘焦点环用朱砂色 2px 描边，确保可见；
- `@media (prefers-reduced-motion: reduce)` 关闭全部动效；
- `@media print` 关闭动效与噪点纹理，保证打印输出干净。

**响应式**：
820px 断点。卡片自动两列，表格允许横向滚动（而非压缩列宽导致文字挤压），
顶栏在窄屏隐藏次要信息、优先保证导航可用。

---

## 十、已知局限（如实说明）

1. **字段抽取基于正则与版式假设**。对排版差异大的合同需要补充模式；抽取失败会如实标记
   `status=failed`/`not_found` 并记录原因，**不会伪造字段值**。
2. **OCR 依赖 RapidOCR 内置中文模型**。手写体、印章遮挡、低分辨率扫描件识别率会下降；
   识别不足时进入 `blocked` 交人工，而不是猜。
3. **`threshold` 型规则只处理能在文本中定位到数值的情形**。数值藏在表格图片里时依赖 OCR 质量；
   抽不到数值时**不命中**（避免假风险），由缺失类规则覆盖"无法判断"的提示。
4. **真实 OA 接入只提供 HTTP 网关骨架**。通用 REST 约定见 `http_gateway.py` 文档字符串，
   具体厂商字段映射需按对接文档补充；演示链路使用 mock 网关，两者接口契约一致。
5. **未实现用户登录与权限体系**。需求 2.4 未要求，调用端定位为内网演示入口。
6. **规则维护目前只提供只读页面**。规则增删改需直接操作数据库或后续扩展接口。
7. **单进程演示**。并发控制依赖数据库行级原子性（可跨进程），但 mock 网关的 JSON 文件
   写入未做文件锁，多进程并发演示需替换为真实审批系统。
8. **演示脚本会复位主任务状态**。为让 `demo_flow.py` 可重复运行，
   每次运行前会把 HT-2024-0001/0003/0004 三个任务复位为 `pending` 并清空模拟评论区。
   这**只影响演示脚本**，不改变业务代码；`app/` 下的服务层仍保持
   "已完成任务默认拒绝重跑"的约束（由 STEP7b 真实验证）。
   生产环境不会有这种复位行为。

---

## 十一、交付清单

| 类别 | 内容 |
| --- | --- |
| 工具服务 | `app/` 全部分层代码 |
| 数据库初始化脚本 | `scripts/init_db.py`（建表 + 26 条规则，幂等） |
| 示例合同文件 | `samples/`（文本层 PDF、**无文本层扫描件 PDF**、DOCX、空白 PDF） |
| 模拟审批数据 | `data/mock_oa/approvals.json`（4 个审批单，覆盖正常/缺附件/空文档） |
| 工具调用说明 | 本文档第三节 + `docs/02-工具调用说明.md` |
| 前端使用说明 | `docs/03-前端使用说明.md`（每个页面、每个按钮的用法） |
| 完整闭环演示 | `scripts/demo_flow.py`（十步，打印全部验收证据） |
| 需求映射 | `docs/01-需求追踪矩阵与技术方案.md` |
| 测试 | `tests/`（141 项） |
| 调用端页面 | `app/web/`（5 个模块 + 法律文书风格视觉设计，见第九点五节） |

> 提交时按课程要求把文件夹命名为 `组名_姓名_项目_行业`，并附测试截图。
> 交付文件夹应**不含** `.venv/`、`data/`、`__pycache__/`、`.pytest_cache/`
> （依赖与运行期数据不入库，收到后按第一节命令重新生成）。
