"""回写测试：验证幂等、并发互斥、失败阻塞与重试。

覆盖需求：
- 2.4.4 回写状态 ``not_written`` / ``writing`` / ``success`` / ``failed``；
- 2.4.4「接口调用失败进入 blocked」；
- 2.4.12「能保存审查结果并将结果写回评论区」；
- 回写超时与重复回写的处理（交接文档质量门要求）。

设计原因：
回写是本系统唯一会修改外部系统状态的操作，出错代价最高：
重复写入会让审批人看到多份意见、无法判断哪份是最终版。
因此本文件对"重复调用""并发调用""外部失败"三条路径都做显式验证，
并且验证方式是**检查外部系统的真实状态**（评论区条数），
而不是只看函数返回值——返回值可能说谎，外部状态不会。
"""

from __future__ import annotations

import pytest

from app.domain.errors import CommentWriteError, TaskStateError
from app.tools import contract_tools


def _prepare(case_id: int, approval_code: str) -> int:
    """准备一个"已审查、已保存结果、尚未回写"的任务。

    参数：
        case_id: 任务主键。
        approval_code: 审批单业务唯一标识（未使用，保留以对称）。
    返回：
        ``review_id``。
    关联：
        被本文件各测试调用，避免每个测试重复写准备代码。
    """
    result = contract_tools.run_full_pipeline(case_id)
    return result["review"]["review_id"]


def test_first_write_succeeds_and_records_log(pulled):
    """验证首次回写成功并留下一条成功日志。

    参数：
        pulled: 已拉取环境夹具。
    关联：
        ``services.comment_service.write_comment_for_result``。
    """
    _result, tasks = pulled
    approval_code = "HT-2024-0001"
    case_id = tasks[approval_code]["task_id"]

    review_id = _prepare(case_id, approval_code)
    written = contract_tools.write_approval_comment(approval_code, review_id)

    assert written["write_status"] == "success"
    assert written["already_written"] is False
    assert written["write_response_text"], "应保留外部响应原文"

    logs = contract_tools.comment_service.list_comment_logs(case_id)
    success_logs = [log for log in logs if log["write_status"] == "success"]
    assert len(success_logs) == 1


def test_duplicate_write_is_idempotent(pulled):
    """验证重复回写命中幂等短路，不产生第二条评论。

    参数：
        pulled: 已拉取环境夹具。
    关联：
        ``_claim_write`` 的条件更新 + ``_existing_success_log``。

    验证要点（核心不变量）：
        - 第二次调用 ``already_written=True``；
        - 外部系统评论区条数不变；
        - 成功日志仍只有一条。
    """
    from app.approval.factory import get_gateway

    _result, tasks = pulled
    approval_code = "HT-2024-0001"
    case_id = tasks[approval_code]["task_id"]

    review_id = _prepare(case_id, approval_code)
    gateway = get_gateway()

    before = len(gateway.get_comments(approval_code))
    first = contract_tools.write_approval_comment(approval_code, review_id)
    after_first = len(gateway.get_comments(approval_code))

    second = contract_tools.write_approval_comment(approval_code, review_id)
    after_second = len(gateway.get_comments(approval_code))

    assert first["already_written"] is False
    assert after_first == before + 1, "首次回写应新增 1 条评论"

    assert second["already_written"] is True
    assert after_second == after_first, "重复回写不应新增评论"

    logs = contract_tools.comment_service.list_comment_logs(case_id)
    assert len([log for log in logs if log["write_status"] == "success"]) == 1


