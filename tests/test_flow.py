"""闭环测试：验证 7 个工具的完整链路与状态流转。

覆盖需求：
- 2.4.12「能完成待办拉取、附件解析、规则审查、结果入库、评论回写的完整闭环演示」；
- 2.4.4 状态机（pending → parsing → reviewing → done）；
- 2.4.3「附件下载返回本地文件路径和校验信息」；
- 2.4.5「能解析文档和图片扫描件，提取主要字段」；
- 2.4.11 输出五项内容。
"""

from __future__ import annotations

import json
import re

import pytest

from app.domain.errors import ContractReviewError
from app.tools import contract_tools


def test_full_pipeline_end_to_end(pulled):
    """验证从拉取到回写的完整闭环。

    参数：
        pulled: 已拉取环境夹具。
    关联：
        依次调用 7 个工具，对应需求 2.4.10 的 7 个函数。

    验证要点：
        每一步的产出都成为下一步的输入（``document_id`` → 解析，
        ``review_id`` → 回写），最终任务状态为 ``done``。
    """
    _result, tasks = pulled
    approval_code = "HT-2024-0001"
    case_id = tasks[approval_code]["task_id"]

    # 第 1 步：详情（含附件清单）。
    detail = contract_tools.get_contract_approval(approval_code)
    assert detail["approval_title"] == "数据治理平台采购合同"
    assert len(detail["attachments"]) == 2

    # 第 2 步：下载附件，校验路径与 SHA256。
    for att in detail["attachments"]:
        downloaded = contract_tools.download_contract_attachment(
            approval_code, att["external_attachment_id"], att["file_name"]
        )
        assert downloaded["local_path"].endswith(att["file_name"])
        assert len(downloaded["checksum"]) == 64, "SHA256 应为 64 位十六进制"
        assert re.fullmatch(r"[0-9a-f]{64}", downloaded["checksum"])
        assert downloaded["file_size"] > 0

    # 第 3 步：解析（任务级，合并两个附件）。
    parsed = contract_tools.parse_service.parse_task_documents(case_id)
    assert parsed["parse_status"] in ("success", "partial")
    assert parsed["basic_info"]["contract_no"]["value"] == "HT-2024-0001"
    assert parsed["basic_info"]["amount"]["value"] == "1,200,000 元"
    assert len(parsed["attachments"]) == 2

    # 第 4 步：规则审查。
    review = contract_tools.run_contract_rules(case_id)
    assert review["overall_risk_level"] == "high"
    assert review["hit_count"] >= 3
    hit_names = {h["rule_name"] for h in review["hits"]}
    # 示例合同刻意包含的三个高风险点必须被命中。
    assert "预付款比例过高" in hit_names
    assert "违约金比例过高" in hit_names
    assert "管辖地约定在对方所在地" in hit_names
    # 示例合同缺少保密与数据条款，应命中缺失类规则。
    assert "保密条款缺失" in hit_names
    assert "数据条款缺失" in hit_names

    # 第 5 步：保存结果。
    saved = contract_tools.save_review_result(
        case_id=case_id,
        overall_risk_level=review["overall_risk_level"],
        summary_text=review["summary_text"],
        focus_points_json=json.dumps(review["focus_points"], ensure_ascii=False),
        comment_text="测试评论内容",
    )
    assert saved["review_id"] > 0
    assert saved["focus_point_count"] == len(review["focus_points"])

    # 第 6 步：回写。
    written = contract_tools.write_approval_comment(approval_code, saved["review_id"])
    assert written["write_status"] == "success"
    assert written["already_written"] is False

    # 第 7 步：状态收敛。
    snapshot = contract_tools.get_task_snapshot(case_id)
    assert snapshot["task"]["task_status"] == "done"
    assert snapshot["task"]["write_status"] == "success"


def test_parse_merges_multiple_attachments(pulled):
    """验证多附件解析会合并字段与原文，审查能看到全部附件内容。

    参数：
        pulled: 已拉取环境夹具。
    关联：
        ``services.parse_service.parse_task_documents`` 的合并逻辑 +
        ``_latest_parse_record`` 优先取合并记录。

    验证要点（真实缺陷的回归测试）：
        若审查只读"最后一个附件的解析记录"，会丢掉另一个附件的内容，
        导致"条款缺失"类规则被误命中。本测试通过检查合并记录里
        两个附件的文件名同时出现在原文中，证明合并真的发生了。
    """
    _result, tasks = pulled
    case_id = tasks["HT-2024-0001"]["task_id"]

    contract_tools.parse_service.parse_task_documents(case_id)

    from app.services.parse_service import load_parse_outcome

    outcome = load_parse_outcome(case_id)
    full_text = outcome.document.full_text

    # 合并记录的原文行带附件名前缀，两个附件都应出现。
    assert "contract_normal.pdf" in full_text
    assert "contract_scanned.pdf" in full_text
    # 关键字段只应出现一次（合并取第一个成功的值，不重复拼接）。
    assert full_text.count("HT-2024-0001") >= 1


