"""真实审批系统 HTTP 网关：对接企业 OA 的 REST 接口。

职责：
- 实现 :class:`app.approval.gateway.ApprovalGateway`，把 5 个动作映射为 HTTP 请求；
- 统一处理超时、连接失败、非 2xx 响应、JSON 结构不符，全部转成
  :class:`app.domain.errors.ApprovalGatewayError`；
- 把 OA 的字段名映射为本地传输对象。

非职责：
- 不实现鉴权刷新（令牌由配置提供，过期需运维更新；这是刻意的最小实现）；
- 不做分页游标维护（``limit`` 直接作为查询参数透传）；
- 不做重试（理由见网关接口约定）。

设计原因：
需求 2.4 只要求"审批系统触发待办拉取、提供审批详情、接收评论回写"，
并未指定厂商与字段。因此本实现采用一组**通用 REST 约定**（见下），
真实对接时只需调整 ``_ENDPOINTS`` 与三个 ``_map_*`` 方法的字段名，
服务层与工具层无需改动。

约定接口（相对 ``settings.approval_base_url``）：
- ``GET  /api/approvals/pending?limit=N``        → ``{"data": [{...}]}``
- ``GET  /api/approvals/{instance_id}``          → ``{"data": {...}}``
- ``GET  /api/approvals/{instance_id}/attachments/{attachment_id}`` → 文件二进制
- ``POST /api/approvals/{instance_id}/comments``  → ``{"success": true, "message": "..."}``
- ``GET  /api/approvals/{instance_id}/comments``  → ``{"data": [{"content": "..."}]}``

主要协作模块：
- ``approval/factory.py`` 在 ``CR_APPROVAL_MODE=http`` 时实例化本类；
- 配置来自 ``app.config.settings`` 的 ``approval_base_url`` / ``approval_token`` /
  ``approval_timeout_seconds``。
"""

from __future__ import annotations

import hashlib
import tempfile
from datetime import datetime
from pathlib import Path

import httpx

from app.approval.gateway import (
    ApprovalDetail,
    ApprovalGateway,
    AttachmentInfo,
    CommentWriteResult,
    DownloadedFile,
    PendingApproval,
)
from app.domain.errors import ApprovalGatewayError, NotFoundError

# 接口路径模板。集中定义便于真实对接时按厂商文档调整，不影响其余代码。
_ENDPOINTS = {
    "pending": "/api/approvals/pending",
    "detail": "/api/approvals/{instance_id}",
    "attachment": "/api/approvals/{instance_id}/attachments/{attachment_id}",
    "comments": "/api/approvals/{instance_id}/comments",
}


