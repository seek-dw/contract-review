"""深层规则测试：验证跨条款推理能发现人容易漏掉的风险。

覆盖内容：
- D001 付款挂钩验收 + 验收单方裁量 → 组合陷阱
- D002 违约金封顶额相对合同金额偏低 → 量级校验
- D003 付款账期跨越合同到期日 → 时序推演
- D004 扣质保金但无质保期约定 → 引用缺失
- D005 管辖地与履约地错配 → 交叉比对
- 反向验证：条件不成立时**不得**命中（避免假风险）

设计原因：
这些规则的共同特征是"单看每条条款都正常，合起来才有洞"。
因此测试必须构造**两条正常条款的组合**，而不能只改一条。
每个检查都配一对正反用例：
- 正向：组合成立 → 必须命中；
- 反向：只差一个条件 → 必须不命中。
反向用例比正向更重要——它防止规则变成"见谁都报"的噪音源。
"""

from __future__ import annotations

from datetime import date, timedelta

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
from app.review.deep_rules import run_deep_checks


def _build_outcome(
    clauses: dict[str, str],
    basic: dict[str, str] | None = None,
) -> ParseOutcome:
    """构造一个解析产物，用于深层规则测试。

    参数：
        clauses: 条款键 → 条款原文。未给出的条款记为"未检出"。
        basic: 基础信息字段键 → 值。未给出的记为"未找到"。
    返回：
        :class:`ParseOutcome`。
    关联：
        被本文件所有测试调用。

    说明：
        文档全文由各条款文本拼成，模拟真实解析产物的形态。
        这样 D004（全文找质保期）这类需要全文的检查也能被测到。
    """
    clause_fields: dict[str, ParsedField] = {}
    lines: list[TextLine] = []
    line_no = 0

    for key, label in CLAUSE_LABELS.items():
        text = clauses.get(key)
        if text:
            for piece in text.splitlines():
                line_no += 1
                lines.append(TextLine(page=1, line_no=line_no, text=piece))
            clause_fields[key] = ParsedField(
                key=key,
                label=label,
                value=text,
                evidence=FieldEvidence(
                    snippet=text[:120],
                    location="第1页第1行",
                    status=ExtractStatus.SUCCESS,
                ),
            )
        else:
            clause_fields[key] = ParsedField(
                key=key,
                label=label,
                value=None,
                evidence=FieldEvidence(
                    snippet="",
                    location="",
                    status=ExtractStatus.NOT_FOUND,
                    detail="测试构造：未提供",
                ),
            )

    basic_fields: dict[str, ParsedField] = {}
    for key, label in BASIC_INFO_LABELS.items():
        value = (basic or {}).get(key)
        basic_fields[key] = ParsedField(
            key=key,
            label=label,
            value=value,
            evidence=FieldEvidence(
                snippet=value or "",
                location="第1页第1行" if value else "",
                status=ExtractStatus.SUCCESS if value else ExtractStatus.NOT_FOUND,
                detail="" if value else "测试构造：未提供",
            ),
        )

    document = DocumentText(
        lines=tuple(lines), page_count=1, ocr_pages=(), text_pages=(1,)
    )
    return ParseOutcome(
        status=ParseStatus.SUCCESS,
        basic_info=basic_fields,
        clauses=clause_fields,
        document=document,
    )


def _codes(hits) -> set[str]:
    """取出命中列表里的规则编码集合。

    参数：
        hits: 命中列表。
    返回：
        编码集合，便于断言"某条规则是否命中"。
    """
    return {h.rule_code for h in hits}


# ---------------------------------------------------------------------------
# D001：付款挂钩验收 + 验收单方裁量
# ---------------------------------------------------------------------------


def test_d001_combo_trap_detected():
    """验证「付款挂钩验收 + 验收由对方单方决定」的组合陷阱被检出。

    返回：
        无。

    为什么这是人工易漏的：
        付款条款"验收合格后 30 日内支付"很标准；
        验收条款"验收标准由甲方确定"也很常见。
        但两条组合，甲方可以永远不出验收结论，付款义务永不触发。
        必须把两条放在一起看才能发现。
    """
    outcome = _build_outcome(
        clauses={
            "payment": "第三条 付款方式\n验收合格后 30 日内支付合同总金额的 70%。",
            "acceptance": "第四条 验收\n验收标准由甲方确定，甲方验收合格后出具确认书。",
        }
    )
    hits = run_deep_checks(outcome)
    assert "D001" in _codes(hits), f"应命中 D001，实际：{_codes(hits)}"

    hit = next(h for h in hits if h.rule_code == "D001")
    assert hit.risk_level == RiskLevel.HIGH
    # 证据里必须写明是哪两个表述组合导致的，让人能复核
    assert "验收合格后" in hit.evidence_text
    assert "由甲方确定" in hit.evidence_text


