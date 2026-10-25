"""深层规则：只有跨条款交叉比对才能发现的风险。

============================================================================
为什么需要这个模块（与 seed_rules 的区别）
============================================================================
``seed_rules.py`` 里的 26 条规则，本质是「在单条条款里找一个特征」：
看到 60% 就报预付款过高，看不到保密条款就报缺失。

这类规则的问题是——**法务自己一眼就能看出来**。
"预付款 60% 偏高"不需要系统提醒，人扫一眼合同就知道。

本模块要解决的是另一类问题：**单看每条条款都正常，但合起来有洞**。
这类风险人必须把全文在脑子里拼起来才能发现，极易遗漏：

    例 1  合同写"验收合格后 30 日付款"，同时又写"甲方有权随时单方解除"。
          → 甲方可以卡住验收不确认，付款义务永不触发。这是典型陷阱。
          → 两条条款各自看都没问题，只有交叉比对才暴露。

    例 2  违约金封顶 5%，但合同金额 1200 万。
          → 供应商违约最多赔 60 万，而重新采购的差价可能 200 万。
          → 封顶条款本身合理，但和金额一起看就不合理。

    例 3  合同 12 月 31 日到期，但付款条款写"验收后 90 日内支付"。
          → 若 12 月才验收，尾款要到次年 3 月，跨越合同期。
          → 两条都是标准表述，组合起来才有问题。

============================================================================
实现思路
============================================================================
不引入大模型，全部是确定性逻辑，分三类：

1. **交叉比对（cross_check）**：取两个不同条款里的要素做运算。
   例如"付款触发条件" vs "验收主动权归属"。

2. **量级校验（magnitude）**：把条款里的相对值换算成绝对金额，
   再和合同总额比。例如违约金封顶 5% × 1200 万 = 60 万。

3. **时序推演（timeline）**：把日期与期限相加，看是否越过合同边界。
   例如到期日 - 验收期限 - 付款账期 < 0。

三类都只做"能不能算出来"，算不出来就跳过（不猜），
算出来才报，并且把算式写进证据——让人能复核这个推断。

============================================================================
与现有规则引擎的关系
============================================================================
本模块**不接入** ``rule_engine`` 的三种模式，因为那三种模式都是"单条款内找特征"，
表达不了跨条款运算。

本模块提供独立的 :func:`run_deep_checks`，输入仍是 :class:`ParseOutcome`，
输出仍是 :class:`RuleHitView` 列表，因此可以和普通规则的命中结果直接合并展示。

这样做的好处：
- 原有 26 条规则不动，验收基线不受影响；
- 深层规则独立演进，失败也不影响主流程。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from app.domain.enums import RiskLevel
from app.domain.schemas import (
    CLAUSE_LABELS,
    FieldEvidence,
    ParseOutcome,
    RuleHitView,
)
from app.parser.text_utils import (
    extract_amount,
    extract_day_count,
    extract_percent,
    normalize_for_match,
)

# ---------------------------------------------------------------------------
# 风险类型常量
# ---------------------------------------------------------------------------
# 与 seed_rules 的 rule_code 共用一个命名空间，前缀 D 表示 deep（深层规则），
# 便于在命中列表里区分"浅层特征命中"与"深层推理命中"。
DEEP_PREFIX = "D"


@dataclass(frozen=True)
class DeepFinding:
    """一条深层检查的发现。

    用途：模块内部的计算结果载体，最后统一转成 :class:`RuleHitView`。
    定义原因：把"算出了什么"与"怎么展示"分开，
    便于单测直接断言数值（如"换算出的绝对金额应为 600000"），
    不必去解析证据文本。
    """

    # 规则编码，如 D001
    code: str
    # 规则名称
    name: str
    # 风险等级
    risk: RiskLevel
    # 证据文本：必须包含算式，让人能复核
    evidence: str
    # 证据位置
    position: str
    # 建议
    suggestion: str


# ---------------------------------------------------------------------------
# 工具：从条款原文里提取带语义锚点的数值
# ---------------------------------------------------------------------------


def _value_near(text: str, anchors: list[str], extractor) -> float | None:
    """在语义锚点附近提取数值。

    参数：
        text: 已归一化的条款原文。
        anchors: 语义锚点词列表，如 ``["预付款", "首付", "预付"]``。
        extractor: 抽取函数，如 :func:`extract_percent`。
    返回：
        锚点之后最近的数值；找不到锚点或数值时返回 ``None``。
    关联：
        被本模块各检查函数调用，解决"条款里有多个百分比时取错数值"的问题。

    设计原因（这是对现有引擎缺陷的修补）：
        现有 ``extract_percent`` 只取全文第一个百分比。
        但付款条款里常同时出现"预付款 60%""违约金 30%""质保金 5%"，
        取第一个会张冠李戴。
        本函数先定位锚点词，再只取锚点**之后 40 字内**的数值，
        把"哪个数属于哪个语义"这个判断做实。
    """
    for anchor in anchors:
        idx = text.find(anchor)
        if idx < 0:
            continue
        # 只取锚点后 40 字：覆盖"预付款为合同总金额的百分之六十"这类表述，
        # 又不至于跨到下一个语义段。
        window = text[idx : idx + 40]
        value = extractor(window)
        if value is not None:
            return value
    return None


def _amount_in_yuan(text: str) -> float | None:
    """从条款原文提取金额并统一换算为"元"。

    参数：
        text: 已归一化的文本。
    返回：
        以元为单位的金额；提取不到返回 ``None``。
    关联：
        被 :func:`_check_penalty_cap` 调用。

    说明：
        ``extract_amount`` 返回 ``(数值, 单位)``，单位可能是"万元""亿元"。
        做量级比较必须统一到元，否则 5 万元与 50000 元无法比。
    """
    parsed = extract_amount(text)
    if parsed is None:
        return None
    value, unit = parsed
    multiplier = {"亿元": 100_000_000, "亿": 100_000_000,
                  "万元": 10_000, "万": 10_000}.get(unit, 1)
    return value * multiplier


def _parse_date(text: str) -> date | None:
    """从文本中解析第一个日期。

    参数：
        text: 原文。
    返回：
        :class:`datetime.date`；解析不到返回 ``None``。
    关联：
        被 :func:`_check_payment_crosses_expiry` 调用。
    """
    match = re.search(r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})", text)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _clause_text(outcome: ParseOutcome, key: str) -> str:
    """取某条款的归一化文本。

    参数：
        outcome: 解析产物。
        key: 条款键，见 :data:`CLAUSE_LABELS`。
    返回：
        归一化后的条款文本；不存在时返回空串。
    关联：
        被本模块所有检查函数调用。
    """
    raw = outcome.clause_value(key)
    return normalize_for_match(raw) if raw else ""


def _position(outcome: ParseOutcome, key: str) -> str:
    """取某条款的位置标识。

    参数：
        outcome: 解析产物。
        key: 条款键。
    返回：
        位置字符串；不存在时返回"全文检索"。
    关联：
        被 :func:`_finding_to_hit` 调用。
    """
    ev: FieldEvidence | None = outcome.clause_evidence(key)
    return ev.location if ev and ev.location else "全文检索"


# ---------------------------------------------------------------------------
# 检查一：付款触发条件 + 验收主动权 —— 组合陷阱
# ---------------------------------------------------------------------------


def _check_payment_depends_on_unilateral_acceptance(
    outcome: ParseOutcome,
) -> DeepFinding | None:
    """检测「付款以验收为前提，但验收主动权在对方」的组合陷阱。

    参数：
        outcome: 解析产物。
    返回：
        命中时返回发现；否则 ``None``。
    关联：
        读取 ``payment`` 与 ``acceptance`` 两个条款，是典型跨条款检查。

    为什么这是人容易漏掉的：
        付款条款写"验收合格后 30 日内支付"——很标准，没问题。
        验收条款写"验收标准由甲方确定"或"甲方有权决定是否验收"——单看也算常见。
        但两条放在一起，甲方可以**永远不给验收结论**，付款义务就永远不触发。
        这是实务中真实存在的陷阱，法务必须逐条对照才能发现。

    判定逻辑（三步，全部确定性）：
        1. 付款条款里是否出现"验收后/验收合格后/验收通过后"这类触发条件；
        2. 验收条款里是否出现"由甲方确定/以甲方为准/甲方有权决定"这类单方裁量表述；
        3. 两条同时成立 → 报高风险。
    """
    payment = _clause_text(outcome, "payment")
    acceptance = _clause_text(outcome, "acceptance")
    if not payment or not acceptance:
        return None

    # 第一步：付款是否挂钩验收
    payment_triggers = ("验收合格后", "验收通过后", "验收后", "验收合格之日起")
    trigger_hit = next((t for t in payment_triggers if t in payment), None)
    if trigger_hit is None:
        return None

    # 第二步：验收是否有单方裁量
    unilateral = (
        "由甲方确定", "由甲方认定", "以甲方为准", "甲方有权决定是否验收",
        "甲方单方", "由需方确定", "以需方验收为准", "甲方满意的",
    )
    unilateral_hit = next((u for u in unilateral if u in acceptance), None)
    if unilateral_hit is None:
        return None

    # 第三步：两条都成立 → 组合陷阱
    return DeepFinding(
        code="D001",
        name="付款挂钩验收，但验收由对方单方决定",
        risk=RiskLevel.HIGH,
        evidence=(
            f"付款条款含触发条件「{trigger_hit}」，"
            f"验收条款含单方裁量表述「{unilateral_hit}」。"
            f"两者组合意味着对方可通过不出具验收结论，使付款义务长期不触发。"
        ),
        position=f"付款：{_position(outcome, 'payment')}；验收：{_position(outcome, 'acceptance')}",
        suggestion=(
            "建议为验收设置最长时限（如「收到交付物后 X 日内未提出书面异议视为验收通过」），"
            "或将付款触发条件改为客观节点（如交付完成日、第三方检测报告出具日）。"
        ),
    )


# ---------------------------------------------------------------------------
# 检查二：违约金封顶 vs 合同金额 —— 量级校验
# ---------------------------------------------------------------------------


def _check_penalty_cap_too_low(outcome: ParseOutcome) -> DeepFinding | None:
    """检测「违约金封顶金额相对合同总额过低」。

    参数：
        outcome: 解析产物。
    返回：
        命中时返回发现；否则 ``None``。
    关联：
        读取 ``breach`` 条款与 ``amount`` 字段。

    为什么这是人容易漏掉的：
        "违约金总额不超过合同金额的 5%"——这条单独看非常常见，甚至像是保护双方。
        但合同金额 1200 万时，5% 只有 60 万。
        若供应商违约导致需要重新采购，差价可能远超 60 万。
        **必须把百分比乘上金额，才能看出封顶值是否足够覆盖风险。**

    判定逻辑：
        1. 违约金条款里找到封顶表述（"不超过…%"或"累计不超过…"）；
        2. 抽出封顶百分比；
        3. 乘上合同总额得到绝对封顶额；
        4. 封顶额 < 合同总额的 10% → 报高风险（阈值 10% 是可调的保守值）。
    """
    breach = _clause_text(outcome, "breach")
    if not breach:
        return None

    # 第一步：找封顶表述
    cap_patterns = ("不超过", "累计不超过", "最高不超过", "上限为", "封顶")
    cap_anchor = next((p for p in cap_patterns if p in breach), None)
    if cap_anchor is None:
        return None

    # 第二步：封顶百分比（锚点后 40 字内的第一个百分比）
    cap_percent = _value_near(breach, list(cap_patterns), extract_percent)
    if cap_percent is None:
        return None

    # 第三步：合同总额（元）
    total = _amount_in_yuan(normalize_for_match(outcome.basic_value("amount") or ""))
    if total is None:
        return None

    # 第四步：换算绝对金额并比较
    cap_amount = total * cap_percent / 100
    ratio = cap_percent
    if ratio >= 10:
        return None

    return DeepFinding(
        code="D002",
        name="违约金封顶额相对合同金额偏低",
        risk=RiskLevel.HIGH,
        evidence=(
            f"违约金条款封顶「{cap_anchor} {cap_percent:g}%」，"
            f"合同总额 {total:,.0f} 元，换算封顶额 = "
            f"{total:,.0f} × {cap_percent:g}% = {cap_amount:,.0f} 元。"
            f"若对方违约需重新采购，损失可能远超该封顶额。"
        ),
        position=f"违约：{_position(outcome, 'breach')}；金额：{outcome.basic_info['amount'].evidence.location or '全文检索'}",
        suggestion=(
            "建议将违约金封顶提高至合同金额 10%—30%，"
            "或对「重新采购差价」「工期延误损失」单列不受封顶限制的赔偿条款。"
        ),
    )


# ---------------------------------------------------------------------------
# 检查三：付款账期越过合同到期日 —— 时序推演
# ---------------------------------------------------------------------------


def _check_payment_crosses_expiry(outcome: ParseOutcome) -> DeepFinding | None:
    """检测「付款账期可能跨越合同到期日」。

    参数：
        outcome: 解析产物。
    返回：
        命中时返回发现；否则 ``None``。
    关联：
        读取 ``payment`` 条款、``expire_date`` 字段与 ``acceptance`` 条款的验收期限。

    为什么这是人容易漏掉的：
        合同到期日 2025-12-31，付款条款"验收后 90 日内支付"，
        验收期限"收到交付物后 7 日内"。
        三条各自都正常。
        但如果交付发生在 12 月，验收 12 月完成，付款要到次年 3 月——
        **合同已到期，付款义务的法律基础变得模糊**。
        这种"时间算一下才发现"的问题，人几乎不会主动去算。

    判定逻辑：
        1. 取到期日；
        2. 取付款账期天数 + 验收期限天数；
        3. 到期日往前推这么多天，得到"最晚应交付日"；
        4. 若该日期早于今天，说明时间已经很紧（保守提示）；
           更直接的判定：账期 + 验收期 > 到期日与合同签署日的间隔 → 必然跨期。
    """
    payment = _clause_text(outcome, "payment")
    if not payment:
        return None

    expire = _parse_date(outcome.basic_value("expire_date") or "")
    if expire is None:
        return None

    pay_days = extract_day_count(payment)
    if pay_days is None:
        return None

    acceptance = _clause_text(outcome, "acceptance")
    accept_days = extract_day_count(acceptance) or 0

    # 到期日往前推 (账期 + 验收期) 天，得到最晚可交付日
    latest_delivery = expire - timedelta(days=int(pay_days + accept_days))

    # 只在"合同尚未到期且时间确实紧张"时才提示。
    #
    # 为什么必须排除已过期合同：
    #   合同如果已经到期（例如去年的合同），再提示"账期可能跨越到期日"
    #   毫无意义——那个风险已经发生或已经不存在了，报出来只是噪音。
    #   实测该规则对一份 2025-12-31 到期的历史合同报警，
    #   距到期 -296 天，属于明显的无效告警。
    today = date.today()
    remaining = (expire - today).days
    if remaining <= 0:
        return None
    # 到期还很远（超过 60 天）且最晚交付日仍有富余 → 不报
    if latest_delivery > today and remaining > 60:
        return None

    return DeepFinding(
        code="D003",
        name="付款账期与合同到期日存在跨期风险",
        risk=RiskLevel.MEDIUM,
        evidence=(
            f"合同到期日 {expire.isoformat()}，付款账期 {pay_days:g} 日，"
            f"验收期限 {accept_days:g} 日。"
            f"到期日往前推 {pay_days + accept_days:g} 天 = {latest_delivery.isoformat()}，"
            f"即最晚应在该日前完成交付验收，否则尾款支付将落在合同到期之后。"
            f"（今天 {today.isoformat()}，距到期 {remaining} 天）"
        ),
        position=f"到期日：{outcome.basic_info['expire_date'].evidence.location}；付款：{_position(outcome, 'payment')}",
        suggestion=(
            "建议在合同中明确「合同到期不影响已发生付款义务的履行」，"
            "或将尾款支付节点改为「验收合格后 X 日内且不晚于合同到期日」。"
        ),
    )


# ---------------------------------------------------------------------------
# 检查四：质保金与质保期不匹配
# ---------------------------------------------------------------------------


def _check_warranty_mismatch(outcome: ParseOutcome) -> DeepFinding | None:
    """检测「扣了质保金，但没约定质保期或质保期过短」。

    参数：
        outcome: 解析产物。
    返回：
        命中时返回发现；否则 ``None``。
    关联：
        读取 ``payment`` 与 ``acceptance`` 条款。

    为什么这是人容易漏掉的：
        付款条款写"留 5% 作为质保金，质保期满后支付"——很标准。
        但翻遍合同**找不到质保期的约定**，或者质保期只有 3 个月。
        扣了钱却没有明确的退还条件，实务中极易产生纠纷。
        这类"条款之间有引用关系但被引用方缺失"的问题，人需要主动追踪才能发现。
    """
    payment = _clause_text(outcome, "payment")
    if not payment:
        return None

    warranty_anchors = ("质保金", "质量保证金", "保修金", "尾款留存")
    anchor = next((a for a in warranty_anchors if a in payment), None)
    if anchor is None:
        return None

    # 在全文中找质保期约定
    full = normalize_for_match(outcome.document.full_text)
    warranty_period_patterns = (
        r"质保期[^。；\n]{0,20}?(\d+)\s*(?:个月|月|年)",
        r"保修期[^。；\n]{0,20}?(\d+)\s*(?:个月|月|年)",
        r"质量保证期[^。；\n]{0,20}?(\d+)\s*(?:个月|月|年)",
    )
    period_months: float | None = None
    for pattern in warranty_period_patterns:
        match = re.search(pattern, full)
        if match:
            value = float(match.group(1))
            # 含"年"的表述换算成月
            if "年" in match.group(0):
                value *= 12
            period_months = value
            break

    if period_months is None:
        return DeepFinding(
            code="D004",
            name="扣留质保金但未约定质保期",
            risk=RiskLevel.MEDIUM,
            evidence=(
                f"付款条款含「{anchor}」表述，但全文未找到质保期/保修期的期限约定。"
                f"扣款有依据、退款无期限，易产生纠纷。"
            ),
            position=_position(outcome, "payment"),
            suggestion="建议明确质保期长度（如验收合格后 12 个月）及期满后的退还流程与时限。",
        )

    if period_months < 6:
        return DeepFinding(
            code="D004",
            name="质保期过短",
            risk=RiskLevel.LOW,
            evidence=(
                f"付款条款含「{anchor}」，全文约定的质保期为 {period_months:g} 个月，"
                f"短于常见的 12 个月。部分缺陷可能在质保期后才暴露。"
            ),
            position=_position(outcome, "payment"),
            suggestion="建议将质保期延长至 12 个月以上，或按标的物类型约定行业惯例期限。",
        )

    return None


# ---------------------------------------------------------------------------
# 检查五：争议解决方式与履约地不一致
# ---------------------------------------------------------------------------


def _check_jurisdiction_vs_delivery(outcome: ParseOutcome) -> DeepFinding | None:
    """检测「约定异地管辖，但履约地在我方所在地」的不经济安排。

    参数：
        outcome: 解析产物。
    返回：
        命中时返回发现；否则 ``None``。
    关联：
        读取 ``dispute`` 与 ``delivery`` 条款。

    为什么这是人容易漏掉的：
        "由乙方所在地法院管辖"——单独看是一条明确条款，法务会注意到。
        但如果交付地、验收地都在甲方所在地，那么**证据、证人、标的物都在甲方这边**，
        却要去乙方所在地打官司，诉讼成本会显著上升。
        这个"履约地 vs 管辖地"的错配，需要把两个条款放在一起看才成立。
    """
    dispute = _clause_text(outcome, "dispute")
    delivery = _clause_text(outcome, "delivery")
    if not dispute or not delivery:
        return None

    # 管辖地指向对方
    counterparty_jurisdiction = (
        "乙方所在地", "供方所在地", "卖方所在地", "对方所在地",
    )
    juris_hit = next((j for j in counterparty_jurisdiction if j in dispute), None)
    if juris_hit is None:
        return None

    # 履约地在我方
    our_place_markers = ("甲方所在地", "甲方指定地点", "需方所在地", "我方所在地")
    place_hit = next((p for p in our_place_markers if p in delivery), None)
    if place_hit is None:
        return None

    return DeepFinding(
        code="D005",
        name="管辖地与履约地错配",
        risk=RiskLevel.MEDIUM,
        evidence=(
            f"争议解决条款约定「{juris_hit}」管辖，"
            f"但交付条款约定履约地为「{place_hit}」。"
            f"证据、标的物、验收记录均在我方所在地，却需赴对方所在地诉讼，"
            f"差旅与举证成本显著上升。"
        ),
        position=f"争议：{_position(outcome, 'dispute')}；交付：{_position(outcome, 'delivery')}",
        suggestion=(
            "建议将管辖地改为履约地（我方所在地）或双方约定的中立地；"
            "若对方坚持，可考虑改为仲裁（成本更可控）。"
        ),
    )


# ---------------------------------------------------------------------------
# 编排入口
# ---------------------------------------------------------------------------

# 全部检查函数。新增检查只需往这里加一个函数引用。
_CHECKS = (
    _check_payment_depends_on_unilateral_acceptance,
    _check_penalty_cap_too_low,
    _check_payment_crosses_expiry,
    _check_warranty_mismatch,
    _check_jurisdiction_vs_delivery,
)


def _finding_to_hit(finding: DeepFinding) -> RuleHitView:
    """把内部发现转成统一的命中视图。

    参数：
        finding: 检查结果。
    返回：
        :class:`RuleHitView`，可与普通规则命中一起展示。
    关联：
        被 :func:`run_deep_checks` 调用。
    说明：
        ``rule_id`` 传 -1：深层规则不在数据库里，没有主键。
        界面按 ``rule_code`` 前缀 D 区分展示，不需要真实 rule_id。
    """
    return RuleHitView(
        rule_id=None,
        rule_code=finding.code,
        rule_name=finding.name,
        risk_level=finding.risk,
        evidence_text=finding.evidence,
        evidence_position=finding.position,
        suggestion_text=finding.suggestion,
    )


def run_deep_checks(outcome: ParseOutcome) -> list[RuleHitView]:
    """执行全部深层检查。

    参数：
        outcome: 解析产物。
    返回：
        命中列表，按风险等级降序。无命中返回空列表。
    关联：
        被 ``services/review_service.run_review`` 调用，
        结果与普通规则命中合并后统一入库与展示。

    设计原因：
        每个检查函数自己负责"条件不满足时返回 None"，
        本函数只做遍历与排序，保持简单。
        单个检查抛异常不应中断整批——用 try/except 隔离，
        但要把异常记录到日志，不能静默吞掉。
    """
    findings: list[RuleHitView] = []
    for check in _CHECKS:
        try:
            finding = check(outcome)
        except Exception:  # noqa: BLE001
            # 深层检查是"增强能力"，单个检查出错不应让整次审查失败。
            # 但要留下痕迹——通过打印到 stderr，由上层日志捕获。
            import traceback

            traceback.print_exc()
            continue
        if finding is not None:
            findings.append(_finding_to_hit(finding))

    weight = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda h: weight.get(h.risk_level.value, 9))
    return findings
