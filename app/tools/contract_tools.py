"""合同审查工具：需求 2.4.10 规定的 7 个对外工具函数。

职责：
- 提供 7 个稳定入口，构成完整的"拉取 → 详情 → 下载 → 解析 → 审查 → 保存 → 回写"闭环；
- 每个函数返回 JSON 可序列化的字典，可直接被 HTTP 层、Agent 工具调用或脚本使用；
- 在此层做**入参边界校验**（类型、范围、必填），业务约束由服务层负责。

非职责：
- 不实现业务逻辑（全部委托给 ``app.services.*``）；
- 不决定 HTTP 状态码（见 ``app.web.routes``）。

7 个工具与需求原文的对应关系：

| 需求原文签名 | 本模块函数 |
| --- | --- |
| ``list_pending_contract_approvals(limit)`` | :func:`list_pending_contract_approvals` |
| ``get_contract_approval(instance_id)`` | :func:`get_contract_approval` |
| ``download_contract_attachment(instance_id, attachment_id, file_name)`` | :func:`download_contract_attachment` |
| ``parse_contract_document(document_id)`` | :func:`parse_contract_document` |
| ``run_contract_rules(case_id)`` | :func:`run_contract_rules` |
| ``save_review_result(case_id, overall_risk_level, summary_text, focus_points_json, comment_text)`` | :func:`save_review_result` |
| ``write_approval_comment(instance_id, review_id)`` | :func:`write_approval_comment` |

ID 语义约定（避免同一实体多套标识）：
- ``instance_id``：审批单业务唯一标识（``approval_code``）；
- ``case_id``：本地任务主键（``approval_tasks.id``）；
- ``document_id``：本地附件主键（``approval_attachments.id``）；
- ``review_id``：本地结果主键（``review_results.id``）。

主要协作模块：
- ``app.web.routes`` 把 HTTP 请求转发到本模块；
- ``scripts/demo_flow.py`` 按顺序调用本模块的 7 个函数完成闭环演示；
- ``tests/`` 直接调用本模块验证各分支。
"""

from __future__ import annotations

import json

from app.domain.enums import RiskLevel
from app.domain.errors import NotFoundError, TaskStateError
from app.services import (
    attachment_service,
    comment_service,
    log_service,
    parse_service,
    result_service,
    review_service,
    task_service,
)


def list_pending_contract_approvals(limit: int = 20) -> dict:
    """拉取待处理审批单列表并按业务唯一标识去重保存。

    参数：
        limit: 最大拉取条数，取值 1—200。超出范围时抛 ``ValueError``。
    返回：
        字典，含 ``created``/``updated``/``skipped``/``total`` 与 ``items`` 明细。
        ``items`` 每项含审批编号、标题、申请人、申请时间、附件数量、本次动作、
        当前任务状态（覆盖需求 2.4.3 要求的全部字段）。
    关联：
        调用 ``services.task_service.pull_pending_tasks``；
        被 ``web.routes`` 的"拉取待办"按钮与演示脚本第 1 步调用。
    异常：
        审批系统不可用抛 :class:`app.domain.errors.ApprovalGatewayError`。
    """
    if not isinstance(limit, int) or limit < 1 or limit > 200:
        raise ValueError(f"limit 必须是 1—200 的整数，当前为 {limit!r}")
    return task_service.pull_pending_tasks(limit)


def get_contract_approval(instance_id: str) -> dict:
    """查询单个审批单详情。

    参数：
        instance_id: 审批单业务唯一标识（``approval_code``）。
    返回：
        字典，含审批信息、表单数据、附件信息、当前处理状态（需求 2.4.3 四部分）。
    关联：
        调用 ``services.task_service.refresh_task_detail``；
        被演示脚本第 2 步与调用端「详情查看模块」使用。
    异常：
        审批单在本地不存在抛 :class:`NotFoundError`；
        审批系统不可用抛 :class:`ApprovalGatewayError`。
    """
    if not instance_id or not str(instance_id).strip():
        raise ValueError("instance_id 不能为空")
    return task_service.refresh_task_detail(str(instance_id).strip())


