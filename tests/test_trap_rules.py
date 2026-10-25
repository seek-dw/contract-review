"""合同陷阱规则测试：验证基于真实司法案例的陷阱能被检出。

覆盖内容：
- T001 争议解决同时约定仲裁与诉讼（仲裁条款无效）
- T002 定金超过合同金额 20%（超出部分无定金效力）
- T003 质量条款空心化（只写"符合国标"无量化指标）
- T004 履约期限模糊（"尽快/及时"）
- T005 按日计违约金无封顶
- T006 赊销但无所有权保留
- T007 委托开发知识产权归属不明

每个规则都配正反用例。反向用例尤其重要——
这些规则如果误报，会让审批人对整个系统失去信任。

案例来源：
1. 山东德衡律师事务所《供应链合同十大法律陷阱》
   https://www.deheng.com/dhgc/flgc/25764.html
2. 浙江法院网《避雷合同中的这5类"模糊条款"》
   https://zjsfgkw.gov.cn/art/2025/6/17/art_56_37364.html
"""

from __future__ import annotations

import pytest

from app.domain.enums import ExtractStatus, ParseStatus, RiskLevel
from app.domain.schemas import (
    BASIC_INFO_LABELS,
    CLAUSE_LABELS,
    DocumentText,
    FieldEvidence,
    ParseOutcome,
    ParsedField,
    TextLine,
)
from app.review.trap_rules import run_trap_checks


def _outcome(
    clauses: dict[str, str], basic: dict[str, str] | None = None
) -> ParseOutcome:
    """构造解析产物。

    参数：
        clauses: 条款键 → 原文。
        basic: 基础字段键 → 值。
    返回：
        :class:`ParseOutcome`。
    关联：
        被本文件所有测试调用。
    """
    fields = {}
    lines = []
    n = 0
    for key, label in CLAUSE_LABELS.items():
        text = clauses.get(key)
        if text:
            for piece in text.splitlines():
                n += 1
                lines.append(TextLine(1, n, piece))
            fields[key] = ParsedField(
                key, label, text,
                FieldEvidence(text[:100], "第1页第1行", ExtractStatus.SUCCESS),
            )
        else:
            fields[key] = ParsedField(
                key, label, None,
                FieldEvidence("", "", ExtractStatus.NOT_FOUND, "未提供"),
            )

    basics = {}
    for key, label in BASIC_INFO_LABELS.items():
        v = (basic or {}).get(key)
        basics[key] = ParsedField(
            key, label, v,
            FieldEvidence(v or "", "第1页第1行" if v else "",
                          ExtractStatus.SUCCESS if v else ExtractStatus.NOT_FOUND, ""),
        )

    return ParseOutcome(
        status=ParseStatus.SUCCESS,
        basic_info=basics,
        clauses=fields,
        document=DocumentText(tuple(lines), 1, (), (1,)),
    )


def _codes(hits) -> set[str]:
    """取命中编码集合。"""
    return {h.rule_code for h in hits}


# ---------------------------------------------------------------------------
# T001 仲裁与诉讼并列
# ---------------------------------------------------------------------------


def test_t001_arbitration_and_litigation_detected():
    """验证「可仲裁或可起诉」的并列约定被检出。

    依据：《仲裁法解释》第七条 —— 这种约定仲裁协议无效。
    真实后果：当事人以为两条路都能走，实际仲裁约定作废。
    """
    outcome = _outcome({
        "dispute": "因本合同产生的争议，可向北京仲裁委员会申请仲裁，或向甲方所在地人民法院起诉。",
    })
    hits = run_trap_checks(outcome)
    assert "T001" in _codes(hits)

    hit = next(h for h in hits if h.rule_code == "T001")
    assert hit.risk_level == RiskLevel.HIGH
    # 必须给出法条依据
    assert "仲裁法" in hit.evidence_text
    assert "无效" in hit.evidence_text


def test_t001_not_triggered_for_pure_arbitration():
    """反向验证：只约定仲裁时不命中。"""
    outcome = _outcome({
        "dispute": "因本合同产生的争议，提交北京仲裁委员会仲裁。",
    })
    assert "T001" not in _codes(run_trap_checks(outcome))


def test_t001_not_triggered_for_pure_litigation():
    """反向验证：只约定诉讼时不命中。"""
    outcome = _outcome({
        "dispute": "因本合同产生的争议，由甲方所在地人民法院管辖。",
    })
    assert "T001" not in _codes(run_trap_checks(outcome))


