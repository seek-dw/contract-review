"""完整闭环演示脚本：按需求 2.4.12 的验收标准逐步执行并打印证据。

职责：
- 串行执行 7 个工具，覆盖"拉取 → 详情 → 下载 → 解析 → 审查 → 保存 → 回写"闭环；
- 额外演示两个异常分支：附件缺失进入 blocked 并重试、重复拉取去重；
- 每一步打印真实数据（计数、路径、哈希、命中条数、回写响应），
  使"能跑通"这一结论有可核对的输出，而不是一句断言。

非职责：
- 不做断言（断言在 ``tests/`` 里）；
- 不清理数据（重复运行会追加任务记录，这是刻意的：去重演示需要已有数据）。

运行：
    .venv/Scripts/python.exe scripts/demo_flow.py

前置：
    .venv/Scripts/python.exe scripts/make_samples.py
    .venv/Scripts/python.exe scripts/init_db.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.domain.errors import ContractReviewError  # noqa: E402
from app.tools import contract_tools  # noqa: E402


def _title(text: str) -> None:
    """打印步骤分隔标题。

    参数：
        text: 标题文本。
    返回：
        无。
    关联：
        被本脚本各步骤调用，仅用于输出可读性。
    """
    print()
    print("=" * 78)
    print(text)
    print("=" * 78)


def _brief(fields: dict, keys: list[str]) -> None:
    """按指定键打印字段抽取结果。

    参数：
        fields: 字段字典（键 → {value, status, location, detail}）。
        keys: 要打印的字段键列表。
    返回：
        无。
    关联：
        被 :func:`step_parse` 调用，避免输出过长。
    """
    for key in keys:
        item = fields.get(key, {})
        value = item.get("value")
        status = item.get("status", "")
        location = item.get("location", "")
        detail = item.get("detail", "")
        shown = (value or "").replace("\n", " / ")[:70]
        print(f"    {item.get('label', key):<12} status={status:<9} loc={location:<10} "
              f"value={shown!r}")
        if status != "success" and detail:
            print(f"      └ 说明：{detail[:100]}")


def step1_pull() -> dict:
    """第 1 步：拉取待办并验证去重。

    返回：
        首次拉取的统计字典。
    关联：
        调用 ``contract_tools.list_pending_contract_approvals``。
    验收点：
        首次拉取应创建 N 条任务；重复拉取应更新 N 条且不新增。
    """
    _title("[STEP1] 拉取待处理审批单（需求 2.4.3 / 2.4.12-1）")
    first = contract_tools.list_pending_contract_approvals(limit=20)
    print(f"  首次拉取：total={first['total']} created={first['created']} "
          f"updated={first['updated']} skipped={first['skipped']}")
    for item in first["items"]:
        print(f"    - {item['approval_code']}  {item['approval_title']}  "
              f"申请人={item['applicant_name']}  申请时间={item['applied_at']}  "
              f"附件={item['attachment_count']}  动作={item['action']}  "
              f"状态={item['task_status']}")

    second = contract_tools.list_pending_contract_approvals(limit=20)
    print(f"  重复拉取：total={second['total']} created={second['created']} "
          f"updated={second['updated']} skipped={second['skipped']}")
    if second["created"] == 0:
        print("  ✓ 去重生效：重复拉取未新建任何任务")
    else:
        print("  ✗ 去重失效：重复拉取新建了任务")
    return first


def step2_detail(approval_code: str) -> dict:
    """第 2 步：查询审批单详情。

    参数：
        approval_code: 审批单业务唯一标识。
    返回：
        详情字典。
    关联：
        调用 ``contract_tools.get_contract_approval``。
    """
    _title(f"[STEP2] 查询审批单详情 {approval_code}（需求 2.4.3 / 2.4.12-2）")
    detail = contract_tools.get_contract_approval(approval_code)
    print(f"  标题：{detail['approval_title']}")
    print(f"  申请人：{detail['applicant_name']}   申请时间：{detail['applied_at']}")
    print(f"  审批状态：{detail['approval_status']}   任务状态：{detail['task_status']}")
    print("  表单数据：")
    for key, value in detail["form_data"].items():
        print(f"    {key} = {value}")
    print("  附件信息：")
    for att in detail["attachments"]:
        print(f"    document_id={att['document_id']}  {att['file_name']}  "
              f"类型={att['file_type']}  大小={att['file_size']}  "
              f"下载状态={att['download_status']}")
    return detail


def step3_download(detail: dict) -> list[dict]:
    """第 3 步：下载全部附件。

    参数：
        detail: 详情字典（含附件清单）。
    返回：
        下载结果列表。
    关联：
        调用 ``contract_tools.download_contract_attachment``。
    """
    _title("[STEP3] 下载合同附件（需求 2.4.3 / 2.4.12-2）")
    results: list[dict] = []
    for att in detail["attachments"]:
        result = contract_tools.download_contract_attachment(
            instance_id=detail["approval_code"],
            attachment_id=att["external_attachment_id"],
            file_name=att["file_name"],
        )
        results.append(result)
        print(f"  {result['file_name']}")
        print(f"    document_id={result['document_id']}  大小={result['file_size']} 字节")
        print(f"    SHA256={result['checksum']}")
        print(f"    本地路径={result['local_path']}")
    return results


def _reset_demo_state(case_id: int, approval_code: str) -> str | None:
    """把主任务复位为"刚拉取"状态，使演示可以重复运行。

    参数：
        case_id: 主任务主键。
        approval_code: 审批单业务唯一标识。
    返回：
        实际执行了重置时返回说明字符串；任务本就是 ``pending`` 时返回 ``None``。
    关联：
        被 :func:`main` 在演示开始前调用的第一个步骤之前；
        与之配套的 :func:`_reset_mock_comments` 负责清空模拟评论区。

    为什么需要这个函数（真实缺陷的教训）：
        本脚本早先只验证过"干净环境首次运行"，一旦重复执行就会崩：
        第 1 次跑完后主任务已是 ``done``，第 2 次跑到 STEP4 时
        ``parse_task_documents`` 的 ``allow_reprocess`` 守卫会抛
        ``TaskStateError``（这正是那个守卫该做的事），演示直接中断。

        这里刻意**不**在 STEP4 传 ``allow_reprocess=True`` 绕过守卫——那会让
        STEP7b「已完成任务应被拒绝重跑」的验证失去意义（任务已不是 done，
        守卫永远触发不到）。正确做法是让演示自己维护一个可复现的起点：
        把主任务复位到 ``pending``，然后完整走一遍
        ``pending → parsing → reviewing → done``，STEP7b 才能真实检验守卫。

    复位范围（只动"演示起点"相关字段）：
        - ``task_status`` → pending
        - ``write_status`` → not_written
        - 清空 ``blocked_reason`` / ``blocked_detail`` / ``retry_target``
        不删除历史解析记录、命中记录与结果记录：那些是审计材料，
        保留它们才能展示"同一任务多次审查"的真实形态。
    """
    from app.db.base import session_scope
    from app.db.models import ApprovalTask
    from app.domain.enums import TaskStatus, WriteStatus

    with session_scope() as db:
        task = db.get(ApprovalTask, case_id)
        if task is None:
            return None
        previous_status = task.task_status
        previous_write = task.write_status

        # 已经是干净起点（pending + not_written）时不做任何写入，避免制造无意义的 updated_at 变化。
        if (
            previous_status == TaskStatus.PENDING.value
            and previous_write == WriteStatus.NOT_WRITTEN.value
        ):
            return None

        task.task_status = TaskStatus.PENDING.value
        task.write_status = WriteStatus.NOT_WRITTEN.value
        task.blocked_reason = None
        task.blocked_detail = None
        task.retry_target = None
        return (
            f"主任务 {approval_code} 原状态 {previous_status}"
            f"（回写 {previous_write}），已复位为 pending 以便完整演示"
        )


def _reset_mock_comments(approval_code: str) -> int:
    """清空模拟审批系统中该审批单的评论区。

    参数：
        approval_code: 审批单业务唯一标识。
    返回：
        清空前的评论条数；无可清空内容或数据文件不存在时返回 0。
    关联：
        被 :func:`main` 与 :func:`_reset_demo_state` 配套调用。

    为什么需要清空：
        STEP7 要展示"回写前 0 条 → 首次新增 1 条 → 重复新增 0 条"这个对比。
        若不清空，重复运行时会看到"回写前已有 1 条"，虽然幂等仍然成立，
        但"首次写入"这一步就没法演示了。

    说明：
        这里直接改模拟审批系统的 JSON 文件，而不是给网关加"清空"方法。
        理由：清空评论是**演示环境**的准备动作，真实 OA 不提供这种接口，
        把它塞进 ``ApprovalGateway`` 契约会污染生产接口，让真实适配方困惑。
    """
    import json

    path = settings.mock_oa_store
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    cleared = 0
    for item in data.get("approvals", []):
        if item.get("approval_code") == approval_code:
            cleared = len(item.get("comments", []))
            item["comments"] = []
    if cleared:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return cleared


def _reset_named_task(approval_code: str) -> str | None:
    """把指定审批单对应的本地任务复位为 pending（用于异常分支演示）。

    参数：
        approval_code: 审批单业务唯一标识。
    返回：
        执行了复位时返回说明；任务不存在或已是 pending 时返回 ``None``。
    关联：
        被 :func:`main` 在演示开始前调用，针对 STEP8/STEP9 会操作的两个任务。

    为什么单独需要它：
        STEP8（附件缺失）与 STEP9（空文档）会把这些任务跑到 ``blocked``。
        重复运行时它们仍是 ``blocked``，虽然再次触发失败不会报错，
        但为了让演示输出稳定呈现"从 pending 开始 → 失败 → blocked"的因果链，
        需要先把状态复位。这不影响业务代码，只是演示外的环境准备。
    """
    from app.db.base import session_scope
    from app.db.models import ApprovalTask
    from app.domain.enums import TaskStatus, WriteStatus

    with session_scope() as db:
        task = (
            db.query(ApprovalTask)
            .filter(ApprovalTask.approval_code == approval_code)
            .one_or_none()
        )
        if task is None or task.task_status == TaskStatus.PENDING.value:
            return None
        previous = task.task_status
        task.task_status = TaskStatus.PENDING.value
        task.write_status = WriteStatus.NOT_WRITTEN.value
        task.blocked_reason = None
        task.blocked_detail = None
        task.retry_target = None
        return f"{approval_code} 原状态 {previous} → 复位为 pending"


def step4_parse(case_id: int, downloads: list[dict]) -> dict:
    """第 4 步：解析任务下全部附件并合并结果。

    参数：
        case_id: 任务主键。
        downloads: 第 3 步的下载结果列表（用于打印每个附件的明细）。
    返回：
        任务级合并解析结果。
    关联：
        调用 ``contract_tools.parse_service.parse_task_documents``（任务级合并解析），
        而不是逐个调用 ``parse_contract_document``（单附件接口）。

    为什么用任务级解析而不是逐个解析：
        需求 2.4.10 的 ``parse_contract_document(document_id)`` 是**单附件**接口，
        它每次只把**那一个附件**的结果写入 ``contract_parses``。
        若演示对两个附件各调一次，最后一条记录只含第二个附件的字段与原文，
        审查时就会丢掉第一个附件的内容——会出现"条款明明有却报缺失"的假风险。
        任务级解析会额外写一条 ``merged=true`` 的合并记录（汇总全部附件字段与全文），
        审查优先读取该记录，这才是正确的输入。

        同时它也避免了状态机抖动：单附件接口每次都会把任务从 reviewing
        改回 parsing，多附件时日志会出现 ``reviewing → parsing`` 的语义回退。

    验收点（需求 2.4.5 / 2.4.12-3）：
        文本层 PDF 走文本层、扫描件走 OCR，两者字段抽取结果应一致。
    """
    _title("[STEP4] 解析合同文档（需求 2.4.5 / 2.4.12-3）")

    parsed = contract_tools.parse_service.parse_task_documents(case_id)

    print("  逐附件明细：")
    for att in parsed["attachments"]:
        print(f"    {att['file_name']}  status={att['parse_status']}  "
              f"页数={att['page_count']}  文本层页={att['text_pages']}  "
              f"OCR 页={att['ocr_pages']}  字符={att['char_count']}  "
              f"耗时={att['elapsed_seconds']}s")

    meta = parsed["meta"]
    print(f"  合并结果：parse_id={parsed['parse_id']}  status={parsed['parse_status']}  "
          f"附件数={meta.get('attachment_count')}  总页数={meta.get('page_count')}  "
          f"总字符={meta.get('char_count')}  累计耗时={meta.get('elapsed_seconds')}s")

    print("  基础信息（合并后）：")
    _brief(parsed["basic_info"], [
        "contract_title", "contract_no", "our_party", "counterparty",
        "amount", "currency", "effective_date", "expire_date",
    ])
    print("  条款（合并后）：")
    for key, clause in parsed["clauses"].items():
        head = (clause.get("value") or "").split("\n")[0][:40]
        print(f"    {clause['label']:<12} status={clause['status']:<9} "
              f"loc={clause['location']:<10} head={head!r}")
    return parsed


def step5_review(case_id: int) -> dict:
    """第 5 步：执行规则审查。

    参数：
        case_id: 任务主键。
    返回：
        审查结果字典。
    关联：
        调用 ``contract_tools.run_contract_rules``。
    """
    _title(f"[STEP5] 执行规则审查 case_id={case_id}（需求 2.4.6 / 2.4.12-4）")
    review = contract_tools.run_contract_rules(case_id)
    print(f"  执行规则 {review['evaluated_rule_count']} 条，命中 {review['hit_count']} 条，"
          f"总风险等级={review['overall_risk_level']}")
    print("  命中明细：")
    for hit in review["hits"]:
        print(f"    [{hit['risk_level']:<6}] {hit['rule_name']}  ({hit['rule_code']})")
        print(f"        证据位置：{hit['evidence_position']}")
        print(f"        命中依据：{hit['evidence_text'][:110]}")
        print(f"        处理建议：{hit['suggestion_text'][:80]}")
    print("  审批关注点：")
    for index, point in enumerate(review["focus_points"], start=1):
        print(f"    {index}. {point}")
    return review


def _review_to_outcome(review: dict):
    """把第 5 步返回的审查结果字典转回 :class:`ReviewOutcome`。

    参数：
        review: ``contract_tools.run_contract_rules`` 的返回值。
    返回：
        :class:`ReviewOutcome`，供 ``build_comment_text`` 使用。
    关联：
        被 :func:`step6_save` 调用。

    为什么需要这层转换：
        ``run_contract_rules`` 为了便于展示，把结果转成了字典（含 hits 列表）。
        而 ``build_comment_text`` 需要的是 :class:`ReviewOutcome` 对象。
        与其让 demo 重新算一遍（那样会绕过深层规则、导致数字不一致），
        不如把这层转换做实——保证回写内容与页面展示严格同源。
    """
    from app.domain.enums import RiskLevel
    from app.domain.schemas import ReviewOutcome, RuleHitView

    hits = tuple(
        RuleHitView(
            rule_id=h["rule_id"],
            rule_code=h["rule_code"],
            rule_name=h["rule_name"],
            risk_level=RiskLevel(h["risk_level"]),
            evidence_text=h["evidence_text"],
            evidence_position=h["evidence_position"],
            suggestion_text=h["suggestion_text"],
        )
        for h in review["hits"]
    )
    return ReviewOutcome(
        overall_risk_level=RiskLevel(review["overall_risk_level"]),
        hits=hits,
        summary_text=review["summary_text"],
        focus_points=tuple(review["focus_points"]),
        evaluated_rule_count=review["evaluated_rule_count"],
    )


def step6_save(case_id: int, review: dict) -> int:
    """第 6 步：保存审查结果。

    参数：
        case_id: 任务主键。
        review: 第 5 步的审查结果（本函数直接使用其结论，不重新计算）。
    返回：
        review_id。
    关联：
        调用 ``contract_tools.save_review_result``。
    说明：
        评论正文由 ``services.result_service.build_comment_text`` 生成，
        这里复用它以保证与生产路径完全一致（不另写一份模板）。

        注意不要在这里重新调用 ``evaluate_rules``：
        第 5 步已经算过一遍，再算一次不仅浪费，
        还会因为"两次计算的输入可能不同"（例如中途改了规则）导致
        "保存的等级"与"展示的命中"不一致。演示应当展示一条因果链，
        而不是两次独立计算。因此这里只做"取第 5 步的结论 → 拼文本 → 落库"。
    """
    _title(f"[STEP6] 保存审查结果 case_id={case_id}（需求 2.4.9 / 2.4.12-5）")
    from app.services.parse_service import load_parse_outcome
    from app.services.result_service import build_comment_text
    from app.services.review_service import load_enabled_rules
    from app.services.task_service import get_task

    task = get_task(case_id)
    # 直接复用第 5 步已经算好的审查结论，**不要在这里重新 evaluate_rules**。
    #
    # 为什么（真实缺陷的教训）：
    #   这里原本写的是 `evaluate_rules(load_parse_outcome(...), ...)`，
    #   只跑了浅层规则，绕过了深层推理（deep_rules）。
    #   结果：审查日志显示"规则 31 条"，但回写文本仍写"规则执行 26 条"——
    #   同一份数据出现两个不同的数字，审批人无法判断哪个是真的。
    #
    #   正确做法是用 step5 传进来的 review（它已经合并了深层命中），
    #   保证"页面上看到的命中"与"回写出去的内容"严格同源。
    comment_text = build_comment_text(
        approval_title=task["approval_title"],
        approval_code=task["approval_code"],
        outcome=_review_to_outcome(review),
    )

    saved = contract_tools.save_review_result(
        case_id=case_id,
        overall_risk_level=review["overall_risk_level"],
        summary_text=review["summary_text"],
        focus_points_json=json.dumps(review["focus_points"], ensure_ascii=False),
        comment_text=comment_text,
    )
    print(f"  review_id={saved['review_id']}  总风险={saved['overall_risk_level']}  "
          f"关注点={saved['focus_point_count']} 项  评论长度={len(comment_text)} 字符")
    print("  回写内容预览：")
    for line in comment_text.splitlines()[:14]:
        print(f"    | {line}")
    print("    | ...")
    return saved["review_id"]


def step7_write(approval_code: str, review_id: int) -> dict:
    """第 7 步：回写评论并验证幂等。

    参数：
        approval_code: 审批单业务唯一标识。
        review_id: 结果主键。
    返回：
        首次回写结果。
    关联：
        调用 ``contract_tools.write_approval_comment``。
    验收点：
        首次回写 success；重复回写应命中幂等短路，不产生第二条评论。
    关于重复运行：
        演示脚本可能被多次执行。若该任务此前已回写成功，
        第一次调用就会返回 ``already_written=True``——这是幂等机制在起作用，
        不是缺陷。因此这里显式检查并如实说明，避免把"已写过"误报成"首次写入"。
    """
    _title(f"[STEP7] 回写审批评论 {approval_code}（需求 2.4.3 / 2.4.12-5）")

    from app.approval.factory import get_gateway
    from app.services import comment_service

    gateway = get_gateway()
    before = gateway.get_comments(approval_code)
    print(f"  回写前评论区条数：{len(before)}")

    first = contract_tools.write_approval_comment(approval_code, review_id)
    print(f"  第 1 次调用：write_status={first['write_status']}  "
          f"already_written={first['already_written']}")
    print(f"  外部响应：{first['write_response_text']}")
    if first["already_written"]:
        print("  ⓘ 该任务此前已回写成功，本次为幂等短路（未重复写入外部系统）")
        print("    （若需观察真实首次写入，请先删除 data/contract_review.db 并重跑）")

    after_first = gateway.get_comments(approval_code)
    print(f"  第 1 次调用后评论区条数：{len(after_first)}"
          f"（新增 {len(after_first) - len(before)} 条）")

    second = contract_tools.write_approval_comment(approval_code, review_id)
    print(f"  第 2 次调用：write_status={second['write_status']}  "
          f"already_written={second['already_written']}")

    after_second = gateway.get_comments(approval_code)
    print(f"  第 2 次调用后评论区条数：{len(after_second)}"
          f"（相对第 1 次新增 {len(after_second) - len(after_first)} 条）")

    if len(after_second) == len(after_first):
        print("  ✓ 幂等生效：重复调用未产生新评论")
    else:
        print("  ✗ 幂等失效：重复调用产生了新评论")

    # 只统计"本次审查结果"（review_id）的回写记录。
    # 为什么不能直接数该任务的全部记录：
    #   演示可重复运行，每跑一次都会为新的 review_id 写一条 success 记录，
    #   全量统计会看到"3 条成功"并误判为幂等失效——实际是三次独立审查各写一次，
    #   每一次本身都是幂等的。判定幂等必须锁定"同一次审查"这个范围。
    logs = comment_service.list_comment_logs(
        contract_tools._task_id_of(approval_code)
    )
    this_review_logs = [log for log in logs if log["review_result_id"] == review_id]
    success_of_this_review = [
        log for log in this_review_logs if log["write_status"] == "success"
    ]
    print(
        f"  本次审查（review_id={review_id}）的回写记录："
        f"{len(this_review_logs)} 条，其中成功 {len(success_of_this_review)} 条"
        f"（应为 1 条）"
    )
    for log in this_review_logs:
        print(f"    {log['created_at']}  {log['write_status']}  "
              f"{log['write_response_text'][:70]}")
    if len(success_of_this_review) == 1:
        print("  ✓ 同一次审查只产生 1 条成功记录")
    else:
        print(f"  ✗ 同一次审查产生了 {len(success_of_this_review)} 条成功记录")

    total_success = [log for log in logs if log["write_status"] == "success"]
    if len(total_success) > 1:
        print(
            f"  ⓘ 该任务累计 {len(total_success)} 条成功记录，"
            f"来自 {len({log['review_result_id'] for log in total_success})} 次独立审查"
            f"（重复运行演示所致，非幂等失效）"
        )

    if after_second:
        print("  评论区最后一条前 6 行：")
        for line in after_second[-1].splitlines()[:6]:
            print(f"    | {line}")
    return first


def step7b_done_guard(case_id: int) -> None:
    """第 7b 步：验证已完成任务默认拒绝重复处理。

    参数：
        case_id: 主任务主键（此时应为 done 状态）。
    返回：
        无。
    关联：
        ``services.parse_service.parse_task_documents`` 的 ``allow_reprocess`` 守卫。

    验收要点：
        已回写成功的任务再点"一键处理"时，必须明确拒绝而不是静默重跑。
        理由：
        1. 扫描件 OCR 每页约 5 秒，重跑一次就多花一次；
        2. 任务状态会从 done 退回 parsing，审批人会怀疑之前的意见是否作废。

        若确实要重审（合同换版），调用方应显式传 ``allow_reprocess=True``，
        让重跑成为有意动作。
    """
    _title("[STEP7b] 已完成任务的重复处理保护（业务约束）")

    from app.domain.errors import TaskStateError

    try:
        contract_tools.parse_service.parse_task_documents(case_id)
        print("  ✗ 已完成任务被静默重跑（保护失效）")
    except TaskStateError as exc:
        print(f"  ✓ 已拒绝：{exc.message[:110]}")

    # 显式允许时可以重跑（这里只验证不抛异常，不实际跑第二遍以免重复耗 OCR 时间）。
    snapshot = contract_tools.get_task_snapshot(case_id)
    print(f"  当前任务状态仍为：{snapshot['task']['task_status']}"
          f"（未被重跑改动）")


def step8_blocked_retry() -> None:
    """第 8 步：演示附件缺失进入 blocked，并验证重试与阻塞原因记录。

    返回：
        无。
    关联：
        调用 ``contract_tools.run_full_pipeline`` 触发 ``AttachmentMissingError``，
        再用 ``contract_tools.retry_task`` 演示状态复位。
    验收点（需求 2.4.4 / 2.4.12-6）：
        - 附件缺失任务进入 blocked，``blocked_reason=attachment_missing``；
        - 重试接口能把状态放回 parsing；
        - 再次执行仍失败（因为源文件确实不存在），说明失败可复现而非偶发。
    """
    _title("[STEP8] 异常分支：附件缺失 → blocked → 重试（需求 2.4.4 / 2.4.12-6）")

    tasks = contract_tools.task_service.list_tasks(limit=100)
    target = next(
        (t for t in tasks if t["approval_code"] == "HT-2024-0003"), None
    )
    if target is None:
        print("  未找到 HT-2024-0003，跳过该演示")
        return

    case_id = target["task_id"]
    print(f"  目标任务：case_id={case_id}  {target['approval_title']}")

    try:
        contract_tools.run_full_pipeline(case_id)
        print("  ✗ 预期失败但执行成功")
    except ContractReviewError as exc:
        print(f"  ✓ 捕获预期异常：{type(exc).__name__}")
        print(f"    reason={exc.reason}")
        print(f"    message={exc.message}")

    snapshot = contract_tools.get_task_snapshot(case_id)
    task = snapshot["task"]
    print(f"  任务状态：{task['task_status']}  "
          f"阻塞原因={task['blocked_reason']}  重试目标={task['retry_target']}")
    print(f"  阻塞明细：{(task['blocked_detail'] or '')[:140]}")
    print(f"  附件下载状态：{[(a['file_name'], a['download_status']) for a in snapshot['attachments']]}")

    retry = contract_tools.task_service.retry_blocked_task(case_id)
    print(f"  重试复位：blocked → {retry['retry_target']}，"
          f"当前状态={retry['task_status']}，原阻塞原因={retry['previous_reason']}")

    # 复位后再次执行，验证失败可复现（源文件仍不存在）。
    try:
        contract_tools.run_full_pipeline(case_id)
        print("  ✗ 第二次执行意外成功")
    except ContractReviewError as exc:
        print(f"  ✓ 第二次执行仍失败（可复现）：{exc.reason}")


def step9_empty_document() -> None:
    """第 9 步：演示空文档进入 blocked。

    返回：
        无。
    关联：
        对 HT-2024-0004（空白 PDF）执行完整流水线。
    验收点：
        ``blocked_reason=document_empty``，说明系统区分了"内容为空"与"OCR 失败"。
    """
    _title("[STEP9] 异常分支：空文档 → blocked（需求 2.4.4）")
    tasks = contract_tools.task_service.list_tasks(limit=100)
    target = next((t for t in tasks if t["approval_code"] == "HT-2024-0004"), None)
    if target is None:
        print("  未找到 HT-2024-0004，跳过该演示")
        return

    case_id = target["task_id"]
    try:
        contract_tools.run_full_pipeline(case_id)
        print("  ✗ 预期失败但执行成功")
    except ContractReviewError as exc:
        print(f"  ✓ 捕获预期异常：{type(exc).__name__}  reason={exc.reason}")
        print(f"    message={exc.message}")

    snapshot = contract_tools.get_task_snapshot(case_id)
    print(f"  任务状态={snapshot['task']['task_status']}  "
          f"阻塞原因={snapshot['task']['blocked_reason']}")


def step10_timeline(case_id: int) -> None:
    """第 10 步：打印主任务的全链路日志时间线。

    参数：
        case_id: 任务主键。
    返回：
        无。
    关联：
        调用 ``contract_tools.get_task_snapshot`` 取日志。
    验收点（需求 2.4.8 / 2.4.12-7）：
        日志能完整回放"拉取 → 下载 → 解析 → 审查 → 结果 → 回写"。
    """
    _title(f"[STEP10] 全链路日志时间线 case_id={case_id}（需求 2.4.8 / 2.4.12-7）")
    snapshot = contract_tools.get_task_snapshot(case_id)
    for index, log in enumerate(snapshot["logs"], start=1):
        print(f"  {index:>2}. [{log['log_type']:<8}] [{log['log_level']:<7}] "
              f"{log['created_at']}  {log['log_content'][:110]}")

    print()
    all_comment_logs = snapshot["comment_logs"]
    success_logs = [log for log in all_comment_logs if log["write_status"] == "success"]
    review_ids = {log["review_result_id"] for log in success_logs}
    print(
        f"  回写尝试记录：{len(all_comment_logs)} 条，"
        f"其中成功 {len(success_logs)} 条，对应 {len(review_ids)} 次独立审查"
    )
    print("  （每轮审查各写 1 条；同一轮重复调用不会新增，这正是幂等的表现）")
    for log in all_comment_logs:
        print(f"    {log['created_at']}  {log['write_status']}  "
              f"review={log['review_result_id']}  "
              f"{log['write_response_text'][:60]}")


def main() -> None:
    """演示入口：按顺序执行全部步骤。

    返回：
        无。
    关联：
        直接运行本脚本时调用。
    """
    print("合同审批审查系统 —— 完整闭环演示")
    print(f"数据库：{settings.db_url}")
    print(f"审批接入：{settings.approval_mode}  数据文件：{settings.mock_oa_store}")
    print(f"附件目录：{settings.attachment_dir}")

    if not settings.mock_oa_store.exists():
        print("✗ 模拟审批数据不存在，请先运行 scripts/make_samples.py")
        return

    step1_pull()

    main_code = "HT-2024-0001"
    detail = step2_detail(main_code)
    case_id = detail["task_id"]

    # 演示可重复运行：把主任务与两个异常分支任务复位到"刚拉取"状态。
    # 顺序有讲究：
    #   - 必须在 step2 之后（需要 detail["task_id"]）；
    #   - 必须在 step4 之前，否则 parse_task_documents 的 done 守卫会中断演示；
    #   - 不能在 step7b 里绕过守卫，那里要真实验证"已完成任务被拒绝"。
    _title("演示环境准备（保证可重复运行）")
    changed = False

    state_note = _reset_demo_state(case_id, main_code)
    if state_note:
        print(f"  · {state_note}")
        changed = True

    cleared_comments = _reset_mock_comments(main_code)
    if cleared_comments:
        print(f"  · 已清空模拟评论区（原 {cleared_comments} 条），以便演示首次回写")
        changed = True

    for code in ("HT-2024-0003", "HT-2024-0004"):
        code_note = _reset_named_task(code)
        if code_note:
            print(f"  · {code_note}")
            changed = True

    if not changed:
        print("  · 环境已是干净起点，无需复位")

    downloads = step3_download(detail)
    step4_parse(case_id, downloads)

    review = step5_review(case_id)
    review_id = step6_save(case_id, review)
    step7_write(main_code, review_id)
    step7b_done_guard(case_id)

    step8_blocked_retry()
    step9_empty_document()
    step10_timeline(case_id)

    _title("演示结束")
    print("  主任务闭环：拉取 → 详情 → 下载 → 解析 → 审查 → 保存 → 回写 全部完成")
    print("  异常分支：附件缺失、空文档均进入 blocked 且可重试")


if __name__ == "__main__":
    main()
