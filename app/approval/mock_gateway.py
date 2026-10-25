"""本地模拟审批系统网关：用 JSON 文件模拟外部 OA 的待办、详情、附件与评论区。

职责：
- 实现 :class:`app.approval.gateway.ApprovalGateway` 的全部 5 个动作；
- 把审批单、附件清单、评论区状态保存在一个 JSON 文件里，使"回写后能再读出来"这一
  验收点（需求 2.4.12）可在无外部依赖的情况下被真实验证；
- 附件下载实现为"从源文件复制到目标目录"，源文件路径由 JSON 中的 ``file_path`` 指定。

非职责：
- 不模拟 OA 的鉴权、流程引擎、审批通过/驳回逻辑——本系统的边界是不代替人工审批；
- 不生成合同文件（示例文件由 ``scripts/make_samples.py`` 生成）。

设计原因：
- 为什么用文件而不是内存字典：回写状态必须跨进程可见。演示脚本、Web 调用端、
  测试可能在不同进程里访问同一份"外部系统数据"，内存字典会各自看到不同副本。
- 为什么下载要真的复制文件：需求要求"下载合同附件并返回本地文件路径和校验信息"，
  如果只返回源路径，就无法验证校验值与路径安全逻辑。

主要协作模块：
- ``approval/factory.py`` 在 ``CR_APPROVAL_MODE=mock`` 时实例化本类；
- 数据文件路径取自 ``settings.mock_oa_store``。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from app.approval.gateway import (
    ApprovalDetail,
    ApprovalGateway,
    AttachmentInfo,
    CommentWriteResult,
    DownloadedFile,
    PendingApproval,
)
from app.domain.errors import ApprovalGatewayError, NotFoundError


class MockApprovalGateway(ApprovalGateway):
    """基于 JSON 文件的模拟审批系统。

    用途：让完整审查闭环在没有任何外部服务的情况下可运行、可测试。
    生命周期：每次工具调用创建一个实例（无内存状态，所有状态都在 JSON 文件里）。
    协作对象：``services/*`` 通过网关接口调用；JSON 文件由 ``scripts/make_samples.py``
    初始化。

    并发说明：
    本类不做文件级加锁。演示与测试为单进程串行调用；若需并发演示，
    应在部署层保证同一时刻只有一个写入者，或替换为真实审批系统。
    """

    def __init__(self, store_path: Path) -> None:
        """初始化网关。

        参数：
            store_path: 模拟审批系统数据文件路径（JSON）。
        关联：
            由 ``approval/factory.py`` 传入 ``settings.mock_oa_store``。
        """
        self._store_path = store_path

    # ---- 内部读写工具 ----

    def _load(self) -> dict:
        """读取并解析 JSON 存储。

        返回：
            存储字典，结构为 ``{"approvals": [...]}``。
        关联：
            被本类所有方法调用；文件不存在时抛 :class:`ApprovalGatewayError`，
            因为"审批系统不可用"与"审批系统没有数据"必须区分开。
        """
        if not self._store_path.exists():
            raise ApprovalGatewayError(
                f"模拟审批系统数据文件不存在：{self._store_path}；"
                "请先运行 scripts/make_samples.py 初始化"
            )
        try:
            return json.loads(self._store_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ApprovalGatewayError(f"模拟审批系统数据文件格式错误：{exc}") from exc

    def _save(self, data: dict) -> None:
        """把存储字典写回 JSON 文件。

        参数：
            data: 完整存储字典。
        关联：
            被 :meth:`write_comment` 调用。
        副作用：
            覆盖写入 ``store_path``。写入前确保父目录存在。
        """
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        self._store_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _find(self, data: dict, instance_id: str) -> dict:
        """在存储中按审批编号定位审批单。

        参数：
            data: 存储字典。
            instance_id: 审批单业务唯一标识。
        返回：
            审批单字典。
        关联：
            被 :meth:`get_approval`、:meth:`download_attachment`、:meth:`write_comment`、
            :meth:`get_comments` 共用，避免重复查找逻辑。
        异常：
            审批单不存在时抛 :class:`app.domain.errors.NotFoundError`。

        为什么不是 ``ApprovalGatewayError``：
            两者对外语义完全不同——
            - ``NotFoundError`` → HTTP 404，"你问的这个对象不存在"；
            - ``ApprovalGatewayError`` → HTTP 400/5xx，"外部系统出故障了，稍后重试"。
            把"审批单不存在"报成"接口故障"会误导调用方去重试一个永远不存在的对象。
        """
        for item in data.get("approvals", []):
            if item.get("approval_code") == instance_id:
                return item
        raise NotFoundError(f"审批单不存在：{instance_id}")

    @staticmethod
    def _parse_time(raw: str | None) -> datetime | None:
        """把 ISO 时间字符串解析为 ``datetime``。

        参数：
            raw: ISO 8601 字符串，允许为 ``None`` 或空串。
        返回：
            解析后的时间；空值返回 ``None``，格式非法返回 ``None``（不阻断拉取）。
        关联：
            被 :meth:`list_pending` 与 :meth:`get_approval` 使用。
        设计原因：
            单个审批单的时间格式错误不应导致整批拉取失败，因此这里容错返回 ``None``。
        """
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    # ---- 网关接口实现 ----

    def list_pending(self, limit: int) -> list[PendingApproval]:
        """拉取待处理审批单列表。

        参数：
            limit: 最大条数。
        返回：
            按申请时间倒序的待办摘要列表；只包含 ``approval_status`` 为 ``pending``
            或 ``in_progress`` 的审批单。
        关联：
            被 ``services/task_service.py::pull_pending_tasks`` 调用。
        """
        data = self._load()
        pending: list[PendingApproval] = []
        for item in data.get("approvals", []):
            # 只把"仍待处理"的审批单作为待办返回。已归档/已完成的审批单即使存在也不拉取，
            # 这是审批系统待办列表的常规语义，避免重复审查历史合同。
            if item.get("approval_status") not in ("pending", "in_progress"):
                continue
            pending.append(
                PendingApproval(
                    approval_code=item["approval_code"],
                    approval_title=item.get("approval_title", ""),
                    applicant_name=item.get("applicant_name", ""),
                    applied_at=self._parse_time(item.get("applied_at")),
                    attachment_count=len(item.get("attachments", [])),
                )
            )
        pending.sort(key=lambda p: p.applied_at or datetime.min, reverse=True)
        return pending[:limit]

    def get_approval(self, instance_id: str) -> ApprovalDetail:
        """查询审批单详情。

        参数：
            instance_id: 审批单业务唯一标识。
        返回：
            含表单数据与附件清单的详情对象。
        关联：
            被 ``services/task_service.py::refresh_task_detail`` 调用。
        """
        item = self._find(self._load(), instance_id)
        attachments = tuple(
            AttachmentInfo(
                external_attachment_id=str(att.get("external_attachment_id", "")),
                file_name=att.get("file_name", ""),
                file_size=int(att.get("file_size", 0)),
                file_kind=att.get("file_kind", ""),
            )
            for att in item.get("attachments", [])
        )
        return ApprovalDetail(
            approval_code=item["approval_code"],
            approval_title=item.get("approval_title", ""),
            applicant_name=item.get("applicant_name", ""),
            applied_at=self._parse_time(item.get("applied_at")),
            approval_status=item.get("approval_status", ""),
            form_data=dict(item.get("form_data", {})),
            attachments=attachments,
        )

    def download_attachment(
        self, instance_id: str, external_attachment_id: str, file_name: str
    ) -> DownloadedFile:
        """从模拟存储的源路径复制附件到临时目录。

        参数：
            instance_id: 审批单业务唯一标识。
            external_attachment_id: 外部附件编号。
            file_name: 期望文件名；实际使用存储中记录的文件名，保证扩展名可信。
        返回：
            含临时本地路径与 SHA256 的下载结果。
        关联：
            被 ``services/attachment_service.py::download_task_attachment`` 调用，
            由后者负责把临时文件移动到正式附件目录。
        副作用：
            在系统临时目录创建文件，调用方必须负责清理或移动。
        异常：
            源文件缺失（模拟"附件缺失"场景）或附件编号不存在时抛
            :class:`ApprovalGatewayError`。
        """
        item = self._find(self._load(), instance_id)
        attachment = next(
            (
                att
                for att in item.get("attachments", [])
                if str(att.get("external_attachment_id", "")) == external_attachment_id
            ),
            None,
        )
        if attachment is None:
            raise ApprovalGatewayError(
                f"审批单 {instance_id} 下不存在附件 {external_attachment_id}"
            )

        source = Path(attachment.get("source_path", ""))
        if not source.exists():
            # 这是需求 2.4.4「附件缺失」的真实触发路径：外部记录里有附件，但文件取不到。
            raise ApprovalGatewayError(
                f"附件文件不存在：{source}（审批单 {instance_id}，附件 {external_attachment_id}）"
            )

        target_name = attachment.get("file_name") or file_name or source.name
        temp_dir = Path(tempfile.mkdtemp(prefix="contract_dl_"))
        target = temp_dir / target_name
        shutil.copyfile(source, target)
        payload = target.read_bytes()
        return DownloadedFile(
            local_path=str(target),
            file_name=target_name,
            file_size=len(payload),
            checksum=hashlib.sha256(payload).hexdigest(),
        )

    def write_comment(self, instance_id: str, comment_text: str) -> CommentWriteResult:
        """把评论追加到模拟审批系统的评论区。

        参数：
            instance_id: 审批单业务唯一标识。
            comment_text: 评论文本。
        返回：
            成功标志与响应说明。
        关联：
            被 ``services/comment_service.py::write_comment_for_result`` 调用。
        副作用：
            修改 JSON 文件中的 ``comments`` 数组。
        """
        data = self._load()
        item = self._find(data, instance_id)
        comments = item.setdefault("comments", [])
        comments.append(
            {
                "content": comment_text,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "author": "合同审查助手",
            }
        )
        self._save(data)
        return CommentWriteResult(
            success=True,
            response_text=f"评论已写入，当前评论数 {len(comments)}",
        )

    def get_comments(self, instance_id: str) -> list[str]:
        """读取审批单评论区正文列表。

        参数：
            instance_id: 审批单业务唯一标识。
        返回：
            评论正文列表，按写入顺序。
        关联：
            被演示脚本与调用端用于展示回写结果，并验证重复回写不会新增评论。
        """
        item = self._find(self._load(), instance_id)
        return [c.get("content", "") for c in item.get("comments", [])]