def test_t001_not_triggered_for_sequential_dispute_resolution():
    """反向验证：协商→仲裁的阶梯约定不命中。

    说明：
        "协商不成的，提交仲裁" 这类表述里可能同时出现"法院"字样
        （如"无需向法院起诉"），但实际是单选。
        规则用"或"连接来判断并列，避免误报。
    """
    outcome = _outcome({
        "dispute": "双方应友好协商；协商不成的，提交上海仲裁委员会仲裁。",
    })
    assert "T001" not in _codes(run_trap_checks(outcome))


# ---------------------------------------------------------------------------
# T002 定金超 20%
# ---------------------------------------------------------------------------


def test_t002_deposit_over_20_percent_detected():
    """验证定金 30% 被检出。

    依据：《民法典》第 586 条 —— 超过 20% 部分不产生定金效力。
    真实案例：300 万定金，法院只支持 400 万双倍返还（20% × 2），
             超出的 100 万变成普通预付款。
    """
    outcome = _outcome(
        clauses={"payment": "合同签订后支付定金 30%，卖方违约双倍返还。"},
        basic={"amount": "人民币 10,000,000 元"},
    )
    hits = run_trap_checks(outcome)
    assert "T002" in _codes(hits)

    hit = next(h for h in hits if h.rule_code == "T002")
    assert hit.risk_level == RiskLevel.HIGH
    assert "586" in hit.evidence_text or "五百八十六" in hit.evidence_text


def test_t002_not_triggered_at_exactly_20_percent():
    """反向验证：定金正好 20% 时不命中（合法上限）。"""
    outcome = _outcome(
        clauses={"payment": "支付定金 20%。"},
        basic={"amount": "人民币 10,000,000 元"},
    )
    assert "T002" not in _codes(run_trap_checks(outcome))


def test_t002_not_triggered_without_deposit():
    """反向验证：没有"定金"字样时不命中。

    说明：
        预付款、首付款、保证金都不适用定金罚则，
        不能把"预付款 60%"当成"定金 60%"来报。
    """
    outcome = _outcome(
        clauses={"payment": "支付预付款 60%。"},
        basic={"amount": "人民币 10,000,000 元"},
    )
    assert "T002" not in _codes(run_trap_checks(outcome))


# ---------------------------------------------------------------------------
# T003 质量条款空心化
# ---------------------------------------------------------------------------


def test_t003_vague_quality_detected():
    """验证「符合国家标准」但无量化指标被检出。

    依据：《民法典》第 511 条 —— 质量不明按国标履行。
    真实案例：面粉砷含量 0.4mg/kg（国标 ≤0.5，内控 ≤0.3），
             法院判供应商未违约，买方承担全部损失。
    """
    outcome = _outcome({
        "acceptance": "产品质量应符合国家标准，验收合格后付款。",
        "delivery": "乙方按约定交付货物。",
    })
    hits = run_trap_checks(outcome)
    assert "T003" in _codes(hits)

    hit = next(h for h in hits if h.rule_code == "T003")
    assert "511" in hit.evidence_text or "五百一十一" in hit.evidence_text


def test_t003_not_triggered_with_quantitative_metrics():
    """反向验证：有量化指标时不命中。"""
    outcome = _outcome({
        "acceptance": "钢材屈服强度 ≥345MPa，执行 GB/T1591-2018 标准，验收合格后付款。",
    })
    assert "T003" not in _codes(run_trap_checks(outcome))


def test_t003_not_triggered_without_vague_phrase():
    """反向验证：没有"符合标准"这类泛化表述时不命中。"""
    outcome = _outcome({
        "acceptance": "验收标准见附件二技术规格书。",
    })
    assert "T003" not in _codes(run_trap_checks(outcome))


# ---------------------------------------------------------------------------
# T004 履约期限模糊
# ---------------------------------------------------------------------------


def test_t004_vague_deadline_detected():
    """验证「尽快发货」被检出。

    依据：《民法典》第 511 条第（四）项 —— 期限不明可随时履行。
    真实后果：无法主张逾期违约，因为根本没有可计算的逾期起点。
    """
    outcome = _outcome({
        "delivery": "乙方应在收到订单后尽快发货。",
    })
    hits = run_trap_checks(outcome)
    assert "T004" in _codes(hits)

    hit = next(h for h in hits if h.rule_code == "T004")
    assert "尽快" in hit.evidence_text


