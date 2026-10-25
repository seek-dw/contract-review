"""对比演示：浅层规则 vs 深层规则，看清"人能发现的"和"人会漏掉的"。

运行：
    uv run --python 3.12 python scripts/demo_deep_value.py

这个脚本用三份**故意设计的合同**说明问题：
每份合同单独看每条条款都正常，只有交叉比对才暴露风险。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
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
from app.review.deep_rules import run_deep_checks


def build(clauses: dict[str, str], basic: dict[str, str] | None = None) -> ParseOutcome:
    """构造解析产物（简化版，只用于演示）。

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
        "title": "案例一：付款挂钩验收 + 验收由对方单方决定",
        "why": (
            "付款条款「验收合格后 30 日内支付」——标准写法。\n"
            "     验收条款「验收标准由甲方确定」——也常见。\n"
            "     但两条合起来：甲方可以永远不出验收结论，付款义务永不触发。\n"
            "     ★ 单看任何一条都没问题，必须对照两条才能发现。"
        ),
        "outcome": build(
            clauses={
                "payment": "第三条 付款方式\n验收合格后 30 日内支付合同总金额的 70%。",
                "acceptance": "第四条 验收\n验收标准由甲方确定，甲方验收合格后出具确认书。",
            }
        ),
    },
    {
        "title": "案例二：违约金封顶 5% × 合同 1200 万",
        "why": (
            "「违约金累计不超过合同总金额的 5%」——看起来很常见，甚至像保护双方。\n"
            "     但合同 1200 万时封顶只有 60 万。\n"
            "     若对方违约需重新采购，差价可能远超 60 万。\n"
            "     ★ 必须做一次乘法才能看出来，人通常不会去算。"
        ),
        "outcome": build(
            clauses={
                "breach": "第五条 违约责任\n任何一方违约，违约金累计不超过合同总金额的 5%。",
            },
            basic={"amount": "人民币 12,000,000 元"},
        ),
    },
    {
        "title": "案例三：付款账期跨越合同到期日",
        "why": (
            "合同 30 天后到期，付款账期 90 天，验收期限 7 天。\n"
            "     三个数字分散在不同条款，各自都正常。\n"
            "     但 90 + 7 = 97 天 > 30 天，尾款必然落在合同到期之后。\n"
            "     ★ 要做一次日期减法，人几乎不会主动去算。"
        ),
        "outcome": build(
            clauses={
                "payment": "剩余款项在验收合格后 90 日内支付。",
                "acceptance": "甲方应在收到交付物后 7 日内完成验收。",
            },
            basic={"expire_date": (date.today() + timedelta(days=30)).isoformat()},
        ),
    },
    {
        "title": "案例四：扣了质保金，但全文找不到质保期",
        "why": (
            "付款条款「留 5% 作为质保金，质保期满后支付」——标准。\n"
            "     但需要主动去全文找「质保期是多久」，找不到才是问题。\n"
            "     ★ 这类「引用了某个东西但被引用方缺失」的问题，必须追踪才能发现。"
        ),
        "outcome": build(
            clauses={
                "payment": "合同总金额的 5% 作为质保金，质保期满后 30 日内无息退还。",
                "acceptance": "验收标准见附件。",
            }
        ),
    },
    {
        "title": "案例五：异地管辖 + 本地履约",
        "why": (
            "「由乙方所在地法院管辖」——明确条款，法务会注意到。\n"
            "     但交付地在我方，证据、标的物、验收记录都在我方这边。\n"
            "     却要跑去对方所在地打官司，诉讼成本显著上升。\n"
            "     ★ 要把两个条款对照，才成立「错配」。"
        ),
        "outcome": build(
            clauses={
                "dispute": "因本合同产生的争议，由乙方所在地人民法院管辖。",
                "delivery": "乙方应将货物交付至甲方所在地指定仓库。",
            }
        ),
    },
]


def main() -> None:
    """逐个案例执行深层检查并打印结果。

    返回：
        无。
    """
    print("=" * 78)
    print("深层规则价值演示：只有跨条款推理才能发现的风险")
    print("=" * 78)
    print()
    print("对比说明：")
    print("  浅层规则（26 条）＝ 在单条条款里找一个特征 → 法务自己扫一眼就能发现")
    print("  深层规则（5 条）  ＝ 跨条款交叉比对      → 单看每条都正常，合起来才有洞")
    print()

    total = 0
    for index, case in enumerate(CASES, start=1):
        print("-" * 78)
        print(f"{case['title']}")
        print("-" * 78)
        print("  为什么人容易漏掉：")
        print(f"     {case['why']}")
        print()

        hits = run_deep_checks(case["outcome"])
        if not hits:
            print("  ⚠ 未检出（该案例的构造应命中，请检查实现）")
            print()
            continue

        for hit in hits:
            total += 1
            print(f"  ✓ 检出 [{hit.risk_level.value.upper()}] {hit.rule_name}")
            print(f"    规则编码：{hit.rule_code}")
            print(f"    证据位置：{hit.evidence_position}")
            print(f"    推理依据：{hit.evidence_text}")
            print(f"    处理建议：{hit.suggestion_text}")
            print()

    print("=" * 78)
    print(f"共检出 {total} 条深层风险。这些都不是「看一眼就知道」的问题。")
    print("=" * 78)


if __name__ == "__main__":
    main()