def test_d001_not_triggered_without_unilateral_terms():
    """反向验证：验收条款没有单方裁量表述时，不命中 D001。

    返回：
        无。

    说明：
        这是防止规则变成噪音的关键用例。
        付款挂钩验收本身是正常的，只有叠加单方裁量才是陷阱。
    """
    outcome = _build_outcome(
        clauses={
            "payment": "验收合格后 30 日内支付。",
            "acceptance": "双方共同验收，验收标准见附件二。",
        }
    )
    hits = run_deep_checks(outcome)
    assert "D001" not in _codes(hits)


def test_d001_not_triggered_when_payment_not_linked_to_acceptance():
    """反向验证：付款不挂钩验收时，即使验收有单方裁量也不命中。

    返回：
        无。
    """
    outcome = _build_outcome(
        clauses={
            "payment": "合同签订后 10 日内支付 30%，交付后支付 70%。",
            "acceptance": "验收标准由甲方确定。",
        }
    )
    hits = run_deep_checks(outcome)
    assert "D001" not in _codes(hits)


# ---------------------------------------------------------------------------
# D002：违约金封顶额相对合同金额偏低
# ---------------------------------------------------------------------------


def test_d002_penalty_cap_too_low_detected():
    """验证「违约金封顶 5% × 1200 万 = 60 万」这类量级问题被检出。

    返回：
        无。

    为什么这是人工易漏的：
        "违约金总额不超过合同金额的 5%" 单独看很常见，甚至像是保护双方。
        但合同 1200 万时封顶只有 60 万，重新采购的差价可能远超。
        **必须做乘法才能看出来**，人通常不会去算。
    """
    outcome = _build_outcome(
        clauses={
            "breach": "第五条 违约责任\n任何一方违约，违约金累计不超过合同总金额的 5%。",
        },
        basic={"amount": "人民币 12,000,000 元"},
    )
    hits = run_deep_checks(outcome)
    assert "D002" in _codes(hits), f"应命中 D002，实际：{_codes(hits)}"

    hit = next(h for h in hits if h.rule_code == "D002")
    assert hit.risk_level == RiskLevel.HIGH
    # 证据必须包含算式，让人能复核这个推断
    assert "5%" in hit.evidence_text
    assert "600,000" in hit.evidence_text, f"应算出封顶额 60 万，实际证据：{hit.evidence_text}"


def test_d002_not_triggered_when_cap_is_reasonable():
    """反向验证：封顶 20% 时不命中（属于合理区间）。

    返回：
        无。
    """
    outcome = _build_outcome(
        clauses={
            "breach": "违约金累计不超过合同总金额的 20%。",
        },
        basic={"amount": "人民币 12,000,000 元"},
    )
    hits = run_deep_checks(outcome)
    assert "D002" not in _codes(hits)


def test_d002_not_triggered_without_amount():
    """反向验证：合同金额缺失时无法换算，不命中（不猜）。

    返回：
        无。

    说明：
        "算不出来"与"算出来没问题"必须区分。
        前者跳过，后者放行；都不能报风险，否则就是假警报。
    """
    outcome = _build_outcome(
        clauses={
            "breach": "违约金累计不超过合同总金额的 5%。",
        }
    )
    hits = run_deep_checks(outcome)
    assert "D002" not in _codes(hits)


# ---------------------------------------------------------------------------
# D003：付款账期跨越合同到期日
# ---------------------------------------------------------------------------


def test_d003_payment_crosses_expiry_detected():
    """验证「到期日临近 + 长账期」的跨期风险被检出。

    返回：
        无。

    为什么这是人工易漏的：
        到期日、验收期限、付款账期三个数字分散在不同条款。
        要发现"尾款可能落在合同到期之后"，必须做日期减法。
        人几乎不会主动去算这个。
    """
    # 构造一个 30 天后到期的合同，账期 90 天 + 验收 7 天
    expire = date.today() + timedelta(days=30)
    outcome = _build_outcome(
        clauses={
            "payment": "剩余款项在验收合格后 90 日内支付。",
            "acceptance": "甲方应在收到交付物后 7 日内完成验收。",
        },
        basic={"expire_date": expire.isoformat()},
    )
    hits = run_deep_checks(outcome)
    assert "D003" in _codes(hits), f"应命中 D003，实际：{_codes(hits)}"

    hit = next(h for h in hits if h.rule_code == "D003")
    # 证据必须写出推演过程：到期日 - (账期+验收期) = 最晚交付日
    assert "90" in hit.evidence_text
    assert "7" in hit.evidence_text
    assert expire.isoformat() in hit.evidence_text