def test_t004_not_triggered_with_specific_days():
    """反向验证：给了具体天数时不命中。"""
    outcome = _outcome({
        "delivery": "乙方应在收到订单后 15 个工作日内尽快发货。",
    })
    assert "T004" not in _codes(run_trap_checks(outcome))


def test_t004_not_triggered_for_clear_deadline():
    """反向验证：明确的期限表述不命中。"""
    outcome = _outcome({
        "delivery": "乙方应于 2025 年 6 月 30 日前完成全部交付。",
    })
    assert "T004" not in _codes(run_trap_checks(outcome))


# ---------------------------------------------------------------------------
# T005 按日违约金无封顶
# ---------------------------------------------------------------------------


def test_t005_daily_penalty_without_cap_detected():
    """验证「每逾期一日 0.5%」且无封顶被检出。

    依据：《民法典》第 585 条 + 《九民纪要》第 50 条。
    真实案例：日 0.5% × 200 天 = 100%，违约金等于合同全额。
    """
    outcome = _outcome({
        "breach": "乙方逾期交付的，每逾期一日按合同总金额的 0.5% 支付违约金。",
    })
    hits = run_trap_checks(outcome)
    assert "T005" in _codes(hits)

    hit = next(h for h in hits if h.rule_code == "T005")
    # 证据里应算出"逾期多少天等于合同金额"
    assert "200" in hit.evidence_text, f"应算出 200 天，实际：{hit.evidence_text}"


def test_t005_not_triggered_with_cap():
    """反向验证：有累计上限时不命中。"""
    outcome = _outcome({
        "breach": "每逾期一日按合同总金额的 0.5% 支付违约金，累计不超过合同总金额的 20%。",
    })
    assert "T005" not in _codes(run_trap_checks(outcome))


def test_t005_not_triggered_for_lump_sum_penalty():
    """反向验证：一次性违约金（非按日）不命中。"""
    outcome = _outcome({
        "breach": "任何一方违约，应支付合同总金额 10% 的违约金。",
    })
    assert "T005" not in _codes(run_trap_checks(outcome))


# ---------------------------------------------------------------------------
# T006 赊销无所有权保留
# ---------------------------------------------------------------------------


def test_t006_credit_sale_without_retention_detected():
    """验证「验收后付款但无所有权保留」被检出。

    依据：《民法典》第 641、642 条。
    真实案例：地产商破产，未约定所有权保留的货物被列入破产财产，
             清偿率通常低于 30%。
    """
    outcome = _outcome({
        "payment": "货物验收合格后 30 日内支付全部价款。",
    })
    hits = run_trap_checks(outcome)
    assert "T006" in _codes(hits)

    hit = next(h for h in hits if h.rule_code == "T006")
    assert "641" in hit.evidence_text or "六百四十一" in hit.evidence_text


def test_t006_not_triggered_with_retention_clause():
    """反向验证：约定了所有权保留时不命中。"""
    outcome = _outcome({
        "payment": "货物验收合格后 30 日内支付全部价款。",
        "delivery": "买受人付清全部价款前，标的物所有权归出卖人所有。",
    })
    assert "T006" not in _codes(run_trap_checks(outcome))


def test_t006_not_triggered_for_prepaid():
    """反向验证：款到发货（非赊销）时不命中。"""
    outcome = _outcome({
        "payment": "甲方应在发货前支付全部价款。",
    })
    assert "T006" not in _codes(run_trap_checks(outcome))


# ---------------------------------------------------------------------------
# T007 委托开发知识产权归属不明
# ---------------------------------------------------------------------------


def test_t007_dev_without_ip_ownership_detected():
    """验证「委托开发但无知识产权归属」被检出。

    依据：《民法典》第 859 条 —— 默认归研究开发人（对方）。
    真实案例：家电企业委托开发模具未约定归属，
             供应商把图纸卖给竞争对手，法院判专利权归供应商。
    """
    outcome = _outcome({
        "delivery": "乙方受甲方委托开发定制模具，并按约定交付。",
    })
    hits = run_trap_checks(outcome)
    assert "T007" in _codes(hits)

    hit = next(h for h in hits if h.rule_code == "T007")
    assert hit.risk_level == RiskLevel.HIGH
    assert "859" in hit.evidence_text or "八百五十九" in hit.evidence_text


def test_t007_not_triggered_when_ip_assigned_to_us():
    """反向验证：明确约定成果归我方时不命中。"""
    outcome = _outcome({
        "delivery": "乙方受甲方委托开发定制模具。",
        "intellectual_property": "本项目形成的开发成果知识产权归甲方所有。",
    })
    assert "T007" not in _codes(run_trap_checks(outcome))


