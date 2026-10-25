"""任务服务：审批任务的拉取、去重、状态流转、阻塞与重试。

职责：
- 从审批系统拉取待办，按 ``approval_code`` 去重保存（需求 2.4.4）；
- 维护任务状态机：pending → parsing → reviewing → done，异常进入 blocked；
- 记录阻塞原因与重试目标，支持人工重试（需求 2.4.4）；
- 维护附件记录（与审批系统给出的附件清单对齐）。

非职责：
- 不下载附件内容（见 ``attachment_service``）；
- 不解析文档（见 ``parse_service``）；
- 不执行规则（见 ``review_service``）。

设计原因：
需求 2.4.4 有两条硬约束，决定了本模块的形态：
1. **"重复拉取时只更新已有记录，不重复创建新任务"**
   → 去重必须按业务唯一标识而非本地主键；且更新时**不能覆盖任务状态**，
   否则一次重复拉取会把已完成的审查任务重置回 pending，丢失全部进度。
2. **"blocked 状态允许人工重试，重试后重新进入 parsing 或 reviewing"**
   → 阻塞时必须记下"重试该回到哪个状态"，否则重试逻辑只能靠猜。
   本模块用 ``retry_target`` 字段显式记录该意图。

状态机的合法迁移（在本模块集中校验，避免各调用方各自判断）：

```
pending   → parsing            （开始解析）
parsing   → reviewing          （解析成功）
parsing   → blocked            （附件缺失 / OCR 失败 / 内容为空）
reviewing → done               （结果保存且回写成功）
reviewing → blocked            （回写失败）
blocked   → parsing | reviewing（人工重试）
```

主要协作模块：
- ``tools/contract_tools.py`` 调用 :func:`pull_pending_tasks`、
  :func:`refresh_task_detail`、:func:`retry_blocked_task`；
- ``parse_service``、``review_service``、``comment_service`` 调用
  :func:`mark_status` / :func:`mark_blocked`。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.approval.factory import get_gateway
from app.approval.gateway import ApprovalDetail
from app.db.base import session_scope
from app.db.models import ApprovalAttachment, ApprovalTask, utc_now
from app.domain.enums import DownloadStatus, LogLevel, LogType, TaskStatus, WriteStatus
from app.domain.errors import ContractReviewError, NotFoundError, TaskStateError
from app.services.log_service import append_log_in_session


def _get_task_or_raise(db: Session, task_id: int) -> ApprovalTask:
    """按主键取任务，不存在时抛业务异常。

    参数：
        db: 当前会话。
        task_id: 任务主键。
    返回：
        :class:`app.db.models.ApprovalTask` 实例。
    关联：
        被本模块所有需要任务对象的方法调用，统一"未找到"的异常语义。
    """
    task = db.get(ApprovalTask, task_id)
    if task is None:
        raise NotFoundError(f"任务不存在：task_id={task_id}")
    return task


def _get_task_by_code_or_raise(db: Session, approval_code: str) -> ApprovalTask:
    """按审批编号取任务，不存在时抛业务异常。

    参数：
        db: 当前会话。
        approval_code: 审批单业务唯一标识。
    返回：
        任务实例。
    关联：
        被 :func:`refresh_task_detail` 与工具层使用。
    """
    task = (
        db.query(ApprovalTask)
        .filter(ApprovalTask.approval_code == approval_code)
        .one_or_none()
    )
    if task is None:
        raise NotFoundError(f"审批单尚未拉取到本地：approval_code={approval_code}")
    return task


def _sync_attachment_records(
    db: Session, task: ApprovalTask, detail: ApprovalDetail
) -> None:
    """把审批系统的附件清单同步为本地附件记录。

    参数：
        db: 当前会话。
        task: 所属任务。
        detail: 审批单详情（含附件清单）。
    返回：
        无。
    关联：
        被 :func:`pull_pending_tasks` 与 :func:`refresh_task_detail` 调用；
        后续由 ``attachment_service`` 按这些记录下载文件。
    设计原因：
        需求 2.4.4 要求"附件缺失"进入 blocked。要做到这点，
        必须在下载**之前**就为每个外部附件建一条本地记录（状态 pending），
        否则附件根本没下载时，系统连"缺了哪个附件"都不知道。
        同步采用"按外部附件编号 upsert"：已存在则保留本地下载状态，
        不存在则新增；外部已删除的附件不主动删除本地记录（保留审计痕迹）。
    """
    existing = {
        att.external_attachment_id: att for att in task.attachments
    }
    for info in detail.attachments:
        record = existing.get(info.external_attachment_id)
        if record is None:
            db.add(
                ApprovalAttachment(
                    task_id=task.id,
                    external_attachment_id=info.external_attachment_id,
                    file_name=info.file_name,
                    file_type=_suffix_of(info.file_name),
                    file_size=info.file_size,
                    download_status=DownloadStatus.PENDING.value,
                )
            )
        else:
            # 只更新外部可变字段；本地已下载的 file_path / checksum 不能被覆盖，
            # 否则重复拉取会让已解析的文件"消失"。
            record.file_name = info.file_name or record.file_name
            record.file_type = _suffix_of(record.file_name)
            if info.file_size:
                record.file_size = info.file_size

    # 附件数量以同步后的本地记录为准，保证待办列表展示与实际记录一致。
    task.attachment_count = len(detail.attachments)


def _suffix_of(file_name: str) -> str:
    """取文件扩展名（小写，不含点号）。

    参数：
        file_name: 文件名。
    返回：
        扩展名；无扩展名时返回空串。
    关联：
        被 :func:`_sync_attachment_records` 调用，写入 ``file_type`` 供解析器分派。
    """
    if "." not in file_name:
        return ""
    return file_name.rsplit(".", 1)[-1].lower()


def pull_pending_tasks(limit: int = 20) -> dict:
    """拉取待处理审批单并按业务唯一标识去重保存。

    参数：
        limit: 单次最大拉取条数，必须为正整数。
    返回：
        统计字典：``created``（新建数）、``updated``（更新数）、``skipped``（跳过数）、
        ``total``（本次拉取总数）、``items``（逐条明细，供演示展示去重证据）。
    关联：
        被 ``tools/contract_tools.py::list_pending_contract_approvals`` 调用。
    异常：
        审批系统不可用时抛 :class:`app.domain.errors.ApprovalGatewayError`，
        此时本地不产生任何新任务，已有任务状态不变。
    """
    gateway = get_gateway()
    pending = gateway.list_pending(limit)

    created = 0
    updated = 0
    skipped = 0
    items: list[dict] = []

    with session_scope() as db:
        for summary in pending:
            task = (
                db.query(ApprovalTask)
                .filter(ApprovalTask.approval_code == summary.approval_code)
                .one_or_none()
            )

            if task is None:
                # 新建任务：只写外部给出的元信息，状态从 pending 开始。
                task = ApprovalTask(
                    approval_code=summary.approval_code,
                    approval_title=summary.approval_title,
                    applicant_name=summary.applicant_name,
                    applied_at=summary.applied_at,
                    attachment_count=summary.attachment_count,
                    task_status=TaskStatus.PENDING.value,
                    write_status=WriteStatus.NOT_WRITTEN.value,
                )
                db.add(task)
                db.flush()  # 取得自增 id，后续同步附件需要 task.id
                created += 1
                action = "created"
                append_log_in_session(
                    db, LogType.PULL,
                    f"新建任务：审批编号 {summary.approval_code}，标题「{summary.approval_title}」，"
                    f"申请人 {summary.applicant_name}，附件 {summary.attachment_count} 个",
                    task_id=task.id,
                )
            else:
                # 已存在：只更新外部可能变化的展示字段。
                # 关键：绝不覆盖 task_status / write_status ——
                # 重复拉取不能把已完成的审查打回起点，否则会丢进度、重复回写。
                task.approval_title = summary.approval_title or task.approval_title
                task.applicant_name = summary.applicant_name or task.applicant_name
                if summary.applied_at is not None:
                    task.applied_at = summary.applied_at
                task.updated_at = utc_now()
                updated += 1
                action = "updated"
                append_log_in_session(
                    db, LogType.PULL,
                    f"更新已存在任务（去重命中）：审批编号 {summary.approval_code}，"
                    f"当前状态 {task.task_status}，回写状态 {task.write_status}",
                    task_id=task.id,
                )

            # 同步附件记录。详情接口失败不应让整批拉取失败：
            # 该任务保持 pending，附件记录留待后续刷新，其余任务照常处理。
            try:
                detail = gateway.get_approval(summary.approval_code)
                _sync_attachment_records(db, task, detail)
            except ContractReviewError as exc:
                skipped += 1
                append_log_in_session(
                    db, LogType.PULL,
                    f"拉取审批详情失败，附件记录未同步：审批编号 {summary.approval_code}，"
                    f"原因 {exc.message}",
                    task_id=task.id,
                    level=LogLevel.WARNING,
                )
                items.append(
                    {
                        "approval_code": summary.approval_code,
                        "approval_title": task.approval_title,
                        "applicant_name": task.applicant_name,
                        "applied_at": _iso(task.applied_at),
                        "attachment_count": task.attachment_count,
                        "action": "detail_failed",
                        "task_status": task.task_status,
                    }
                )
                continue

            items.append(
                {
                    "approval_code": summary.approval_code,
                    "approval_title": task.approval_title,
                    "applicant_name": task.applicant_name,
                    "applied_at": _iso(task.applied_at),
                    "attachment_count": task.attachment_count,
                    "action": action,
                    "task_status": task.task_status,
                }
            )

    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "total": len(pending),
        "items": items,
    }


def _iso(value: datetime | None) -> str | None:
    """把可选时间格式化为 ISO 字符串。

    参数：
        value: 时间对象或 ``None``。
    返回：
        ISO 字符串或 ``None``。
    关联：
        被 :func:`pull_pending_tasks` 与 :func:`list_tasks` 用于构造返回字典。
    """
    return value.isoformat(timespec="seconds") if value else None


def refresh_task_detail(approval_code: str) -> dict:
    """刷新单个审批单的详情与附件清单。

    参数：
        approval_code: 审批单业务唯一标识。
    返回：
        详情字典，含审批信息、表单数据、附件信息、当前处理状态（需求 2.4.3）。
    关联：
        被 ``tools/contract_tools.py::get_contract_approval`` 调用；
        附件信息取自本地记录（含下载状态），表单数据取自外部系统。
    """
    gateway = get_gateway()
    detail = gateway.get_approval(approval_code)

    with session_scope() as db:
        task = _get_task_by_code_or_raise(db, approval_code)
        _sync_attachment_records(db, task, detail)
        db.flush()

        attachments = [
            {
                "document_id": att.id,
                "external_attachment_id": att.external_attachment_id,
                "file_name": att.file_name,
                "file_type": att.file_type,
                "file_size": att.file_size,
                "download_status": att.download_status,
                "file_path": att.file_path,
                "checksum": att.checksum,
                "download_error": att.download_error,
            }
            for att in task.attachments
        ]
        return {
            "task_id": task.id,
            "approval_code": task.approval_code,
            "approval_title": task.approval_title,
            "applicant_name": task.applicant_name,
            "applied_at": _iso(task.applied_at),
            "approval_status": detail.approval_status,
            "task_status": task.task_status,
            "write_status": task.write_status,
            "blocked_reason": task.blocked_reason,
            "blocked_detail": task.blocked_detail,
            "form_data": detail.form_data,
            "attachments": attachments,
        }


def mark_status(task_id: int, status: TaskStatus, step_note: str = "") -> None:
    """更新任务状态并记录日志。

    参数：
        task_id: 任务主键。
        status: 目标状态。
        step_note: 中文说明，写入日志便于回放。
    返回：
        无。
    关联：
        被 ``parse_service``、``review_service``、``comment_service`` 调用。
    说明：
        本函数不校验迁移合法性——调用方处于明确的流程节点，
        额外校验只会产生"合法流程被误拦"的风险。合法性由各服务的流程保证。
        但当目标状态与当前状态相同时不写日志：多附件解析会在每个附件前后
        各调一次本函数，若不去重，日志里会堆满 ``reviewing → reviewing``
        这类无信息量的记录，反而淹没真正的状态变化。
    """
    with session_scope() as db:
        task = _get_task_or_raise(db, task_id)
        previous = task.task_status
        if previous == status.value:
            return
        task.task_status = status.value
        task.updated_at = utc_now()
        append_log_in_session(
            db, LogType.REVIEW if status == TaskStatus.REVIEWING else LogType.PARSE,
            f"任务状态 {previous} → {status.value}" + (f"：{step_note}" if step_note else ""),
            task_id=task_id,
        )


def mark_blocked(task_id: int, error: ContractReviewError) -> None:
    """把任务置为阻塞状态，记录原因与重试目标。

    参数：
        task_id: 任务主键。
        error: 触发阻塞的业务异常，携带 ``reason`` 与 ``retry_target``。
    返回：
        无。
    关联：
        被 ``parse_service``、``attachment_service``、``comment_service`` 的
        异常处理分支调用；需求 2.4.4 要求四类失败都进入 blocked。
    设计原因：
        阻塞原因（``blocked_reason``）是机器可读代码，供重试逻辑与统计使用；
        阻塞明细（``blocked_detail``）是中文说明，供人工排查。
        两者分开存，避免为了给人看而污染机器判断依据。
    """
    with session_scope() as db:
        task = _get_task_or_raise(db, task_id)
        previous = task.task_status
        task.task_status = TaskStatus.BLOCKED.value
        task.blocked_reason = error.reason
        task.blocked_detail = error.message
        # 记录重试目标：解析类失败回 parsing，回写类失败回 reviewing。
        task.retry_target = (
            error.retry_target.value if error.retry_target else TaskStatus.PARSING.value
        )
        task.updated_at = utc_now()
        append_log_in_session(
            db, LogType.PARSE,
            f"任务进入阻塞：{previous} → blocked，原因代码 {error.reason}，"
            f"明细「{error.message}」，重试目标 {task.retry_target}",
            task_id=task_id,
            level=LogLevel.ERROR,
        )


def retry_blocked_task(task_id: int) -> dict:
    """人工重试阻塞任务。

    参数：
        task_id: 任务主键。
    返回：
        字典，含 ``task_id``、``previous_reason``、``retry_target``、``task_status``。
    关联：
        被 ``tools/contract_tools.py`` 的重试入口与调用端「重试」按钮调用。
    异常：
        任务不存在抛 :class:`NotFoundError`；
        任务当前不是 ``blocked`` 抛 :class:`TaskStateError`
        （需求 2.4.4 只允许 blocked 任务重试，这是业务约束，必须显式拒绝）。
    说明：
        本函数只负责"把状态放回可执行位置并清空阻塞标记"，
        真正的重新解析/重新回写由调用方按 ``retry_target`` 决定调用哪个工具。
        这样重试入口与执行逻辑解耦，便于调用端按需选择。
    """
    with session_scope() as db:
        task = _get_task_or_raise(db, task_id)

        if task.task_status != TaskStatus.BLOCKED.value:
            raise TaskStateError(
                f"只有 blocked 状态的任务允许重试，当前状态为 {task.task_status}"
            )

        previous_reason = task.blocked_reason or "unknown"
        # 重试目标在阻塞时已记录；缺失时按"重新解析"处理，这是最保守的选择。
        target = task.retry_target or TaskStatus.PARSING.value

        task.task_status = target
        task.blocked_reason = None
        task.blocked_detail = None
        task.retry_target = None
        # 回写失败的重试需要允许再次写入，因此把回写状态复位。
        if target == TaskStatus.REVIEWING.value:
            task.write_status = WriteStatus.NOT_WRITTEN.value
        task.updated_at = utc_now()

        append_log_in_session(
            db, LogType.RETRY,
            f"人工重试：blocked → {target}，原阻塞原因 {previous_reason}",
            task_id=task_id,
        )

        return {
            "task_id": task.id,
            "approval_code": task.approval_code,
            "previous_reason": previous_reason,
            "retry_target": target,
            "task_status": task.task_status,
        }


def list_tasks(
    status: str | None = None, limit: int = 100
) -> list[dict]:
    """查询任务列表。

    参数：
        status: 可选状态过滤（见 :class:`TaskStatus`）；``None`` 表示全部。
        limit: 最大返回条数。
    返回：
        任务字典列表，按创建时间倒序。
    关联：
        被 ``web/routes.py`` 的待办列表页与 ``tools/contract_tools.py`` 使用。
    """
    with session_scope() as db:
        query = db.query(ApprovalTask)
        if status:
            query = query.filter(ApprovalTask.task_status == status)
        rows = query.order_by(ApprovalTask.id.desc()).limit(limit).all()
        return [
            {
                "task_id": row.id,
                "approval_code": row.approval_code,
                "approval_title": row.approval_title,
                "applicant_name": row.applicant_name,
                "applied_at": _iso(row.applied_at),
                "attachment_count": row.attachment_count,
                "task_status": row.task_status,
                "write_status": row.write_status,
                "blocked_reason": row.blocked_reason,
                "blocked_detail": row.blocked_detail,
                "created_at": _iso(row.created_at),
                "updated_at": _iso(row.updated_at),
            }
            for row in rows
        ]


def get_task(task_id: int) -> dict:
    """查询单个任务的基本信息。

    参数：
        task_id: 任务主键。
    返回：
        任务字典。
    关联：
        被 ``parse_service``、``review_service`` 与工具层用于获取
        ``approval_code``（下载附件、回写评论都需要它）。
    异常：
        任务不存在抛 :class:`NotFoundError`。
    """
    with session_scope() as db:
        task = _get_task_or_raise(db, task_id)
        return {
            "task_id": task.id,
            "approval_code": task.approval_code,
            "approval_title": task.approval_title,
            "applicant_name": task.applicant_name,
            "task_status": task.task_status,
            "write_status": task.write_status,
            "blocked_reason": task.blocked_reason,
            "blocked_detail": task.blocked_detail,
            "retry_target": task.retry_target,
        }
