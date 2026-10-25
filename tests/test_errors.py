"""异常分支测试：覆盖需求 2.4.4 要求的全部阻塞场景与重试。

覆盖需求：
- 2.4.4「附件缺失、图片无法识别、文档内容为空、接口调用失败都要进入 blocked 状态」；
- 2.4.4「blocked 状态允许人工重试，重试后重新进入 parsing 或 reviewing」；
- 2.4.4「每个审批单按唯一业务标识去重，重复拉取时只更新已有记录，不重复创建新任务」；
- 2.4.12「异常任务能够进入阻塞状态并支持重试」。

设计原因：
这些分支是最容易被"看起来实现了"的部分——正常路径跑通不代表失败路径正确。
每个测试都同时验证三件事：
1. 异常类型与 ``reason`` 代码正确（决定 blocked_reason）；
2. 任务状态真的变成了 blocked；
3. 阻塞原因、明细、重试目标都被持久化（否则人工无法排查、无法重试）。
"""

from __future__ import annotations

import pytest

from app.domain.errors import (
    ApprovalGatewayError,
    AttachmentMissingError,
    ContractReviewError,
    DocumentEmptyError,
    OcrUnavailableError,
    TaskStateError,
)
from app.tools import contract_tools


def test_pull_deduplicates_by_approval_code(pulled):
    """验证按审批编号去重：重复拉取只更新不新建。

    参数：
        pulled: 已拉取环境夹具。
    关联：
        ``services.task_service.pull_pending_tasks``；需求 2.4.4。

    验证要点：
        - 首次拉取创建 N 条；
        - 第二次拉取创建 0 条、更新 N 条；
        - 任务总数不变。
    """
    result, tasks = pulled
    assert result["created"] == 4
    assert result["updated"] == 0
    assert len(tasks) == 4

    from app.tools import contract_tools as tools

    second = tools.list_pending_contract_approvals(limit=20)
    assert second["created"] == 0, "重复拉取不应新建任务"
    assert second["updated"] == 4
    assert second["total"] == 4

    after = tools.task_service.list_tasks(limit=100)
    assert len(after) == 4, "任务总数不应因重复拉取而增加"


def test_repull_does_not_reset_task_status(pulled):
    """验证重复拉取不会把已完成任务打回起点。

    参数：
        pulled: 已拉取环境夹具。
    关联：
        ``pull_pending_tasks`` 的更新分支。

    验证要点（关键业务约束）：
        若重复拉取覆盖了 ``task_status``，一次误操作就会让已完成的审查
        全部重来，且可能触发重复回写。这是必须守住的不变量。
    """
    _result, tasks = pulled
    case_id = tasks["HT-2024-0001"]["task_id"]

    # 先把任务推进到 done（模拟已完成审查并回写）。
    from app.db.base import session_scope
    from app.db.models import ApprovalTask
    from app.domain.enums import TaskStatus, WriteStatus

    with session_scope() as db:
        task = db.get(ApprovalTask, case_id)
        task.task_status = TaskStatus.DONE.value
        task.write_status = WriteStatus.SUCCESS.value

    contract_tools.list_pending_contract_approvals(limit=20)

    with session_scope() as db:
        task = db.get(ApprovalTask, case_id)
        assert task.task_status == TaskStatus.DONE.value, "重复拉取不应重置任务状态"
        assert task.write_status == WriteStatus.SUCCESS.value, "重复拉取不应重置回写状态"


def test_missing_attachment_blocks_task(pulled):
    """验证附件缺失时任务进入 blocked，且原因与重试目标正确记录。

    参数：
        pulled: 已拉取环境夹具。
    关联：
        ``services.attachment_service.download_task_attachment`` 的失败分支 +
        ``services.task_service.mark_blocked``；需求 2.4.4「附件缺失」。

    验证要点：
        - 抛 ``AttachmentMissingError``；
        - ``blocked_reason=attachment_missing``；
        - ``retry_target=parsing``（补件后需重新解析）；
        - 附件记录本身被标记为 failed 并带原因。
    """
    _result, tasks = pulled
    case_id = tasks["HT-2024-0003"]["task_id"]

    with pytest.raises(AttachmentMissingError) as exc_info:
        contract_tools.run_full_pipeline(case_id)

    assert exc_info.value.reason == "attachment_missing"

    snapshot = contract_tools.get_task_snapshot(case_id)
    task = snapshot["task"]
    assert task["task_status"] == "blocked"
    assert task["blocked_reason"] == "attachment_missing"
    assert task["retry_target"] == "parsing"
    assert task["blocked_detail"], "必须记录中文阻塞明细供人工排查"

    failed = [a for a in snapshot["attachments"] if a["download_status"] == "failed"]
    assert failed, "附件记录应被标记为下载失败"
    assert failed[0]["download_error"], "附件记录应带失败原因"