class HttpApprovalGateway(ApprovalGateway):
    """通过 HTTP 调用真实审批系统的网关实现。

    用途：生产接入路径；演示环境使用 :class:`MockApprovalGateway`。
    生命周期：每次工具调用创建一个实例，内部 ``httpx.Client`` 按调用创建并关闭，
    避免连接在长生命周期对象中泄漏。
    协作对象：``services/*`` 通过网关接口调用。
    """

    def __init__(self, base_url: str, token: str, timeout_seconds: float) -> None:
        """初始化网关。

        参数：
            base_url: 审批系统根地址，不含尾部斜杠。
            token: 访问令牌；为空时不发送 ``Authorization`` 头（部分内网 OA 走网关鉴权）。
            timeout_seconds: 单次请求超时秒数。
        关联：
            由 ``approval/factory.py`` 按配置构造。
        """
        self._base_url = base_url
        self._token = token
        self._timeout = timeout_seconds

    def _headers(self) -> dict[str, str]:
        """构造请求头。

        返回：
            含 ``Authorization``（令牌非空时）与 JSON 接受头的字典。
        关联：
            被所有 HTTP 方法调用。
        """
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _request_json(self, method: str, url: str, **kwargs) -> dict:
        """发起请求并解析 JSON 响应。

        参数：
            method: HTTP 方法。
            url: 完整 URL。
            **kwargs: 透传给 ``httpx`` 的额外参数（``params`` / ``json``）。
        返回：
            解析后的响应字典。
        关联：
            被 :meth:`list_pending`、:meth:`get_approval`、:meth:`write_comment`、
            :meth:`get_comments` 调用。
        异常：
            超时、连接失败、非 2xx、JSON 非法时抛 :class:`ApprovalGatewayError`，
            消息中包含方法与 URL，便于运维定位。
        """
        try:
            with httpx.Client(timeout=self._timeout, headers=self._headers()) as client:
                response = client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise ApprovalGatewayError(f"审批系统请求超时：{method} {url}") from exc
        except httpx.HTTPError as exc:
            raise ApprovalGatewayError(f"审批系统请求失败：{method} {url}（{exc}）") from exc

        if response.status_code == 404:
            # 404 表示"对象不存在"，与"服务故障"语义不同：
            # 前者重试无用，后者可以重试。因此单独映射为 NotFoundError。
            raise NotFoundError(f"审批系统未找到该资源：{method} {url}")
        if response.status_code >= 400:
            raise ApprovalGatewayError(
                f"审批系统返回错误：{method} {url} → HTTP {response.status_code} "
                f"{response.text[:200]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ApprovalGatewayError(
                f"审批系统返回内容不是合法 JSON：{method} {url}"
            ) from exc

    @staticmethod
    def _parse_time(raw: str | None) -> datetime | None:
        """解析 OA 返回的时间字符串。

        参数：
            raw: ISO 8601 字符串或空值。
        返回：
            解析结果；空值或格式非法返回 ``None``（单条时间异常不阻断整批拉取）。
        关联：
            被 :meth:`list_pending` 与 :meth:`get_approval` 使用。
        """
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    def list_pending(self, limit: int) -> list[PendingApproval]:
        """拉取待处理审批单列表。

        参数：
            limit: 最大条数，作为查询参数透传给 OA。
        返回：
            待办摘要列表。
        关联：
            被 ``services/task_service.py::pull_pending_tasks`` 调用。
        """
        url = f"{self._base_url}{_ENDPOINTS['pending']}"
        payload = self._request_json("GET", url, params={"limit": limit})
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise ApprovalGatewayError(f"待办接口返回结构不符，缺少 data 数组：{url}")

        result: list[PendingApproval] = []
        for row in rows:
            code = str(row.get("instance_id") or row.get("approval_code") or "").strip()
            if not code:
                # 缺少业务唯一标识的记录无法去重，直接跳过并计入日志由上层感知条数差异。
                continue
            attachments = row.get("attachments") or []
            result.append(
                PendingApproval(
                    approval_code=code,
                    approval_title=str(row.get("title") or row.get("approval_title") or ""),
                    applicant_name=str(row.get("applicant") or row.get("applicant_name") or ""),
                    applied_at=self._parse_time(row.get("applied_at") or row.get("apply_time")),
                    attachment_count=int(
                        row.get("attachment_count", len(attachments) if attachments else 0)
                    ),
                )
            )
        return result

    def get_approval(self, instance_id: str) -> ApprovalDetail:
        """查询审批单详情。

        参数：
            instance_id: 审批单业务唯一标识。
        返回：
            审批详情对象。
        关联：
            被 ``services/task_service.py::refresh_task_detail`` 调用。
        """
        url = f"{self._base_url}{_ENDPOINTS['detail'].format(instance_id=instance_id)}"
        payload = self._request_json("GET", url)
        row = payload.get("data")
        if not isinstance(row, dict):
            raise ApprovalGatewayError(f"详情接口返回结构不符，缺少 data 对象：{url}")

        attachments = tuple(
            AttachmentInfo(
                external_attachment_id=str(
                    att.get("attachment_id") or att.get("external_attachment_id") or ""
                ),
                file_name=str(att.get("file_name") or ""),
                file_size=int(att.get("file_size") or 0),
                file_kind=str(att.get("file_kind") or ""),
            )
            for att in (row.get("attachments") or [])
        )
        form_raw = row.get("form_data") or {}
        return ApprovalDetail(
            approval_code=instance_id,
            approval_title=str(row.get("title") or row.get("approval_title") or ""),
            applicant_name=str(row.get("applicant") or row.get("applicant_name") or ""),
            applied_at=self._parse_time(row.get("applied_at") or row.get("apply_time")),
            approval_status=str(row.get("status") or row.get("approval_status") or ""),
            form_data={str(k): str(v) for k, v in form_raw.items()},
            attachments=attachments,
        )

    def download_attachment(
        self, instance_id: str, external_attachment_id: str, file_name: str
    ) -> DownloadedFile:
        """下载附件二进制内容到临时文件。

        参数：
            instance_id: 审批单业务唯一标识。
            external_attachment_id: 外部附件编号。
            file_name: 期望文件名；服务端未给出文件名时使用它。
        返回：
            含临时路径、大小与 SHA256 的下载结果。
        关联：
            被 ``services/attachment_service.py::download_task_attachment`` 调用。
        副作用：
            在系统临时目录写入文件，由调用方移动或清理。
        """
        url = f"{self._base_url}" + _ENDPOINTS["attachment"].format(
            instance_id=instance_id, attachment_id=external_attachment_id
        )
        try:
            with httpx.Client(timeout=self._timeout, headers=self._headers()) as client:
                response = client.get(url)
        except httpx.TimeoutException as exc:
            raise ApprovalGatewayError(f"附件下载超时：{url}") from exc
        except httpx.HTTPError as exc:
            raise ApprovalGatewayError(f"附件下载失败：{url}（{exc}）") from exc

        if response.status_code >= 400:
            raise ApprovalGatewayError(
                f"附件下载失败：{url} → HTTP {response.status_code}"
            )
        content = response.content
        if not content:
            raise ApprovalGatewayError(f"附件内容为空：{url}")

        # 优先使用服务端 Content-Disposition 中的文件名，它比调用方传入的更可信。
        resolved_name = file_name
        disposition = response.headers.get("content-disposition", "")
        if "filename=" in disposition:
            resolved_name = disposition.split("filename=")[-1].strip().strip('"; ')

        temp_dir = Path(tempfile.mkdtemp(prefix="contract_dl_"))
        target = temp_dir / resolved_name
        target.write_bytes(content)
        return DownloadedFile(
            local_path=str(target),
            file_name=resolved_name,
            file_size=len(content),
            checksum=hashlib.sha256(content).hexdigest(),
        )

    def write_comment(self, instance_id: str, comment_text: str) -> CommentWriteResult:
        """把评论写入审批单评论区。

        参数：
            instance_id: 审批单业务唯一标识。
            comment_text: 评论文本。
        返回：
            回写结果；OA 明确返回 ``success=false`` 时 ``success=False``，
            调用失败（超时/非 2xx）时抛 :class:`ApprovalGatewayError`。
        关联：
            被 ``services/comment_service.py::write_comment_for_result`` 调用。
        """
        url = f"{self._base_url}{_ENDPOINTS['comments'].format(instance_id=instance_id)}"
        payload = self._request_json(
            "POST", url, json={"content": comment_text, "author": "contract-review-bot"}
        )
        success = bool(payload.get("success", True))
        return CommentWriteResult(
            success=success,
            response_text=str(payload.get("message") or payload),
        )

    def get_comments(self, instance_id: str) -> list[str]:
        """读取审批单评论区正文。

        参数：
            instance_id: 审批单业务唯一标识。
        返回：
            评论正文列表。
        关联：
            被演示脚本与调用端用于展示回写结果。
        """
        url = f"{self._base_url}{_ENDPOINTS['comments'].format(instance_id=instance_id)}"
        payload = self._request_json("GET", url)
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise ApprovalGatewayError(f"评论接口返回结构不符，缺少 data 数组：{url}")
        return [str(row.get("content", "")) for row in rows]
