"""规则引擎测试：验证三种匹配模式、风险汇总与种子规则覆盖度。

覆盖需求：
- 2.4.6「规则至少覆盖预付款比例、付款周期、自动续约、违约责任、管辖地、
  主体信息缺失、金额缺失、保密缺失、数据处理、知识产权、验收标准缺失」；
- 2.4.11「输出总风险等级与审批关注点」；
- 2.4.6「命中结果至少包含规则名称、风险等级、命中证据、证据位置、建议说明」。

设计原因：
本文件用手工构造的 :class:`ParseOutcome` 直接测引擎，
不经过文件读取与数据库——规则逻辑是纯函数，单独测既快又能精确构造边界。
"""

from __future__ import annotations

import pytest

from app.domain.enums import ExtractStatus, MatchMode, ParseStatus, RiskLevel, RuleStatus
from app.domain.schemas import (
    CLAUSE_LABELS,
    DocumentText,
    FieldEvidence,
    ParseOutcome,
    ParsedField,
    TextLine,
)
from app.review.rule_engine import RuleSpec, evaluate_rules
from app.review.seed_rules import SEED_RULES


def _line(text: str, page: int = 1, line_no: int = 1) -> TextLine:
    """构造一行文本。

    参数：
        text: 行内容。
        page: 页码。
        line_no: 行号。
    返回：
        :class:`TextLine`。
    关联：
        被本文件的 :func:`_outcome` 用于拼装文档。
    """
    return TextLine(page=page, line_no=line_no, text=text)


def _outcome(
    lines: list[str],
    basic: dict[str, str] | None = None,
    clauses: dict[str, str] | None = None,
) -> ParseOutcome:
    """构造一个解析产物用于规则测试。

    参数：
        lines: 原文行列表。
        basic: 基础信息字段（键 → 值），未给出的字段记为 not_found。
        clauses: 条款字段（键 → 值），未给出的条款记为 not_found。
    返回：
        :class:`ParseOutcome`。
    关联：
        被本文件各测试构造输入；刻意让"未给出的字段/条款"处于 not_found 状态，
        以便 missing 模式规则能被正确触发。
    """
    text_lines = tuple(
        _line(text, page=1, line_no=index) for index, text in enumerate(lines, start=1)
    )
    document = DocumentText(
        lines=text_lines, page_count=1, ocr_pages=(), text_pages=(1,)
    )

    basic_fields: dict[str, ParsedField] = {}
    for key, label in [
        ("contract_title", "合同标题"), ("contract_no", "合同编号"),
        ("our_party", "签约主体（我方）"), ("counterparty", "对方名称"),
        ("amount", "合同金额"), ("currency", "币种"),
        ("effective_date", "生效时间"), ("expire_date", "到期时间"),
    ]:
        value = (basic or {}).get(key)
        status = ExtractStatus.SUCCESS if value else ExtractStatus.NOT_FOUND
        basic_fields[key] = ParsedField(
            key=key, label=label, value=value,
            evidence=FieldEvidence(
                snippet=value or "", location="第1页第1行" if value else "",
                status=status, detail="" if value else "测试构造：未提供",
            ),
        )

    clause_fields: dict[str, ParsedField] = {}
    for key, label in CLAUSE_LABELS.items():
        value = (clauses or {}).get(key)
        status = ExtractStatus.SUCCESS if value else ExtractStatus.NOT_FOUND
        clause_fields[key] = ParsedField(
            key=key, label=label, value=value,
            evidence=FieldEvidence(
                snippet=value or "", location="第1页第1行" if value else "",
                status=status, detail="" if value else "测试构造：未提供",
            ),
        )

    return ParseOutcome(
        status=ParseStatus.SUCCESS,
        basic_info=basic_fields,
        clauses=clause_fields,
        document=document,
    )


