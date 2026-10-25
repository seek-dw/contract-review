"""附件服务：合同附件的下载、落盘、校验与元数据维护。

职责：
- 通过审批网关下载附件到本地受控目录，按 ``{approval_code}/`` 分目录存放；
- 计算 SHA256 与文件大小，作为校验信息（需求 2.4.3）；
- 维护 ``approval_attachments`` 的下载状态、路径、校验值与失败原因；
- 下载失败（附件缺失、接口异常）时把任务置为 blocked（需求 2.4.4）。

非职责：
- 不解析文档（见 ``parse_service``）；
- 不决定是否开始审查（见工具层与服务编排）。

设计原因：
本模块有三个必须做对的地方：

1. **路径安全**。文件名来自外部审批系统，是不可信输入。
   若直接拼接，``../../etc/passwd`` 或 ``C:\\Windows\\x.pdf`` 会写到目录外。
   因此 :func:`_safe_target_path` 做三件事：取 basename、过滤危险字符、
   校验最终路径仍在附件根目录内。这是必要的边界校验，不是冗余防御。

2. **失败也要有记录**。需求 2.4.4 要求"附件缺失"进入 blocked。
   若下载失败就直接抛异常，附件记录仍是 ``pending``，人工看不出是哪一步失败。
   因此下载失败时把 ``download_status`` 置为 ``failed`` 并写入 ``download_error``。

3. **重试幂等**。同一附件重复下载时覆盖同名文件、复用同一条记录，
   不新增记录，避免附件表膨胀、避免解析到旧文件。

主要协作模块：
- ``tools/contract_tools.py::download_contract_attachment`` 是入口；
- ``parse_service`` 通过 :func:`get_attachment` 取本地路径；
- 网关来自 ``app.approval.factory.get_gateway``。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from app.approval.factory import get_gateway
from app.config import settings
from app.db.base import session_scope
from app.db.models import ApprovalAttachment, ApprovalTask
from app.domain.enums import DownloadStatus, LogLevel, LogType
from app.domain.errors import (
    ApprovalGatewayError,
    AttachmentMissingError,
    NotFoundError,
)
from app.services.log_service import append_log_in_session
from app.services.task_service import mark_blocked

# 文件名中允许保留的字符。其余字符统一替换为下划线。
# 设计原因：中文文件名要保留（合同名常为中文），但要排除路径分隔符、
# 控制字符与 Windows 保留字符，避免跨平台写入失败或路径穿越。
_SAFE_NAME_PATTERN = re.compile(r"[^\w\u4e00-\u9fff.\-（）()]+")

# 文件名最大长度（字符）。多数文件系统限制 255 字节，中文占 3 字节，
# 取 80 字符留足余量，避免超长文件名导致写入失败。
_MAX_NAME_LENGTH = 80


def _safe_file_name(file_name: str, fallback: str = "attachment") -> str:
    """把外部文件名清洗为安全的本地文件名。

    参数：
        file_name: 外部系统给出的文件名（不可信）。
        fallback: 清洗后为空时使用的兜底名。
    返回：
        只含安全字符的文件名，保留扩展名。
    关联：
        被 :func:`_safe_target_path` 调用。
    处理步骤：
        1. 取 basename（去掉任何目录部分，含 Windows 与 POSIX 分隔符）；
        2. 替换不安全字符为下划线；
        3. 截断过长的主名，但保留扩展名。
    """
    # 同时处理两种分隔符：外部系统可能给出 Windows 路径，运行环境也可能是 Linux。
    base = file_name.replace("\\", "/").split("/")[-1].strip()
    if not base or base in (".", ".."):
        base = fallback

    cleaned = _SAFE_NAME_PATTERN.sub("_", base).strip("._")
    if not cleaned:
        cleaned = fallback

    # 保留扩展名做长度截断，否则截断后可能丢失 .pdf 导致解析器无法分派。
    if len(cleaned) > _MAX_NAME_LENGTH and "." in cleaned:
        stem, suffix = cleaned.rsplit(".", 1)
        cleaned = stem[: _MAX_NAME_LENGTH - len(suffix) - 1] + "." + suffix
    elif len(cleaned) > _MAX_NAME_LENGTH:
        cleaned = cleaned[:_MAX_NAME_LENGTH]
    return cleaned


def _safe_target_path(approval_code: str, attachment_id: int, file_name: str) -> Path:
    """计算附件的本地落盘路径并校验其位于附件根目录内。

    参数：
        approval_code: 审批单业务唯一标识，用作子目录名。
        attachment_id: 本地附件主键，用作文件名前缀，保证同一审批单下不重名。
        file_name: 外部文件名。
    返回：
        安全的绝对路径。
    关联：
        被 :func:`download_task_attachment` 调用。
    异常：
        最终路径不在 ``settings.attachment_dir`` 之下时抛 ``ValueError``。
        这属于安全边界被突破的信号，必须显式失败而不是静默改名。
    """
    # 审批编号同样来自外部，也要清洗：它会被用作目录名。
    safe_code = _safe_file_name(approval_code, fallback="unknown")
    target_dir = (settings.attachment_dir / safe_code).resolve()

    root = settings.attachment_dir.resolve()
    if root not in target_dir.parents and target_dir != root:
        raise ValueError(f"附件目录越界：{target_dir} 不在 {root} 之下")

    target = (target_dir / f"{attachment_id}_{_safe_file_name(file_name)}").resolve()
    if root not in target.parents:
        raise ValueError(f"附件路径越界：{target} 不在 {root} 之下")
    return target


def download_task_attachment(task_id: int, document_id: int) -> dict:
    """下载指定任务的某个附件。

    参数：
        task_id: 任务主键。
        document_id: 附件主键（即工具接口中的 ``document_id``）。
    返回：
        字典，含 ``document_id``、``local_path``、``file_name``、``file_size``、
        ``checksum``、``download_status``（需求 2.4.3 要求的路径与校验信息）。
    关联：
        被 ``tools/contract_tools.py::download_contract_attachment`` 调用；
        下载成功后由 ``parse_service`` 使用 ``local_path`` 解析。
    异常：
        附件或任务不存在抛 :class:`NotFoundError`；
        外部附件取不到抛 :class:`AttachmentMissingError`，
        同时把任务置为 blocked（需求 2.4.4）。
    """
    gateway = get_gateway()

    with session_scope() as db:
        attachment = db.get(ApprovalAttachment, document_id)
        if attachment is None:
            raise NotFoundError(f"附件记录不存在：document_id={document_id}")
        if attachment.task_id != task_id:
            raise NotFoundError(
                f"附件 {document_id} 不属于任务 {task_id}（实际属于 {attachment.task_id}）"
            )
        task = db.get(ApprovalTask, task_id)
        if task is None:
            raise NotFoundError(f"任务不存在：task_id={task_id}")

        approval_code = task.approval_code
        external_id = attachment.external_attachment_id
        file_name = attachment.file_name

    # 网络调用放在事务外：下载可能耗时数秒甚至超时，
    # 长事务会长时间占用数据库连接，且失败时无需回滚任何本地写入。
    try:
        downloaded = gateway.download_attachment(approval_code, external_id, file_name)
    except ApprovalGatewayError as exc:
        # 外部取不到文件 → 记为附件缺失，任务进入 blocked。
        _mark_attachment_failed(document_id, exc.message)
        error = AttachmentMissingError(
            f"附件下载失败：审批编号 {approval_code}，附件 {external_id}"
            f"（{file_name}），原因：{exc.message}"
        )
        mark_blocked(task_id, error)
        raise error from exc

    target = _safe_target_path(approval_code, document_id, downloaded.file_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    # 先写到同目录的临时文件再原子替换，避免中断留下半截文件被后续解析读到。
    temp_target = target.with_suffix(target.suffix + ".part")
    shutil.move(downloaded.local_path, temp_target)
    temp_target.replace(target)

    with session_scope() as db:
        attachment = db.get(ApprovalAttachment, document_id)
        attachment.file_path = str(target)
        attachment.file_name = downloaded.file_name
        attachment.file_size = downloaded.file_size
        attachment.checksum = downloaded.checksum
        attachment.download_status = DownloadStatus.SUCCESS.value
        attachment.download_error = None
        attachment.file_type = (
            target.suffix.lower().lstrip(".") or attachment.file_type
        )
        append_log_in_session(
            db, LogType.DOWNLOAD,
            f"附件下载成功：{downloaded.file_name}，{downloaded.file_size} 字节，"
            f"SHA256 {downloaded.checksum[:16]}…，本地路径 {target}",
            task_id=task_id,
        )
        return {
            "document_id": attachment.id,
            "task_id": task_id,
            "file_name": attachment.file_name,
            "file_type": attachment.file_type,
            "file_size": attachment.file_size,
            "local_path": attachment.file_path,
            "checksum": attachment.checksum,
            "download_status": attachment.download_status,
        }


def _mark_attachment_failed(document_id: int, reason: str) -> None:
    """把附件记录标记为下载失败并写入原因。

    参数：
        document_id: 附件主键。
        reason: 失败原因（中文）。
    返回：
        无。
    关联：
        被 :func:`download_task_attachment` 的失败分支调用。
    设计原因：
        失败必须落库，否则人工无法在页面上看到"哪个附件没取到、为什么"，
        重试也无从下手。
    """
    with session_scope() as db:
        attachment = db.get(ApprovalAttachment, document_id)
        if attachment is None:
            return
        attachment.download_status = DownloadStatus.FAILED.value
        attachment.download_error = reason
        append_log_in_session(
            db, LogType.DOWNLOAD,
            f"附件下载失败：{attachment.file_name}，原因 {reason}",
            task_id=attachment.task_id,
            level=LogLevel.ERROR,
        )


def download_all_attachments(task_id: int) -> list[dict]:
    """下载某任务下全部尚未成功下载的附件。

    参数：
        task_id: 任务主键。
    返回：
        已成功下载的附件字典列表（含 ``document_id`` 与 ``local_path``）。
    关联：
        被 ``parse_service.parse_task_documents`` 与演示脚本调用。
    异常：
        任一附件下载失败时抛 :class:`AttachmentMissingError`（任务已置 blocked）。
        这里选择"快速失败"而非"跳过继续"：合同通常有多个附件（正文 + 附件清单），
        缺一个就可能导致条款抽取不全，继续审查会产生不可信的结论。
    """
    with session_scope() as db:
        rows = (
            db.query(ApprovalAttachment)
            .filter(ApprovalAttachment.task_id == task_id)
            .order_by(ApprovalAttachment.id.asc())
            .all()
        )
        if not rows:
            # 没有任何附件记录，说明审批单本身没给附件或尚未同步详情。
            raise AttachmentMissingError(
                f"任务 {task_id} 下没有任何附件记录，无法解析合同"
            )
        pending_ids = [
            row.id
            for row in rows
            if row.download_status != DownloadStatus.SUCCESS.value
        ]

    results: list[dict] = []
    for document_id in pending_ids:
        results.append(download_task_attachment(task_id, document_id))

    # 已下载过的附件也要返回，供解析阶段统一处理。
    with session_scope() as db:
        rows = (
            db.query(ApprovalAttachment)
            .filter(
                ApprovalAttachment.task_id == task_id,
                ApprovalAttachment.download_status == DownloadStatus.SUCCESS.value,
            )
            .order_by(ApprovalAttachment.id.asc())
            .all()
        )
        return [
            {
                "document_id": row.id,
                "task_id": row.task_id,
                "file_name": row.file_name,
                "file_type": row.file_type,
                "file_size": row.file_size,
                "local_path": row.file_path,
                "checksum": row.checksum,
                "download_status": row.download_status,
            }
            for row in rows
        ]


def get_attachment(document_id: int) -> dict:
    """查询附件记录。

    参数：
        document_id: 附件主键。
    返回：
        附件字典，含本地路径与下载状态。
    关联：
        被 ``parse_service`` 在解析前校验文件是否已就绪时调用。
    异常：
        记录不存在抛 :class:`NotFoundError`。
    """
    with session_scope() as db:
        attachment = db.get(ApprovalAttachment, document_id)
        if attachment is None:
            raise NotFoundError(f"附件记录不存在：document_id={document_id}")
        return {
            "document_id": attachment.id,
            "task_id": attachment.task_id,
            "external_attachment_id": attachment.external_attachment_id,
            "file_name": attachment.file_name,
            "file_type": attachment.file_type,
            "file_size": attachment.file_size,
            "local_path": attachment.file_path,
            "checksum": attachment.checksum,
            "download_status": attachment.download_status,
            "download_error": attachment.download_error,
        }


def list_attachments(task_id: int) -> list[dict]:
    """查询某任务的全部附件记录。

    参数：
        task_id: 任务主键。
    返回：
        附件字典列表，按主键升序。
    关联：
        被调用端「详情查看模块」与 ``tools/contract_tools.py`` 使用。
    """
    with session_scope() as db:
        rows = (
            db.query(ApprovalAttachment)
            .filter(ApprovalAttachment.task_id == task_id)
            .order_by(ApprovalAttachment.id.asc())
            .all()
        )
        return [
            {
                "document_id": row.id,
                "file_name": row.file_name,
                "file_type": row.file_type,
                "file_size": row.file_size,
                "download_status": row.download_status,
                "file_path": row.file_path,
                "checksum": row.checksum,
                "download_error": row.download_error,
            }
            for row in rows
        ]