def test_t007_not_triggered_without_dev_scenario():
    """反向验证：纯采购（无开发）时不命中。"""
    outcome = _outcome({
        "delivery": "乙方向甲方交付标准型号设备 10 台。",
    })
    assert "T007" not in _codes(run_trap_checks(outcome))


# ---------------------------------------------------------------------------
# 整体
# ---------------------------------------------------------------------------


def test_multiple_traps_sorted_by_risk():
    """验证多条陷阱命中按风险降序。"""
    outcome = _outcome(
        clauses={
            "dispute": "可申请仲裁或向人民法院起诉。",       # T001 高
            "payment": "支付定金 30%。",                     # T002 高
            "delivery": "乙方应尽快发货。",                  # T004 中
        },
        basic={"amount": "人民币 1,000,000 元"},
    )
    hits = run_trap_checks(outcome)
    assert len(hits) >= 3

    weights = [{"high": 0, "medium": 1, "low": 2}[h.risk_level.value] for h in hits]
    assert weights == sorted(weights)


def test_all_traps_have_legal_basis():
    """验证每条陷阱命中的证据里都包含法条依据。

    说明：
        这是本模块与普通规则的核心区别——不是"系统觉得有问题"，
        而是"按某条法律，这样写会导致某后果"。
        没有法条依据的命中等于主观判断，价值大打折扣。
    """
    outcome = _outcome(
        clauses={
            "dispute": "可申请仲裁或向人民法院起诉。",
            "payment": "支付定金 30%。货物验收合格后 30 日内付清。",
            "acceptance": "质量符合国家标准。",
            "delivery": "乙方应尽快发货，并受甲方委托开发定制部件。",
            "breach": "每逾期一日按合同总金额的 0.5% 支付违约金。",
        },
        basic={"amount": "人民币 1,000,000 元"},
    )
    hits = run_trap_checks(outcome)
    assert hits, "该合同应命中多条陷阱"

    for hit in hits:
        assert "【法律依据】" in hit.evidence_text, (
            f"{hit.rule_code} 缺少法条依据"
        )
        # 依据里应出现法律名称
        assert any(
            law in hit.evidence_text
            for law in ("民法典", "仲裁法", "专利法", "九民纪要", "担保制度解释")
        ), f"{hit.rule_code} 的法条依据未指明法律名称"


def test_empty_outcome_no_traps():
    """验证空解析产物不产生命中。"""
    assert run_trap_checks(_outcome({})) == []


# ---------------------------------------------------------------------------
# T008 不可抗力条款笼统
# ---------------------------------------------------------------------------


def test_t008_force_majeure_vague_detected():
    """验证「不可抗力按法律规定执行」被检出。

    依据：《民法典》180 条、590 条 —— 需及时通知并提供证明。
    真实案例：跨境电商仅写"疫情属不可抗力"，未含政府管控措施，
             法院只免除 30% 责任（非全部免除）。

    说明：
        法条依据里用的是**中文数字**（"第一百八十条"），
        不是阿拉伯数字。断言必须按实际文本写，
        否则测试会因为"找 180 找不到"而误判为功能失效
        （这条是本人在写测试时真实犯过的错）。
    """
    outcome = _outcome({
        "breach": "因不可抗力导致不能履行的，按法律规定处理，不承担违约责任。",
    })
    hits = run_trap_checks(outcome)
    assert "T008" in _codes(hits)

    hit = next(h for h in hits if h.rule_code == "T008")
    assert "一百八十条" in hit.evidence_text, (
        f"应引用《民法典》第一百八十条，实际证据：{hit.evidence_text}"
    )
    assert "五百九十条" in hit.evidence_text, (
        f"应引用《民法典》第五百九十条（通知义务），实际证据：{hit.evidence_text}"
    )


def test_t008_not_triggered_with_events_and_notice():
    """反向验证：列举了事件且约定了通知时限时不命中。"""
    outcome = _outcome({
        "breach": (
            "不可抗力包括但不限于地震、战争、疫情、政府行政命令。"
            "事件发生后 5 个工作日内书面通知对方并提供证明。"
        ),
    })
    assert "T008" not in _codes(run_trap_checks(outcome))


def test_t008_not_triggered_without_force_majeure():
    """反向验证：没有不可抗力条款时不命中。"""
    outcome = _outcome({
        "breach": "任何一方违约应支付违约金。",
    })
    assert "T008" not in _codes(run_trap_checks(outcome))


