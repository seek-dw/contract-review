"""审查服务：编排规则审查并把命中写入数据库。

职责：
- 从数据库载入启用中的规则，转成规则引擎所需的 :class:`RuleSpec`；
- 调用 ``review/rule_engine.evaluate_rules`` 得到审查结论；
- 把命中写入 ``rule_hits``（含规则快照），供历史回查；
- 重复审查时先清理该任务的旧命中，避免命中翻倍。

非职责：
- 不执行匹配逻辑（见 ``review/rule_engine.py``）；
- 不生成回写文本（见 ``result_service.py``）。

设计原因：
1. **命中记录带规则快照**。规则是可编辑配置，若命中记录只存 ``rule_id``，
   日后改了规则名称或等级，历史命中展示就会"变成另一个规则"。
   因此写入时同时保存规则编码、名称、等级、建议的快照。
2. **重复审查先清旧命中**。同一任务可能因重试而多次审查，
   若不清旧记录，命中数会累加，统计与展示全部失真。
   清空范围严格限定为"本任务的命中"，不影响其他任务。
3. **只取启用规则**。停用规则不参与求值，但已存在的历史命中记录保留
   （在 ``rule_hits`` 里），因为那是"当时确实命中过"的事实。

主要协作模块：
- ``tools/contract_tools.py::run_contract_rules`` 是入口；
- ``result_service.save_review_result`` 消费本模块产出的 :class:`ReviewOutcome`。
"""

from __future__ import annotations

from app.db.base import session_scope
from app.db.models import ReviewRule, RuleHit
from app.domain.enums import (
    HitStatus,
    LogType,
    MatchMode,
    RiskLevel,
    RuleStatus,
    TaskStatus,
    max_risk_level,
)
from app.domain.schemas import ReviewOutcome, RuleHitView
from app.review.deep_rules import run_deep_checks
from app.review.rule_engine import RuleSpec, evaluate_rules
from app.review.trap_rules import run_trap_checks
from app.services.log_service import append_log_in_session
from app.services.parse_service import load_parse_outcome
from app.services.task_service import get_task, mark_status

# 深层检查的数量。仅用于在"执行规则数"里如实计入，
# 让审批人知道本次审查的覆盖面包含了几项跨条款推理。
# 与 deep_rules._CHECKS 的长度保持一致；数量变了要同步改这里。
_DEEP_CHECK_COUNT = (
    "D001", "D002", "D003", "D004", "D005",
)

# 合同陷阱检查的数量（来自真实司法案例）。同上，仅用于统计展示。
_TRAP_CHECK_COUNT = (
    "T001", "T002", "T003", "T004", "T005", "T006",
    "T007", "T008", "T009", "T010", "T011", "T012",
)


def load_enabled_rules() -> list[RuleSpec]:
    """载入全部启用中的规则，按优先级升序。

    返回：
        :class:`RuleSpec` 列表。
    关联：
        被 :func:`run_review` 调用。
    说明：
        排序在这里完成而不是在引擎里，因为"先求值哪条规则"属于配置语义
        （priority 字段的职责），不是算法细节。
    """
    with session_scope() as db:
        rows = (
            db.query(ReviewRule)
            .filter(ReviewRule.rule_status == RuleStatus.ENABLED.value)
            .order_by(ReviewRule.priority.asc(), ReviewRule.id.asc())
            .all()
        )
        return [
            RuleSpec(
                rule_id=row.id,
                rule_code=row.rule_code,
                rule_name=row.rule_name,
                risk_level=RiskLevel(row.risk_level),
                match_mode=MatchMode(row.match_mode),
                match_text=row.match_text,
                suggestion_text=row.suggestion_text,
                priority=row.priority,
                condition_json=row.condition_json,
            )
            for row in rows
        ]


