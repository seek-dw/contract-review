"""合同陷阱演示：用真实司法案例说明系统能发现什么。

运行：
    uv run --python 3.12 python scripts/demo_trap_value.py

============================================================================
资料来源
============================================================================
本演示中的陷阱与法律后果，全部来自公开的法律实务资料：

1. 山东德衡律师事务所 郭培明《供应链合同十大法律陷阱》(2025-05-21)
   https://www.deheng.com/dhgc/flgc/25764.html

2. 浙江法院网《避雷合同中的这5类"模糊条款"》(开化县人民法院, 2025-06-17)
   https://zjsfgkw.gov.cn/art/2025/6/17/art_56_37364.html

每个案例都包含：
- 真实案例中的后果（法院怎么判的）
- 法条依据（为什么这么判）
- 本系统的检测结果（能不能发现）
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.domain.enums import ExtractStatus, ParseStatus
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


def build(clauses: dict[str, str], basic: dict[str, str] | None = None) -> ParseOutcome:
    """构造解析产物（演示用简化版）。

    参数：
        clauses: 条款键 → 原文。
        basic: 基础字段键 → 值。
    返回：
        :class:`ParseOutcome`。
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
                FieldEvidence(text[:80], "第1页第1行", ExtractStatus.SUCCESS),
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