def download_contract_attachment(
    instance_id: str, attachment_id: str, file_name: str = ""
) -> dict:
    """下载合同附件并返回本地路径与校验信息。

    参数：
        instance_id: 审批单业务唯一标识。
        attachment_id: 外部附件编号（审批系统给出的 ``external_attachment_id``）。
        file_name: 期望文件名；可为空，此时使用审批系统记录的文件名。
    返回：
        字典，含 ``document_id``、``local_path``、``file_name``、``file_size``、
        ``checksum``、``download_status``（需求 2.4.3 要求的路径与校验信息）。
    关联：
        内部先把 ``instance_id`` + ``attachment_id`` 解析为本地 ``document_id``，
        再调用 ``services.attachment_service.download_task_attachment``；
        被演示脚本第 3 步调用。
    异常：
        审批单或附件记录不存在抛 :class:`NotFoundError`；
        外部文件取不到抛 :class:`AttachmentMissingError`（任务进入 blocked）。
    设计原因：
        需求签名用的是外部附件编号，而服务层按本地主键工作。
        这个映射必须在这里做，因为只有工具层同时知道两套标识。
    """
    if not instance_id or not str(instance_id).strip():
        raise ValueError("instance_id 不能为空")
    if not attachment_id or not str(attachment_id).strip():
        raise ValueError("attachment_id 不能为空")

    detail = task_service.refresh_task_detail(str(instance_id).strip())
    matched = next(
        (
            att
            for att in detail["attachments"]
            if att["external_attachment_id"] == str(attachment_id).strip()
        ),
        None,
    )
    if matched is None:
        raise NotFoundError(
            f"审批单 {instance_id} 下不存在附件 {attachment_id}；"
            f"现有附件：{[att['external_attachment_id'] for att in detail['attachments']]}"
        )

    return attachment_service.download_task_attachment(
        task_id=detail["task_id"],
        document_id=matched["document_id"],
    )


def _task_id_of(instance_id: str) -> int:
    """按审批编号取本地任务主键。

    参数：
        instance_id: 审批单业务唯一标识。
    返回：
        任务主键。
    关联：
        被 :func:`write_approval_comment` 调用——回写工具只需要审批编号与结果主键，
        不需要拉取审批详情，因此单独做一次轻量查询。
    """
    tasks = task_service.list_tasks(limit=500)
    for task in tasks:
        if task["approval_code"] == instance_id:
            return task["task_id"]
    raise NotFoundError(f"审批单尚未拉取到本地：{instance_id}")


def parse_contract_document(document_id: int) -> dict:
    """解析合同文档并返回结构化字段。

    参数：
        document_id: 附件主键（``approval_attachments.id``，由下载工具返回）。
    返回：
        字典，含 ``parse_id``、``parse_status``、``basic_info``（8 项基础信息）、
        ``clauses``（8 类条款）、``meta``（页数、OCR 页、耗时）。
        每个字段含 ``value``/``snippet``/``location``/``status``/``detail``
        （需求 2.4.5 要求的四项）。
    关联：
        调用 ``services.parse_service.parse_contract_document``；
        被演示脚本第 4 步调用。
    异常：
        附件不存在或未下载抛 :class:`NotFoundError` / :class:`AttachmentMissingError`；
        解析失败抛对应解析异常（任务进入 blocked）。
    """
    if not isinstance(document_id, int) or document_id <= 0:
        raise ValueError(f"document_id 必须是正整数，当前为 {document_id!r}")
    return parse_service.parse_contract_document(document_id)


