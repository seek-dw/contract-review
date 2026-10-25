"""合同陷阱规则：基于真实法律实务案例的可检测陷阱。

============================================================================
本模块与 seed_rules / deep_rules 的区别
============================================================================
- ``seed_rules.py``（26 条）：需求 2.4.6 要求的基础特征匹配，法务扫一眼就能发现。
- ``deep_rules.py``（5 条）：跨条款交叉比对，需要做运算才能发现。
- **本模块**：来自**真实司法案例与法院裁判观点**的陷阱，
  每一条都有明确的法条依据或判例后果。

============================================================================
资料来源（全部为公开的法律实务文章与法院发布内容）
============================================================================
1. 山东德衡律师事务所 郭培明《供应链合同十大法律陷阱》(2025-05-21)
   https://www.deheng.com/dhgc/flgc/25764.html
   提供了 10 类陷阱的真实案例、法条依据、量化阈值。

2. 浙江法院网《避雷合同中的这5类"模糊条款"》(开化县人民法院, 2025-06-17)
   https://zjsfgkw.gov.cn/art/2025/6/17/art_56_37364.html
   提供了法院视角的模糊条款清单与《民法典》条款对照。

**重要说明**：
本模块只把"可以从文本中确定性判断"的陷阱做成规则。
有些陷阱（如"上下游违约金比例差超 70% 构成显失公平"）
需要上下游两份合同才能判断，单份合同无法检测，因此**不实现**——
不实现比实现一个会误报的规则更好。

============================================================================
设计原则
============================================================================
1. **每条规则都标注法条依据**：命中时告诉审批人"违反了哪一条"，
   而不是只说"我觉得有问题"。
2. **只报"确定缺失/确定违规"的情况**：模糊表述本身不必然违法，
   但某些表述已被法律明确否定（如同时约定仲裁和诉讼 → 仲裁条款无效），
   这类才报。
3. **量化阈值来自法条或判例**，不是我拍的：
   - 定金超过合同金额 20% → 《民法典》第 586 条，超出部分不产生定金效力
   - 违约金超过实际损失 30% → 《九民纪要》第 50 条，可请求调减
   - 质量约定不明 → 《民法典》第 511 条，按国标履行（可能不符买方需求）
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.enums import RiskLevel
from app.domain.schemas import (
    ParseOutcome,
    RuleHitView,
)
from app.parser.text_utils import (
    extract_amount,
    extract_percent,
    normalize_for_match,
)


@dataclass(frozen=True)
class TrapFinding:
    """一条陷阱命中。

    用途：模块内部载体，最后转成 :class:`RuleHitView`。
    字段比 :class:`app.review.deep_rules.DeepFinding` 多一个 ``legal_basis``，
    因为本模块的每条规则都有法条依据，必须展示给审批人。
    """

    # 规则编码，前缀 T 表示 Trap
    code: str
    # 规则名称
    name: str
    # 风险等级
    risk: RiskLevel
    # 命中的原文或缺失说明
    evidence: str
    # 证据位置
    position: str
    # 法条依据（这是本模块的核心价值：不是主观判断，是有依据的）
    legal_basis: str
    # 处理建议
    suggestion: str


def _clause(outcome: ParseOutcome, key: str) -> str:
    """取某条款的归一化文本。

    参数：
        outcome: 解析产物。
        key: 条款键。
    返回：
        归一化文本；不存在时返回空串。
    关联：
        被本模块所有检查函数调用。
    """
    raw = outcome.clause_value(key)
    return normalize_for_match(raw) if raw else ""


def _pos(outcome: ParseOutcome, key: str) -> str:
    """取某条款的位置标识。

    参数：
        outcome: 解析产物。
        key: 条款键。
    返回：
        位置字符串；不存在时返回"全文检索"。
    """
    ev = outcome.clause_evidence(key)
    return ev.location if ev and ev.location else "全文检索"


def _full(outcome: ParseOutcome) -> str:
    """取全文归一化文本。

    参数：
        outcome: 解析产物。
    返回：
        归一化全文。
    关联：
        被需要全文搜索的检查调用。
    """
    return normalize_for_match(outcome.document.full_text)


# ===========================================================================
# T001 争议解决条款：同时约定仲裁和诉讼 → 仲裁条款无效
# ===========================================================================
# 依据：浙江法院网《避雷合同中的这5类"模糊条款"》
#   "同时约定仲裁和诉讼" 属于争议解决条款的典型模糊情形。
#   法条：《最高人民法院关于适用〈仲裁法〉若干问题的解释》第七条
#   "当事人约定争议可以向仲裁机构申请仲裁也可以向人民法院起诉的，
#    仲裁协议无效。"
#
# 为什么这是真实陷阱：
#   当事人以为"两条路都能走更灵活"，实际结果是仲裁约定作废，
#   只能走诉讼。如果原本想用仲裁的保密性和效率，目的完全落空。


def _check_arbitration_and_litigation_conflict(
    outcome: ParseOutcome,
) -> TrapFinding | None:
    """检测争议解决条款同时约定仲裁与诉讼。

    参数：
        outcome: 解析产物。
    返回：
        命中时返回发现，否则 ``None``。
    关联：
        读取 ``dispute`` 条款。
    """
    dispute = _clause(outcome, "dispute")
    if not dispute:
        return None

    has_arbitration = any(k in dispute for k in ("仲裁", "仲裁委员会", "仲裁机构"))
    has_litigation = any(k in dispute for k in ("法院", "人民法院", "诉讼", "起诉"))

    if not (has_arbitration and has_litigation):
        return None

    # 排除"先仲裁后诉讼"的合法阶梯约定：
    # 例如"协商不成的，提交仲裁"这类表述里也可能同时出现"法院"字样，
    # 但实际是单选。用"或"连接才视为并列可选。
    if not re.search(r"(仲裁|法院)[^。；]{0,30}?(或|或者)[^。；]{0,30}?(仲裁|法院|诉讼)", dispute):
        return None

    return TrapFinding(
        code="T001",
        name="争议解决条款同时约定仲裁与诉讼",
        risk=RiskLevel.HIGH,
        evidence=(
            f"争议解决条款同时出现仲裁与诉讼两种方式，且以「或」并列。"
            f"原文：{dispute[:160]}"
        ),
        position=_pos(outcome, "dispute"),
        legal_basis=(
            "《最高人民法院关于适用〈中华人民共和国仲裁法〉若干问题的解释》第七条："
            "当事人约定争议可以向仲裁机构申请仲裁也可以向人民法院起诉的，仲裁协议无效。"
        ),
        suggestion=(
            "二选一：若要仲裁，删除诉讼表述并明确仲裁机构全称；"
            "若要诉讼，删除仲裁表述并明确具体法院。"
        ),
    )


# ===========================================================================
# T002 定金比例超过合同金额 20%
# ===========================================================================
# 依据：《民法典》第 586 条
#   "定金的数额由当事人约定；但是，不得超过主合同标的额的百分之二十，
#    超过部分不产生定金的效力。"
#
# 真实案例（德衡文章第五类）：
#   某设备采购合同约定定金 30%（300 万），卖方违约，买方主张双倍返还 600 万。
#   法院认定超过 20% 的部分（100 万）无效，仅支持返还 400 万。
#
# 为什么这是真实陷阱：
#   付了 30% 定金的一方以为能主张 60% 的双倍返还，
#   实际只能主张 40%（20% × 2），差额部分变成普通预付款，无担保效力。


def _check_deposit_over_limit(outcome: ParseOutcome) -> TrapFinding | None:
    """检测定金比例超过合同金额 20%。

    参数：
        outcome: 解析产物。
    返回：
        命中时返回发现，否则 ``None``。
    关联：
        读取 ``payment`` 条款与 ``amount`` 字段。
    """
    payment = _clause(outcome, "payment")
    if not payment or "定金" not in payment:
        return None

    # 只在"定金"锚点后取百分比，避免拿到条款里其他百分比
    idx = payment.find("定金")
    window = payment[idx : idx + 40]
    percent = extract_percent(window)
    if percent is None:
        return None
    if percent <= 20:
        return None

    total = extract_amount(normalize_for_match(outcome.basic_value("amount") or ""))
    total_note = ""
    if total is not None:
        total_value = total[0]
        excess = total_value * (percent - 20) / 100
        total_note = (
            f"合同金额 {total_value:,.0f} 元，超出部分约 {excess:,.0f} 元不产生定金效力。"
        )

    return TrapFinding(
        code="T002",
        name="定金比例超过合同金额 20%",
        risk=RiskLevel.HIGH,
        evidence=(
            f"付款条款约定定金 {percent:g}%，超过法定上限 20%。{total_note}"
        ),
        position=_pos(outcome, "payment"),
        legal_basis=(
            "《民法典》第五百八十六条：定金的数额由当事人约定；"
            "但是，不得超过主合同标的额的百分之二十，超过部分不产生定金的效力。"
        ),
        suggestion=(
            "将定金比例压至 20% 以内；超出部分改为预付款并明确其不适用定金罚则。"
            "若需更强担保，改用银行保函或履约保证金。"
        ),
    )


# ===========================================================================
# T003 质量条款"空心化"：只写"符合国家标准"而无量化指标
# ===========================================================================
# 依据：《民法典》第 511 条第（一）项
#   "质量要求不明确的，按照强制性国家标准履行；没有强制性国家标准的，
#    按照推荐性国家标准履行……"
#
# 真实案例（德衡文章第一类）：
#   某烘焙企业与面粉供应商约定"面粉符合食品安全标准"，未明确重金属限值。
#   后因砷含量 0.4mg/kg 超标（国标限值 ≤0.5，行业内控 ≤0.3）导致产品召回。
#   法院认定：合同未约定特殊标准时优先适用国标，供应商未违约。
#   买方承担全部损失。
#
# 为什么这是真实陷阱：
#   "符合国家标准"看起来是质量保证，实际是**把标准降到最低线**。
#   买方真正的内控要求（≤0.3）没写进合同，法院只能按国标（≤0.5）判。


def _check_quality_vague(outcome: ParseOutcome) -> TrapFinding | None:
    """检测质量/验收条款只有"符合标准"而无量化指标。

    参数：
        outcome: 解析产物。
    返回：
        命中时返回发现，否则 ``None``。
    关联：
        读取 ``acceptance`` 与 ``delivery`` 条款。

    判定逻辑：
        1. 出现"符合国家标准""符合行业标准""符合相关标准"这类泛化表述；
        2. 全文找不到任何量化指标（数字+单位，如 MPa、mg/kg、小时、次）；
        3. 两条同时成立 → 报中风险。
    """
    acceptance = _clause(outcome, "acceptance")
    delivery = _clause(outcome, "delivery")
    combined = f"{acceptance}\n{delivery}"
    if not combined.strip():
        return None

    vague_patterns = (
        "符合国家标准", "符合行业标准", "符合相关标准", "符合国家规定",
        "满足国家标准", "达到国家标准", "符合规范要求",
    )
    vague_hit = next((p for p in vague_patterns if p in combined), None)
    if vague_hit is None:
        return None

    # 全文找量化指标：数字后跟技术单位
    quantitative = re.search(
        r"\d+(?:\.\d+)?\s*"
        r"(?:MPa|MPa|mg/kg|mg/L|μm|um|mm|cm|℃|度|小时|min|分钟|次|%|N|kg|g)",
        _full(outcome),
    )
    if quantitative:
        return None

    return TrapFinding(
        code="T003",
        name="质量条款仅有「符合标准」表述，缺少量化指标",
        risk=RiskLevel.MEDIUM,
        evidence=(
            f"质量/验收条款使用「{vague_hit}」这类泛化表述，"
            f"全文未检出任何量化技术指标（如强度、含量、精度、时长）。"
            f"发生争议时法院将按国家标准认定，可能低于我方实际需求。"
        ),
        position=_pos(outcome, "acceptance") or _pos(outcome, "delivery"),
        legal_basis=(
            "《民法典》第五百一十一条第（一）项：质量要求不明确的，"
            "按照强制性国家标准履行；没有强制性国家标准的，按照推荐性国家标准履行。"
        ),
        suggestion=(
            "采用「量化指标 + 引用标准 + 验收程序」写法，"
            "例如「屈服强度 ≥345MPa，执行 GB/T1591-2018，"
            "买方委托第三方检测，不合格项检测费由卖方承担」。"
            "并补充「合同标准高于国标时以合同为准」的排除条款。"
        ),
    )


# ===========================================================================
# T004 履约期限使用"尽快""及时"等模糊表述
# ===========================================================================
# 依据：《民法典》第 511 条第（四）项
#   "履行期限不明确的，债务人可以随时履行，债权人也可以随时请求履行，
#    但是应当给对方必要的准备时间。"
#
# 依据：浙江法院网《避雷合同中的这5类"模糊条款"》
#   "采用'尽快''尽早''择机'等抽象表述，未明确具体时间节点。
#    如供货合同中'立即发货'未约定具体几日内发货。"
#
# 为什么这是真实陷阱：
#   "尽快发货"听起来是承诺，实际法律效果是"债务人可以随时履行"。
#   我方无法据此主张逾期违约，因为根本没有可计算的逾期起点。


def _check_vague_deadline(outcome: ParseOutcome) -> TrapFinding | None:
    """检测履约期限使用模糊表述。

    参数：
        outcome: 解析产物。
    返回：
        命中时返回发现，否则 ``None``。
    关联：
        读取 ``delivery`` 条款。
    """
    delivery = _clause(outcome, "delivery")
    if not delivery:
        return None

    vague_terms = ("尽快", "尽早", "及时", "立即", "择机", "视情况", "抓紧")
    hit = next((t for t in vague_terms if t in delivery), None)
    if hit is None:
        return None

    # 如果同一句里已经给了具体天数，则不算模糊
    idx = delivery.find(hit)
    window = delivery[max(0, idx - 20) : idx + 30]
    if re.search(r"\d+\s*(?:个)?\s*(?:工作日|日|天|周|月)", window):
        return None

    return TrapFinding(
        code="T004",
        name="履约期限使用「尽快/及时」等模糊表述",
        risk=RiskLevel.MEDIUM,
        evidence=(
            f"交付条款使用「{hit}」这类不确定表述，且未给出具体天数。"
            f"原文：{window}"
        ),
        position=_pos(outcome, "delivery"),
        legal_basis=(
            "《民法典》第五百一十一条第（四）项：履行期限不明确的，"
            "债务人可以随时履行，债权人也可以随时请求履行，但应当给对方必要的准备时间。"
        ),
        suggestion=(
            "改为具体天数，例如「合同生效后 15 个工作日内发货」。"
            "若确需弹性，约定上限：「最迟不超过 30 日」。"
        ),
    )


# ===========================================================================
# T005 违约金无上限且比例偏高
# ===========================================================================
# 依据：《民法典》第 585 条第二款
#   "约定的违约金过分高于造成的损失的，人民法院或者仲裁机构可以根据
#    当事人的请求予以适当减少。"
# 依据：《九民纪要》第 50 条
#   违约金超过实际损失 30% 的，一般可认定为"过分高于"。
#
# 真实案例（德衡文章第二类）：
#   某汽车座椅供应商逾期交货按日 0.5% 支付违约金。
#   因下游延迟，甲方索赔 2000 万（含预期利润 1200 万），
#   而乙方仅能从下游追偿 300 万。法院认定违约金比例差超 70% 显失公平。
#
# 为什么这是真实陷阱：
#   日 0.5% 看似不高，但逾期 60 天就是 30%，逾期 200 天就超过合同金额本身。
#   且未设上限时，违约金可能远超合同金额，虽然可请求调减，
#   但需要诉讼、举证，成本已经发生。


def _check_daily_penalty_no_cap(outcome: ParseOutcome) -> TrapFinding | None:
    """检测按日计违约金但无累计上限。

    参数：
        outcome: 解析产物。
    返回：
        命中时返回发现，否则 ``None``。
    关联：
        读取 ``breach`` 条款。

    判定逻辑：
        1. 出现"每逾期一日""按日""每日" + 百分比；
        2. 全文找不到"不超过""上限""封顶""最高"这类封顶表述；
        3. 两条同时成立 → 报中风险。
    """
    breach = _clause(outcome, "breach")
    if not breach:
        return None

    daily_pattern = re.search(
        # 注意：``normalize_for_match`` 会把中文数字转阿拉伯数字并加空格，
        # 所以"每逾期一日"在归一化后变成"每逾期1 日"。
        # 正则必须容忍这个空格，否则永远匹配不到（实测踩过）。
        r"(每\s*逾期\s*\d*\s*日|每\s*日|按\s*日|每\s*天)"
        r"[^。；]{0,20}?(\d+(?:\.\d+)?)\s*%",
        breach,
    )
    if not daily_pattern:
        return None

    daily_percent = float(daily_pattern.group(2))

    # 已有封顶则不报
    if any(k in breach for k in ("不超过", "上限", "封顶", "最高不超过", "累计不超过")):
        return None

    # 换算：逾期多少天等于合同金额
    days_to_full = 100 / daily_percent if daily_percent else 0

    return TrapFinding(
        code="T005",
        name="按日计违约金但未设累计上限",
        risk=RiskLevel.MEDIUM,
        evidence=(
            f"违约条款约定按日 {daily_percent:g}% 计违约金，且未设累计上限。"
            f"按此计算，逾期 {days_to_full:.0f} 天违约金即等于合同总金额。"
        ),
        position=_pos(outcome, "breach"),
        legal_basis=(
            "《民法典》第五百八十五条第二款：约定的违约金过分高于造成的损失的，"
            "人民法院或者仲裁机构可以根据当事人的请求予以适当减少。"
            "《九民纪要》第 50 条：超过实际损失 30% 一般可认定为过分高于。"
        ),
        suggestion=(
            "增加累计上限，例如「违约金累计不超过合同总金额的 20%」。"
            "同时明确「违约金不足以弥补损失的，守约方可另行主张赔偿」。"
        ),
    )


# ===========================================================================
# T006 所有权保留条款缺失（赊销场景）
# ===========================================================================
# 依据：《民法典》第 641 条、第 642 条
#   第 641 条允许约定所有权保留；第 642 条规定取回权。
#
# 真实案例（德衡文章第七类）：
#   某建材供应商向地产商供货，未约定所有权保留。
#   后地产商破产，货物被列入破产财产。
#   若约定了所有权保留，供应商可就未付款货物行使取回权；
#   未约定的，只能按普通债权清偿（清偿率通常低于 30%）。
#
# 为什么这是真实陷阱：
#   "货已发出、款未收齐"是赊销常态。没有所有权保留，
#   一旦买方破产，卖方从"能取回货"降级为"排队分钱"，
#   两者回收率差距可能是 100% vs 30%。


def _check_no_ownership_retention(outcome: ParseOutcome) -> TrapFinding | None:
    """检测赊销场景下缺少所有权保留条款。

    参数：
        outcome: 解析产物。
    返回：
        命中时返回发现，否则 ``None``。
    关联：
        读取 ``payment`` 条款与全文。

    判定逻辑：
        1. 付款条款存在分期/账期（如"验收后 30 日内""分期支付""尾款"）；
        2. 全文找不到所有权保留表述；
        3. 两条同时成立 → 报中风险。
    """
    payment = _clause(outcome, "payment")
    if not payment:
        return None

    # 判断是否存在赊销特征（先交货后付款）
    credit_markers = ("验收后", "验收合格后", "货到后", "交付后", "月结", "账期", "尾款", "分期")
    has_credit = any(m in payment for m in credit_markers)
    if not has_credit:
        return None

    full = _full(outcome)
    retention_markers = ("所有权保留", "保留所有权", "所有权归出卖人", "付清全部价款前", "所有权不发生转移")
    if any(m in full for m in retention_markers):
        return None

    return TrapFinding(
        code="T006",
        name="赊销付款但未约定所有权保留",
        risk=RiskLevel.MEDIUM,
        evidence=(
            "付款条款存在先交货后付款安排（含账期/验收后付款/尾款），"
            "但全文未找到所有权保留约定。若买方在付清前破产或被查封，"
            "货物将被列入其财产，卖方只能按普通债权受偿。"
        ),
        position=_pos(outcome, "payment"),
        legal_basis=(
            "《民法典》第六百四十一条：当事人可以在买卖合同中约定"
            "买受人未履行支付价款或者其他义务的，标的物的所有权属于出卖人。"
            "第六百四十二条：当事人约定出卖人保留合同标的物的所有权，"
            "在标的物所有权转移前，买受人有下列情形之一，造成出卖人损害的，"
            "出卖人有权取回标的物。"
        ),
        suggestion=(
            "增加所有权保留条款：「买受人付清全部价款前，标的物所有权归出卖人所有」。"
            "大额交易建议按《民法典担保制度解释》第 57 条办理登记，以对抗善意第三人。"
        ),
    )


# ===========================================================================
# T007 知识产权归属未约定（委托开发/定制场景）
# ===========================================================================
# 依据：《民法典》第 859 条（原技术合同法规则延续）
#   "委托开发完成的发明创造，除当事人另有约定外，申请专利的权利属于研究开发人。"
# 依据：《专利法》第 6 条
#
# 真实案例（德衡文章第九类）：
#   某家电企业委托供应商开发定制模具，合同未约定技术成果归属。
#   供应商擅自将模具 CAD 图纸转让给竞争对手。
#   法院认定：委托开发未约定权属时，专利权归受托人（供应商）。
#   委托方丧失市场竞争力，损失市场份额 35%。
#
# 为什么这是真实陷阱：
#   "我出钱你开发"的直觉是"成果归我"，
#   但法律默认规则是"归开发方"。不写清楚就等于白出钱。


def _check_ip_ownership_default_risk(outcome: ParseOutcome) -> TrapFinding | None:
    """检测委托开发场景下知识产权归属约定不明。

    参数：
        outcome: 解析产物。
    返回：
        命中时返回发现，否则 ``None``。
    关联：
        读取 ``intellectual_property`` 条款与全文。

    判定逻辑：
        1. 全文出现委托开发/定制/开发成果等特征词；
        2. 知识产权条款未检出，或检出但未明确"归甲方/归委托方"；
        3. 报高风险。
    """
    full = _full(outcome)

    # 判断是否存在委托开发场景
    dev_markers = ("委托开发", "定制开发", "委托乙方开发", "开发成果", "定制", "二次开发", "研发")
    if not any(m in full for m in dev_markers):
        return None

    ip_clause = _clause(outcome, "intellectual_property")

    # 已明确归属给委托方（我方）→ 不算风险
    our_ownership = (
        "知识产权归甲方", "成果归甲方", "著作权归甲方", "专利权归甲方",
        "归委托方所有", "归甲方所有", "知识产权属于甲方",
    )
    if ip_clause and any(k in ip_clause for k in our_ownership):
        return None

    if not ip_clause:
        evidence = (
            "全文存在委托开发/定制开发表述，但未检出知识产权条款。"
            "依默认规则，委托开发成果的专利申请权归研究开发人（即对方）。"
        )
        position = "全文检索"
    else:
        evidence = (
            f"存在委托开发/定制开发表述，知识产权条款未明确约定成果归我方所有。"
            f"原文：{ip_clause[:140]}"
        )
        position = _pos(outcome, "intellectual_property")

    return TrapFinding(
        code="T007",
        name="委托开发场景下知识产权归属约定不明",
        risk=RiskLevel.HIGH,
        evidence=evidence,
        position=position,
        legal_basis=(
            "《民法典》第八百五十九条：委托开发完成的发明创造，"
            "除当事人另有约定外，申请专利的权利属于研究开发人。"
            "《专利法》第六条：职务发明创造申请专利的权利属于该单位。"
        ),
        suggestion=(
            "明确约定「乙方因履行本合同所产生的专利申请权、软件著作权、"
            "技术秘密使用权归甲方所有」，并可约定一次性补偿金额。"
            "同时对核心技术人员约定竞业限制，由对方承担连带责任。"
        ),
    )


# ===========================================================================
# T008 不可抗力条款泛化，未列举具体事件且未约定通知义务
# ===========================================================================
# 依据：《民法典》第 180 条
#   "因不可抗力不能履行民事义务的，不承担民事责任。
#    当事人迟延履行后发生不可抗力的，不免除其违约责任。"
# 依据：第 590 条
#   "当事人一方因不可抗力不能履行合同的，应当及时通知对方，
#    以减轻可能给对方造成的损失，并应当在合理期限内提供证明。"
#
# 真实案例（德衡文章第六类）：
#   某跨境电商合同约定"疫情属不可抗力"，但未包含政府管控措施。
#   2022 年因物流园区封闭导致延迟交货，买方索赔仓储费 50 万元。
#   法院依据最高法指导意见，认定"疫情防控措施"属于不可抗力扩大解释范围，
#   判决供应商免除 30% 责任（赔偿 35 万元）——注意，**不是全部免除**。
#
# 为什么这是真实陷阱：
#   条款写"不可抗力按法律规定执行"，看似稳妥，实际把认定权交给法院。
#   而且法律要求"及时通知 + 提供证明"，不约定具体时限，
#   一旦对方主张不可抗力，我方难以证明其未及时通知，免责抗辩容易被支持。


def _check_force_majeure_vague(outcome: ParseOutcome) -> TrapFinding | None:
    """检测不可抗力条款过于笼统。

    参数：
        outcome: 解析产物。
    返回：
        命中时返回发现，否则 ``None``。
    关联：
        读取全文，因为不可抗力条款可能出现在任何位置。

    判定逻辑（三条同时满足才报）：
        1. 全文出现"不可抗力"；
        2. 没有具体事件列举（地震、战争、疫情、政府命令等至少出现一个）；
        3. 没有约定通知时限（找不到"X 日内通知"这类表述）。
    """
    full = _full(outcome)
    if "不可抗力" not in full:
        return None

    # 第 2 步：是否有具体事件列举
    event_markers = (
        "地震", "台风", "洪水", "战争", "罢工", "疫情", "瘟疫",
        "政府命令", "行政命令", "司法强制", "征收", "征用", "封锁",
    )
    has_events = any(m in full for m in event_markers)

    # 第 3 步：是否约定通知时限。
    #
    # 【重要】不能要求"通知"紧跟在"不可抗力"同一句内。
    # 实测踩过：原文
    #   "不可抗力包括但不限于地震、战争……。事件发生后 5 个工作日内书面通知对方。"
    # 通知条款在**下一句**，若正则写成
    #   ``不可抗力[^。；]{0,60}?\d+日``
    # 会被句号截断，导致明明约定了通知时限却被判为"未约定"，
    # 产生误报。
    #
    # 正确做法：在全文里找"X 日内 + 通知"这个组合，
    # 不限定它与"不可抗力"的相对位置。
    has_notice = bool(
        re.search(
            r"\d+\s*(?:个)?\s*(?:工作日|日|天)[^。；\n]{0,30}?"
            r"(?:通知|告知|书面通知|告知书)",
            full,
        )
    )

    if has_events and has_notice:
        return None

    missing: list[str] = []
    if not has_events:
        missing.append("未列举具体不可抗力事件（如地震、战争、疫情、政府命令）")
    if not has_notice:
        missing.append("未约定事件发生后的通知时限与证明提供义务")

    return TrapFinding(
        code="T008",
        name="不可抗力条款过于笼统",
        risk=RiskLevel.MEDIUM,
        evidence=(
            f"合同约定不可抗力但存在缺陷：{'；'.join(missing)}。"
            f"认定范围与通知义务均不明确，一旦发生争议，"
            f"免责主张能否成立取决于法院裁量。"
        ),
        position="全文检索",
        legal_basis=(
            "《民法典》第一百八十条：因不可抗力不能履行民事义务的，不承担民事责任。"
            "《民法典》第五百九十条：当事人一方因不可抗力不能履行合同的，"
            "应当及时通知对方，以减轻可能给对方造成的损失，"
            "并应当在合理期限内提供证明。"
        ),
        suggestion=(
            "采用「列举 + 兜底」写法：「包括但不限于地震、战争、疫情、"
            "政府行政命令及司法强制措施，具体以事件发生地省级以上政府发布的认定为准」。"
            "并约定通知时限：「事件发生后 5 个工作日内以书面方式送达《不可抗力告知书》，"
            "附政府公告或公证证明」。"
        ),
    )


# ===========================================================================
# T009 合同解除条件缺失
# ===========================================================================
# 依据：《民法典》第 563 条（法定解除权）
#   "有下列情形之一的，当事人可以解除合同：
#    （一）因不可抗力致使不能实现合同目的；
#    （四）当事人一方迟延履行债务或者有其他违约行为致使不能实现合同目的……"
#
# 真实案例（德衡文章第八类）：
#   某新能源企业与锂矿供应商签订长协价合同，履行期间锂价暴涨 300%，
#   继续履行将导致亏损超亿元。
#   法院依据《民法典》第 533 条"情势变更"原则判决解除合同，
#   并由甲方按涨价前价格补偿乙方 20% 预期损失。
#
# 为什么这是真实陷阱：
#   法定解除权存在，但"严重违约""合同目的落空"需要**举证**。
#   事前约定明确的解除触发条件，可以大幅降低举证难度。
#   没有约定解除条件，一方可能陷入"僵尸合同"——
#   市场环境剧变后仍必须履约。


def _check_no_termination_conditions(outcome: ParseOutcome) -> TrapFinding | None:
    """检测合同缺少明确的解除触发条件。

    参数：
        outcome: 解析产物。
    返回：
        命中时返回发现，否则 ``None``。
    关联：
        读取全文。

    【重要】本规则经过一次大幅收窄，原因是实测发现误报率 100%：
        初版逻辑是"全文找不到'解除合同'就报"，结果在 12 个演示案例中
        **12 个全部命中**——因为这些案例都只构造了 1-2 个条款，
        自然不会有解除条款。这种"见谁都报"的规则只会制造噪音，
        让审批人关掉整个系统。

    收窄后的条件（三个都要满足才报）：
        1. 找不到解除/终止相关表述；
        2. 合同**有实质规模**（至少 3 个条款被检出）——
           条款很少的合同可能是残篇，报"缺解除条款"没有意义；
        3. 存在**长期履约特征**（有效期 1 年以上、或含"长期""年度""框架"），
           因为短期一次性交易不约定解除条件的风险确实较低。

    这样收窄后，只有在"一份完整的、长期的合同却完全没有解除机制"时才提示，
    这才是真正值得关注的场景。
    """
    full = _full(outcome)
    if not full.strip():
        return None

    termination_markers = ("解除合同", "合同解除", "有权解除", "终止合同", "合同终止")
    has_termination = any(m in full for m in termination_markers)

    if has_termination:
        # 有解除条款，检查是否有量化触发条件。
        #
        # 【重要】量化条件可能出现在"解除"的**前面**，不能只往后找。
        # 实测踩过：原文"乙方连续 3 批交货不合格的，甲方有权解除合同"，
        # 数字"3 批"在"解除"之前，只往后匹配的正则会漏判，
        # 导致本该放行的合同被误报为"缺少量化标准"。
        # 因此用两个方向的正则分别判断，任一成立即视为已有量化条件。
        has_quantitative_trigger = bool(
            # 方向一：解除在前，数字在后（如"解除合同……连续 3 批"）
            re.search(r"解除[^。；\n]{0,50}?\d+\s*(?:次|批|日|天|个月|%|倍)", full)
            # 方向二：数字在前，解除在后（如"连续 3 批……有权解除"）
            or re.search(r"\d+\s*(?:次|批|日|天|个月|%|倍)[^。；\n]{0,50}?解除", full)
        )
        if has_quantitative_trigger:
            return None

        return TrapFinding(
            code="T009",
            name="解除条件缺少量化标准",
            risk=RiskLevel.LOW,
            evidence=(
                "合同有解除相关约定，但未给出可量化的触发条件"
                "（如「连续 3 批不合格」「价格波动超 ±50%」）。"
                "「严重违约」「重大损失」这类表述在诉讼中仍需举证。"
            ),
            position="全文检索",
            legal_basis=(
                "《民法典》第五百六十三条规定的法定解除权，"
                "以「致使不能实现合同目的」为要件，需结合个案举证。"
            ),
            suggestion=(
                "将解除条件量化为可核验的事实，例如「连续 3 批抽检不合格」"
                "「逾期交付超过 30 日」「价格波动超过合同价 ±50%」。"
            ),
        )

    # 收窄条件一：合同需有实质规模（至少 3 个条款被检出）。
    # 条款极少的样本多半是残篇或测试构造，报"缺解除条款"没有实际意义。
    detected_clauses = sum(
        1 for field in outcome.clauses.values() if field.value
    )
    if detected_clauses < 3:
        return None

    # 收窄条件二：需有长期履约特征。
    # 一次性短期交易不约定解除条件的风险确实较低，
    # 只有长期合同（框架协议、年度合同、多年期服务）才值得提示。
    long_term_markers = (
        "长期", "年度", "框架协议", "框架合同", "多年", "续期", "续约",
    )
    has_long_term = any(m in full for m in long_term_markers)
    # 或者有效期跨 1 年以上
    if not has_long_term:
        has_long_term = bool(
            re.search(r"有效期[^。；\n]{0,30}?(?:[2-9]|\d{2,})\s*年", full)
            or re.search(r"合同期限[^。；\n]{0,30}?(?:[2-9]|\d{2,})\s*年", full)
        )
    if not has_long_term:
        return None

    return TrapFinding(
        code="T009",
        name="长期合同未约定解除条件",
        risk=RiskLevel.MEDIUM,
        evidence=(
            f"本合同已检出 {detected_clauses} 个条款且属长期履约，"
            "但全文未找到合同解除/终止的相关约定。"
            "发生根本违约或市场剧变时只能依赖法定解除权，"
            "而「严重违约」「合同目的落空」需要举证，诉讼成本高、周期长。"
        ),
        position="全文检索",
        legal_basis=(
            "《民法典》第五百六十三条：有下列情形之一的，当事人可以解除合同："
            "（一）因不可抗力致使不能实现合同目的；"
            "（四）当事人一方迟延履行债务或者有其他违约行为致使不能实现合同目的。"
            "《民法典》第五百三十三条：合同成立后，合同的基础条件发生了"
            "当事人在订立合同时无法预见的、不属于商业风险的重大变化，"
            "继续履行合同对于当事人一方明显不公平的，受不利影响的当事人"
            "可以与对方重新协商；在合理期限内协商不成的，"
            "当事人可以请求人民法院或者仲裁机构变更或者解除合同。"
        ),
        suggestion=(
            "明确约定「触发条件 + 解除程序」，例如「乙方连续 3 批交货不合格，"
            "或市场价格波动超过合同价 ±50% 时，甲方有权书面通知解除合同」。"
            "并补充清算条款：解除后已付款项退还、未使用货物退回、按实际履行量结算。"
        ),
    )


# ===========================================================================
# T010 违约金与定金并存，未明确择一适用
# ===========================================================================
# 依据：《民法典》第 588 条
#   "当事人既约定违约金，又约定定金的，一方违约时，
#    对方可以选择适用违约金或者定金条款。"
#
# 依据（德衡文章第五类）：
#   "定金超限、违约金与定金竞合（《民法典》第 588 条规定择一适用）"
#
# 为什么这是真实陷阱：
#   同时约定违约金和定金，看起来是"双重保障"，
#   但法律上**只能择一适用**，不是叠加。
#   若合同没写清楚，当事人容易误以为可以同时主张，
#   在谈判或诉讼中做出错误预期。


def _check_penalty_and_deposit_coexist(outcome: ParseOutcome) -> TrapFinding | None:
    """检测违约金与定金并存但未明确择一适用。

    参数：
        outcome: 解析产物。
    返回：
        命中时返回发现，否则 ``None``。
    关联：
        读取 ``payment`` 与 ``breach`` 条款。
    """
    payment = _clause(outcome, "payment")
    breach = _clause(outcome, "breach")

    if "定金" not in payment:
        return None
    if "违约金" not in breach:
        return None

    # 已明确择一适用则不报
    full = _full(outcome)
    if any(k in full for k in ("择一适用", "二者选其一", "只能选择其中", "不重复适用")):
        return None

    return TrapFinding(
        code="T010",
        name="违约金与定金并存，未明确择一适用",
        risk=RiskLevel.LOW,
        evidence=(
            "付款条款约定定金，违约条款约定违约金，但未说明两者关系。"
            "按法律规定只能择一适用，不能叠加主张。"
            "若双方对此理解不一致，谈判或诉讼中容易产生预期偏差。"
        ),
        position=f"付款：{_pos(outcome, 'payment')}；违约：{_pos(outcome, 'breach')}",
        legal_basis=(
            "《民法典》第五百八十八条：当事人既约定违约金，又约定定金的，"
            "一方违约时，对方可以选择适用违约金或者定金条款。"
        ),
        suggestion=(
            "明确约定「定金与违约金择一适用，守约方有权选择对其更有利的一项」，"
            "避免双方对能否叠加产生争议。"
        ),
    )


# ===========================================================================
# T011 付款方式约定"以实际结算为准"但无结算依据
# ===========================================================================
# 依据：《民法典》第 511 条第（二）项
#   "价款或者报酬不明确的，按照订立合同时履行地的市场价格履行。"
#
# 依据：浙江法院网《避雷合同中的这5类"模糊条款"》
#   "总价或单价不明确，如合同仅约定'总价款以实际结算为准'，
#    但未明确结算依据。"
#
# 为什么这是真实陷阱：
#   "以实际结算为准"听起来灵活，实际是把定价权悬空。
#   发生争议时法院按"订立合同时履行地的市场价格"认定，
#   这个价格未必是我方谈判时预期的价格。


def _check_settlement_without_basis(outcome: ParseOutcome) -> TrapFinding | None:
    """检测「以实际结算为准」但缺少结算依据。

    参数：
        outcome: 解析产物。
    返回：
        命中时返回发现，否则 ``None``。
    关联：
        读取 ``payment`` 条款。
    """
    payment = _clause(outcome, "payment")
    if not payment:
        return None

    vague_settlement = (
        "以实际结算为准", "按实际结算", "据实结算", "按实结算",
        "以实际发生为准", "另行结算",
    )
    hit = next((v for v in vague_settlement if v in payment), None)
    if hit is None:
        return None

    # 有明确结算依据则不报
    basis_markers = (
        "单价", "按单价", "参照", "依据附件", "价格表", "报价单",
        "结算单", "计量", "按重量", "按面积", "按工时", "定额",
    )
    if any(b in payment for b in basis_markers):
        return None

    return TrapFinding(
        code="T011",
        name="约定「以实际结算为准」但未明确结算依据",
        risk=RiskLevel.MEDIUM,
        evidence=(
            f"付款条款使用「{hit}」表述，但未约定单价、计量方式或价格依据。"
            f"发生争议时法院将按订立合同时履行地的市场价格认定，"
            f"该价格未必符合我方谈判预期。"
        ),
        position=_pos(outcome, "payment"),
        legal_basis=(
            "《民法典》第五百一十一条第（二）项：价款或者报酬不明确的，"
            "按照订立合同时履行地的市场价格履行；"
            "依法应当执行政府定价或者政府指导价的，依照规定履行。"
        ),
        suggestion=(
            "补充结算依据，例如「按附件《价格表》单价 × 实际验收合格数量结算」，"
            "并约定计量方法与验收确认流程。"
        ),
    )


# ===========================================================================
# T012 涉外合同未约定适用法律
# ===========================================================================
# 依据：《涉外民事关系法律适用法》第 41 条
#   "当事人可以协议选择合同适用的法律。当事人没有选择的，
#    适用履行义务最能体现该合同特征的一方当事人经常居所地法律
#    或者其他与该合同有最密切联系的法律。"
#
# 依据（德衡文章第三类）：
#   "跨境合同未同步约定适用法律，导致实体法适用冲突
#    （如大陆法系与英美法系的违约认定差异）。"
#   真实案例：中国设备制造商与德国采购商未约定管辖地，
#   后需在德国起诉，耗时 3 年，诉讼成本占争议金额 40%。
#
# 为什么这是真实陷阱：
#   涉外合同不约定适用法律，等于把"用哪国法律判断对错"交给法院。
#   不同法系对违约的认定、赔偿范围、举证责任差异极大。


def _check_foreign_no_governing_law(outcome: ParseOutcome) -> TrapFinding | None:
    """检测涉外合同未约定适用法律。

    参数：
        outcome: 解析产物。
    返回：
        命中时返回发现，否则 ``None``。
    关联：
        读取全文，判断是否为涉外合同。

    判定逻辑：
        1. 全文出现涉外特征（外币、境外地名、进出口、跨境等）；
        2. 找不到"适用中华人民共和国法律"或类似适用法律条款；
        3. 报中风险。
    """
    full = _full(outcome)
    if not full.strip():
        return None

    # 涉外特征
    foreign_markers = (
        "美元", "欧元", "日元", "港币", "USD", "EUR", "JPY", "HKD",
        "境外", "跨境", "进出口", "出口", "进口", "保税",
        "香港", "澳门", "台湾", "新加坡", "德国", "美国", "日本",
        "外国", "海外",
    )
    if not any(m in full for m in foreign_markers):
        return None

    # 已约定适用法律则不报
    governing_law = (
        "适用中华人民共和国法律", "适用中国法律", "适用中华人民共和国",
        "本合同的解释与适用", "适用法律为", "受中国法律管辖",
        "适用《联合国国际货物销售合同公约》", "CISG",
    )
    if any(g in full for g in governing_law):
        return None

    return TrapFinding(
        code="T012",
        name="涉外合同未约定适用法律",
        risk=RiskLevel.MEDIUM,
        evidence=(
            "合同存在涉外因素（外币结算/跨境交付/境外主体），"
            "但未约定适用的法律。发生争议时，由法院按最密切联系原则确定准据法，"
            "不同法系对违约认定与赔偿范围差异极大，结果不可预期。"
        ),
        position="全文检索",
        legal_basis=(
            "《涉外民事关系法律适用法》第四十一条：当事人可以协议选择合同适用的法律。"
            "当事人没有选择的，适用履行义务最能体现该合同特征的一方当事人"
            "经常居所地法律或者其他与该合同有最密切联系的法律。"
        ),
        suggestion=(
            "明确约定「本合同适用中华人民共和国法律」，"
            "并同步约定争议解决方式（境内仲裁或法院管辖），"
            "避免境外应诉。"
        ),
    )


# ===========================================================================
# 编排
# ===========================================================================

_TRAP_CHECKS = (
    _check_arbitration_and_litigation_conflict,
    _check_deposit_over_limit,
    _check_quality_vague,
    _check_vague_deadline,
    _check_daily_penalty_no_cap,
    _check_no_ownership_retention,
    _check_ip_ownership_default_risk,
    _check_force_majeure_vague,
    _check_no_termination_conditions,
    _check_penalty_and_deposit_coexist,
    _check_settlement_without_basis,
    _check_foreign_no_governing_law,
)


def _to_hit(finding: TrapFinding) -> RuleHitView:
    """把陷阱发现转成统一命中视图。

    参数：
        finding: 陷阱发现。
    返回：
        :class:`RuleHitView`。

    说明：
        证据文本里把 ``legal_basis`` 一并拼进去，
        因为这是本模块的核心价值——**每条命中都有法条依据**，
        不是"系统觉得有问题"，而是"这样做会导致什么法律后果"。
    """
    return RuleHitView(
        rule_id=None,
        rule_code=finding.code,
        rule_name=finding.name,
        risk_level=finding.risk,
        evidence_text=f"{finding.evidence}\n【法律依据】{finding.legal_basis}",
        evidence_position=finding.position,
        suggestion_text=finding.suggestion,
    )


def run_trap_checks(outcome: ParseOutcome) -> list[RuleHitView]:
    """执行全部合同陷阱检查。

    参数：
        outcome: 解析产物。
    返回：
        命中列表，按风险降序。
    关联：
        被 ``services/review_service.run_review`` 调用，
        与浅层规则、深层推理的命中合并。
    """
    hits: list[RuleHitView] = []
    for check in _TRAP_CHECKS:
        try:
            finding = check(outcome)
        except Exception:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            continue
        if finding is not None:
            hits.append(_to_hit(finding))

    weight = {"high": 0, "medium": 1, "low": 2}
    hits.sort(key=lambda h: weight.get(h.risk_level.value, 9))
    return hits
