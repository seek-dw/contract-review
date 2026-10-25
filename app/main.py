"""FastAPI 应用装配模块：创建应用、挂载路由与静态资源、统一异常处理。

职责：
- 创建 :data:`app` 实例，注册调用端路由与静态目录；
- 启动时确保数据库引擎已初始化（避免首个请求才发现配置问题）；
- 把工具层的业务异常映射为 HTTP 响应（JSON 接口用），页面路由自行处理重定向。

非职责：
- 不实现业务逻辑；
- 不注册鉴权中间件（需求 2.4 未要求）。

设计原因：
需求 2.4 的交付物是"工具服务"，因此本模块同时提供两种入口：
- **HTML 页面**：人工操作与演示（``/``、``/approvals/{code}`` 等）；
- **JSON 工具接口**（``/api/tools/*``）：供审批系统的 Agent 或外部编排器调用，
  与需求 2.4.10 的 7 个函数一一对应。

启动：
    .venv/Scripts/python -m uvicorn app.main:app --port 8080
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db.base import get_engine
from app.domain.errors import ContractReviewError, NotFoundError, TaskStateError
from app.tools import contract_tools
from app.web.routes import router as web_router

# 已知的本机工具探测路径。
#
# 背景（真实排查记录）：
#   浏览器里装了本机服务探测类插件（常见于本地大模型前端扩展）后，
#   它会连锁扫描本机常见端口，请求 ``/tools``、``/props``、``/v1/models`` 等路径，
#   用于判断"这个端口后面是不是它要连的服务"。
#   这些路径**不属于本系统**，本系统也不应伪装成那些服务。
#
# 为什么保留这份清单（而不是删掉让它们自然 404）：
#   这些探测请求会以每秒数次的高频反复出现，把 access log 完全淹没，
#   导致真正的错误（例如某个真实接口 500）难以被看见。
#   用一份显式清单把它们单独标记出来，日志才可用。
#
# 【重要】响应设计的三次返工记录——这里踩过两个坑，都写下来避免再犯：
#
#   坑 1：返回 204 No Content
#     最初的实现返回 204，理由是"没有内容就返回无内容状态码"。
#     结果前端报错：
#       Failed to execute 'json' on 'Response': Unexpected end of JSON input
#     原因：204 按 HTTP 规范**不允许有响应体**。客户端拿到空 body 去
#     ``JSON.parse()`` 必然抛异常，于是"我让它安静"变成了"我让它崩溃"。
#     这是本文件最严重的一次误判：只考虑了服务端日志，没考虑客户端行为。
#
#   坑 2：返回 404 纯文本
#     改回 404 后界面不崩了，但日志里仍是刺眼的 404，且部分客户端会把
#     非 JSON 的 404 body 当成解析失败再报一次错。
#
#   最终方案：返回 **404 + 合法 JSON body**。
#     - 状态码保持 404，语义诚实："这里没有这个资源"；
#     - body 是合法 JSON，客户端 `res.json()` 不会抛异常；
#     - 内容是结构化的"不支持"说明，调用方可据此判断而非猜测。
#
#   为什么不返回 200 + JSON：
#     那会让插件误判"找到了它要的服务"，可能进一步发起更多请求，
#     属于用错误信息换取表面安静。
_PROBE_PATHS: tuple[str, ...] = (
    "/tools",
    "/props",
    "/build.json",
    "/v1/models",
    "/sw.js",
    "/favicon.ico",
    "/robots.txt",
)

# 探测路径的响应体。
# 设计为与业务错误同构（``ok`` / ``error_type`` / ``message``），
# 这样调用方用同一套解析逻辑就能处理，不需要为探测路径写特殊分支。
_PROBE_BODY: dict[str, object] = {
    "ok": False,
    "error_type": "not_supported",
    "message": "本服务不提供该接口。本系统为合同审批审查服务，"
    "可用接口见 /docs 或 /api/tools/*。",
}

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """应用生命周期钩子：启动时确保数据库引擎已创建。

    参数：
        _app: FastAPI 实例（本函数不使用）。
    返回：
        异步上下文管理器：启动阶段执行初始化，退出阶段无清理动作。
    关联：
        在 :data:`app` 构造时通过 ``lifespan=_lifespan`` 注册；
        内部调用 ``db.base.get_engine``。

    设计原因：
        1. **为什么需要启动钩子**：延迟初始化会让"数据库路径写错"这类配置问题
           推迟到第一个请求才暴露。启动时就建引擎，问题在启动日志里立刻可见。
        2. **为什么用 lifespan 而不是 ``@app.on_event("startup")``**：
           安装的 FastAPI 0.161 已弃用 ``on_event``，运行测试会报
           ``DeprecationWarning``。lifespan 是当前推荐写法，
           且能同时表达"启动做某事、关闭做清理"。
           本项目关闭时没有必须执行的清理（SQLite 连接由引擎池管理），
           因此退出段留空——但保留结构，将来要加清理时不必改注册方式。
        3. **为什么定义在 app 之前**：``FastAPI(lifespan=...)`` 在构造时就要拿到
           这个函数对象，定义在之后会导致 ``NameError``。
    """
    get_engine()
    yield


app = FastAPI(
    title="合同审批审查系统",
    description=(
        "面向企业合同审批场景的自动审查工具服务。"
        "拉取待审合同、解析文档与扫描件、执行规则审查、生成风险意见并写回审批评论区。"
        "本系统不代替人工审批。"
    ),
    version="1.0.0",
    lifespan=_lifespan,
)

# 静态资源（CSS）。目录取自配置，保证与模板目录同源。
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")

# 调用端页面路由。
app.include_router(web_router)


def _absorb_probe() -> JSONResponse:
    """回答浏览器扩展对本机端口的探测请求。

    返回：
        HTTP 404 + 合法 JSON body（:data:`_PROBE_BODY`）。
    关联：
        由下方循环为本函数按 :data:`_PROBE_PATHS` 逐个注册；
        不进 OpenAPI 文档，避免把"别人的探测路径"混进本系统的接口说明里误导对接方。

    为什么是 404 + JSON，而不是 204：
        见 :data:`_PROBE_PATHS` 上方的三次返工记录。核心一句：
        204 按规范不带响应体，客户端 ``res.json()`` 会抛
        ``Unexpected end of JSON input``——为了日志好看而让前端崩溃，是本末倒置。
        现在状态码诚实（404 = 此资源不存在），body 可解析（JSON），
        两边都不得罪。

    说明：
        本函数不做任何业务动作。
        若某个路径在未来成为本系统真正的路由（例如真要做 ``/favicon.ico``），
        必须先把它从 :data:`_PROBE_PATHS` 移除，否则会与真实路由冲突
        （FastAPI 中先注册者优先，会造成真实路由失效却不报错）。
        ``tests/test_web.py::test_real_tool_routes_not_shadowed_by_probe``
        正是为了守住这条边界。
    """
    return JSONResponse(status_code=404, content=_PROBE_BODY)


def _absorb_probe_method_not_allowed() -> JSONResponse:
    """回答探测路径上的非 GET 请求（如 ``POST /tools``）。

    返回：
        HTTP 405 + 合法 JSON body。
    关联：
        由下方第二个循环按 :data:`_PROBE_PATHS` 注册 POST/PUT/PATCH/DELETE；
        与 :func:`_absorb_probe` 共用同一份 body 结构。

    为什么需要这个函数：
        探测插件会先 GET 再 POST 试探。FastAPI 对"路径存在但方法不许"的请求
        自动返回 405，**默认 body 为空**，客户端 ``res.json()`` 一样会抛
        ``Unexpected end of JSON input``。
        也就是说：只修 GET 的 204 不够，405 是同一个坑的另一个入口。

        这条是从用户实机日志里发现的：
            POST /tools HTTP/1.1" 405 Method Not Allowed
        若只盯着 204 看，就会漏掉它。
    """
    return JSONResponse(status_code=405, content=_PROBE_BODY)


# FastAPI 的 ``app.get()`` 只接受单个路径字符串，不支持一次传一组路径，
# 因此这里显式循环注册。写成循环而不是复制 7 个装饰器，是为了让
# "探测路径清单"只有 :data:`_PROBE_PATHS` 一处定义，改的时候不会漏。
for _probe_path in _PROBE_PATHS:
    app.add_api_route(
        _probe_path,
        _absorb_probe,
        methods=["GET"],
        include_in_schema=False,
        status_code=404,
        response_class=JSONResponse,
    )


# 探测路径的非 GET 请求（日志里真实出现过 ``POST /tools``）也要有 JSON body。
#
# 为什么单独处理：
#   探测插件会先 GET 再 POST 尝试。FastAPI 对"路径存在但方法不允许"的请求
#   自动返回 405，且**默认 body 为空**——客户端 ``res.json()`` 同样会抛
#   ``Unexpected end of JSON input``，与 204 是同一个坑。
#   因此这里显式接管 405，返回结构化的 JSON 说明。
#
# 为什么用 405 而不是 404：
#   方法不匹配是 405 的标准语义；保持准确的状态码，
#   调用方（包括日志分析工具）才能正确区分"路径不对"与"方法不对"。
for _probe_path in _PROBE_PATHS:
    app.add_api_route(
        _probe_path,
        _absorb_probe_method_not_allowed,
        methods=["POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
        status_code=405,
        response_class=JSONResponse,
    )


# ---------------------------------------------------------------------------
# 统一异常处理
# ---------------------------------------------------------------------------


@app.exception_handler(NotFoundError)
def _handle_not_found(_request: Request, exc: NotFoundError) -> JSONResponse:
    """把"未找到"映射为 404。

    参数：
        _request: 请求对象（未使用）。
        exc: 业务异常。
    返回：
        JSON 错误响应。
    关联：
        被 FastAPI 在抛出 :class:`NotFoundError` 时自动调用。
    """
    return JSONResponse(
        status_code=404,
        content={"ok": False, "error_type": "not_found", "message": exc.message},
    )


@app.exception_handler(TaskStateError)
def _handle_state_error(_request: Request, exc: TaskStateError) -> JSONResponse:
    """把状态冲突映射为 409。

    参数：
        _request: 请求对象。
        exc: 业务异常。
    返回：
        JSON 错误响应。
    关联：
        用于"对非阻塞任务重试""并发回写"等场景——调用方应稍后重试或改变操作。
    """
    return JSONResponse(
        status_code=409,
        content={"ok": False, "error_type": "state_conflict", "message": exc.message},
    )


@app.exception_handler(ContractReviewError)
def _handle_business_error(_request: Request, exc: ContractReviewError) -> JSONResponse:
    """把其他业务异常映射为 400。

    参数：
        _request: 请求对象。
        exc: 业务异常。
    返回：
        JSON 错误响应，含 ``reason`` 便于调用方按原因分支处理。
    关联：
        覆盖附件缺失、OCR 失败、内容为空、接口失败、回写失败等。
        这些是"业务规则不满足"，不是服务端缺陷，因此用 400 而非 500。
    """
    return JSONResponse(
        status_code=400,
        content={
            "ok": False,
            "error_type": exc.reason,
            "message": exc.message,
            "retry_target": exc.retry_target.value if exc.retry_target else None,
        },
    )


# ---------------------------------------------------------------------------
# JSON 工具接口（需求 2.4.10 的 7 个函数）
# 说明：与页面路由分开，因为页面用表单 + 重定向，接口用 JSON。
# 两者调用同一套工具函数，不存在两份业务逻辑。
# ---------------------------------------------------------------------------


@app.get("/api/tools/list_pending", tags=["工具接口"])
def api_list_pending(limit: int = 20) -> dict:
    """工具：拉取待处理审批单列表。

    参数：
        limit: 最大条数，1—200。
    返回：
        ``{"ok": true, "data": {...}}``。
    关联：
        :func:`app.tools.contract_tools.list_pending_contract_approvals`。
    """
    return {"ok": True, "data": contract_tools.list_pending_contract_approvals(limit)}


@app.get("/api/tools/approval/{instance_id}", tags=["工具接口"])
def api_get_approval(instance_id: str) -> dict:
    """工具：查询审批单详情。

    参数：
        instance_id: 审批单业务唯一标识。
    返回：
        ``{"ok": true, "data": {...}}``。
    关联：
        :func:`app.tools.contract_tools.get_contract_approval`。
    """
    return {"ok": True, "data": contract_tools.get_contract_approval(instance_id)}


@app.post("/api/tools/download", tags=["工具接口"])
def api_download(instance_id: str, attachment_id: str, file_name: str = "") -> dict:
    """工具：下载合同附件。

    参数：
        instance_id: 审批单业务唯一标识。
        attachment_id: 外部附件编号。
        file_name: 期望文件名，可为空。
    返回：
        ``{"ok": true, "data": {...}}``。
    关联：
        :func:`app.tools.contract_tools.download_contract_attachment`。
    """
    return {
        "ok": True,
        "data": contract_tools.download_contract_attachment(
            instance_id, attachment_id, file_name
        ),
    }


@app.post("/api/tools/parse", tags=["工具接口"])
def api_parse(document_id: int) -> dict:
    """工具：解析合同文档。

    参数：
        document_id: 附件主键。
    返回：
        ``{"ok": true, "data": {...}}``。
    关联：
        :func:`app.tools.contract_tools.parse_contract_document`。
    """
    return {"ok": True, "data": contract_tools.parse_contract_document(document_id)}


@app.post("/api/tools/review", tags=["工具接口"])
def api_review(case_id: int) -> dict:
    """工具：执行规则审查。

    参数：
        case_id: 任务主键。
    返回：
        ``{"ok": true, "data": {...}}``。
    关联：
        :func:`app.tools.contract_tools.run_contract_rules`。
    """
    return {"ok": True, "data": contract_tools.run_contract_rules(case_id)}


@app.post("/api/tools/save-result", tags=["工具接口"])
def api_save_result(
    case_id: int,
    overall_risk_level: str,
    summary_text: str,
    focus_points_json: str,
    comment_text: str,
) -> dict:
    """工具：保存审查结果。

    参数：
        case_id: 任务主键。
        overall_risk_level: low/medium/high。
        summary_text: 中文摘要。
        focus_points_json: 关注点 JSON 数组字符串。
        comment_text: 回写内容。
    返回：
        ``{"ok": true, "data": {...}}``。
    关联：
        :func:`app.tools.contract_tools.save_review_result`。
    """
    return {
        "ok": True,
        "data": contract_tools.save_review_result(
            case_id, overall_risk_level, summary_text, focus_points_json, comment_text
        ),
    }


@app.post("/api/tools/write-comment", tags=["工具接口"])
def api_write_comment(instance_id: str, review_id: int) -> dict:
    """工具：回写审批评论。

    参数：
        instance_id: 审批单业务唯一标识。
        review_id: 结果主键。
    返回：
        ``{"ok": true, "data": {...}}``。
    关联：
        :func:`app.tools.contract_tools.write_approval_comment`。
    """
    return {
        "ok": True,
        "data": contract_tools.write_approval_comment(instance_id, review_id),
    }


@app.post("/api/tools/run-pipeline", tags=["工具接口"])
def api_run_pipeline(case_id: int) -> dict:
    """工具：一键执行完整流水线（不含回写）。

    参数：
        case_id: 任务主键。
    返回：
        ``{"ok": true, "data": {...}}``。
    关联：
        :func:`app.tools.contract_tools.run_full_pipeline`；
        这是便捷编排接口，不属于需求 2.4.10 的 7 个必需工具。
    """
    return {"ok": True, "data": contract_tools.run_full_pipeline(case_id)}


@app.get("/api/tools/snapshot/{case_id}", tags=["工具接口"])
def api_snapshot(case_id: int) -> dict:
    """工具：查询任务全量快照。

    参数：
        case_id: 任务主键。
    返回：
        ``{"ok": true, "data": {...}}``。
    关联：
        :func:`app.tools.contract_tools.get_task_snapshot`。
    """
    return {"ok": True, "data": contract_tools.get_task_snapshot(case_id)}


@app.get("/health", tags=["运维"])
def health() -> dict:
    """健康检查接口。

    返回：
        数据库连接串与审批接入模式，便于确认当前运行配置。
    关联：
        运维与演示前的环境确认使用；不访问外部系统，因此不会因 OA 故障而失败。
    """
    return {
        "ok": True,
        "db_url": settings.db_url,
        "approval_mode": settings.approval_mode,
        "ocr_dpi": settings.ocr_dpi,
    }