# ---------------------------------------------------------------------------
# T009 解除条件缺失
# ---------------------------------------------------------------------------


def test_t009_no_termination_clause_detected():
    """验证「长期合同完全无解除约定」时命中。

    依据：《民法典》563 条 —— 法定解除权存在但需举证。
    真实案例：锂价暴涨 300%，长协合同若无约定解除条件，
             只能依赖情势变更原则，诉讼成本高。

    验证要点（收窄后的条件）：
        必须是「条款数量足够（≥3）+ 长期履约特征 + 无解除条款」三条同时成立。
        初版只看"无解除条款"，结果 12 个演示案例全部命中，误报率 100%，
        这种"见谁都报"的规则只会让用户关掉系统。
    """
    outcome = _outcome({
        # 构造 3 个以上条款，且含长期特征
        "payment": "本合同为年度框架协议，验收合格后 30 日内付款。",
        "delivery": "乙方按季度分批交付货物。",
        "acceptance": "甲方应在收到货物后 7 日内验收。",
        "breach": "任何一方违约应支付违约金。",
    })
    hits = run_trap_checks(outcome)
    assert "T009" in _codes(hits), f"应命中 T009，实际：{_codes(hits)}"

    hit = next(h for h in hits if h.rule_code == "T009")
    assert "五百六十三" in hit.evidence_text


def test_t009_not_triggered_for_short_term_contract():
    """反向验证：短期一次性交易不报（收窄条件二）。

    说明：
        短期采购合同不约定解除条件的风险确实较低。
        若这种也报，等于给所有简单合同都加一条噪音。
    """
    outcome = _outcome({
        "payment": "验收合格后 30 日内付款。",
        "delivery": "乙方按约定交付货物。",
        "acceptance": "甲方应在 7 日内验收。",
        "breach": "违约方支付违约金。",
    })
    assert "T009" not in _codes(run_trap_checks(outcome))


def test_t009_not_triggered_for_thin_contract():
    """反向验证：条款过少的样本不报（收窄条件一）。

    说明：
        条款极少的样本多半是残篇或测试构造，
        报"缺解除条款"没有实际意义。
    """
    outcome = _outcome({
        "payment": "本合同为年度框架协议，验收后付款。",
    })
    assert "T009" not in _codes(run_trap_checks(outcome))


def test_t009_quantitative_termination_not_triggered():
    """反向验证：有量化解除条件时不命中。

    验证要点（真实缺陷的回归测试）：
        原文"乙方连续 3 批交货不合格的，甲方有权解除合同"中，
        数字"3 批"出现在"解除"**之前**。
        初版正则只查"解除"之后的数字，导致漏判、误报为"缺少量化标准"。
        现在用双向正则，任一方向成立即视为已有量化条件。
    """
    outcome = _outcome({
        "payment": "本合同为年度框架协议。",
        "delivery": "乙方连续 3 批交货不合格的，甲方有权解除合同。",
        "acceptance": "甲方应在 7 日内验收。",
    })
    assert "T009" not in _codes(run_trap_checks(outcome))


def test_t009_vague_termination_low_risk():
    """验证有解除条款但缺量化标准时给低风险提示。"""
    outcome = _outcome({
        "delivery": "乙方严重违约的，甲方有权解除合同。",
    })
    hits = run_trap_checks(outcome)
    t009 = [h for h in hits if h.rule_code == "T009"]
    assert t009, "应命中 T009"
    assert t009[0].risk_level == RiskLevel.LOW


# ---------------------------------------------------------------------------
# T010 违约金与定金并存
# ---------------------------------------------------------------------------


def test_t010_penalty_and_deposit_coexist_detected():
    """验证违约金与定金并存但未说明关系时命中。

    依据：《民法典》588 条 —— 只能择一适用，不能叠加。
    """
    outcome = _outcome({
        "payment": "合同签订后支付定金 15%。",
        "breach": "任何一方违约，应支付合同总金额 10% 的违约金。",
    })
    hits = run_trap_checks(outcome)
    assert "T010" in _codes(hits)

    hit = next(h for h in hits if h.rule_code == "T010")
    assert "588" in hit.evidence_text or "五百八十八" in hit.evidence_text


def test_t010_not_triggered_when_choice_explicit():
    """反向验证：已明确择一适用时不命中。"""
    outcome = _outcome({
        "payment": "支付定金 15%。",
        "breach": "违约金与定金择一适用，守约方有权选择。",
    })
    assert "T010" not in _codes(run_trap_checks(outcome))