def run_review(task_id: int) -> ReviewOutcome:
    """对任务执行规则审查。

    参数：
        task_id: 任务主键。
    返回：
        :class:`ReviewOutcome` 审查结论。
    关联：
        被 ``tools/contract_tools.py::run_contract_rules`` 与
        ``result_service`` 的编排调用。
    异常：
        任务不存在或无解析记录时抛 :class:`NotFoundError`。
    副作用：
        清理并重写该任务的 ``rule_hits``；把任务状态推进到 ``reviewing``。
    """
    task = get_task(task_id)

    # 校验：没有解析结果就不应审查，否则规则只能看到空文档，
    # 会产出一堆"字段缺失"的假命中，比不审查更糟。
    outcome = load_parse_outcome(task_id)
    rules = load_enabled_rules()

    review = evaluate_rules(outcome, rules)

    # 合并深层检查结果与陷阱检查结果。
    #
    # 三层规则的分工：
    #   1. 浅层（seed_rules，26 条）：单条款特征匹配，法务扫一眼就能发现；
    #   2. 深层（deep_rules，5 条）：跨条款交叉比对，需要运算才能发现；
    #   3. 陷阱（trap_rules，7 条）：来自真实司法案例，每条都有法条依据，
    #      告诉审批人"这样约定会导致什么法律后果"。
    #
    # 第 3 层是价值最高的：它不是"我觉得有问题"，
    # 而是"按《民法典》第 X 条，这样写会导致 Y 后果"。
    deep_hits = run_deep_checks(outcome)
    trap_hits = run_trap_checks(outcome)
    merged_hits = _merge_hits(review.hits, deep_hits, trap_hits)
    overall = max_risk_level([h.risk_level.value for h in merged_hits]) if merged_hits else review.overall_risk_level

    # 重新生成摘要与关注点：必须基于**合并后**的命中集合。
    #
    # 为什么不能沿用 evaluate_rules 生成的 summary_text：
    #   那份摘要只统计了浅层规则的命中数与风险分布，
    #   深层规则命中后（例如 D002 高风险）摘要里不会体现，
    #   会出现"回写文本说命中 5 条、但实际命中 7 条"的不一致。
    #   实测该问题：规则数已从 26 变为 31，但回写文本仍写"规则执行 26 条"。
    from app.review.rule_engine import _build_focus_points, _build_summary

    merged_outcome = ReviewOutcome(
        overall_risk_level=overall,
        hits=merged_hits,
        summary_text="",
        focus_points=(),
        evaluated_rule_count=(
            review.evaluated_rule_count
            + len(_DEEP_CHECK_COUNT)
            + len(_TRAP_CHECK_COUNT)
        ),
    )
    summary = _build_summary(merged_outcome)
    focus_points = _build_focus_points(merged_hits)

    review = ReviewOutcome(
        overall_risk_level=overall,
        hits=merged_hits,
        summary_text=summary,
        focus_points=focus_points,
        evaluated_rule_count=merged_outcome.evaluated_rule_count,
    )
    _replace_hits(task_id, review.hits)

    mark_status(
        task_id,
        TaskStatus.REVIEWING,
        f"规则审查完成，执行 {review.evaluated_rule_count} 条规则，"
        f"命中 {len(review.hits)} 条"
        f"（陷阱 {len(trap_hits)} 条、深层推理 {len(deep_hits)} 条），"
        f"整体风险 {review.overall_risk_level.value}",
    )

    with session_scope() as db:
        append_log_in_session(
            db, LogType.REVIEW,
            f"审查完成：审批编号 {task['approval_code']}，"
            f"规则 {review.evaluated_rule_count} 条、命中 {len(review.hits)} 条"
            f"（深层推理 {len(deep_hits)} 条）、"
            f"整体风险 {review.overall_risk_level.value}",
            task_id=task_id,
        )
    return review


def _merge_hits(
    rule_hits: tuple[RuleHitView, ...],
    deep_hits: list[RuleHitView],
    trap_hits: list[RuleHitView] | None = None,
) -> tuple[RuleHitView, ...]:
    """合并三层规则的命中，按风险降序。

    参数：
        rule_hits: 浅层规则命中（seed_rules）。
        deep_hits: 深层推理命中（deep_rules，编码前缀 D）。
        trap_hits: 合同陷阱命中（trap_rules，编码前缀 T）。
    返回：
        合并后的命中元组。
    关联：
        被 :func:`run_review` 调用。

    排序策略：
        1. 先按风险等级降序（高→中→低）；
        2. 同级时按"信息价值"排：陷阱(T) > 深层(D) > 浅层。
           理由：陷阱类命中带法条依据，是审批人最需要看到的；
           浅层命中（如"预付款 60% 偏高"）法务自己也能发现，排最后。
    """
    weight = {"high": 0, "medium": 1, "low": 2}

    def sort_key(hit: RuleHitView) -> tuple[int, int]:
        """计算命中项的排序键。

        参数：
            hit: 单条命中。
        返回：
            ``(风险权重, 层级权重)`` 二元组，越小越靠前。
        关联：
            作为 ``sorted`` 的 key 被 :func:`_merge_hits` 使用。

        排序规则：
            第一维按风险（高→中→低）；
            第二维按信息价值（陷阱 T > 深层 D > 浅层），
            保证同级时带法条依据的命中优先呈现。
        """
        code = hit.rule_code
        if code.startswith("T"):
            tier = 0   # 陷阱：有法条依据，最高优先级
        elif code.startswith("D"):
            tier = 1   # 深层推理
        else:
            tier = 2   # 浅层特征
        return (weight.get(hit.risk_level.value, 9), tier)

    all_hits = list(rule_hits) + list(deep_hits) + list(trap_hits or [])
    return tuple(sorted(all_hits, key=sort_key))


