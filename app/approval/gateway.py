"""审批网关契约模块：定义外部审批系统的接口与传输数据结构。

职责：
- 声明 :class:`ApprovalGateway` 抽象接口，锁定审批系统必须具备的 5 个能力；
- 定义网关与业务层之间的传输对象（待办摘要、审批详情、附件描述、下载结果、回写结果）；
- 规定各实现的共同行为约定（异常类型、超时语义）。

非职责：
- 不实现任何具体协议（HTTP、文件、SDK 调用），实现见 ``mock_gateway`` 与 ``http_gateway``；
- 不落库，不写日志。

设计原因：
- 传输对象用不可变 dataclass 而非 ORM 对象：外部系统的字段与本地表字段并不一一对应
  （例如外部有 ``form_fields`` 而本地不存），用独立结构做一次显式映射，
  避免外部字段变化直接冲击数据库。
- 网关方法只抛 :class:`ApprovalGatewayError`：让调用方只需处理一种外部失败，
  不必关心是超时、连接拒绝还是返回结构错误。

主要协作模块：
- ``services/task_service.py`` 调用 :meth:`list_pending` 与 :meth:`get_approval`；
- ``services/attachment_service.py`` 调用 :meth:`download_attachment`；
- ``services/comment_service.py`` 调用 :meth:`write_comment`；
- ``approval/factory.py`` 负责实例化具体实现。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class PendingApproval:
    """待办审批单摘要。

    用途：待办拉取工具的返回元素，对应需求 2.4.3 要求的字段集合。
    来源：审批系统待办列表接口。
    """

    # 审批单业务唯一标识，去重键。
    approval_code: str
    # 审批单标题（通常为合同名称）。
    approval_title: str
    # 申请人姓名。
    applicant_name: str
    # 申请时间。外部系统可能不返回，此时为 None，调用端展示为「未知」。
    applied_at: datetime | None
    # 附件数量。外部系统直接给出时用它，避免再逐个查详情。
    attachment_count: int


@dataclass(frozen=True)
class AttachmentInfo:
    """审批单附件描述。

    用途：详情接口返回的附件清单元素；下载时用 ``external_attachment_id`` 定位文件。
    """

    # 外部系统内的附件编号，下载与重试都以它为准。
    external_attachment_id: str
    # 文件名（含扩展名），用于推断格式与本地命名。
    file_name: str
    # 文件字节数；外部未提供时为 0，下载后以实际大小为准。
    file_size: int
    # 文件类型标记（外部可能给 ``contract`` / ``attachment``），仅作展示参考，
    # 真正的格式判断使用扩展名，不依赖外部标记。
    file_kind: str = ""


@dataclass(frozen=True)
class ApprovalDetail:
    """审批单详情。

    用途：对应需求 2.4.3「审批详情工具」的返回，包含审批信息、表单数据、附件信息、
    当前处理状态四部分。
    """

    # 审批单业务唯一标识。
    approval_code: str
    # 标题。
    approval_title: str
    # 申请人。
    applicant_name: str
    # 申请时间。
    applied_at: datetime | None
    # 当前处理状态（外部系统的状态原文，如 ``pending`` / ``in_progress``）。
    # 保留原文而不映射为本地枚举：外部状态机与本地任务状态语义不同，强行映射会失真。
    approval_status: str
    # 表单数据：键值对，键为表单字段名，值为字符串。
    form_data: dict[str, str] = field(default_factory=dict)
    # 附件清单。
    attachments: tuple[AttachmentInfo, ...] = ()


@dataclass(frozen=True)
class DownloadedFile:
    """附件下载结果。

    用途：需求 2.4.3 要求下载工具返回"本地文件路径和校验信息"，本结构即该返回。
    """

    # 本地绝对路径。
    local_path: str
    # 文件名。
    file_name: str
    # 字节数。
    file_size: int
    # SHA256 十六进制摘要，用于校验本地副本完整性。
    checksum: str


@dataclass(frozen=True)
class CommentWriteResult:
    """评论回写结果。

    用途：需求 2.4.3 要求回写工具"返回回写结果"，本结构承载成功标志与外部响应原文。
    """

    # 外部系统是否确认写入成功。
    success: bool
    # 外部系统返回的原始响应文本，成功与失败都保留，便于对账。
    response_text: str


class ApprovalGateway(ABC):
    """审批系统网关接口。

    用途：把"外部审批系统"抽象成 5 个稳定动作，使业务层不感知协议细节。
    生命周期：由 ``approval/factory.py`` 在每次工具调用时按配置创建（无状态，可复用）。
    协作对象：``services/*`` 是唯一调用方。

    实现约定：
    - 任何失败（网络、超时、结构不符、业务拒绝）都必须抛
      :class:`app.domain.errors.ApprovalGatewayError`，消息中包含接口名与关键参数；
    - 不得返回半成品对象（例如缺少 ``approval_code`` 的详情），宁可抛错；
    - 不得自行重试。重试策略属于业务层（需求 2.4.4 要求人工触发重试），
      网关内自动重试会让"失败可复现"这一验收点失效。
    """

    @abstractmethod
    def list_pending(self, limit: int) -> list[PendingApproval]:
        """拉取待处理审批单列表。

        参数：
            limit: 最大返回条数，必须为正整数；实现应按外部接口上限截断。
        返回：
            待办摘要列表，按申请时间倒序。
        关联：
            被 ``services/task_service.py::pull_pending_tasks`` 调用。
        """

    @abstractmethod
    def get_approval(self, instance_id: str) -> ApprovalDetail:
        """查询单个审批单详情。

        参数：
            instance_id: 审批单业务唯一标识（即 ``approval_code``）。
        返回：
            审批详情；审批单不存在时抛 :class:`ApprovalGatewayError`。
        关联：
            被 ``services/task_service.py::refresh_task_detail`` 与详情工具调用。
        """

    @abstractmethod
    def download_attachment(
        self, instance_id: str, external_attachment_id: str, file_name: str
    ) -> DownloadedFile:
        """下载合同附件到本地。

        参数：
            instance_id: 审批单业务唯一标识。
            external_attachment_id: 外部附件编号。
            file_name: 期望的文件名，用于推断扩展名与本地命名。
        返回：
            下载结果，含本地路径与 SHA256 校验值。
        关联：
            被 ``services/attachment_service.py::download_task_attachment`` 调用。
        副作用：
            在实现内部把文件写入磁盘（由调用方指定目录或实现内部临时目录，
            最终由附件服务移动到正式目录）。
        """

    @abstractmethod
    def write_comment(self, instance_id: str, comment_text: str) -> CommentWriteResult:
        """把审查意见写入审批单评论区。

        参数：
            instance_id: 审批单业务唯一标识。
            comment_text: 完整评论文本（由 ``services/result_service.py`` 生成）。
        返回：
            回写结果；外部拒绝时 ``success=False`` 并附原因，而非抛异常——
            因为"外部明确拒绝"与"调用失败"在业务上需要分别记录（前者重试也无用）。
        关联：
            被 ``services/comment_service.py::write_comment_for_result`` 调用。
        """

    @abstractmethod
    def get_comments(self, instance_id: str) -> list[str]:
        """读取审批单现有评论。

        参数：
            instance_id: 审批单业务唯一标识。
        返回：
            评论正文列表，按时间正序。
        关联：
            被演示脚本与调用端用于展示"回写后的评论区快照"，
            同时是回写幂等性的验证手段（重复回写后可对比条数）。
        """
