"""回写服务：把审查意见写回审批评论区，并保证幂等。

职责：
- 生成并调用审批网关的 ``write_comment``，把结果写入评论区；
- 维护 ``approval_tasks.write_status`` 状态机：not_written → writing → success/failed；
- 每次尝试都写一条 ``comment_logs``（成功与失败都记）；
- 失败（超时、外部拒绝、接口异常）时把任务置为 blocked 并允许重试。

非职责：
- 不生成评论内容（见 ``result_service.build_comment_text``）；
- 不决定是否应该回写（由调用方按流程决定）。

设计原因：
需求 2.4.4 要求回写状态包含 ``not_written`` / ``writing`` / ``success`` / ``failed``。
这四个状态的存在本身就说明了三个必须解决的问题：

1. **重复回写**。同一份意见重复写进评论区，会让审批人以为有多轮审查。
   解决：``success`` 状态下再次调用直接返回上次结果，不再调用外部系统。
2. **并发回写**。两个请求同时触发回写，可能都通过"未成功"检查然后都写一次。
   解决：用**条件更新**抢占 ``writing`` 状态——
   ``UPDATE ... WHERE write_status != 'success'``，
   只有受影响行数为 1 的请求才能继续，另一个请求拿到 0 行则说明有并发写在进行。
   这依赖数据库的行级原子性，不依赖进程内锁（多进程部署时进程内锁无效）。
3. **失败可重试**。回写失败不能让整个审查白做。
   解决：失败只置 ``failed`` + 任务 ``blocked``，重试目标为 ``reviewing``，
   重试时只需重新回写，不必重新解析。

主要协作模块：
- ``tools/contract_tools.py::write_approval_comment`` 是入口；
- ``result_service.get_result_by_id`` 提供评论内容；
- ``task_service.get_task`` 提供 ``approval_code``。
"""

from __future__ import annotations

from sqlalchemy import update

from app.approval.factory import get_gateway
from app.db.base import session_scope
from app.db.models import ApprovalTask, CommentLog, utc_now
from app.domain.enums import LogLevel, LogType, TaskStatus, WriteStatus
from app.domain.errors import (
    ApprovalGatewayError,
    CommentWriteError,
    NotFoundError,
    TaskStateError,
)
from app.services.log_service import append_log_in_session
from app.services.result_service import get_result_by_id
from app.services.task_service import get_task, mark_blocked


def _claim_write(task_id: int) -> bool:
    """抢占回写权（把状态从"可写"原子地改为 writing）。

    参数：
        task_id: 任务主键。
    返回：
        抢占成功返回 ``True``；返回 ``False`` 表示当前状态为 ``success``
        （已回写过）或 ``writing``（已有并发写正在进行）。
    关联：
        被 :func:`write_comment_for_result` 调用，是幂等与并发控制的核心。
    设计原因：
        用一条 ``UPDATE ... WHERE write_status IN ('not_written','failed')``
        完成"检查 + 占位"，避免"先 SELECT 判断再 UPDATE"的检查-使用竞态。

        可写状态只包含 ``not_written`` 与 ``failed``：
        - ``success`` 不能再写（否则重复刷评论）；
        - ``writing`` 不能再写（否则两个请求都会调外部系统）。
        最初实现只排除了 ``success``，导致并发时第二个请求能覆盖 ``writing``
        并再次写入——这是测试真实抓到的缺陷，不是假想问题。

        返回值不区分"已成功"与"正在写"，由调用方再查一次状态给出准确提示。
    """
    with session_scope() as db:
        result = db.execute(
            update(ApprovalTask)
            .where(
                ApprovalTask.id == task_id,
                ApprovalTask.write_status.in_(
                    [WriteStatus.NOT_WRITTEN.value, WriteStatus.FAILED.value]
                ),
            )
            .values(write_status=WriteStatus.WRITING.value, updated_at=utc_now())
        )
        return result.rowcount == 1


def _current_write_status(task_id: int) -> str:
    """读取任务当前回写状态。

    参数：
        task_id: 任务主键。
    返回：
        回写状态字符串。
    关联：
        被 :func:`write_comment_for_result` 在抢占失败时调用，用于区分
        "已成功"与"并发写入中"两种情形。
    """
    with session_scope() as db:
        task = db.get(ApprovalTask, task_id)
        if task is None:
            raise NotFoundError(f"任务不存在：task_id={task_id}")
        return task.write_status