def _replace_hits(task_id: int, hits: tuple[RuleHitView, ...]) -> None:
    """用新的命中集合替换该任务的旧命中。

    参数：
        task_id: 任务主键。
        hits: 本次审查的命中列表。
    返回：
        无。
    关联：
        被 :func:`run_review` 调用。
    设计原因：
        采用"删除 + 重写"而非"逐条更新"：命中集合本身会随规则变更而变化
        （某条规则被停用后不再命中），逐条更新无法处理"本次少了几条"的情况。
        删除范围限定在 ``task_id``，因此不会影响其他任务的历史记录。
    """
    with session_scope() as db:
        db.query(RuleHit).filter(RuleHit.task_id == task_id).delete(
            synchronize_session=False
        )
        for hit in hits:
            db.add(
                RuleHit(
                    task_id=task_id,
                    rule_id=hit.rule_id,
                    evidence_text=hit.evidence_text,
                    evidence_position=hit.evidence_position,
                    hit_status=HitStatus.ACTIVE.value,
                    # 规则快照：即使规则日后被改名、改等级或删除，
                    # 这条命中仍能还原"当时命中的是什么规则、什么等级、什么建议"。
                    rule_code_snapshot=hit.rule_code,
                    rule_name_snapshot=hit.rule_name,
                    risk_level_snapshot=hit.risk_level.value,
                    suggestion_snapshot=hit.suggestion_text,
                )
            )


def list_hits(task_id: int) -> list[dict]:
    """查询任务的命中列表。

    参数：
        task_id: 任务主键。
    返回：
        命中字典列表，按风险等级降序（高→中→低），同级按主键升序。
        每项含规则名称、风险等级、命中证据、证据位置、建议（需求 2.4.6）。
    关联：
        被 ``web/routes.py`` 的「规则命中模块」与 ``result_service`` 使用。
    """
    # 风险等级排序权重，与 app.domain.enums.RISK_ORDER 保持一致口径。
    weight = {"high": 0, "medium": 1, "low": 2}
    with session_scope() as db:
        rows = (
            db.query(RuleHit)
            .filter(RuleHit.task_id == task_id)
            .order_by(RuleHit.id.asc())
            .all()
        )
        items = [
            {
                "hit_id": row.id,
                "rule_id": row.rule_id,
                "rule_code": row.rule_code_snapshot,
                "rule_name": row.rule_name_snapshot,
                "risk_level": row.risk_level_snapshot,
                "evidence_text": row.evidence_text,
                "evidence_position": row.evidence_position,
                "suggestion_text": row.suggestion_snapshot,
                "hit_status": row.hit_status,
            }
            for row in rows
        ]
    # 在 Python 侧排序：risk_level 是字符串，直接 SQL 排序会得到字母序（high<low<medium），
    # 不符合业务期望的"高→中→低"。
    items.sort(key=lambda item: weight.get(item["risk_level"], 9))
    return items


def review_summary(task_id: int) -> dict:
    """汇总任务当前审查状态。

    参数：
        task_id: 任务主键。
    返回：
        字典，含 ``hit_count``、``overall_risk_level``、``by_level``（分级计数）。
    关联：
        被调用端「结果展示区」与统计展示使用。
    """
    hits = list_hits(task_id)
    by_level: dict[str, int] = {}
    for hit in hits:
        by_level[hit["risk_level"]] = by_level.get(hit["risk_level"], 0) + 1

    overall = "low"
    for level in ("high", "medium", "low"):
        if by_level.get(level):
            overall = level
            break

    return {
        "task_id": task_id,
        "hit_count": len(hits),
        "overall_risk_level": overall,
        "by_level": by_level,
    }


def update_hit_status(hit_id: int, status: str) -> dict:
    """人工更新单条命中的状态（确认/忽略）。

    参数：
        hit_id: 命中主键。
        status: 目标状态，取值见 :class:`app.domain.enums.HitStatus`。
    返回：
        更新后的命中字典。
    关联：
        被调用端「规则命中模块」的人工确认按钮调用。
    说明：
        不删除命中记录，只改状态——需求要求命中结果可回查，
        删除会让"曾经命中过什么"这一事实消失。
    """
    with session_scope() as db:
        row = db.get(RuleHit, hit_id)
        if row is None:
            from app.domain.errors import NotFoundError

            raise NotFoundError(f"命中记录不存在：hit_id={hit_id}")
        row.hit_status = status
        append_log_in_session(
            db, LogType.REVIEW,
            f"命中记录状态更新：hit_id={hit_id}（{row.rule_name_snapshot}）→ {status}",
            task_id=row.task_id,
        )
        return {
            "hit_id": row.id,
            "task_id": row.task_id,
            "rule_name": row.rule_name_snapshot,
            "risk_level": row.risk_level_snapshot,
            "hit_status": row.hit_status,
        }