def _rule(
    mode: MatchMode,
    match_text: str,
    condition_json: str = "{}",
    risk: RiskLevel = RiskLevel.MEDIUM,
    code: str = "R_TEST",
    name: str = "测试规则",
) -> RuleSpec:
    """构造一条测试规则。

    参数：
        mode: 匹配模式。
        match_text: 匹配文本。
        condition_json: 结构化条件。
        risk: 风险等级。
        code: 规则编码。
        name: 规则名称。
    返回：
        :class:`RuleSpec`。
    关联：
        被本文件各测试用于精确构造被测规则。
    """
    return RuleSpec(
        rule_id=1, rule_code=code, rule_name=name, risk_level=risk,
        match_mode=mode, match_text=match_text, suggestion_text="测试建议",
        priority=1, condition_json=condition_json,
    )


# ---------------------------------------------------------------------------
# 三种匹配模式
# ---------------------------------------------------------------------------


def test_keyword_mode_hits_and_reports_evidence():
    """验证 keyword 模式能命中并给出证据与位置。

    关联：
        ``app.review.rule_engine._eval_keyword``。
    """
    outcome = _outcome(
        ["本合同期满后自动续约一年。"],
        basic={"contract_no": "HT-1"},
    )
    rule = _rule(MatchMode.KEYWORD, "自动续约", condition_json='{"scope": "full"}')
    review = evaluate_rules(outcome, [rule])

    assert len(review.hits) == 1
    hit = review.hits[0]
    assert hit.rule_name == "测试规则"
    assert hit.risk_level == RiskLevel.MEDIUM
    assert "自动续约" in hit.evidence_text
    assert hit.evidence_position == "全文检索"
    assert hit.suggestion_text == "测试建议"


def test_keyword_mode_respects_scope():
    """验证 keyword 模式的 scope 限定只在指定条款内检索。

    关联：
        ``_eval_keyword`` + ``_scope_text``。

    验证要点：
        违约条款内的"违约金"不应被"付款条款"范围的规则命中，
        否则规则会互相串味、产生无法解释的命中。
    """
    outcome = _outcome(
        ["第四条 违约责任", "违约金为合同金额的 10%。"],
        clauses={"breach": "第四条 违约责任\n违约金为合同金额的 10%。",
                 "payment": "第二条 付款方式\n验收后 30 日内支付。"},
    )
    rule_in_payment = _rule(
        MatchMode.KEYWORD, "违约金", condition_json='{"scope": "payment"}'
    )
    review = evaluate_rules(outcome, [rule_in_payment])
    assert review.hits == (), "付款条款内没有'违约金'，不应命中"

    rule_in_breach = _rule(
        MatchMode.KEYWORD, "违约金", condition_json='{"scope": "breach"}'
    )
    review2 = evaluate_rules(outcome, [rule_in_breach])
    assert len(review2.hits) == 1


def test_missing_mode_hits_when_clause_absent():
    """验证 missing 模式在条款缺失时命中，且证据说明是"未检出"。

    关联：
        ``app.review.rule_engine._eval_missing``。

    验证要点：
        证据文本必须明确写"未检出"，不能编造原文——
        这是"证据不可伪造"的直接检验。
    """
    outcome = _outcome(["第一条 服务内容", "乙方向甲方提供服务。"])
    rule = _rule(MatchMode.MISSING, "confidentiality", risk=RiskLevel.MEDIUM,
                 name="保密条款缺失")
    review = evaluate_rules(outcome, [rule])

    assert len(review.hits) == 1
    assert "未检出" in review.hits[0].evidence_text
    assert "保密条款" in review.hits[0].evidence_text
    assert review.hits[0].evidence_position == "全文检索"


def test_missing_mode_no_hit_when_present():
    """验证条款存在时 missing 模式不命中。

    关联：
        ``_eval_missing``。
    """
    outcome = _outcome(
        ["第五条 保密", "双方负有保密义务。"],
        clauses={"confidentiality": "第五条 保密\n双方负有保密义务。"},
    )
    rule = _rule(MatchMode.MISSING, "confidentiality")
    review = evaluate_rules(outcome, [rule])
    assert review.hits == ()