def _record_comment_log(
    task_id: int,
    review_id: int,
    status: WriteStatus,
    response_text: str,
) -> None:
    """写入一条回写日志。

    参数：
        task_id: 任务主键。
        review_id: 结果主键。
        status: 本次尝试的回写状态。
        response_text: 外部返回原文或错误说明。
    返回：
        无。
    关联：
        被 :func:`write_comment_for_result` 的成功与失败分支调用。
    """
    with session_scope() as db:
        db.add(
            CommentLog(
                task_id=task_id,
                review_result_id=review_id,
                write_status=status.value,
                write_response_text=response_text,
            )
        )


def _existing_success_log(task_id: int) -> dict | None:
    """查询该任务最近一次成功的回写日志。

    参数：
        task_id: 任务主键。
    返回：
        日志字典；无成功记录返回 ``None``。
    关联：
        被 :func:`write_comment_for_result` 在"已回写过"分支调用，
        用于向调用方返回上次的真实结果，而不是伪造一条新结果。
    """
    with session_scope() as db:
        row = (
            db.query(CommentLog)
            .filter(
                CommentLog.task_id == task_id,
                CommentLog.write_status == WriteStatus.SUCCESS.value,
            )
            .order_by(CommentLog.id.desc())
            .first()
        )
        if row is None:
            return None
        return {
            "comment_log_id": row.id,
            "write_status": row.write_status,
            "write_response_text": row.write_response_text,
            "created_at": row.created_at.isoformat(timespec="seconds"),
        }


def write_comment_for_result(task_id: int, review_id: int) -> dict:
    """把指定审查结果写回审批评论区。

    参数：
        task_id: 任务主键。
        review_id: 结果主键（``review_results.id``）。
    返回：
        字典，含 ``task_id``、``approval_code``、``review_id``、``write_status``、
        ``write_response_text``、``already_written``（是否命中幂等短路）。
    关联：
        被 ``tools/contract_tools.py::write_approval_comment`` 调用。
    异常：
        任务或结果不存在抛 :class:`NotFoundError`；
        并发写入抛 :class:`TaskStateError`（明确告诉调用方稍后重试）；
        外部失败抛 :class:`CommentWriteError`，同时任务置 blocked。
    """
    task = get_task(task_id)
    result = get_result_by_id(review_id)

    if result["task_id"] != task_id:
        raise NotFoundError(
            f"审查结果 {review_id} 不属于任务 {task_id}（实际属于 {result['task_id']}）"
        )

    # ---- 幂等与并发控制 ----
    if not _claim_write(task_id):
        status = _current_write_status(task_id)
        if status == WriteStatus.SUCCESS.value:
            # 已经成功回写过：直接返回上次结果，绝不重复写入外部系统。
            existing = _existing_success_log(task_id) or {}
            append_safe_log(
                task_id,
                f"重复回写请求被幂等短路：任务已回写成功，未再次调用审批系统"
                f"（review_id={review_id}）",
            )
            return {
                "task_id": task_id,
                "approval_code": task["approval_code"],
                "review_id": review_id,
                "write_status": WriteStatus.SUCCESS.value,
                "write_response_text": existing.get("write_response_text", ""),
                "already_written": True,
            }
        # 状态是 writing：说明另一个请求正在写，本次拒绝执行以免重复。
        raise TaskStateError(
            f"任务 {task_id} 正在回写中（write_status={status}），请稍后查询结果"
        )

    # ---- 调用外部系统 ----
    gateway = get_gateway()
    try:
        response = gateway.write_comment(task["approval_code"], result["comment_text"])
    except ApprovalGatewayError as exc:
        # 超时/连接失败：状态回退为 failed，任务阻塞，允许人工重试。
        _finish_write_failed(task_id, review_id, f"调用审批系统失败：{exc.message}")
        error = CommentWriteError(
            f"评论回写失败：审批编号 {task['approval_code']}，"
            f"结果 {review_id}，原因：{exc.message}"
        )
        mark_blocked(task_id, error)
        raise error from exc

    if not response.success:
        # 外部系统明确拒绝（非异常）：同样记为失败，但说明是外部拒绝，
        # 这类失败重试通常无意义，需要人工排查权限或流程状态。
        _finish_write_failed(task_id, review_id, f"审批系统拒绝写入：{response.response_text}")
        error = CommentWriteError(
            f"审批系统拒绝写入评论：审批编号 {task['approval_code']}，"
            f"响应：{response.response_text}"
        )
        mark_blocked(task_id, error)
        raise error

    # ---- 成功：置 success 并把任务推进到 done ----
    _finish_write_success(task_id, review_id, response.response_text)
    return {
        "task_id": task_id,
        "approval_code": task["approval_code"],
        "review_id": review_id,
        "write_status": WriteStatus.SUCCESS.value,
        "write_response_text": response.response_text,
        "already_written": False,
    }