def run_contract_rules(case_id: int) -> dict:
    """执行规则审查并返回命中结果与风险结论。

    参数：
        case_id: 任务主键（``approval_tasks.id``）。
    返回：
        字典，含 ``overall_risk_level``（总风险等级）、``hits``（命中列表，
        每项含规则名称、风险等级、命中证据、证据位置、建议）、
        ``summary_text``（中文摘要）、``focus_points``（审批关注点）、
        ``evaluated_rule_count``（本次执行的规则数）。
    关联：
        调用 ``services.review_service.run_review`` 与 ``list_hits``；
        被演示脚本第 5 步调用。
    异常：
        任务不存在或无解析记录抛 :class:`NotFoundError`。
    """
    if not isinstance(case_id, int) or case_id <= 0:
        raise ValueError(f"case_id 必须是正整数，当前为 {case_id!r}")

    outcome = review_service.run_review(case_id)
    return {
        "case_id": case_id,
        "overall_risk_level": outcome.overall_risk_level.value,
        "evaluated_rule_count": outcome.evaluated_rule_count,
        "hit_count": len(outcome.hits),
        "summary_text": outcome.summary_text,
        "focus_points": list(outcome.focus_points),
        "hits": review_service.list_hits(case_id),
    }


def save_review_result(
    case_id: int,
    overall_risk_level: str,
    summary_text: str,
    focus_points_json: str,
    comment_text: str,
) -> dict:
    """保存审查结果、命中规则与摘要信息。

    参数：
        case_id: 任务主键。
        overall_risk_level: 总风险等级，必须是 ``low``/``medium``/``high`` 之一。
        summary_text: 中文摘要。
        focus_points_json: 关注点 JSON 数组字符串，如 ``'["a","b"]'``。
        comment_text: 最终写入评论区的文本。
    返回：
        字典，含 ``review_id``、``task_id``、``overall_risk_level``。
    关联：
        调用 ``services.result_service.save_review_result``；
        返回的 ``review_id`` 是回写工具的入参；
        被演示脚本第 6 步调用。
    异常：
        等级取值非法抛 ``ValueError``；关注点不是合法 JSON 数组抛 ``ValueError``。
    """
    if not isinstance(case_id, int) or case_id <= 0:
        raise ValueError(f"case_id 必须是正整数，当前为 {case_id!r}")

    try:
        RiskLevel(overall_risk_level)
    except ValueError as exc:
        raise ValueError(
            f"overall_risk_level 必须是 low/medium/high 之一，当前为 {overall_risk_level!r}"
        ) from exc

    try:
        parsed_focus = json.loads(focus_points_json or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(f"focus_points_json 不是合法 JSON：{exc}") from exc
    if not isinstance(parsed_focus, list):
        raise ValueError("focus_points_json 必须是 JSON 数组")

    review_id = result_service.save_review_result(
        task_id=case_id,
        overall_risk_level=overall_risk_level,
        summary_text=summary_text,
        focus_points_json=json.dumps(parsed_focus, ensure_ascii=False),
        comment_text=comment_text,
    )
    return {
        "review_id": review_id,
        "task_id": case_id,
        "overall_risk_level": overall_risk_level,
        "focus_point_count": len(parsed_focus),
    }


def write_approval_comment(instance_id: str, review_id: int) -> dict:
    """把审查意见写回审批评论区。

    参数：
        instance_id: 审批单业务唯一标识。
        review_id: 结果主键（由保存工具返回）。
    返回：
        字典，含 ``write_status``、``write_response_text``、
        ``already_written``（``True`` 表示命中幂等短路，未重复写入）。
    关联：
        调用 ``services.comment_service.write_comment_for_result``；
        被演示脚本第 7 步调用。
    异常：
        审批单或结果不存在抛 :class:`NotFoundError`；
        已有并发写在执行抛 :class:`TaskStateError`；
        外部写入失败抛 :class:`CommentWriteError`（任务进入 blocked）。
    幂等保证：
        同一 ``review_id`` 重复调用时，第二次不会再次写入外部系统，
        而是返回 ``already_written=True`` 与上次的真实响应。
    """
    if not instance_id or not str(instance_id).strip():
        raise ValueError("instance_id 不能为空")
    if not isinstance(review_id, int) or review_id <= 0:
        raise ValueError(f"review_id 必须是正整数，当前为 {review_id!r}")

    task_id = _task_id_of(str(instance_id).strip())
    return comment_service.write_comment_for_result(task_id, review_id)


# ---------------------------------------------------------------------------
# 便捷编排工具（非需求强制，供演示与调用端使用）
# ---------------------------------------------------------------------------


def run_full_pipeline(case_id: int, allow_reprocess: bool = False) -> dict:
    """对单个任务执行"下载 → 解析 → 审查 → 保存"完整流水线（不含回写）。

    参数：
        case_id: 任务主键。
        allow_reprocess: 是否允许对已完成任务重新处理，默认 ``False``。
            已回写成功的任务若需重新审查（例如合同换版），必须显式传 ``True``。
    返回：
        字典，含 ``approval_code``、``attachments``（下载结果）、``parse``（解析结果）、
        ``review``（审查与结果保存）。
    关联：
        被 ``scripts/demo_flow.py`` 与调用端「一键处理」按钮调用。
    设计原因：
        7 个工具是原子能力，实际演示与人工操作都需要"一次跑完"的编排。
        放在工具层而不是服务层，是因为它不属于业务规则，只是调用顺序的组合。
    异常：
        任一步失败即抛出（任务已被对应服务置为 blocked）；
        已完成任务未显式允许重处理时抛 :class:`TaskStateError`。
    """
    task = task_service.get_task(case_id)
    downloaded = attachment_service.download_all_attachments(case_id)
    parsed = parse_service.parse_task_documents(case_id, allow_reprocess=allow_reprocess)
    reviewed = result_service.run_and_save_review(case_id)
    return {
        "task_id": case_id,
        "approval_code": task["approval_code"],
        "attachments": downloaded,
        "parse": parsed,
        "review": reviewed,
    }


def get_task_snapshot(case_id: int) -> dict:
    """汇总一个任务的全量状态，供调用端页面与演示输出使用。

    参数：
        case_id: 任务主键。
    返回：
        字典，含任务基本信息、附件列表、解析摘要、命中列表、审查结果、
        回写日志、处理日志时间线。
    关联：
        被 ``web.routes`` 的详情页与 ``scripts/demo_flow.py`` 的最终汇总调用。
    说明：
        解析摘要、结果、日志在尚未产生时返回 ``None``/空列表，而不是抛异常——
        页面需要能展示"处理到一半"的任务，这是正常的中间状态。
    """
    task = task_service.get_task(case_id)
    attachments = attachment_service.list_attachments(case_id)
    parse_summary = parse_service.parse_status_of(case_id)
    hits = review_service.list_hits(case_id)
    result = result_service.get_review_result(case_id)
    comment_logs = comment_service.list_comment_logs(case_id)
    logs = log_service.list_task_logs(case_id)

    return {
        "task": task,
        "attachments": attachments,
        "parse": parse_summary,
        "hits": hits,
        "result": result,
        "comment_logs": comment_logs,
        "logs": logs,
    }


def retry_task(case_id: int) -> dict:
    """人工重试阻塞任务，并按重试目标自动续跑。

    参数：
        case_id: 任务主键。
    返回：
        字典，含 ``retry``（重试动作结果）与 ``rerun``（续跑结果）。
    关联：
        调用 ``services.task_service.retry_blocked_task``，
        然后按 ``retry_target`` 决定调用 ``run_full_pipeline`` 还是仅重新回写；
        被调用端「重试」按钮与演示脚本第 6 步调用。
    异常：
        任务不是 blocked 状态抛 :class:`TaskStateError`。
    设计原因：
        ``retry_blocked_task`` 只负责把状态放回可执行位置，
        真正的续跑由本函数完成——这样"重试意图"与"重试执行"分离，
        便于调用端只做状态复位（例如人工修好文件后想手动再点解析）。
    """
    retry_info = task_service.retry_blocked_task(case_id)

    if retry_info["retry_target"] == "reviewing":
        # 解析已成功、仅回写失败：重新生成结果并回写，不必重新解析。
        reviewed = result_service.run_and_save_review(case_id)
        written = comment_service.write_comment_for_result(
            case_id, reviewed["review_id"]
        )
        return {"retry": retry_info, "rerun": {"review": reviewed, "comment": written}}

    # 解析类失败：重跑完整流水线。
    rerun = run_full_pipeline(case_id)
    return {"retry": retry_info, "rerun": rerun}
