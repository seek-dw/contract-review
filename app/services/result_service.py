"""结果服务：保存审查结果并生成回写评论文本。

职责：
- 把 :class:`ReviewOutcome` 落库到 ``review_results``（需求 2.4.9）；
- 生成"回写内容"——最终写入审批评论区的中文文本（需求 2.4.11）；
- 提供结果查询与导出所需的数据组装。

非职责：
- 不执行审查（见 ``review_service``）；
- 不调用外部系统回写（见 ``comment_service``）。

设计原因：
1. **回写文本单独存一份**。``review_results.comment_text`` 保存"实际写出去的内容"，
   而不是每次从命中记录重新拼装。原因：模板会调整、规则会改名，
   若每次都重拼，就无法回答"三个月前那次审查，评论区长什么样"。
   这是对账与审计的硬需求。
2. **评论长度受控**。审批评论区不适合贴几千字。
   本模块按"总风险 → 摘要 → 高风险命中（最多 5 条）→ 关注点（最多 5 条）→ 提示"
   的顺序拼装，超出部分省略并明确告知"完整结果见系统"，
   避免截断造成"看起来结论完整但实际漏了内容"的误导。
3. **必须保留免责说明**。需求 2.4 定位是"不直接代替人工审批"，
   回写内容必须写明这是辅助意见，否则会被当成审批结论使用。

主要协作模块：
- ``tools/contract_tools.py::save_review_result`` 是入口；
- ``comment_service.write_comment_for_result`` 读取 ``review_id`` 对应的评论内容。
"""

from __future__ import annotations

import json

from app.db.base import session_scope
from app.db.models import ReviewResult
from app.domain.enums import LogType, RiskLevel
from app.domain.errors import NotFoundError
from app.domain.schemas import ReviewOutcome
from app.services.log_service import append_log_in_session
from app.services.review_service import list_hits, run_review
from app.services.task_service import get_task

# 风险等级中文标签。与 enums.DISPLAY_NAMES 的取值一致，
# 但这里单独定义是为了让回写文本的措辞可独立调整（页面标签与评论措辞不必相同）。
_RISK_LABELS = {
    RiskLevel.HIGH.value: "高",
    RiskLevel.MEDIUM.value: "中",
    RiskLevel.LOW.value: "低",
}

# 回写文本中最多列出的命中条数与关注点条数。
# 取 5 是因为审批评论区是简读场景，超过 5 条读者会跳过；
# 完整清单在系统页面查看，评论里明确提示这一点。
_MAX_HITS_IN_COMMENT = 5
_MAX_FOCUS_IN_COMMENT = 5


