"""调用端路由：实现需求 2.4.7 的 5 个模块。

职责：
- 提供 5 个页面：待办列表、审批详情（含解析结果、规则命中、结果与回写三个区块）；
- 提供页面操作：拉取待办、一键处理、重试、回写评论；
- 把工具层的业务异常映射为合适的 HTTP 状态码与页面提示。

非职责：
- 不实现业务逻辑（全部委托 ``app.tools.contract_tools``）；
- 不做鉴权（需求 2.4 未要求，定位内网演示入口）。

5 个模块与路由的对应关系（需求 2.4.7）：

| 需求模块 | 路由 / 页面区块 |
| --- | --- |
| 待办调用模块 | ``GET /`` 首页待办表格 + ``POST /actions/pull`` |
| 详情查看模块 | ``GET /approvals/{approval_code}`` 顶部「审批与附件」区块 |
| 解析结果模块 | 同一页面的「解析结果」区块 |
| 规则命中模块 | 同一页面的「规则命中」区块 |
| 结果处理模块 | 同一页面的「结果与回写」区块 + ``POST /actions/write-comment`` |

异常映射原则：
- ``NotFoundError`` → 404（资源不存在）
- ``TaskStateError`` → 409（状态冲突，例如对非阻塞任务重试）
- 其他业务异常 → 400（业务规则不满足）
- 未预期异常 → 500，但保留异常类型名，便于排查
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.domain.enums import display_name
from app.domain.errors import NotFoundError, TaskStateError
from app.services import log_service, review_service, task_service
from app.tools import contract_tools

router = APIRouter()

# 模板引擎。目录取自配置，避免硬编码路径。
templates = Jinja2Templates(directory=str(settings.template_dir))

# 注册中文展示名过滤器，供模板把枚举值渲染为中文标签。
# 放在模块级而不是每次渲染时注册，是因为 Jinja2 的过滤器注册是全局且幂等的。
templates.env.filters["display_name"] = display_name


def _render(
    request: Request, template: str, context: dict, status_code: int = 200
) -> HTMLResponse:
    """渲染模板并注入公共上下文。

    参数：
        request: FastAPI 请求对象，Jinja2 必需（Starlette 1.x 起为第一个位置参数）。
        template: 模板文件名。
        context: 页面专属上下文。
        status_code: HTTP 状态码，默认 200；错误页用 404 等真实状态码，
            使浏览器与自动化脚本能正确识别结果，而不是"页面显示错误但状态是 200"。
    返回：
        HTML 响应。
    关联：
        被全部页面路由调用；统一注入 ``settings`` 与风险等级顺序等公共变量，
        避免每个路由重复拼装。
    说明：
        Starlette 1.9 的 ``TemplateResponse`` 签名是
        ``(request, name, context, ...)``，旧式 ``(name, {"request": ...})``
        会把模板名与字典错位，报 ``unhashable type: 'dict'``。
        因此这里显式按新签名传参。
    """
    base_context = {
        "settings": settings,
        "risk_order": {"high": 0, "medium": 1, "low": 2},
    }
    base_context.update(context)
    return templates.TemplateResponse(
        request, template, base_context, status_code=status_code
    )


@router.get("/", response_class=HTMLResponse)
def index(request: Request, status: str | None = None) -> HTMLResponse:
    """待办列表页（需求 2.4.7「待办调用模块」）。

    参数：
        request: 请求对象。
        status: 可选状态过滤参数，来自查询串。
    返回：
        待办列表页面。
    关联：
        调用 ``services.task_service.list_tasks`` 取任务列表，
        ``services.log_service.list_recent_logs`` 取最近日志。
    """
    tasks = task_service.list_tasks(status=status, limit=200)
    recent_logs = log_service.list_recent_logs(limit=20)
    stats = {
        "total": len(tasks),
        "pending": sum(1 for t in tasks if t["task_status"] == "pending"),
        "blocked": sum(1 for t in tasks if t["task_status"] == "blocked"),
        "done": sum(1 for t in tasks if t["task_status"] == "done"),
    }
    return _render(
        request,
        "index.html",
        {
            "tasks": tasks,
            "recent_logs": recent_logs,
            "stats": stats,
            "current_status": status,
        },
    )


@router.post("/actions/pull")
def action_pull(limit: int = Form(20)) -> RedirectResponse:
    """拉取待办（表单提交）。

    参数：
        limit: 拉取条数。
    返回：
        重定向回首页（PRG 模式，避免刷新重复提交）。
    关联：
        调用 ``contract_tools.list_pending_contract_approvals``。
    异常：
        审批系统不可用时返回首页并带错误参数。
    """
    try:
        result = contract_tools.list_pending_contract_approvals(limit=limit)
    except Exception as exc:  # 页面需要展示失败原因，不能直接 500
        return RedirectResponse(
            url=f"/?error={type(exc).__name__}%3A%20{str(exc)[:120]}", status_code=303
        )
    return RedirectResponse(
        url=(
            f"/?pulled=1&created={result['created']}"
            f"&updated={result['updated']}&total={result['total']}"
        ),
        status_code=303,
    )


@router.get("/approvals/{approval_code}", response_class=HTMLResponse)
def approval_detail(request: Request, approval_code: str) -> HTMLResponse:
    """审批详情页，同时承载「详情查看」「解析结果」「规则命中」「结果处理」四个模块。

    参数：
        request: 请求对象。
        approval_code: 审批单业务唯一标识。
    返回：
        详情页面；审批单不存在时返回 404 页面。
    关联：
        调用 ``contract_tools.get_task_snapshot`` 取全量状态，
        ``contract_tools.get_contract_approval`` 取外部详情与附件清单。
    设计原因：
        需求 2.4.7 把调用端拆成 5 个"模块"，但没要求 5 个独立页面。
        解析结果、规则命中、结果回写都依附于同一个审批单，
        放在同一页面用锚点分区块，比 5 个页面来回跳转更适合审查场景。
    """
    try:
        task_id = contract_tools._task_id_of(approval_code)
        snapshot = contract_tools.get_task_snapshot(task_id)
        detail = contract_tools.get_contract_approval(approval_code)
    except NotFoundError as exc:
        return _render(
            request,
            "error.html",
            {"title": "审批单不存在", "message": exc.message},
            status_code=404,
        )

    # 解析字段按"基础信息 / 条款"分组，模板直接遍历即可，避免模板里做逻辑。
    basic_rows = []
    clause_rows = []
    parse = snapshot.get("parse")
    if parse:
        basic_info = parse.get("basic_info") or {}
        clause_info = parse.get("clauses") or {}
        basic_rows = [
            {
                "label": item.get("label", key),
                "value": item.get("value"),
                "status": item.get("status"),
                "location": item.get("location"),
                "snippet": item.get("snippet"),
                "detail": item.get("detail"),
            }
            for key, item in basic_info.items()
        ]
        clause_rows = [
            {
                "label": item.get("label", key),
                "value": item.get("value"),
                "status": item.get("status"),
                "location": item.get("location"),
                "detail": item.get("detail"),
            }
            for key, item in clause_info.items()
        ]

    return _render(
        request,
        "detail.html",
        {
            "approval_code": approval_code,
            "snapshot": snapshot,
            "detail": detail,
            "basic_rows": basic_rows,
            "clause_rows": clause_rows,
            "parse_meta": (parse or {}).get("meta", {}),
        },
    )


@router.post("/actions/run")
def action_run(
    approval_code: str = Form(...), allow_reprocess: str = Form("0")
) -> RedirectResponse:
    """一键处理：下载 → 解析 → 审查 → 保存 → 回写。

    参数：
        approval_code: 审批单业务唯一标识。
        allow_reprocess: ``"1"`` 表示允许对已完成任务重新处理，默认 ``"0"``。
            已完成（``done``）的任务默认会被服务层拒绝重跑，
            页面上通过一个独立的确认按钮传入 ``"1"``，让重跑成为显式动作。
    返回：
        重定向回详情页，携带成功或失败参数。
    关联：
        调用 ``contract_tools.run_full_pipeline`` 与
        ``contract_tools.write_approval_comment``。
    异常：
        业务异常转为 URL 参数展示（页面需要看到阻塞原因，不能只给 500）。
    """
    try:
        task_id = contract_tools._task_id_of(approval_code)
        result = contract_tools.run_full_pipeline(
            task_id, allow_reprocess=allow_reprocess == "1"
        )
        review_id = result["review"]["review_id"]
        written = contract_tools.write_approval_comment(approval_code, review_id)
        return RedirectResponse(
            url=(
                f"/approvals/{approval_code}?ok=1"
                f"&review_id={review_id}"
                f"&write_status={written['write_status']}"
            ),
            status_code=303,
        )
    except Exception as exc:  # noqa: BLE001
        # 页面路由刻意捕获所有异常：表单提交失败时必须把原因回显到页面，
        # 只抛 500 的话用户看不到任何可操作信息。
        # 业务异常取 reason/message；非业务异常退化为类名，同样可见。
        reason = getattr(exc, "reason", type(exc).__name__)
        message = getattr(exc, "message", str(exc))
        return RedirectResponse(
            url=f"/approvals/{approval_code}?error={reason}%3A%20{message[:150]}",
            status_code=303,
        )


@router.post("/actions/retry")
def action_retry(approval_code: str = Form(...)) -> RedirectResponse:
    """重试阻塞任务。

    参数：
        approval_code: 审批单业务唯一标识。
    返回：
        重定向回详情页。
    关联：
        调用 ``contract_tools.retry_task``。
    异常：
        任务非 blocked 状态时把原因带回页面（409 语义，但页面用参数展示更友好）。
    """
    try:
        task_id = contract_tools._task_id_of(approval_code)
        contract_tools.retry_task(task_id)
        return RedirectResponse(
            url=f"/approvals/{approval_code}?retried=1", status_code=303
        )
    except TaskStateError as exc:
        return RedirectResponse(
            url=f"/approvals/{approval_code}?error=state%3A%20{exc.message[:150]}",
            status_code=303,
        )
    except Exception as exc:  # noqa: BLE001
        # 页面路由刻意捕获所有异常：表单提交失败时必须把原因回显到页面，
        # 只抛 500 的话用户看不到任何可操作信息。
        # 业务异常取 reason/message；非业务异常退化为类名，同样可见。
        reason = getattr(exc, "reason", type(exc).__name__)
        message = getattr(exc, "message", str(exc))
        return RedirectResponse(
            url=f"/approvals/{approval_code}?error={reason}%3A%20{message[:150]}",
            status_code=303,
        )


@router.post("/actions/write-comment")
def action_write_comment(
    approval_code: str = Form(...), review_id: int = Form(...)
) -> RedirectResponse:
    """回写评论（需求 2.4.7「结果处理模块」）。

    参数：
        approval_code: 审批单业务唯一标识。
        review_id: 结果主键。
    返回：
        重定向回详情页，携带回写状态。
    关联：
        调用 ``contract_tools.write_approval_comment``；
        幂等由服务层保证，重复点击不会产生多条评论。
    """
    try:
        result = contract_tools.write_approval_comment(approval_code, review_id)
        return RedirectResponse(
            url=(
                f"/approvals/{approval_code}?write_status={result['write_status']}"
                f"&already={int(result['already_written'])}"
            ),
            status_code=303,
        )
    except Exception as exc:  # noqa: BLE001
        # 页面路由刻意捕获所有异常：表单提交失败时必须把原因回显到页面，
        # 只抛 500 的话用户看不到任何可操作信息。
        # 业务异常取 reason/message；非业务异常退化为类名，同样可见。
        reason = getattr(exc, "reason", type(exc).__name__)
        message = getattr(exc, "message", str(exc))
        return RedirectResponse(
            url=f"/approvals/{approval_code}?error={reason}%3A%20{message[:150]}",
            status_code=303,
        )


@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request) -> HTMLResponse:
    """运行日志页。

    参数：
        request: 请求对象。
    返回：
        日志列表页面。
    关联：
        调用 ``services.log_service.list_recent_logs``。
    说明：
        需求 2.4.7 的 5 个模块没有单列"日志模块"，
        但 2.4.8 要求"日志模块负责记录全链路日志"，管理员需要查看入口，
        因此补充此页。
    """
    return _render(
        request, "logs.html", {"logs": log_service.list_recent_logs(limit=300)}
    )


@router.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request) -> HTMLResponse:
    """规则列表页。

    参数：
        request: 请求对象。
    返回：
        规则列表页面。
    关联：
        调用 ``services.review_service.load_enabled_rules``。
    说明：
        需求 2.4.2 中"系统管理员"需要"维护规则"，2.4.6 定义了规则字段。
        本页提供只读查看（编辑能力通过数据库或后续接口扩展），
        目的是让评审能直接看到 26 条规则如何覆盖 11 类主题。
    """
    rules = review_service.load_enabled_rules()
    return _render(request, "rules.html", {"rules": rules})
