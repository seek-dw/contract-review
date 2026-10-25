"""日志服务：记录并查询审查全链路日志。

职责：
- 向 ``task_logs`` 表追加一条日志（级别 + 阶段 + 中文内容）；
- 按任务回放完整处理时间线，供调用端与演示脚本展示；
- 提供"随业务事务一起写日志"的变体，避免日志与业务操作各自开事务。

非职责：
- 不打印到控制台（演示脚本自己 print）；
- 不做日志归档或清理（本系统为工具服务，数据量小，保留全量更利于追溯）。

设计原因：
需求 2.4.8 要求"日志模块负责记录全链路日志和错误信息"。
关键取舍是**日志必须与业务操作同事务**：如果先提交业务再写日志，
一旦日志写入失败，就会出现"任务状态变了但没有任何记录"的黑箱；
如果日志成功但业务回滚，又会出现"日志说有、数据里没有"的假记录。
因此提供 :func:`append_log_in_session`，由调用方传入当前会话，与业务操作共进退。

主要协作模块：
- 所有 ``services/*`` 在关键节点调用 :func:`append_log_in_session`；
- ``tools/contract_tools.py`` 与 ``web/routes.py`` 调用 :func:`list_task_logs` 展示时间线。
"""

from __future__ import annotations

from app.db.base import session_scope
from app.db.models import TaskLog
from app.domain.enums import LogLevel, LogType


def append_log_in_session(
    db,
    log_type: LogType,
    content: str,
    task_id: int | None = None,
    level: LogLevel = LogLevel.INFO,
) -> TaskLog:
    """在调用方事务内追加一条日志。

    参数：
        db: 调用方的 SQLAlchemy 会话，日志与业务操作共用同一事务。
        log_type: 日志阶段，取值见 :class:`app.domain.enums.LogType`。
        content: 中文日志内容，建议包含关键参数（审批编号、文件名、命中数等）。
        task_id: 关联任务主键；拉取阶段整体失败时可为 ``None``。
        level: 日志级别，默认 ``info``。
    返回：
        新建的 :class:`app.db.models.TaskLog` 实例（未提交，由调用方提交）。
    关联：
        被 ``task_service``、``attachment_service``、``parse_service``、
        ``review_service``、``result_service``、``comment_service`` 调用。
    """
    log = TaskLog(
        task_id=task_id,
        log_level=level.value,
        log_type=log_type.value,
        log_content=content,
    )
    db.add(log)
    return log


def append_log(
    log_type: LogType,
    content: str,
    task_id: int | None = None,
    level: LogLevel = LogLevel.INFO,
) -> None:
    """独立事务追加一条日志。

    参数：
        log_type: 日志阶段。
        content: 中文日志内容。
        task_id: 关联任务主键。
        level: 日志级别。
    返回：
        无。
    关联：
        被无法与业务共事务的场景调用，例如工具层捕获到异常后补记一条失败日志、
        或在事务已回滚后记录"本次操作失败"。
    注意：
        业务代码应优先使用 :func:`append_log_in_session`，本函数只用于
        "业务事务已经结束（提交或回滚）之后仍需留痕"的场景。
    """
    with session_scope() as db:
        append_log_in_session(db, log_type, content, task_id=task_id, level=level)


def list_task_logs(task_id: int) -> list[dict]:
    """查询某任务的完整日志时间线。

    参数：
        task_id: 任务主键。
    返回：
        日志字典列表，按写入顺序（即 ``id`` 升序）排列，每项含
        ``log_level`` / ``log_type`` / ``log_content`` / ``created_at``。
    关联：
        被 ``tools/contract_tools.py`` 与 ``web/routes.py`` 用于展示处理轨迹；
        演示脚本用它在 ``[STEP7]`` 打印全链路证据。
    """
    with session_scope() as db:
        rows = (
            db.query(TaskLog)
            .filter(TaskLog.task_id == task_id)
            .order_by(TaskLog.id.asc())
            .all()
        )
        return [
            {
                "log_level": row.log_level,
                "log_type": row.log_type,
                "log_content": row.log_content,
                "created_at": row.created_at.isoformat(timespec="seconds"),
            }
            for row in rows
        ]


def list_recent_logs(limit: int = 50) -> list[dict]:
    """查询最近的日志（跨任务）。

    参数：
        limit: 最大返回条数。
    返回：
        日志字典列表，按时间倒序，每项额外含 ``task_id``。
    关联：
        被调用端「运行日志」区块与 ``web/routes.py`` 使用，
        便于管理员快速看到最近发生了什么，而不必先打开某个任务。
    """
    with session_scope() as db:
        rows = (
            db.query(TaskLog)
            .order_by(TaskLog.id.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "task_id": row.task_id,
                "log_level": row.log_level,
                "log_type": row.log_type,
                "log_content": row.log_content,
                "created_at": row.created_at.isoformat(timespec="seconds"),
            }
            for row in rows
        ]