def build_comment_text(
    approval_title: str, approval_code: str, outcome: ReviewOutcome
) -> str:
    """生成写入审批评论区的文本。

    参数：
        approval_title: 审批单标题（合同名称）。
        approval_code: 审批单业务唯一标识。
        outcome: 审查结论。
    返回：
        中文评论文本，可直接写入评论区。
    关联：
        被 :func:`save_review_result` 调用；结果存入 ``review_results.comment_text``。
    结构说明：
        1. 标题行：说明这是系统审查意见而非人工结论；
        2. 总风险等级与命中统计；
        3. 中文摘要；
        4. 重点命中（按风险降序取前 N 条，含证据位置与建议）；
        5. 审批关注点（前 N 条）；
        6. 免责与查看完整结果的提示。
    """
    level = _RISK_LABELS.get(outcome.overall_risk_level.value, outcome.overall_risk_level.value)

    # 区分浅层规则命中与深层推理命中，让审批人一眼看出"系统真正多做了哪些判断"。
    #
    # 为什么要在文本里区分：
    #   浅层规则（看到 60% 报预付款过高）法务自己也能发现，价值有限；
    #   深层推理（付款挂钩验收 + 验收单方裁量）是跨条款运算出来的，
    #   人极易漏掉——这才是系统的增量价值。
    #   不区分的话，两类混在"命中 5 条"里，看不出系统做了什么别人做不到的事。
    deep_hits = [h for h in outcome.hits if h.rule_code.startswith("D")]
    rule_hits = [h for h in outcome.hits if not h.rule_code.startswith("D")]

    lines: list[str] = [
        "【合同审查助手意见】",
        f"合同：{approval_title}（{approval_code}）",
        f"总风险等级：{level}",
        f"规则执行：{outcome.evaluated_rule_count} 条；命中：{len(outcome.hits)} 条",
    ]
    if deep_hits:
        lines.append(
            f"其中深层推理命中：{len(deep_hits)} 条"
            f"（跨条款交叉比对，非单条款特征匹配）"
        )
    lines.extend([
        "",
        "一、总体结论",
        outcome.summary_text,
        "",
    ])

    if outcome.hits:
        lines.append(f"二、重点风险项（共 {len(outcome.hits)} 条，此处列出前 "
                     f"{min(len(outcome.hits), _MAX_HITS_IN_COMMENT)} 条）")
        for index, hit in enumerate(outcome.hits[:_MAX_HITS_IN_COMMENT], start=1):
            hit_level = _RISK_LABELS.get(hit.risk_level.value, hit.risk_level.value)
            # 深层推理命中加标记：让人知道这一条是"系统算出来的"而不是"看一眼就知道的"
            marker = "（深层推理）" if hit.rule_code.startswith("D") else ""
            lines.append(f"{index}. [{hit_level}风险] {hit.rule_name}{marker}")
            lines.append(f"   证据位置：{hit.evidence_position}")
            lines.append(f"   命中依据：{_trim(hit.evidence_text, 200)}")
            lines.append(f"   处理建议：{hit.suggestion_text}")
        if len(outcome.hits) > _MAX_HITS_IN_COMMENT:
            lines.append(
                f"   （其余 {len(outcome.hits) - _MAX_HITS_IN_COMMENT} 条命中请在审查系统查看）"
            )
        lines.append("")

    if outcome.focus_points:
        lines.append("三、审批关注点")
        for index, point in enumerate(outcome.focus_points[:_MAX_FOCUS_IN_COMMENT], start=1):
            lines.append(f"{index}. {point}")
        if len(outcome.focus_points) > _MAX_FOCUS_IN_COMMENT:
            lines.append(
                f"（另有 {len(outcome.focus_points) - _MAX_FOCUS_IN_COMMENT} 项关注点见系统）"
            )
        lines.append("")

    lines.extend(
        [
            "四、说明",
            "本意见由合同审查系统按预设规则自动生成，仅作为审批参考，不替代法务与审批人的实质判断。",
            "系统未发现的条款风险不代表合同无风险，请结合业务背景确认。",
        ]
    )
    return "\n".join(lines)