def test_t010_not_triggered_without_deposit():
    """反向验证：没有定金时不命中。"""
    outcome = _outcome({
        "payment": "支付预付款 15%。",
        "breach": "违约方支付违约金 10%。",
    })
    assert "T010" not in _codes(run_trap_checks(outcome))


# ---------------------------------------------------------------------------
# T011 结算依据缺失
# ---------------------------------------------------------------------------


def test_t011_settlement_without_basis_detected():
    """验证「以实际结算为准」但无依据时命中。

    依据：《民法典》511 条第（二）项 —— 按市场价格履行。
    来源：浙江法院网「价格与支付条款」模糊情形。
    """
    outcome = _outcome({
        "payment": "总价款以实际结算为准，验收合格后付款。",
    })
    hits = run_trap_checks(outcome)
    assert "T011" in _codes(hits)

    hit = next(h for h in hits if h.rule_code == "T011")
    assert "511" in hit.evidence_text or "五百一十一" in hit.evidence_text


def test_t011_not_triggered_with_unit_price():
    """反向验证：有单价依据时不命中。"""
    outcome = _outcome({
        "payment": "按附件《价格表》单价 × 实际验收数量结算，以实际结算为准。",
    })
    assert "T011" not in _codes(run_trap_checks(outcome))


def test_t011_not_triggered_with_fixed_total():
    """反向验证：固定总价时不命中。"""
    outcome = _outcome({
        "payment": "合同总金额为 1,200,000 元，验收后 30 日内支付。",
    })
    assert "T011" not in _codes(run_trap_checks(outcome))


# ---------------------------------------------------------------------------
# T012 涉外合同未约定适用法律
# ---------------------------------------------------------------------------


def test_t012_foreign_contract_no_governing_law_detected():
    """验证涉外合同未约定适用法律时命中。

    依据：《涉外民事关系法律适用法》41 条。
    真实案例：中德设备合同未约定管辖，需在德国起诉，
             耗时 3 年，成本占争议金额 40%。
    """
    outcome = _outcome(
        clauses={
            "payment": "甲方以美元支付合同价款，验收后 30 日内付清。",
            "delivery": "乙方将货物运至德国汉堡港。",
        },
        basic={"currency": "USD"},
    )
    hits = run_trap_checks(outcome)
    assert "T012" in _codes(hits)

    hit = next(h for h in hits if h.rule_code == "T012")
    assert "涉外民事关系法律适用法" in hit.evidence_text


def test_t012_not_triggered_when_governing_law_specified():
    """反向验证：约定了适用中国法律时不命中。"""
    outcome = _outcome({
        "payment": "甲方以美元支付价款。",
        "dispute": "本合同适用中华人民共和国法律，争议由上海仲裁委员会仲裁。",
    })
    assert "T012" not in _codes(run_trap_checks(outcome))


def test_t012_not_triggered_for_domestic_contract():
    """反向验证：纯国内合同不命中。"""
    outcome = _outcome({
        "payment": "甲方以人民币支付价款。",
        "delivery": "乙方将货物运至北京仓库。",
    })
    assert "T012" not in _codes(run_trap_checks(outcome))


# ---------------------------------------------------------------------------
# 新增规则的完整性检查
# ---------------------------------------------------------------------------


def test_all_twelve_traps_registered():
    """验证 12 条陷阱全部注册到执行列表。"""
    from app.review.trap_rules import _TRAP_CHECKS

    assert len(_TRAP_CHECKS) == 12, f"应为 12 条，实际 {len(_TRAP_CHECKS)}"


def test_new_traps_have_legal_basis():
    """验证新增的 T008-T012 也带法条依据。"""
    outcome = _outcome(
        clauses={
            "breach": "不可抗力按法律规定处理。违约方支付违约金 10%。",
            "payment": "支付定金 15%，总价以实际结算为准。",
            "delivery": "乙方将货物运至美国洛杉矶港，以美元结算。",
        },
    )
    hits = run_trap_checks(outcome)
    new_codes = {"T008", "T009", "T010", "T011", "T012"}
    found = {h.rule_code for h in hits} & new_codes
    assert found, f"应至少命中一条新规则，实际命中 {_codes(hits)}"

    for hit in hits:
        if hit.rule_code in new_codes:
            assert "【法律依据】" in hit.evidence_text
            assert any(
                law in hit.evidence_text
                for law in ("民法典", "涉外民事关系法律适用法")
            ), f"{hit.rule_code} 未指明法律名称"