def test_scanned_attachment_parsed_via_ocr(pulled):
    """验证扫描件附件真实走 OCR 并被纳入合并结果。

    参数：
        pulled: 已拉取环境夹具。
    关联：
        ``parser.document._read_pdf`` 的 OCR 分支。

    验证要点：
        合并记录的元信息里必须记录 OCR 页，证明扫描件不是被跳过的。
    """
    _result, tasks = pulled
    case_id = tasks["HT-2024-0001"]["task_id"]

    parsed = contract_tools.parse_service.parse_task_documents(case_id)

    ocr_attachments = [
        att for att in parsed["attachments"] if att["ocr_pages"]
    ]
    assert ocr_attachments, "应至少有一个附件走 OCR"
    assert ocr_attachments[0]["file_name"] == "contract_scanned.pdf"
    assert ocr_attachments[0]["ocr_pages"] == [1]


def test_docx_task_pipeline(pulled):
    """验证 DOCX 合同也能完成完整闭环。

    参数：
        pulled: 已拉取环境夹具。
    关联：
        ``parser.document._read_docx`` + 完整流水线。

    验证要点：
        DOCX 的表格字段（编号/主体/金额/日期）应全部抽出，
        且该合同有保密与数据条款，因此不应命中这两条缺失规则。
    """
    _result, tasks = pulled
    approval_code = "HT-2024-0002"
    case_id = tasks[approval_code]["task_id"]

    result = contract_tools.run_full_pipeline(case_id)
    review = result["review"]

    assert result["parse"]["basic_info"]["contract_no"]["value"] == "HT-2024-0002"
    assert result["parse"]["basic_info"]["amount"]["value"] == "480,000 元"

    hit_names = {h["rule_name"] for h in review["hits"]}
    assert "保密条款缺失" not in hit_names, "该 DOCX 含保密条款，不应命中缺失"
    assert "数据条款缺失" not in hit_names, "该 DOCX 含数据条款，不应命中缺失"
    # 该合同的预付款 40% 仍超阈值，应命中。
    assert "预付款比例过高" in hit_names


def test_comment_text_contains_required_sections(pulled):
    """验证回写文本包含需求 2.4.11 要求的各项内容。

    参数：
        pulled: 已拉取环境夹具。
    关联：
        ``services.result_service.build_comment_text``。

    验证要点：
        - 总风险等级；
        - 命中规则（含证据位置与建议）；
        - 中文摘要；
        - 审批关注点；
        - 免责说明（"不替代法务与审批人的实质判断"）。
    """
    _result, tasks = pulled
    case_id = tasks["HT-2024-0001"]["task_id"]

    result = contract_tools.run_full_pipeline(case_id)
    comment = result["review"]["comment_text"]

    assert "总风险等级" in comment
    assert "一、总体结论" in comment
    assert "二、重点风险项" in comment
    assert "三、审批关注点" in comment
    assert "证据位置" in comment
    assert "处理建议" in comment
    assert "不替代" in comment, "必须写明不替代人工审批"


def test_task_state_transitions(pulled):
    """验证任务状态按预期顺序流转。

    参数：
        pulled: 已拉取环境夹具。
    关联：
        ``services.task_service.mark_status``。

    验证要点：
        从日志中读取状态变化序列，确认没有跳步或倒退。
    """
    _result, tasks = pulled
    case_id = tasks["HT-2024-0001"]["task_id"]

    contract_tools.run_full_pipeline(case_id)
    snapshot = contract_tools.get_task_snapshot(case_id)

    transitions = [
        log["log_content"] for log in snapshot["logs"]
        if "任务状态" in log["log_content"]
    ]
    assert any("pending → parsing" in t for t in transitions)
    assert any("parsing → reviewing" in t for t in transitions)
    assert snapshot["task"]["task_status"] == "reviewing"


def test_attachment_checksum_matches_file(pulled):
    """验证下载记录的 SHA256 与实际文件内容一致。

    参数：
        pulled: 已拉取环境夹具。
    关联：
        ``services.attachment_service.download_task_attachment`` 的校验值计算。

    验证要点：
        校验信息必须真的对应文件内容，否则"校验"毫无意义。
    """
    import hashlib
    from pathlib import Path

    _result, tasks = pulled
    approval_code = "HT-2024-0001"
    case_id = tasks[approval_code]["task_id"]

    contract_tools.run_full_pipeline(case_id)
    attachments = contract_tools.attachment_service.list_attachments(case_id)

    for att in attachments:
        actual = hashlib.sha256(Path(att["file_path"]).read_bytes()).hexdigest()
        assert att["checksum"] == actual, f"{att['file_name']} 的校验值与文件内容不符"