def _finish_write_success(task_id: int, review_id: int, response_text: str) -> None:
    """回写成功后的状态收敛。

    参数：
        task_id: 任务主键。
        review_id: 结果主键。
        response_text: 外部响应原文。
    返回：
        无。
    关联：
        被 :func:`write_comment_for_result` 的成功分支调用。
    设计原因：
        回写成功 = 闭环完成，因此任务直接置 ``done``。
        这与"审查完成"（reviewing）是两个里程碑：
        只有意见真正送达审批人，本次任务才算结束。
    """
    with session_scope() as db:
        task = db.get(ApprovalTask, task_id)
        task.write_status = WriteStatus.SUCCESS.value
        task.task_status = TaskStatus.DONE.value
        task.blocked_reason = None
        task.blocked_detail = None
        task.retry_target = None
        task.updated_at = utc_now()
        db.add(
            CommentLog(
                task_id=task_id,
                review_result_id=review_id,
                write_status=WriteStatus.SUCCESS.value,
                write_response_text=response_text,
            )
        )
        append_log_in_session(
            db, LogType.COMMENT,
            f"评论回写成功：review_id={review_id}，响应「{response_text}」，任务置为 done",
            task_id=task_id,
        )


def _finish_write_failed(task_id: int, review_id: int, reason: str) -> None:
    """回写失败后的状态收敛。

    参数：
        task_id: 任务主键。
        review_id: 结果主键。
        reason: 失败原因（中文）。
    返回：
        无。
    关联：
        被 :func:`write_comment_for_result` 的失败分支调用；
        任务阻塞由调用方通过 ``mark_blocked`` 完成。
    说明：
        这里只负责"把回写状态改为 failed 并记日志"，
        不直接改任务状态，因为任务阻塞需要携带异常对象
        （含 ``retry_target``），由调用方统一处理更清晰。
    """
    with session_scope() as db:
        task = db.get(ApprovalTask, task_id)
        if task is not None:
            task.write_status = WriteStatus.FAILED.value
            task.updated_at = utc_now()
        db.add(
            CommentLog(
                task_id=task_id,
                review_result_id=review_id,
                write_status=WriteStatus.FAILED.value,
                write_response_text=reason,
            )
        )
        append_log_in_session(
            db, LogType.COMMENT,
            f"评论回写失败：review_id={review_id}，原因 {reason}",
            task_id=task_id,
            level=LogLevel.ERROR,
        )


def append_safe_log(task_id: int, content: str) -> None:
    """在独立事务中补记一条日志。

    参数：
        task_id: 任务主键。
        content: 日志内容。
    返回：
        无。
    关联：
        被 :func:`write_comment_for_result` 的幂等短路分支调用。
    设计原因：
        幂等短路发生在"抢占失败"之后，此时没有可用的业务事务，
        因此用独立事务记录，确保这次请求也留下痕迹。
    """
    with session_scope() as db:
        append_log_in_session(db, LogType.COMMENT, content, task_id=task_id)


def list_comment_logs(task_id: int) -> list[dict]:
    """查询任务的全部回写尝试记录。

    参数：
        task_id: 任务主键。
    返回：
        日志字典列表，按主键升序（时间正序），含状态、响应原文、时间。
    关联：
        被调用端「结果处理模块」展示回写历史，
        演示脚本用它证明"重复回写没有产生第二条记录"。
    """
    with session_scope() as db:
        rows = (
            db.query(CommentLog)
            .filter(CommentLog.task_id == task_id)
            .order_by(CommentLog.id.asc())
            .all()
        )
        return [
            {
                "comment_log_id": row.id,
                "review_result_id": row.review_result_id,
                "write_status": row.write_status,
                "write_response_text": row.write_response_text,
                "created_at": row.created_at.isoformat(timespec="seconds"),
            }
            for row in rows
        ]


def write_status_of(task_id: int) -> str:
    """查询任务当前回写状态。

    参数：
        task_id: 任务主键。
    返回：
        回写状态字符串。
    关联：
        被 ``web/routes.py`` 与 ``tools/contract_tools.py`` 用于状态展示。
    """
    return _current_write_status(task_id)