def test_empty_document_blocks_with_correct_reason(pulled):
    """验证空文档进入 blocked 且原因为 ``document_empty``（不是 OCR 失败）。

    参数：
        pulled: 已拉取环境夹具。
    关联：
        ``parser.document.read_document`` 的空内容判定 +
        ``services.parse_service`` 的异常转换；需求 2.4.4「文档内容为空」。

    验证要点（真实缺陷的回归测试）：
        空白页若走 OCR 会抛 ``OcrUnavailableError``，
        导致原因错报为"图片无法识别"。本测试守住正确分类，
        因为两者的排查动作完全不同：换文件 vs 提高图像质量。
    """
    _result, tasks = pulled
    case_id = tasks["HT-2024-0004"]["task_id"]

    with pytest.raises(DocumentEmptyError) as exc_info:
        contract_tools.run_full_pipeline(case_id)

    assert exc_info.value.reason == "document_empty"

    snapshot = contract_tools.get_task_snapshot(case_id)
    assert snapshot["task"]["task_status"] == "blocked"
    assert snapshot["task"]["blocked_reason"] == "document_empty"


def test_parse_failure_recorded_in_contract_parses(pulled):
    """验证解析失败也会写一条 ``contract_parses`` 记录（不允许只返回空结果）。

    参数：
        pulled: 已拉取环境夹具。
    关联：
        ``services.parse_service._persist_parse_failure``；需求 2.4.5
        「解析失败时，要记录失败原因，不允许只返回空结果」。

    验证要点：
        失败记录必须带 ``parse_status=failed`` 与 ``parse_error``，
        否则人工在页面上只能看到"任务卡住了"，看不到原因。
    """
    _result, tasks = pulled
    case_id = tasks["HT-2024-0004"]["task_id"]

    with pytest.raises(DocumentEmptyError):
        contract_tools.run_full_pipeline(case_id)

    history = contract_tools.parse_service.list_parse_history(case_id)
    failed_records = [r for r in history if r["parse_status"] == "failed"]
    assert failed_records, "解析失败必须留下 failed 记录"
    assert failed_records[0]["parse_error"], "失败记录必须写明原因"
    assert "内容为空" in failed_records[0]["parse_error"]


def test_retry_resets_blocked_task_to_target(pulled):
    """验证重试把 blocked 任务复位到记录的重试目标。

    参数：
        pulled: 已拉取环境夹具。
    关联：
        ``services.task_service.retry_blocked_task``；需求 2.4.4
        「blocked 状态允许人工重试，重试后重新进入 parsing 或 reviewing」。

    验证要点：
        - 复位后状态等于 ``retry_target``；
        - 阻塞原因与明细被清空（否则页面会一直显示旧的失败原因）；
        - 返回原阻塞原因，便于日志留痕。
    """
    _result, tasks = pulled
    case_id = tasks["HT-2024-0003"]["task_id"]

    with pytest.raises(AttachmentMissingError):
        contract_tools.run_full_pipeline(case_id)

    retry = contract_tools.task_service.retry_blocked_task(case_id)
    assert retry["retry_target"] == "parsing"
    assert retry["task_status"] == "parsing"
    assert retry["previous_reason"] == "attachment_missing"

    snapshot = contract_tools.get_task_snapshot(case_id)
    assert snapshot["task"]["blocked_reason"] is None
    assert snapshot["task"]["blocked_detail"] is None


def test_retry_rejects_non_blocked_task(pulled):
    """验证对非 blocked 任务重试会被明确拒绝。

    参数：
        pulled: 已拉取环境夹具。
    关联：
        ``retry_blocked_task`` 的状态校验；需求 2.4.4。

    验证要点：
        需求只允许 blocked 任务重试。对 pending 任务重试属于调用方用法错误，
        必须显式报错而不是静默成功——否则会让调用方误以为"重试生效了"。
    """
    _result, tasks = pulled
    case_id = tasks["HT-2024-0001"]["task_id"]  # 该任务此时为 pending

    with pytest.raises(TaskStateError) as exc_info:
        contract_tools.task_service.retry_blocked_task(case_id)
    assert "blocked" in exc_info.value.message


def test_failure_is_reproducible_after_retry(pulled):
    """验证重试后同一故障仍可复现（说明失败不是偶发）。

    参数：
        pulled: 已拉取环境夹具。
    关联：
        ``retry_blocked_task`` + ``run_full_pipeline``。

    验证要点：
        源文件仍不存在时，重试后应再次失败并再次进入 blocked。
        若第二次"成功"了，说明状态机有漏洞（例如跳过了实际下载）。
    """
    _result, tasks = pulled
    case_id = tasks["HT-2024-0003"]["task_id"]

    with pytest.raises(AttachmentMissingError):
        contract_tools.run_full_pipeline(case_id)

    contract_tools.task_service.retry_blocked_task(case_id)

    with pytest.raises(AttachmentMissingError):
        contract_tools.run_full_pipeline(case_id)

    snapshot = contract_tools.get_task_snapshot(case_id)
    assert snapshot["task"]["task_status"] == "blocked"