CASES = [
    {
        "no": "案例一",
        "title": "争议解决条款「仲裁或诉讼」并列",
        "real_case": (
            "实务中常见写法：「可向仲裁委申请仲裁，或向法院起诉」。\n"
            "     当事人以为两条路都能走，更灵活。\n"
            "     实际后果：按《仲裁法解释》第七条，该仲裁协议**无效**。\n"
            "     想用仲裁的保密性和效率，目的完全落空，只能走诉讼。"
        ),
        "source": "浙江法院网《避雷合同中的这5类「模糊条款」》",
        "outcome": build({
            "dispute": "因本合同产生的争议，可向北京仲裁委员会申请仲裁，"
                       "或向甲方所在地人民法院起诉。",
        }),
    },
    {
        "no": "案例二",
        "title": "定金 30%（超过法定 20% 上限）",
        "real_case": (
            "真实案例：某设备采购合同约定定金 30%（300 万），卖方违约。\n"
            "     买方主张双倍返还 600 万，法院只支持 400 万。\n"
            "     依据《民法典》586 条：超过 20% 的部分（100 万）不产生定金效力，\n"
            "     自动转为普通预付款——**没有双倍罚则的保护**。"
        ),
        "source": "山东德衡《供应链合同十大法律陷阱》第五类",
        "outcome": build(
            clauses={"payment": "合同签订后支付定金 30%，卖方违约双倍返还。"},
            basic={"amount": "人民币 10,000,000 元"},
        ),
    },
    {
        "no": "案例三",
        "title": "质量条款「符合国家标准」——把标准降到最低线",
        "real_case": (
            "真实案例：某烘焙企业与面粉商约定「面粉符合食品安全标准」，\n"
            "     未明确重金属限值。后因砷含量 0.4mg/kg 超标\n"
            "     （国标限值 ≤0.5，买方内控 ≤0.3）导致产品召回。\n"
            "     法院判：合同未约定特殊标准时优先适用国标，**供应商未违约**。\n"
            "     买方承担全部召回损失。"
        ),
        "source": "山东德衡《供应链合同十大法律陷阱》第一类",
        "outcome": build({
            "acceptance": "产品质量应符合国家标准，验收合格后付款。",
            "delivery": "乙方按约定交付货物。",
        }),
    },
    {
        "no": "案例四",
        "title": "履约期限写「尽快发货」",
        "real_case": (
            "法院观点：「尽快」「及时」「立即」这类表述，\n"
            "     按《民法典》511 条第（四）项，等于「债务人可以随时履行」。\n"
            "     实际后果：**无法主张逾期违约**——根本没有可计算的逾期起点。\n"
            "     你想追究对方迟延，法院会问「合同约定几号交货？」"
        ),
        "source": "浙江法院网《避雷合同中的这5类「模糊条款」》",
        "outcome": build({
            "delivery": "乙方应在收到订单后尽快发货。",
        }),
    },
    {
        "no": "案例五",
        "title": "按日计违约金但无累计上限",
        "real_case": (
            "真实案例：某汽车座椅供应商约定逾期交货按日 0.5% 违约金。\n"
            "     甲方索赔 2000 万（含预期利润 1200 万），乙方仅能向下游追偿 300 万。\n"
            "     法院援引《九民纪要》第 50 条认定违约金显失公平。\n"
            "     注意：日 0.5% × 200 天 = 100%，违约金就等于合同全额。\n"
            "     虽然可请求调减，但**诉讼成本已经发生**。"
        ),
        "source": "山东德衡《供应链合同十大法律陷阱》第二类",
        "outcome": build({
            "breach": "乙方逾期交付的，每逾期一日按合同总金额的 0.5% 支付违约金。",
        }),
    },
    {
        "no": "案例六",
        "title": "赊销但未约定所有权保留",
        "real_case": (
            "真实案例：某建材供应商向地产商供货，未约定所有权保留。\n"
            "     后地产商破产，货物被列入破产财产。\n"
            "     若约定所有权保留，供应商可取回未付款货物；\n"
            "     未约定的，只能按普通债权清偿——**清偿率通常低于 30%**。\n"
            "     两者回收率差距：100% vs 30%。"
        ),
        "source": "山东德衡《供应链合同十大法律陷阱》第七类",
        "outcome": build({
            "payment": "货物验收合格后 30 日内支付全部价款。",
        }),
    },
    {
        "no": "案例七",
        "title": "委托开发但未约定知识产权归属",
        "real_case": (
            "真实案例：某家电企业委托供应商开发定制模具，合同未约定成果归属。\n"
            "     供应商擅自将模具 CAD 图纸转让给竞争对手。\n"
            "     法院依据《民法典》859 条认定：委托开发未约定权属时，\n"
            "     **专利申请权归受托人（供应商）**。\n"
            "     委托方损失市场份额 35%，且无法阻止对方使用。"
        ),
        "source": "山东德衡《供应链合同十大法律陷阱》第九类",
        "outcome": build({
            "delivery": "乙方受甲方委托开发定制模具，并按约定交付。",
        }),
    },
    {
        "no": "案例八",
        "title": "不可抗力条款笼统——把认定权交给法院",
        "real_case": (
            "真实案例：某跨境电商合同约定「疫情属不可抗力」，\n"
            "     但未包含政府管控措施。2022 年因物流园区封闭导致延迟交货，\n"
            "     买方索赔仓储费 50 万元。\n"
            "     法院依据最高法指导意见，认定「疫情防控措施」属不可抗力扩大解释范围，\n"
            "     判决供应商免除 30% 责任（赔偿 35 万元）——**注意不是全部免除**。\n"
            "     且《民法典》590 条要求「及时通知并提供证明」，\n"
            "     不约定时限，对方未通知也难以追责。"
        ),
        "source": "山东德衡《供应链合同十大法律陷阱》第六类",
        "outcome": build({
            "breach": "因不可抗力导致不能履行的，按法律规定处理，不承担违约责任。",
        }),
    },
    {
        "no": "案例九",
        "title": "长期合同未约定解除条件——可能陷入「僵尸合同」",
        "real_case": (
            "真实案例：某新能源企业与锂矿供应商签长协价合同，\n"
            "     履行期间锂价暴涨 300%，继续履行将亏损超亿元。\n"
            "     法院依《民法典》533 条「情势变更」判决解除合同，\n"
            "     并由甲方按涨价前价格补偿乙方 20% 预期损失。\n"
            "     关键：法定解除权存在，但「严重违约」「合同目的落空」**需要举证**，\n"
            "     事前约定明确的解除触发条件可大幅降低举证难度。\n"
            "\n"
            "     【规则收窄说明】初版只看「无解除条款」就报，结果 12 个案例\n"
            "     全部命中（误报率 100%）。现收窄为三个条件同时成立：\n"
            "     ① 无解除条款 ② 条款数 ≥3 ③ 有长期履约特征。\n"
            "     本案例据此补全为「完整的年度框架协议」。"
        ),
        "source": "山东德衡《供应链合同十大法律陷阱》第八类",
        "outcome": build(
            clauses={
                "payment": "本合同为年度框架协议，乙方按季度供货，甲方验收合格后 30 日内付款。",
                "delivery": "乙方应在每季度末前完成当季交付，具体数量以订单为准。",
                "acceptance": "甲方应在收到货物后 7 日内完成验收，逾期未提出异议视为验收合格。",
                "breach": "任何一方违约，应赔偿对方因此遭受的直接损失。",
            },
            basic={
                "amount": "人民币 8,000,000 元",
                "effective_date": "2025-01-01",
                "expire_date": "2027-12-31",
            },
        ),
    },
    {
        "no": "案例十",
        "title": "违约金与定金并存——以为能双罚，实际只能择一",
        "real_case": (
            "实务常见写法：既约定「支付定金 15%」，又约定「违约方支付违约金 10%」。\n"
            "     当事人以为双重保障，违约时可同时主张。\n"
            "     实际后果：按《民法典》588 条，**只能择一适用**，不能叠加。\n"
            "     若谈判时按「定金双倍 + 违约金」估算损失，预期会严重偏高。"
        ),
        "source": "山东德衡《供应链合同十大法律陷阱》第五类",
        "outcome": build({
            "payment": "合同签订后支付定金 15%。",
            "breach": "任何一方违约，应支付合同总金额 10% 的违约金。",
        }),
    },
    {
        "no": "案例十一",
        "title": "「以实际结算为准」但无结算依据——定价权悬空",
        "real_case": (
            "法院观点：仅约定「总价款以实际结算为准」但未明确结算依据，\n"
            "     属于价格条款约定不明。\n"
            "     按《民法典》511 条第（二）项，法院将按**订立合同时履行地的市场价格**认定。\n"
            "     这个价格未必是谈判时预期的价格——涨了按涨的算，跌了按跌的算，\n"
            "     等于把定价权交给了市场。"
        ),
        "source": "浙江法院网《避雷合同中的这5类「模糊条款」》",
        "outcome": build({
            "payment": "总价款以实际结算为准，验收合格后付款。",
        }),
    },
    {
        "no": "案例十二",
        "title": "涉外合同未约定适用法律——可能被迫境外应诉",
        "real_case": (
            "真实案例：某中国设备制造商与德国采购商签约时未约定管辖地，\n"
            "     后因设备精度争议需在德国起诉。\n"
            "     因两地证据规则差异，案件耗时 3 年，**诉讼成本占争议金额 40%**。\n"
            "     依《涉外民事关系法律适用法》41 条，未选择时由法院按最密切联系原则确定准据法，\n"
            "     不同法系对违约认定与赔偿范围差异极大，结果不可预期。"
        ),
        "source": "山东德衡《供应链合同十大法律陷阱》第三类",
        "outcome": build(
            clauses={
                "payment": "甲方以美元支付合同价款，验收后 30 日内付清。",
                "delivery": "乙方将货物运至德国汉堡港。",
            },
            basic={"currency": "USD"},
        ),
    },
]