def test_d003_not_triggered_when_expiry_is_far():
    """反向验证：到期日还很远时不报警（避免无意义告警）。

    返回：
        无。

    说明：
        合同刚签、到期日在两年后，账期 90 天完全没问题。
        若这种情况也报，规则会变成噪音，用户会关掉它。
    """
    expire = date.today() + timedelta(days=730)
    outcome = _build_outcome(
        clauses={
            "payment": "剩余款项在验收合格后 90 日内支付。",
            "acceptance": "甲方应在收到交付物后 7 日内完成验收。",
        },
        basic={"expire_date": expire.isoformat()},
    )
    hits = run_deep_checks(outcome)
    assert "D003" not in _codes(hits)


def test_d003_not_triggered_for_expired_contract():
    """反向验证：已过期的合同不报跨期风险。

    返回：
        无。

    验证要点（真实缺陷的回归测试）：
        实测该规则对一份 2025-12-31 到期的历史合同报警，
        算出"距到期 -296 天"。这种提示毫无意义——
        风险已经发生或已不存在，报出来只是噪音。
        因此规则里加了 `remaining <= 0` 直接跳过。
    """
    expire = date.today() - timedelta(days=296)
    outcome = _build_outcome(
        clauses={
            "payment": "剩余款项在验收合格后 90 日内支付。",
            "acceptance": "甲方应在收到交付物后 7 日内完成验收。",
        },
        basic={"expire_date": expire.isoformat()},
    )
    hits = run_deep_checks(outcome)
    assert "D003" not in _codes(hits), "已过期合同不应报跨期风险"


def test_day_extraction_ignores_date_fragments():
    """验证天数抽取不会被日期里的"日"污染。

    返回：
        无。

    验证要点（真实缺陷的回归测试，这是本文件最关键的一条）：
        原文"乙方应于 2024 年 12 月 31 日前完成交付。
              甲方应在收到交付物后 7 日内完成验收。"
        早期实现把"31 日"当成 31 天，导致验收期限从 7 日变成 31 日，
        进而让 D003 算出错误的最晚交付日。

        该缺陷不是深层规则的问题，而是底层 ``extract_day_count`` 的问题，
        因此测试放在这里、直接测工具函数，确保修复不被回退。
    """
    from app.parser.text_utils import extract_day_count, normalize_for_match

    cases = [
        # (原文, 期望天数)
        ("乙方应于 2024 年 12 月 31 日前完成全部交付。", None),
        ("甲方应在收到交付物后 7 日内完成验收。", 7.0),
        ("2024 年 12 月 31 日前交付，7 日内验收", 7.0),
        ("剩余款项在验收合格后 90 日内支付。", 90.0),
        ("5 个工作日内支付", 5.0),
    ]
    for text, expected in cases:
        actual = extract_day_count(normalize_for_match(text))
        assert actual == expected, f"「{text}」应抽出 {expected}，实际 {actual}"


def test_d003_not_triggered_without_expiry_date():
    """反向验证：没有到期日时跳过。

    返回：
        无。
    """
    outcome = _build_outcome(
        clauses={
            "payment": "验收合格后 90 日内支付。",
            "acceptance": "7 日内完成验收。",
        }
    )
    hits = run_deep_checks(outcome)
    assert "D003" not in _codes(hits)


# ---------------------------------------------------------------------------
# D004：扣质保金但无质保期
# ---------------------------------------------------------------------------


def test_d004_warranty_amount_without_period_detected():
    """验证「扣了质保金但全文找不到质保期」被检出。

    返回：
        无。

    为什么这是人工易漏的：
        付款条款写"留 5% 作为质保金，质保期满后支付"——很标准。
        但需要主动去全文找"质保期是多久"，找不到才是问题。
        这类"引用了某个东西但被引用方缺失"的问题，人必须追踪才能发现。
    """
    outcome = _build_outcome(
        clauses={
            "payment": "合同总金额的 5% 作为质保金，质保期满后 30 日内无息退还。",
            "acceptance": "验收标准见附件。",
        },
    )
    hits = run_deep_checks(outcome)
    assert "D004" in _codes(hits), f"应命中 D004，实际：{_codes(hits)}"

    hit = next(h for h in hits if h.rule_code == "D004")
    assert "未找到质保期" in hit.evidence_text or "未约定质保期" in hit.name


def test_d004_not_triggered_when_period_specified():
    """反向验证：明确约定了 12 个月质保期时不命中。

    返回：
        无。
    """
    outcome = _build_outcome(
        clauses={
            "payment": "5% 作为质保金，质保期满后支付。",
            "acceptance": "质保期为验收合格后 12 个月。",
        },
    )
    hits = run_deep_checks(outcome)
    assert "D004" not in _codes(hits)