def test_missing_mode_on_basic_field():
    """验证 missing 模式可作用于基础信息字段（如金额缺失）。

    关联：
        ``_eval_missing`` 的字段分支；需求 2.4.6「金额缺失」。
    """
    outcome = _outcome(["合同编号：HT-1"], basic={"contract_no": "HT-1"})
    rule = _rule(MatchMode.MISSING, "amount", risk=RiskLevel.HIGH, name="合同金额缺失")
    review = evaluate_rules(outcome, [rule])

    assert len(review.hits) == 1
    assert "合同金额" in review.hits[0].evidence_text
    assert review.hits[0].risk_level == RiskLevel.HIGH


def test_threshold_mode_percent():
    """验证 threshold 模式按百分比比较，超限命中。

    关联：
        ``app.review.rule_engine._eval_threshold`` + ``text_utils.extract_percent``。
    """
    outcome = _outcome(
        ["第二条 付款方式", "甲方支付合同总金额 60% 作为预付款。"],
        clauses={"payment": "第二条 付款方式\n甲方支付合同总金额 60% 作为预付款。"},
    )
    rule = _rule(
        MatchMode.THRESHOLD, "预付款>30%", risk=RiskLevel.HIGH,
        condition_json='{"scope": "payment", "extract": "percent", "operator": ">", "value": 30}',
    )
    review = evaluate_rules(outcome, [rule])

    assert len(review.hits) == 1
    assert "60%" in review.hits[0].evidence_text
    assert review.hits[0].evidence_position.startswith("第1页")


def test_threshold_mode_not_hit_when_below():
    """验证未超阈值时不命中。

    关联：
        ``_eval_threshold``。
    """
    outcome = _outcome(
        ["预付款为 20%。"],
        clauses={"payment": "预付款为 20%。"},
    )
    rule = _rule(
        MatchMode.THRESHOLD, "预付款>30%",
        condition_json='{"scope": "payment", "extract": "percent", "operator": ">", "value": 30}',
    )
    review = evaluate_rules(outcome, [rule])
    assert review.hits == ()


def test_threshold_mode_returns_none_when_no_number():
    """验证抽不到数值时不命中（不猜、不误报）。

    关联：
        ``_eval_threshold`` 的 ``actual is None`` 分支。

    验证要点：
        "没读到数值"与"读到了且超限"是两件事。若前者也命中，
        会产生无法核对的假风险。
    """
    outcome = _outcome(
        ["付款方式另行协商。"],
        clauses={"payment": "付款方式另行协商。"},
    )
    rule = _rule(
        MatchMode.THRESHOLD, "预付款>30%",
        condition_json='{"scope": "payment", "extract": "percent", "operator": ">", "value": 30}',
    )
    review = evaluate_rules(outcome, [rule])
    assert review.hits == ()


def test_threshold_mode_day_extraction():
    """验证 threshold 模式可抽取天数（付款周期）。

    关联：
        ``_eval_threshold`` 的 ``extract="day"`` 分支 + ``text_utils.extract_day_count``。
    需求：
        2.4.6「付款周期」。
    """
    outcome = _outcome(
        ["剩余款项在验收合格后 90 日内支付。"],
        clauses={"payment": "剩余款项在验收合格后 90 日内支付。"},
    )
    rule = _rule(
        MatchMode.THRESHOLD, "付款周期>60日",
        condition_json='{"scope": "payment", "extract": "day", "operator": ">", "value": 60}',
    )
    review = evaluate_rules(outcome, [rule])
    assert len(review.hits) == 1
    assert "90" in review.hits[0].evidence_text


def test_threshold_handles_chinese_numbers():
    """验证中文数字（如"九十日"）能被正确抽取并比较。

    关联：
        ``text_utils.normalize_cn_numbers`` + ``extract_day_count``。

    验证要点：
        合同常用中文数字表述期限，若不转换会漏掉大量真实条款。
    """
    outcome = _outcome(
        ["剩余款项在验收合格后九十日内支付。"],
        clauses={"payment": "剩余款项在验收合格后九十日内支付。"},
    )
    rule = _rule(
        MatchMode.THRESHOLD, "付款周期>60日",
        condition_json='{"scope": "payment", "extract": "day", "operator": ">", "value": 60}',
    )
    review = evaluate_rules(outcome, [rule])
    assert len(review.hits) == 1