def main() -> None:
    """逐个案例执行陷阱检查并打印结果。

    返回：
        无。
    """
    print("=" * 78)
    print("合同陷阱检测演示：基于真实司法案例")
    print("=" * 78)
    print()
    print("说明：每条规则都有法条依据，不是主观判断。")
    print()

    detected = 0
    for case in CASES:
        print("-" * 78)
        print(f"{case['no']}：{case['title']}")
        print("-" * 78)
        print("  真实后果：")
        print(f"     {case['real_case']}")
        print(f"  资料来源：{case['source']}")
        print()

        hits = run_trap_checks(case["outcome"])
        if not hits:
            print("  ✗ 未检出（该案例应命中，请检查实现）")
            print()
            continue

        for hit in hits:
            detected += 1
            print(f"  ✓ 检出 [{hit.risk_level.value.upper()}] {hit.rule_name}")
            print(f"    编码：{hit.rule_code}")
            # 证据里含法条依据，分行打印更易读
            for line in hit.evidence_text.splitlines():
                print(f"    {line}")
            print(f"    建议：{hit.suggestion_text}")
            print()

    print("=" * 78)
    print(f"共检出 {detected} 条真实陷阱。")
    print("这些不是「看一眼就知道」的问题，而是「按法律会有什么后果」。")
    print("=" * 78)


if __name__ == "__main__":
    main()
