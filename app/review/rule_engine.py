"""规则引擎模块：对解析产物执行规则匹配，输出风险结论。

职责：
- 按三种 ``match_mode`` 求值单条规则：``keyword`` / ``missing`` / ``threshold``；
- 生成 :class:`app.domain.schemas.RuleHitView`，含规则名称、风险等级、命中证据、
  证据位置、建议说明（需求 2.4.6 强制五项）；
- 汇总总风险等级与审批关注点（需求 2.4.6、2.4.11）。

非职责：
- 不读写数据库（规则列表由 ``services/review_service.py`` 载入后传入）；
- 不生成回写文本（见 ``services/result_service.py``）；
- 不做语义推理。所有判断都是确定性的文本/数值比较。

设计原因：
需求 2.4 的定位是"辅助审查，不代替人工审批"。因此本模块的输出刻意保持
**可追溯、可复现、可解释**：
- 每条命中都带原文证据与位置，人能一眼核对；
- 数值判断只做比较，不做"这笔金额是否合理"的主观推断；
- 无命中不等于"合同没问题"，``ReviewOutcome`` 里通过 ``evaluated_rule_count``
  说明审查覆盖面，避免让人误以为"没命中 = 安全"。

主要协作模块：
- ``services/review_service.py`` 调用 :func:`evaluate_rules`；
- ``services/result_service.py`` 消费 :class:`ReviewOutcome` 生成摘要与回写文本。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.domain.enums import ExtractStatus, MatchMode, RiskLevel, max_risk_level
from app.domain.schemas import (
    CLAUSE_LABELS,
    ParseOutcome,
    ReviewOutcome,
    RuleHitView,
)
from app.parser.text_utils import (
    extract_amount,
    extract_day_count,
    extract_month_count,
    extract_percent,
    normalize_for_match,
    snippet_around,
)


@dataclass(frozen=True)
class RuleSpec:
    """规则求值所需的规则定义快照。

    用途：把 ORM 的 ``ReviewRule`` 行转换成纯数据，使规则引擎不依赖数据库模型，
    可被单元测试直接构造。
    定义原因：若引擎直接吃 ORM 对象，测试就必须建库；用独立 dataclass 后，
    测试只需构造几个 :class:`RuleSpec` 即可覆盖全部分支。
    """

    # 规则数据库主键，写入命中记录的 ``rule_id``。
    rule_id: int
    # 规则编码，业务唯一。
    rule_code: str
    # 规则名称（中文）。
    rule_name: str
    # 风险等级。
    risk_level: RiskLevel
    # 匹配模式。
    match_mode: MatchMode
    # 匹配文本，语义随模式变化（见 :class:`app.db.models.ReviewRule`）。
    match_text: str
    # 建议说明。
    suggestion_text: str
    # 求值优先级，数值小的先求值。
    priority: int
    # 结构化条件 JSON 字符串，供 threshold 模式解析。
    condition_json: str = "{}"


def _scope_text(outcome: ParseOutcome, scope: str) -> tuple[str, str]:
    """按范围取出待匹配的文本与位置标识。

    参数：
        outcome: 解析产物。
        scope: 范围标识，取 ``full`` 表示全文，或 :data:`CLAUSE_LABELS` 的键。
    返回：
        ``(文本, 位置标识)``；范围内容不存在时返回 ``("", "")``。
    关联：
        被 :func:`_eval_keyword` 与 :func:`_eval_threshold` 调用。
    设计原因：
        keyword 与 threshold 两种模式都需要"在某个范围内查找"，
        统一入口避免两处各自处理 scope 语义导致行为不一致。
    """
    if scope == "full":
        return outcome.document.full_text, "全文检索"

    clause_value = outcome.clause_value(scope)
    if not clause_value:
        return "", ""
    evidence = outcome.clause_evidence(scope)
    location = evidence.location if evidence else ""
    return clause_value, location


def _eval_keyword(rule: RuleSpec, outcome: ParseOutcome) -> RuleHitView | None:
    """求值关键词型规则。

    参数：
        rule: 规则定义。
        outcome: 解析产物。
    返回：
        命中则返回命中视图；未命中返回 ``None``。
    关联：
        被 :func:`evaluate_rules` 调用。
    匹配规则：
        ``match_text`` 以 ``|`` 分隔多个候选，任一候选在范围内出现即命中。
        每个候选既可以是普通关键词，也可以是正则（含正则元字符时按正则处理）。
        ``condition_json`` 可用 ``{"scope": "payment"}`` 限定只在该条款内检索，
        默认 ``full`` 全文检索。
    设计原因：
        很多风险表述需要在特定条款内判断（如"违约金比例"只应看违约条款），
        全文检索会被无关条款误命中；因此允许规则自带范围。
    """
    condition = _parse_condition(rule.condition_json)
    scope = str(condition.get("scope", "full"))
    text, location = _scope_text(outcome, scope)
    if not text:
        return None

    for candidate in rule.match_text.split("|"):
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            # 含正则元字符时按正则匹配，否则按字面量包含匹配。
            if re.search(candidate, text):
                matched_text = candidate
            else:
                continue
        except re.error:
            # 规则配置写错正则时退化为字面量匹配，避免整条流水线因一条坏规则中断。
            if candidate not in text:
                continue
            matched_text = candidate

        return _build_hit(
            rule=rule,
            evidence_text=snippet_around(text, matched_text, 160),
            evidence_position=location,
        )
    return None


def _eval_missing(rule: RuleSpec, outcome: ParseOutcome) -> RuleHitView | None:
    """求值缺失型规则：目标字段或条款不存在时命中。

    参数：
        rule: 规则定义。
        outcome: 解析产物。
    返回：
        命中则返回命中视图；目标存在则返回 ``None``。
    关联：
        被 :func:`evaluate_rules` 调用。
    目标解析：
        ``match_text`` 以 ``|`` 分隔多个目标键，任一目标缺失即命中。
        键可以是基础信息键（见 :data:`app.domain.schemas.BASIC_INFO_LABELS`）
        或条款键（见 :data:`app.domain.schemas.CLAUSE_LABELS`）。
    设计原因：
        "条款缺失"本身就是合同风险（例如没有保密条款）。这类判断无法靠关键词命中，
        必须显式检查"是否存在"，因此需要独立模式。
        证据说明中写明"未检出"，而不是编造原文，符合"证据不可伪造"的要求。
    """
    for target in rule.match_text.split("|"):
        target = target.strip()
        if not target:
            continue

        if target in CLAUSE_LABELS:
            clause = outcome.clauses.get(target)
            # 条款存在且提取到内容 → 不缺失。
            if clause is not None and clause.value:
                continue
            return _build_hit(
                rule=rule,
                evidence_text=f"全文未检出「{CLAUSE_LABELS[target]}」相关内容",
                evidence_position="全文检索",
            )

        # 基础信息字段：提取状态为 success 且值非空才算存在。
        field = outcome.basic_info.get(target)
        if field is not None and field.evidence.status == ExtractStatus.SUCCESS and field.value:
            continue
        label = field.label if field is not None else target
        detail = field.evidence.detail if field is not None else ""
        return _build_hit(
            rule=rule,
            evidence_text=f"未能从合同中提取「{label}」" + (f"（{detail}）" if detail else ""),
            evidence_position=field.evidence.location if field is not None else "全文检索",
        )
    return None


def _eval_threshold(rule: RuleSpec, outcome: ParseOutcome) -> RuleHitView | None:
    """求值阈值型规则：从原文抽出数值与阈值比较，超限即命中。

    参数：
        rule: 规则定义。
        outcome: 解析产物。
    返回：
        命中则返回命中视图；未超限或抽不到数值时返回 ``None``。
    关联：
        被 :func:`evaluate_rules` 调用。
    条件结构（``condition_json``）：
        ``{"scope": "payment", "extract": "percent", "operator": ">", "value": 30}``
        - ``scope``：在哪个范围抽数（``full`` 或条款键）；
        - ``extract``：抽取类型，支持 ``percent`` / ``day`` / ``month`` / ``amount``；
        - ``operator``：``>`` ``>=`` ``<`` ``<=`` ``==`` ``!=``；
        - ``value``：比较值。
    设计原因：
        金额、比例、期限必须由确定性代码比较，不能让模型"读一读觉得偏高"。
        抽不到数值时返回 ``None``（不命中）而不是命中：
        因为"没读到"与"读到了且超限"是两件事，混为一谈会产生假风险。
        若确实需要提示"无法判断"，由 ``missing`` 类规则覆盖。
    """
    condition = _parse_condition(rule.condition_json)
    scope = str(condition.get("scope", "full"))
    extract_kind = str(condition.get("extract", "percent"))
    operator = str(condition.get("operator", ">"))
    try:
        threshold = float(condition.get("value", 0))
    except (TypeError, ValueError):
        return None

    text, location = _scope_text(outcome, scope)
    if not text:
        return None

    normalized = normalize_for_match(text)
    actual: float | None
    if extract_kind == "percent":
        actual = extract_percent(normalized)
    elif extract_kind == "day":
        actual = extract_day_count(normalized)
    elif extract_kind == "month":
        actual = extract_month_count(normalized)
    elif extract_kind == "amount":
        parsed_amount = extract_amount(normalized)
        actual = parsed_amount[0] if parsed_amount else None
    else:
        return None

    if actual is None:
        return None
    if not _compare(actual, operator, threshold):
        return None

    unit = {"percent": "%", "day": " 日", "month": " 个月", "amount": ""}[extract_kind]
    return _build_hit(
        rule=rule,
        evidence_text=(
            f"检出数值 {actual:g}{unit}，触发条件 {operator} {threshold:g}{unit}；"
            f"原文：{snippet_around(text, '', 160)}"
        ),
        evidence_position=location,
    )


def _compare(actual: float, operator: str, threshold: float) -> bool:
    """按运算符比较两个数值。

    参数：
        actual: 实际值。
        operator: 运算符字符串。
        threshold: 阈值。
    返回：
        比较结果；未知运算符返回 ``False``（视为不命中，避免误报）。
    关联：
        被 :func:`_eval_threshold` 调用。
    """
    if operator == ">":
        return actual > threshold
    if operator == ">=":
        return actual >= threshold
    if operator == "<":
        return actual < threshold
    if operator == "<=":
        return actual <= threshold
    if operator == "==":
        return actual == threshold
    if operator == "!=":
        return actual != threshold
    return False


def _parse_condition(raw: str) -> dict:
    """解析规则的条件 JSON。

    参数：
        raw: JSON 字符串；空串或非法 JSON 时返回空字典。
    返回：
        条件字典。
    关联：
        被 :func:`_eval_keyword` 与 :func:`_eval_threshold` 调用。
    设计原因：
        规则是人工维护的配置，单条规则的条件写坏不应让整次审查崩掉；
        返回空字典后各模式按默认行为处理，同时该规则自然不命中，
        由规则管理页的校验提示问题（而非运行期抛异常）。
    """
    if not raw or raw.strip() in ("", "{}"):
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _build_hit(
    rule: RuleSpec, evidence_text: str, evidence_position: str
) -> RuleHitView:
    """构造命中视图，统一填充五项必需内容。

    参数：
        rule: 规则定义。
        evidence_text: 命中证据文本。
        evidence_position: 证据位置。
    返回：
        :class:`RuleHitView`。
    关联：
        被三个 ``_eval_*`` 函数调用，保证命中结构完全一致。
    """
    return RuleHitView(
        rule_id=rule.rule_id,
        rule_code=rule.rule_code,
        rule_name=rule.rule_name,
        risk_level=rule.risk_level,
        evidence_text=evidence_text,
        evidence_position=evidence_position,
        suggestion_text=rule.suggestion_text,
    )


def _build_summary(outcome: ReviewOutcome) -> str:
    """生成中文风险摘要。

    参数：
        outcome: 已完成汇总的审查结论（hits 已排序）。
    返回：
        一段中文摘要，说明整体等级、命中数量与主要风险点。
    关联：
        被 :func:`evaluate_rules` 调用；结果写入 ``review_results.summary_text``
        并进入回写评论。
    设计原因：
        摘要必须能独立阅读——审批人不一定展开看每条命中，
        所以摘要里要直接点出最严重的两三条，而不是只说"命中 N 条"。
    """
    level_label = {"low": "低", "medium": "中", "high": "高"}[outcome.overall_risk_level.value]

    if not outcome.hits:
        return (
            f"整体风险等级：{level_label}。本次共执行 {outcome.evaluated_rule_count} 条规则，"
            "未发现需要关注的条款风险。该结论仅代表规则层面未命中，"
            "不替代法务对合同实质内容的判断。"
        )

    high_hits = [h for h in outcome.hits if h.risk_level == RiskLevel.HIGH]
    medium_hits = [h for h in outcome.hits if h.risk_level == RiskLevel.MEDIUM]

    parts = [
        f"整体风险等级：{level_label}。",
        f"本次共执行 {outcome.evaluated_rule_count} 条规则，命中 {len(outcome.hits)} 条"
        f"（高风险 {len(high_hits)} 条、中风险 {len(medium_hits)} 条）。",
    ]
    if high_hits:
        names = "、".join(h.rule_name for h in high_hits[:3])
        parts.append(f"需优先处理的高风险项：{names}。")
    elif medium_hits:
        names = "、".join(h.rule_name for h in medium_hits[:3])
        parts.append(f"需确认的中风险项：{names}。")

    parts.append("以上为规则层面的风险提示，最终审批意见请由法务结合业务背景确认。")
    return "".join(parts)


def _build_focus_points(hits: tuple[RuleHitView, ...]) -> tuple[str, ...]:
    """从命中结果提炼审批关注点。

    参数：
        hits: 已排序的命中列表。
    返回：
        去重后的关注点列表，每项为一句中文待确认事项。
    关联：
        被 :func:`evaluate_rules` 调用；写入 ``review_results.focus_points_json``。
    设计原因：
        需求 2.4.6 要求"审查输出必须包含总风险等级和审批关注点列表"。
        关注点与"命中规则"的区别是：命中是"系统发现了什么"，
        关注点是"审批人需要确认什么"，因此用建议说明而非规则名来表述。
        按命中顺序（已按风险降序）去重，保证高风险关注点在前。
    """
    points: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        text = hit.suggestion_text.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        points.append(f"[{hit.rule_name}] {text}")
    return tuple(points)


def evaluate_rules(
    outcome: ParseOutcome, rules: list[RuleSpec]
) -> ReviewOutcome:
    """对解析产物执行全部启用规则，生成审查结论。

    参数：
        outcome: 解析产物。
        rules: 规则列表（调用方应只传入启用中的规则，并按 priority 升序排好）。
    返回：
        :class:`ReviewOutcome`，含总风险等级、命中列表（按风险降序）、摘要、关注点、
        参与求值的规则数。
    关联：
        被 ``services/review_service.py::run_review`` 调用；
        结果由 ``services/result_service.py`` 落库并生成回写文本。
    求值顺序：
        按传入顺序逐条求值（调用方已按 priority 排序），保证同一份合同、
        同一套规则每次得到完全相同的命中顺序——这是"可复现"的必要条件。
    """
    hits: list[RuleHitView] = []
    for rule in rules:
        hit: RuleHitView | None = None
        if rule.match_mode == MatchMode.KEYWORD:
            hit = _eval_keyword(rule, outcome)
        elif rule.match_mode == MatchMode.MISSING:
            hit = _eval_missing(rule, outcome)
        elif rule.match_mode == MatchMode.THRESHOLD:
            hit = _eval_threshold(rule, outcome)
        # 未知模式视为不命中：规则配置错误应由管理页提示，不在运行期报错中断。
        if hit is not None:
            hits.append(hit)

    # 按风险等级降序排列，同级保持规则优先级顺序（sorted 是稳定排序）。
    ordered = tuple(
        sorted(hits, key=lambda h: -{"low": 1, "medium": 2, "high": 3}[h.risk_level.value])
    )

    overall = max_risk_level([h.risk_level.value for h in ordered])

    # 先构造一个临时结论用于生成摘要（摘要需要知道整体等级与命中列表）。
    provisional = ReviewOutcome(
        overall_risk_level=overall,
        hits=ordered,
        summary_text="",
        focus_points=(),
        evaluated_rule_count=len(rules),
    )
    summary = _build_summary(provisional)

    return ReviewOutcome(
        overall_risk_level=overall,
        hits=ordered,
        summary_text=summary,
        focus_points=_build_focus_points(ordered),
        evaluated_rule_count=len(rules),
    )