# ---------------------------------------------------------------------------
# 汇总行为
# ---------------------------------------------------------------------------


def test_overall_risk_is_max_of_hits():
    """验证总风险等级取所有命中的最高等级。

    关联：
        ``evaluate_rules`` + ``enums.max_risk_level``；需求 2.4.11。
    """
    outcome = _outcome(["自动续约", "违约金 10%"])
    rules = [
        _rule(MatchMode.KEYWORD, "自动续约", risk=RiskLevel.LOW,
              code="R_A", name="低风险规则", condition_json='{"scope": "full"}'),
        _rule(MatchMode.KEYWORD, "违约金", risk=RiskLevel.HIGH,
              code="R_B", name="高风险规则", condition_json='{"scope": "full"}'),
        _rule(MatchMode.KEYWORD, "不存在的词", risk=RiskLevel.MEDIUM,
              code="R_C", name="不命中规则", condition_json='{"scope": "full"}'),
    ]
    review = evaluate_rules(outcome, rules)

    assert len(review.hits) == 2
    assert review.overall_risk_level == RiskLevel.HIGH
    # 命中按风险降序，高风险必须排在前面。
    assert review.hits[0].risk_level == RiskLevel.HIGH
    assert review.hits[1].risk_level == RiskLevel.LOW


def test_overall_risk_low_when_no_hits():
    """验证无命中时总风险为 low，且摘要明确说明"不替代人工判断"。

    关联：
        ``evaluate_rules`` + ``_build_summary``。

    验证要点：
        摘要必须写明结论仅代表规则层面未命中，
        否则"无命中"会被误读为"合同无风险"。
    """
    outcome = _outcome(["正常条款内容。"])
    review = evaluate_rules(outcome, [_rule(MatchMode.KEYWORD, "不存在")])

    assert review.hits == ()
    assert review.overall_risk_level == RiskLevel.LOW
    assert "不替代" in review.summary_text


def test_focus_points_generated_and_deduplicated():
    """验证审批关注点被生成且去重。

    关联：
        ``_build_focus_points``；需求 2.4.6「审查输出必须包含审批关注点列表」。
    """
    outcome = _outcome(["自动续约", "违约金 30%"])
    same_suggestion = "统一建议文本"
    rules = [
        RuleSpec(rule_id=1, rule_code="R_A", rule_name="规则A", risk_level=RiskLevel.HIGH,
                 match_mode=MatchMode.KEYWORD, match_text="自动续约",
                 suggestion_text=same_suggestion, priority=1,
                 condition_json='{"scope": "full"}'),
        RuleSpec(rule_id=2, rule_code="R_B", rule_name="规则B", risk_level=RiskLevel.MEDIUM,
                 match_mode=MatchMode.KEYWORD, match_text="违约金",
                 suggestion_text=same_suggestion, priority=2,
                 condition_json='{"scope": "full"}'),
    ]
    review = evaluate_rules(outcome, rules)

    assert len(review.hits) == 2
    # 建议文本相同，关注点应去重为 1 条。
    assert len(review.focus_points) == 1


def test_evaluated_rule_count_reported():
    """验证执行规则数被如实上报。

    关联：
        ``evaluate_rules`` 的 ``evaluated_rule_count``。

    验证要点：
        该数字是"审查覆盖面"的唯一证据。若只报命中数，
        审批人无法判断"是规则少还是合同干净"。
    """
    outcome = _outcome(["无关内容"])
    rules = [_rule(MatchMode.KEYWORD, f"词{i}", code=f"R{i}") for i in range(5)]
    review = evaluate_rules(outcome, rules)
    assert review.evaluated_rule_count == 5
    assert len(review.hits) == 0