def test_d004_flags_short_warranty_period():
    """验证质保期过短（3 个月）时给出低风险提示。

    返回：
        无。

    说明：
        这条是 LOW 等级——不是硬伤，但值得提醒。
        区分等级很重要：全是 HIGH 的规则表等于没有优先级。
    """
    outcome = _build_outcome(
        clauses={
            "payment": "5% 作为质保金，质保期满后支付。",
            "acceptance": "质保期为验收合格后 3 个月。",
        },
    )
    hits = run_deep_checks(outcome)
    d004 = [h for h in hits if h.rule_code == "D004"]
    assert d004, "应命中 D004"
    assert d004[0].risk_level == RiskLevel.LOW


# ---------------------------------------------------------------------------
# D005：管辖地与履约地错配
# ---------------------------------------------------------------------------


def test_d005_jurisdiction_mismatch_detected():
    """验证「异地管辖 + 本地履约」的错配被检出。

    返回：
        无。

    为什么这是人工易漏的：
        "由乙方所在地法院管辖" 单独看是明确条款，法务会注意到。
        但如果交付地在我方，证据和标的物都在我方这边，
        却要跑去对方所在地打官司——这个"错配"需要把两个条款对照才成立。
    """
    outcome = _build_outcome(
        clauses={
            "dispute": "因本合同产生的争议，由乙方所在地人民法院管辖。",
            "delivery": "乙方应将货物交付至甲方所在地指定仓库。",
        },
    )
    hits = run_deep_checks(outcome)
    assert "D005" in _codes(hits), f"应命中 D005，实际：{_codes(hits)}"

    hit = next(h for h in hits if h.rule_code == "D005")
    assert "乙方所在地" in hit.evidence_text
    assert "甲方所在地" in hit.evidence_text


def test_d005_not_triggered_when_delivery_elsewhere():
    """反向验证：履约地也在对方所在地时，不构成错配。

    返回：
        无。
    """
    outcome = _build_outcome(
        clauses={
            "dispute": "由乙方所在地人民法院管辖。",
            "delivery": "乙方应将货物交付至乙方所在地工厂。",
        },
    )
    hits = run_deep_checks(outcome)
    assert "D005" not in _codes(hits)


# ---------------------------------------------------------------------------
# 整体行为
# ---------------------------------------------------------------------------


def test_multiple_deep_findings_sorted_by_risk():
    """验证多条深层命中按风险降序排列。

    返回：
        无。

    说明：
        审批人先看高风险。若排序错乱，重要发现会被埋在中间。
    """
    expire = date.today() + timedelta(days=20)
    outcome = _build_outcome(
        clauses={
            # D001 高风险
            "payment": "验收合格后 30 日内支付，5% 作为质保金。",
            "acceptance": "验收标准由甲方确定，质保期 3 个月。",
            # D002 高风险（金额 1200 万 × 5% = 60 万）
            "breach": "违约金累计不超过合同总金额的 5%。",
        },
        basic={"amount": "人民币 12,000,000 元", "expire_date": expire.isoformat()},
    )
    hits = run_deep_checks(outcome)
    assert len(hits) >= 2

    weights = [{"high": 0, "medium": 1, "low": 2}[h.risk_level.value] for h in hits]
    assert weights == sorted(weights), "命中应按风险降序"


def test_no_clauses_no_findings():
    """验证空解析产物不会产生任何命中。

    返回：
        无。

    说明：
        没有条款就不该报风险——报"XX 缺失"是普通规则的职责，
        深层规则只做交叉比对，无输入则无输出。
    """
    outcome = _build_outcome(clauses={})
    assert run_deep_checks(outcome) == []


def test_single_check_failure_does_not_break_others(monkeypatch):
    """验证单个检查抛异常时，其他检查仍能正常返回结果。

    返回：
        无。

    说明：
        深层规则是"增强能力"，不能因为一条逻辑出错就让整次审查失败。
        但也不能静默——所以实现里用 traceback 打印到 stderr。
    """
    import app.review.deep_rules as mod

    original = mod._check_penalty_cap_too_low

    def boom(_outcome):
        raise RuntimeError("模拟检查内部错误")

    # 让 D002 抛异常，同时保留 D001 可用
    monkeypatch.setattr(mod, "_CHECKS", (
        mod._check_payment_depends_on_unilateral_acceptance,
        boom,
    ))

    outcome = _build_outcome(
        clauses={
            "payment": "验收合格后 30 日内支付。",
            "acceptance": "验收标准由甲方确定。",
        }
    )
    hits = mod.run_deep_checks(outcome)
    # D001 仍应命中，说明异常被隔离
    assert "D001" in _codes(hits)

    monkeypatch.setattr(mod, "_check_penalty_cap_too_low", original)