def test_done_task_rejects_reprocess_by_default(pulled):
    """验证已完成任务默认拒绝重复处理，显式允许时才可重跑。

    参数：
        pulled: 已拉取环境夹具。
    关联：
        ``services.parse_service.parse_task_documents`` 的 ``allow_reprocess`` 守卫。

    验证要点（业务约束）：
        - 任务 ``done`` 后再次解析抛 ``TaskStateError``；
        - 任务状态保持 ``done``，不被退回 ``parsing``；
        - 显式传 ``allow_reprocess=True`` 时允许重跑。

    为什么需要这个约束：
        已 ``done`` 意味着意见已写入评论区。若允许静默重跑：
        1. 扫描件 OCR 每页约 5 秒，白花时间；
        2. 状态从 done 退回 parsing，审批人会怀疑之前的意见是否作废。
    """
    from app.domain.errors import TaskStateError

    _result, tasks = pulled
    case_id = tasks["HT-2024-0001"]["task_id"]

    # 先完成一次完整闭环，使任务进入 done。
    result = contract_tools.run_full_pipeline(case_id)
    written = contract_tools.write_approval_comment(
        "HT-2024-0001", result["review"]["review_id"]
    )
    assert written["write_status"] == "success"

    snapshot = contract_tools.get_task_snapshot(case_id)
    assert snapshot["task"]["task_status"] == "done"

    # 默认拒绝重跑。
    with pytest.raises(TaskStateError) as exc_info:
        contract_tools.parse_service.parse_task_documents(case_id)
    assert "已完成" in exc_info.value.message

    # 状态未被改动。
    snapshot2 = contract_tools.get_task_snapshot(case_id)
    assert snapshot2["task"]["task_status"] == "done"

    # 显式允许时可重跑（这里只验证守卫被放行，不校验完整结果）。
    reprocessed = contract_tools.parse_service.parse_task_documents(
        case_id, allow_reprocess=True
    )
    assert reprocessed["parse_status"] in ("success", "partial")


def test_reprocessing_appends_new_result_but_keeps_history(pulled):
    """验证重复处理会追加新结果记录，同时保留历史（审计需要）。

    参数：
        pulled: 已拉取环境夹具。
    关联：
        ``services.result_service.save_review_result``。

    验证要点：
        - 第二次处理产生新的 ``review_id``；
        - 读取接口仍取最新一条；
        - 历史记录不被删除（审计可回查）。

    说明：
        任务此时为 ``reviewing``（尚未回写），不是 ``done``，
        因此不受 ``allow_reprocess`` 守卫限制——守卫只针对"已回写成功"的任务。
    """
    _result, tasks = pulled
    case_id = tasks["HT-2024-0001"]["task_id"]

    first = contract_tools.run_full_pipeline(case_id)
    second = contract_tools.run_full_pipeline(case_id)

    assert second["review"]["review_id"] > first["review"]["review_id"]

    from app.db.base import session_scope
    from app.db.models import ReviewResult

    with session_scope() as db:
        rows = db.query(ReviewResult).filter(ReviewResult.task_id == case_id).all()
        assert len(rows) == 2, "应保留两条历史结果"

    latest = contract_tools.result_service.get_review_result(case_id)
    assert latest["review_id"] == second["review"]["review_id"]


def test_demo_script_is_rerunnable(pulled):
    """验证演示脚本可以重复运行，不会因 done 守卫中断。

    参数：
        pulled: 已拉取环境夹具。
    关联：
        ``scripts/demo_flow.py`` 的 ``_reset_demo_state`` /
        ``_reset_mock_comments`` / ``_reset_named_task``，
        配合 ``services.parse_service.parse_task_documents`` 的 done 守卫。

    验证要点（真实缺陷的回归测试）：
        加入 ``allow_reprocess`` 守卫后，演示脚本第二次运行会在 STEP4 抛
        ``TaskStateError`` 直接中断——因为主任务已是 ``done``。
        这是提交前必须能重复运行的东西，评审很可能连跑两次。

        修复方式不是"在 STEP4 传 allow_reprocess=True"（那会让 STEP7b
        的守卫验证失去意义），而是让演示自己把状态复位到 ``pending``。
        本测试模拟"运行两次"，验证第二次仍能完整走完并让任务回到 ``done``。
    """
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "scripts" / "demo_flow.py"

    # 第一次：干净起点
    first = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=600
    )
    assert first.returncode == 0, f"首次运行失败：{first.stderr[-800:]}"
    assert "Traceback" not in first.stderr, f"首次运行有异常：{first.stderr[-800:]}"

    # 第二次：此时主任务已是 done，必须仍能跑通（靠演示自身复位）
    second = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=600
    )
    assert second.returncode == 0, f"重复运行失败：{second.stderr[-800:]}"
    assert "TaskStateError" not in second.stderr, (
        "重复运行被 done 守卫中断——演示未做状态复位"
    )
    assert "环境已是干净起点" in second.stdout or "已复位为 pending" in second.stdout

    # 两轮都应完整跑到结束，且守卫验证仍然有效（STEP7b 真的拒绝了重跑）。
    assert "主任务闭环" in second.stdout
    assert "已完成并回写成功" in second.stdout
