"""领域异常模块：定义可区分的业务失败类型。

职责：
- 把"失败原因"变成有类型的异常，让服务层能据此决定任务进入 ``blocked`` 的原因与重试目标；
- 每个异常携带 ``reason``（给机器判断）与 ``message``（给人看的中文说明）。

非职责：
- 不做日志记录（由 ``services/log_service.py`` 统一记录）；
- 不携带 HTTP 状态码（由 ``web/routes.py`` 与 ``tools/contract_tools.py`` 决定对外表现）。

设计原因：需求 2.4.4 要求"附件缺失、图片无法识别、文档内容为空、接口调用失败都要进入
blocked 状态"。如果只用 ``Exception``，服务层无法区分该写哪种 ``blocked_reason``、
重试时该回到 ``parsing`` 还是 ``reviewing``。因此为每种阻塞原因定义一个异常类。

主要协作模块：
- ``approval/*`` 抛 :class:`ApprovalGatewayError`；
- ``services/attachment_service.py`` 抛 :class:`AttachmentMissingError`；
- ``services/parse_service.py`` 抛 :class:`OcrUnavailableError` / :class:`DocumentEmptyError`；
- ``services/task_service.py`` 捕获它们并调用 ``mark_blocked``。
"""

from __future__ import annotations

from app.domain.enums import TaskStatus


class ContractReviewError(Exception):
    """本系统所有业务异常的基类。

    用途：让服务层用一个 ``except ContractReviewError`` 捕获全部预期内失败，
    同时把 ``RuntimeError`` 等编程错误暴露出来，不被静默吞掉。
    """

    # 阻塞原因的机器可读标识，写入 ``approval_tasks.blocked_reason``。
    reason: str = "unknown"
    # 重试时任务应回到的状态；None 表示按失败阶段由调用方决定。
    retry_target: TaskStatus | None = None

    def __init__(self, message: str) -> None:
        """初始化异常。

        参数：
            message: 面向人的中文失败说明，会原样写入日志与 ``blocked_reason`` 明细。
        关联：
            由各子类在具体失败点抛出，由 ``services/task_service.py::mark_blocked`` 消费。
        """
        super().__init__(message)
        self.message = message


class ApprovalGatewayError(ContractReviewError):
    """审批系统接口调用失败（超时、连接失败、返回非预期结构）。

    用途：把外部系统故障与本地逻辑缺陷区分开——前者可重试，后者需要改代码。
    重试目标：由抛出方指定，拉取阶段失败不改变任务状态，下载阶段失败回到 ``parsing``。
    """

    reason = "approval_gateway_error"

    def __init__(self, message: str, retry_target: TaskStatus | None = None) -> None:
        """初始化异常并允许指定重试目标。

        参数：
            message: 失败说明，建议包含接口名与关键参数。
            retry_target: 重试后应回到的任务状态；下载失败传 ``TaskStatus.PARSING``。
        关联：
            由 ``approval/mock_gateway.py``、``approval/http_gateway.py`` 抛出。
        """
        super().__init__(message)
        self.retry_target = retry_target


class AttachmentMissingError(ContractReviewError):
    """附件缺失或下载失败。

    用途：对应需求 2.4.4「附件缺失」进入 blocked 的判定。
    重试目标：``parsing``——补齐附件后需要重新解析。
    """

    reason = "attachment_missing"
    retry_target = TaskStatus.PARSING


class OcrUnavailableError(ContractReviewError):
    """扫描件无法识别（OCR 引擎异常或识别结果不可用）。

    用途：对应需求 2.4.4「图片无法识别」进入 blocked 的判定。
    重试目标：``parsing``——可能是渲染分辨率或文件质量问题，人工换件后可重试。
    """

    reason = "ocr_unavailable"
    retry_target = TaskStatus.PARSING


class DocumentEmptyError(ContractReviewError):
    """文档内容为空（无文本层且 OCR 无有效输出，或文件本身无内容）。

    用途：对应需求 2.4.4「文档内容为空」进入 blocked 的判定。
    重试目标：``parsing``——人工确认文件后重试。
    """

    reason = "document_empty"
    retry_target = TaskStatus.PARSING


class UnsupportedDocumentError(ContractReviewError):
    """文档格式不受支持（如 .xlsx、.zip 合同包）。

    用途：明确区分"不支持"与"解析失败"，避免把格式问题误报为 OCR 问题。
    重试目标：``parsing``——人工转换格式后可重试。
    """

    reason = "unsupported_document"
    retry_target = TaskStatus.PARSING


class CommentWriteError(ContractReviewError):
    """审批评论回写失败（超时、外部拒绝、返回非预期结构）。

    用途：对应需求 2.4.4 中回写状态 ``failed``，任务进入 blocked 等待人工重试。
    重试目标：``reviewing``——结果已存在，重试只需重新回写，不必重新解析。
    """

    reason = "comment_write_failed"
    retry_target = TaskStatus.REVIEWING


class TaskStateError(ContractReviewError):
    """任务状态不满足当前操作前提（如对非 blocked 任务执行重试）。

    用途：把状态机约束的违反显式化，而不是静默返回成功。
    重试目标：None——这是调用方的用法错误，不是任务本身受阻。
    """

    reason = "invalid_task_state"


class NotFoundError(ContractReviewError):
    """查询的实体不存在（任务、附件、解析记录、结果）。

    用途：让工具层能返回明确的"未找到"，而不是空对象。
    重试目标：None。
    """

    reason = "not_found"