def _trim(text: str, limit: int) -> str:
    """把文本截断到指定长度并加省略号。

    参数：
        text: 原文。
        limit: 最大长度。
    返回：
        截断后的文本。
    关联：
        被 :func:`build_comment_text` 调用，避免单条证据占满评论。
    """
    text = (text or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def save_review_result(
    task_id: int,
    overall_risk_level: str,
    summary_text: str,
    focus_points_json: str,
    comment_text: str,
) -> int:
    """保存审查结果记录。

    参数：
        task_id: 任务主键。
        overall_risk_level: 总风险等级（``low``/``medium``/``high``）。
        summary_text: 中文摘要。
        focus_points_json: 关注点 JSON 数组字符串。
        comment_text: 最终回写内容。
    返回：
        新建结果记录的主键（即工具接口的 ``review_id``）。
    关联：
        被 ``tools/contract_tools.py::save_review_result`` 调用；
        ``comment_service`` 用返回的 ``review_id`` 执行回写。
    说明：
        本函数只负责落库，不校验参数之间的一致性
        （例如 ``overall_risk_level`` 是否与命中记录一致）——
        那是工具层的职责，服务层信任调用方传入的是刚刚由审查产生的结论。

    关于重复处理：
        同一任务重新处理时会新增一条结果记录，并**保留历史**。
        保留而不是覆盖的理由：审查结论属于审计材料，
        "上一次系统给的意见是什么"必须可回查，否则规则调整后无法解释差异。
        读取侧统一取最新一条（见 :func:`get_review_result`），
        因此累积历史不会影响展示与回写。
        历史记录数量由 ``review_results`` 与 ``rule_hits`` 的清理策略区分：
        命中记录会被重写（因为它是"当前结论的组成部分"），
        结果记录会追加（因为它是"某次结论的快照"）。
    """
    task = get_task(task_id)
    with session_scope() as db:
        record = ReviewResult(
            task_id=task_id,
            overall_risk_level=overall_risk_level,
            summary_text=summary_text,
            focus_points_json=focus_points_json,
            comment_text=comment_text,
        )
        db.add(record)
        db.flush()
        result_id = record.id
        append_log_in_session(
            db, LogType.RESULT,
            f"审查结果已保存：review_id={result_id}，审批编号 {task['approval_code']}，"
            f"总风险 {overall_risk_level}，评论长度 {len(comment_text)} 字符",
            task_id=task_id,
        )
        return result_id


def run_and_save_review(task_id: int) -> dict:
    """执行审查并保存结果（一步完成审查 + 落库 + 生成评论）。

    参数：
        task_id: 任务主键。
    返回：
        字典，含 ``review_id``、``overall_risk_level``、``summary_text``、
        ``focus_points``、``comment_text``、``hits``、``evaluated_rule_count``。
    关联：
        被 ``tools/contract_tools.py`` 的闭环入口与演示脚本调用。
    设计原因：
        工具接口把"执行规则审查"与"保存结果"拆成两个函数（需求 2.4.10 如此规定），
        但实际使用中几乎总是连着做。提供本函数避免调用方重复写编排代码，
        同时保留两个底层函数供需要单独调用的场景使用。
    """
    task = get_task(task_id)
    outcome = run_review(task_id)
    comment_text = build_comment_text(
        approval_title=task["approval_title"],
        approval_code=task["approval_code"],
        outcome=outcome,
    )
    review_id = save_review_result(
        task_id=task_id,
        overall_risk_level=outcome.overall_risk_level.value,
        summary_text=outcome.summary_text,
        focus_points_json=json.dumps(list(outcome.focus_points), ensure_ascii=False),
        comment_text=comment_text,
    )
    return {
        "review_id": review_id,
        "task_id": task_id,
        "approval_code": task["approval_code"],
        "overall_risk_level": outcome.overall_risk_level.value,
        "summary_text": outcome.summary_text,
        "focus_points": list(outcome.focus_points),
        "comment_text": comment_text,
        "evaluated_rule_count": outcome.evaluated_rule_count,
        "hits": list_hits(task_id),
    }


def get_review_result(task_id: int) -> dict | None:
    """查询任务最新一条审查结果。

    参数：
        task_id: 任务主键。
    返回：
        结果字典，含六部分输出（需求 2.4.11）；无结果返回 ``None``。
    关联：
        被调用端「结果展示区」与 ``tools/contract_tools.py::get_review_result`` 使用。
    """
    with session_scope() as db:
        record = (
            db.query(ReviewResult)
            .filter(ReviewResult.task_id == task_id)
            .order_by(ReviewResult.id.desc())
            .first()
        )
        if record is None:
            return None
        return {
            "review_id": record.id,
            "task_id": record.task_id,
            "overall_risk_level": record.overall_risk_level,
            "summary_text": record.summary_text,
            "focus_points": json.loads(record.focus_points_json or "[]"),
            "comment_text": record.comment_text,
            "created_at": record.created_at.isoformat(timespec="seconds"),
        }


def get_result_by_id(review_id: int) -> dict:
    """按结果主键查询审查结果。

    参数：
        review_id: 结果主键（``review_results.id``）。
    返回：
        结果字典。
    关联：
        被 ``comment_service.write_comment_for_result`` 调用，
        需要拿到 ``task_id`` 与 ``comment_text`` 才能执行回写。
    异常：
        记录不存在抛 :class:`NotFoundError`。
    """
    with session_scope() as db:
        record = db.get(ReviewResult, review_id)
        if record is None:
            raise NotFoundError(f"审查结果不存在：review_id={review_id}")
        return {
            "review_id": record.id,
            "task_id": record.task_id,
            "overall_risk_level": record.overall_risk_level,
            "summary_text": record.summary_text,
            "focus_points": json.loads(record.focus_points_json or "[]"),
            "comment_text": record.comment_text,
        }