def test_write_failure_blocks_task_and_retry_target_is_reviewing(pulled):
    """验证回写失败时任务进入 blocked，重试目标为 reviewing。

    参数：
        pulled: 已拉取环境夹具。
    关联：
        ``comment_service`` 的失败分支 + ``task_service.mark_blocked``。

    验证要点：
        回写失败后不需要重新解析——解析结果还在，重试只需重新回写。
        因此 ``retry_target`` 必须是 ``reviewing`` 而不是 ``parsing``。
    """
    from app.approval.factory import set_gateway_override
    from app.approval.gateway import (
        ApprovalDetail,
        ApprovalGateway,
        CommentWriteResult,
        DownloadedFile,
        PendingApproval,
    )
    from app.domain.errors import ApprovalGatewayError

    class _WriteFailingGateway(ApprovalGateway):
        """仅评论写入失败的网关，其余动作正常（用于精确验证回写分支）。"""

        def __init__(self) -> None:
            """初始化替身：持有真实网关的惰性引用。

            返回：
                无。
            关联：
                被 :meth:`_delegate` 首次调用时填充 ``_real``。
            """
            self._real = None

        def _delegate(self):
            """惰性获取真实 mock 网关，避免导入顺序问题。"""
            if self._real is None:
                from app.approval.factory import get_gateway as real_factory
                from app.approval.factory import set_gateway_override as _set

                _set(None)
                self._real = real_factory()
                _set(self)
            return self._real

        def list_pending(self, limit: int) -> list[PendingApproval]:
            """透传给真实网关。"""
            return self._delegate().list_pending(limit)

        def get_approval(self, instance_id: str) -> ApprovalDetail:
            """透传给真实网关。"""
            return self._delegate().get_approval(instance_id)

        def download_attachment(
            self, instance_id: str, external_attachment_id: str, file_name: str
        ) -> DownloadedFile:
            """透传给真实网关。"""
            return self._delegate().download_attachment(
                instance_id, external_attachment_id, file_name
            )

        def write_comment(self, instance_id: str, comment_text: str) -> CommentWriteResult:
            """模拟评论接口超时。"""
            raise ApprovalGatewayError("模拟故障：评论接口超时（测试注入）")

        def get_comments(self, instance_id: str) -> list[str]:
            """透传给真实网关。"""
            return self._delegate().get_comments(instance_id)

    _result, tasks = pulled
    approval_code = "HT-2024-0001"
    case_id = tasks[approval_code]["task_id"]

    review_id = _prepare(case_id, approval_code)

    set_gateway_override(_WriteFailingGateway())
    try:
        with pytest.raises(CommentWriteError) as exc_info:
            contract_tools.write_approval_comment(approval_code, review_id)
        assert exc_info.value.reason == "comment_write_failed"
        assert exc_info.value.retry_target is not None
        assert exc_info.value.retry_target.value == "reviewing"
    finally:
        set_gateway_override(None)

    snapshot = contract_tools.get_task_snapshot(case_id)
    assert snapshot["task"]["task_status"] == "blocked"
    assert snapshot["task"]["blocked_reason"] == "comment_write_failed"
    assert snapshot["task"]["retry_target"] == "reviewing"
    assert snapshot["task"]["write_status"] == "failed"

    logs = contract_tools.comment_service.list_comment_logs(case_id)
    assert any(log["write_status"] == "failed" for log in logs)


def test_retry_after_write_failure_only_rewrites(pulled):
    """验证回写失败后重试只需重新回写，不必重新解析。

    参数：
        pulled: 已拉取环境夹具。
    关联：
        ``tools.contract_tools.retry_task`` 的 reviewing 分支。

    验证要点：
        重试后应重新生成结果并回写成功，且不产生新的解析记录
        （解析记录数不变证明没有重复解析）。
    """
    from app.approval.factory import set_gateway_override
    from app.approval.gateway import (
        ApprovalDetail,
        ApprovalGateway,
        CommentWriteResult,
        DownloadedFile,
        PendingApproval,
    )
    from app.domain.errors import ApprovalGatewayError

    class _ToggleGateway(ApprovalGateway):
        """可控的写入失败网关：第一次写失败，之后恢复正常。"""

        def __init__(self) -> None:
            """初始化替身：默认下一次写入失败。

            返回：
                无。
            关联：
                ``fail_next`` 被 :meth:`write_comment` 读取，第一次置为 ``False``，
                因此第二次调用会透传给真实网关。
            """
            self.fail_next = True
            self._real = None

        def _delegate(self):
            """惰性获取真实 mock 网关。"""
            if self._real is None:
                from app.approval.factory import get_gateway as real_factory
                from app.approval.factory import set_gateway_override as _set

                _set(None)
                self._real = real_factory()
                _set(self)
            return self._real

        def list_pending(self, limit: int) -> list[PendingApproval]:
            """透传。"""
            return self._delegate().list_pending(limit)

        def get_approval(self, instance_id: str) -> ApprovalDetail:
            """透传。"""
            return self._delegate().get_approval(instance_id)

        def download_attachment(self, instance_id, external_attachment_id, file_name):
            """透传。"""
            return self._delegate().download_attachment(
                instance_id, external_attachment_id, file_name
            )

        def write_comment(self, instance_id: str, comment_text: str) -> CommentWriteResult:
            """按开关决定失败或透传。"""
            if self.fail_next:
                self.fail_next = False
                raise ApprovalGatewayError("模拟故障：首次写入超时")
            return self._delegate().write_comment(instance_id, comment_text)

        def get_comments(self, instance_id: str) -> list[str]:
            """透传。"""
            return self._delegate().get_comments(instance_id)

    _result, tasks = pulled
    approval_code = "HT-2024-0001"
    case_id = tasks[approval_code]["task_id"]

    review_id = _prepare(case_id, approval_code)
    parses_before = len(contract_tools.parse_service.list_parse_history(case_id))

    set_gateway_override(_ToggleGateway())
    try:
        with pytest.raises(CommentWriteError):
            contract_tools.write_approval_comment(approval_code, review_id)

        # 重试：应只重新回写。
        retried = contract_tools.retry_task(case_id)
        assert retried["retry"]["retry_target"] == "reviewing"
        assert "comment" in retried["rerun"]
        assert retried["rerun"]["comment"]["write_status"] == "success"
    finally:
        set_gateway_override(None)

    parses_after = len(contract_tools.parse_service.list_parse_history(case_id))
    assert parses_after == parses_before, "重试回写不应产生新的解析记录"

    snapshot = contract_tools.get_task_snapshot(case_id)
    assert snapshot["task"]["task_status"] == "done"
    assert snapshot["task"]["write_status"] == "success"