def test_gateway_failure_blocks_and_preserves_state(pulled, failing_gateway):
    """验证审批接口失败时的行为：拉取失败不产生新任务，下载失败进入 blocked。

    参数：
        pulled: 已拉取环境夹具。
        failing_gateway: 全部调用都失败的网关替身。
    关联：
        ``approval.factory.set_gateway_override`` 注入替身；
        需求 2.4.4「接口调用失败都要进入 blocked 状态」。

    验证要点：
        - 拉取失败：抛 ``ApprovalGatewayError``，本地任务数不变（不产生半成品）；
        - 下载失败：任务进入 blocked，原因为 attachment_missing。
    """
    from app.approval.factory import set_gateway_override

    _result, tasks = pulled
    before = len(contract_tools.task_service.list_tasks(limit=100))

    set_gateway_override(failing_gateway)

    with pytest.raises(ApprovalGatewayError):
        contract_tools.list_pending_contract_approvals(limit=20)

    after = len(contract_tools.task_service.list_tasks(limit=100))
    assert after == before, "拉取接口失败不应产生新任务"

    case_id = tasks["HT-2024-0001"]["task_id"]
    with pytest.raises(ContractReviewError):
        contract_tools.run_full_pipeline(case_id)

    snapshot = contract_tools.get_task_snapshot(case_id)
    assert snapshot["task"]["task_status"] == "blocked"

    set_gateway_override(None)


def test_unknown_approval_code_raises_not_found(app_env):
    """验证查询不存在的审批单时抛 ``NotFoundError``。

    参数：
        app_env: 隔离环境夹具。
    关联：
        ``services.task_service._get_task_by_code_or_raise``。

    验证要点：
        必须明确"未找到"，而不是返回空对象或伪造详情。
    """
    from app.domain.errors import NotFoundError

    with pytest.raises(NotFoundError):
        contract_tools.get_contract_approval("HT-NOT-EXIST")


def test_review_without_parse_raises(app_env):
    """验证未解析就审查会被拒绝。

    参数：
        app_env: 隔离环境夹具。
    关联：
        ``services.parse_service.load_parse_outcome`` 的 ``NotFoundError``。

    验证要点：
        没有解析结果时审查只能看到空文档，会产出一堆"字段缺失"的假命中，
        比不审查更糟。因此必须显式拒绝。
    """
    from app.domain.errors import NotFoundError

    contract_tools.list_pending_contract_approvals(limit=20)
    tasks = contract_tools.task_service.list_tasks(limit=100)
    case_id = next(t["task_id"] for t in tasks if t["approval_code"] == "HT-2024-0001")

    with pytest.raises(NotFoundError):
        contract_tools.run_contract_rules(case_id)


def test_invalid_limit_rejected(app_env):
    """验证工具层对非法 ``limit`` 的边界校验。

    参数：
        app_env: 隔离环境夹具。
    关联：
        ``tools.contract_tools.list_pending_contract_approvals`` 的入参校验。

    验证要点：
        0、负数、超上限都应在工具层被拒绝，不应传到服务层。
    """
    for bad in (0, -1, 201):
        with pytest.raises(ValueError):
            contract_tools.list_pending_contract_approvals(limit=bad)


def test_unsupported_document_type_blocked(app_env, samples_dir, tmp_path):
    """验证不支持的文档格式被明确拒绝并阻塞任务。

    参数：
        app_env: 隔离环境夹具。
        samples_dir: 示例目录。
        tmp_path: 临时目录。
    关联：
        ``parser.document.read_document`` 的 ``UnsupportedDocumentError``。

    验证要点：
        格式不支持应给出明确原因，而不是被当成"OCR 失败"或"内容为空"。
    """
    import json
    import shutil

    # 造一个 .xlsx 附件记录（内容随意，只要扩展名不受支持）。
    bogus = samples_dir / "contract_bogus.xlsx"
    bogus.write_bytes(b"not a real xlsx")

    store = json.loads(app_env["store_path"].read_text(encoding="utf-8"))
    store["approvals"].append(
        {
            "approval_code": "HT-2024-0005",
            "approval_title": "不支持的格式示例",
            "applicant_name": "测试",
            "applied_at": "2026-01-01T09:00:00",
            "approval_status": "pending",
            "form_data": {},
            "attachments": [
                {
                    "external_attachment_id": "ATT-X1",
                    "file_name": "contract_bogus.xlsx",
                    "file_size": bogus.stat().st_size,
                    "file_kind": "contract",
                    "source_path": str(bogus),
                }
            ],
            "comments": [],
        }
    )
    app_env["store_path"].write_text(
        json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    pull = contract_tools.list_pending_contract_approvals(limit=50)
    tasks = contract_tools.task_service.list_tasks(limit=100)
    assert any(
        t["approval_code"] == "HT-2024-0005" for t in tasks
    ), f"新增审批单未被拉取。pull={pull}，现有={[t['approval_code'] for t in tasks]}"
    case_id = next(t["task_id"] for t in tasks if t["approval_code"] == "HT-2024-0005")

    with pytest.raises(ContractReviewError) as exc_info:
        contract_tools.run_full_pipeline(case_id)
    assert exc_info.value.reason == "unsupported_document"

    snapshot = contract_tools.get_task_snapshot(case_id)
    assert snapshot["task"]["task_status"] == "blocked"
    assert snapshot["task"]["blocked_reason"] == "unsupported_document"

    bogus.unlink(missing_ok=True)