def test_unknown_match_mode_does_not_crash():
    """验证未知匹配模式不会让整次审查崩掉。

    关联：
        ``evaluate_rules`` 的模式分派。

    验证要点：
        规则是人工维护的配置，单条规则配置错误不应中断整条流水线，
        但也不应产生命中（避免误报）。
    """
    outcome = _outcome(["自动续约"])
    bad_rule = RuleSpec(
        rule_id=1, rule_code="R_BAD", rule_name="坏规则",
        risk_level=RiskLevel.HIGH, match_mode="unknown_mode",  # type: ignore[arg-type]
        match_text="自动续约", suggestion_text="", priority=1,
    )
    review = evaluate_rules(outcome, [bad_rule])
    assert review.hits == ()
    assert review.evaluated_rule_count == 1


# ---------------------------------------------------------------------------
# 种子规则覆盖度
# ---------------------------------------------------------------------------


def test_seed_rules_cover_required_topics():
    """验证种子规则覆盖需求 2.4.6 列出的 11 类主题。

    关联：
        ``app.review.seed_rules.SEED_RULES``。

    验证要点：
        每个主题至少有一条规则，且规则编码唯一。
        这是"规则覆盖度"的可核对证据，而不是口头声明。
    """
    # 11 类主题到"应能在规则编码或名称中找到的关键词"的映射。
    required_topics = {
        "预付款比例": ("ADVANCE_PAYMENT", "预付款"),
        "付款周期": ("PAYMENT_PERIOD", "付款周期"),
        "自动续约": ("AUTO_RENEWAL", "自动续约"),
        "违约责任": ("PENALTY", "违约金"),
        "管辖地": ("JURISDICTION", "管辖"),
        "主体信息缺失": ("NO_OUR_PARTY", "签约主体"),
        "金额缺失": ("NO_AMOUNT", "金额缺失"),
        "保密缺失": ("NO_CONFIDENTIALITY", "保密"),
        "数据处理": ("DATA", "数据"),
        "知识产权": ("IP", "知识产权"),
        "验收标准缺失": ("ACCEPTANCE", "验收"),
    }

    all_text = " ".join(
        f"{rule.rule_code} {rule.rule_name}" for rule in SEED_RULES
    )
    for topic, keywords in required_topics.items():
        assert any(kw in all_text for kw in keywords), f"缺少主题：{topic}"

    codes = [rule.rule_code for rule in SEED_RULES]
    assert len(codes) == len(set(codes)), "规则编码必须唯一"


def test_seed_rules_have_required_fields():
    """验证每条种子规则都具备需求 2.4.6 规定的字段且取值合法。

    关联：
        ``SeedRule`` 定义 + ``ReviewRule`` 表结构。

    验证要点：
        需求原文列出 8 个必需字段，这里逐条校验取值非空且枚举合法。
    """
    import json

    for rule in SEED_RULES:
        assert rule.rule_code and rule.rule_name
        assert rule.risk_level in RiskLevel
        assert rule.rule_status == RuleStatus.ENABLED
        assert rule.match_mode in MatchMode
        assert rule.match_text
        assert rule.suggestion_text
        # condition_json 必须是合法 JSON（threshold 规则依赖它求值）。
        parsed = json.loads(rule.condition_json)
        assert isinstance(parsed, dict)


def test_seed_rules_threshold_conditions_wellformed():
    """验证 threshold 型规则的条件结构完整。

    关联：
        ``_eval_threshold`` 对 ``condition_json`` 的字段要求。

    验证要点：
        threshold 规则若缺 ``scope``/``extract``/``operator``/``value``，
        会静默不命中，等于这条规则形同虚设——必须显式测出来。
    """
    import json

    threshold_rules = [r for r in SEED_RULES if r.match_mode == MatchMode.THRESHOLD]
    assert threshold_rules, "至少应有一条 threshold 规则"

    for rule in threshold_rules:
        cond = json.loads(rule.condition_json)
        assert "scope" in cond, f"{rule.rule_code} 缺少 scope"
        assert "extract" in cond, f"{rule.rule_code} 缺少 extract"
        assert "operator" in cond, f"{rule.rule_code} 缺少 operator"
        assert "value" in cond, f"{rule.rule_code} 缺少 value"
        assert cond["extract"] in ("percent", "day", "month", "amount")
        assert cond["operator"] in (">", ">=", "<", "<=", "==", "!=")