def test_write_with_mismatched_review_id_rejected(pulled):
    """验证结果与任务不匹配时回写被拒绝。

    参数：
        pulled: 已拉取环境夹具。
    关联：
        ``comment_service.write_comment_for_result`` 的归属校验。

    验证要点：
        防止把 A 合同的审查意见写到 B 合同的评论区——
        这是最容易造成严重后果的一类错误。
    """
    from app.domain.errors import NotFoundError

    _result, tasks = pulled
    case_a = tasks["HT-2024-0001"]["task_id"]
    code_a = "HT-2024-0001"
    code_b = "HT-2024-0002"

    review_id = _prepare(case_a, code_a)

    with pytest.raises(NotFoundError) as exc_info:
        contract_tools.write_approval_comment(code_b, review_id)
    assert "不属于" in exc_info.value.message


def test_write_status_tracks_state_transitions(pulled):
    """验证回写状态字段随操作正确变化。

    参数：
        pulled: 已拉取环境夹具。
    关联：
        ``approval_tasks.write_status`` 状态机。

    验证要点：
        初始 ``not_written`` → 回写后 ``success``。
        中间态 ``writing`` 只在写入过程中短暂存在，事务内不可观测，
        因此这里验证首尾两端（中间态由并发测试覆盖）。
    """
    _result, tasks = pulled
    approval_code = "HT-2024-0001"
    case_id = tasks[approval_code]["task_id"]

    snapshot = contract_tools.get_task_snapshot(case_id)
    assert snapshot["task"]["write_status"] == "not_written"

    review_id = _prepare(case_id, approval_code)
    contract_tools.write_approval_comment(approval_code, review_id)

    snapshot2 = contract_tools.get_task_snapshot(case_id)
    assert snapshot2["task"]["write_status"] == "success"


def test_concurrent_write_is_rejected(pulled):
    """验证并发回写时只有一个请求能执行。

    参数：
        pulled: 已拉取环境夹具。
    关联：
        ``_claim_write`` 的条件更新（``UPDATE ... WHERE write_status != 'success'``）。

    验证要点：
        手工把状态置为 ``writing``（模拟另一个请求正在进行），
        此时新请求应被拒绝并抛 ``TaskStateError``，
        而不是也去写外部系统。
    """
    from app.db.base import session_scope
    from app.db.models import ApprovalTask
    from app.domain.enums import WriteStatus

    _result, tasks = pulled
    approval_code = "HT-2024-0001"
    case_id = tasks[approval_code]["task_id"]

    review_id = _prepare(case_id, approval_code)

    # 模拟"另一个请求已抢占 writing 状态"。
    with session_scope() as db:
        task = db.get(ApprovalTask, case_id)
        task.write_status = WriteStatus.WRITING.value

    with pytest.raises(TaskStateError) as exc_info:
        contract_tools.write_approval_comment(approval_code, review_id)
    assert "正在回写" in exc_info.value.message
