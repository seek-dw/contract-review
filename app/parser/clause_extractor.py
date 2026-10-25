"""条款抽取模块：从文档文本中提取 8 类合同条款及其原文范围。

职责：
- 抽取付款、交付、验收、违约、保密、数据、知识产权、争议解决八类条款；
- 为每类条款记录"条款原文范围"（起止行）、原文片段、位置、提取状态；
- 支持"条款标题定位"与"关键词兜底"两种策略，并明确标注结果来源。

非职责：
- 不判断条款内容的风险（见 ``review/rule_engine.py``）；
- 不解析条款内的具体数值（规则引擎按需从原文抽取）。

设计原因：
需求 2.4.5 要求条款信息同样保留原文片段与位置。条款与基础信息的关键差异是：
基础信息是"短值"，条款是"一段文本"，且合同里可能根本没有某一类条款
（例如没有保密条款——这本身就是需要审查的风险点）。
因此本模块的核心语义是：**"检出并给出范围"或"明确判定未检出"**，
后者会驱动规则引擎的 ``missing`` 模式命中。

抽取策略：
1. **标题定位**：识别形如"第X条 保密"、"三、保密条款"、"6. 数据与个人信息"的标题行，
   条款范围从标题行开始，到下一个条款标题行为止；
2. **关键词兜底**：无标题但正文出现该条款的强特征词（如"保密义务""违约金"）时，
   取该行及其后若干行作为范围，并标注来源为"关键词兜底"。

主要协作模块：
- ``services/parse_service.py`` 调用 :func:`extract_clauses`；
- 结果写入 ``contract_parses.clause_info_json``；
- ``review/rule_engine.py`` 用条款原文做关键词匹配与数值抽取。
"""

from __future__ import annotations

import re

from app.config import settings
from app.domain.enums import ExtractStatus
from app.domain.schemas import CLAUSE_LABELS, DocumentText, FieldEvidence, ParsedField
from app.parser.text_utils import snippet_around

# 条款类型到"标题关键词"的映射。
# 定义原因：条款标题用词随模板变化（"保密"可能写作"保密义务""商业秘密"），
# 用关键词集合而非单一词，能覆盖常见变体。
_TITLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "payment": ("付款", "支付", "结算", "价款支付", "费用支付"),
    "delivery": ("交付", "交货", "履行期限", "服务提供"),
    "acceptance": ("验收", "检验", "确认验收", "接收标准"),
    "breach": ("违约", "违约责任", "赔偿责任", "逾期责任"),
    "confidentiality": ("保密", "商业秘密", "保密义务", "信息保密"),
    "data": ("数据", "个人信息", "数据处理", "隐私", "数据安全"),
    "intellectual_property": ("知识产权", "著作权", "专利", "商标", "成果归属"),
    "dispute": ("争议解决", "争议", "仲裁", "诉讼", "管辖", "适用法律"),
}

# 关键词兜底用的强特征词：这些词出现在正文中基本可判定存在该条款。
# 与标题关键词分开定义，是因为兜底匹配的是"条款内容特征"而非"标题"，
# 两者混用会导致标题定位逻辑误判。
_CONTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "payment": ("预付款", "付款方式", "支付方式", "款项支付"),
    "delivery": ("交付时间", "交付地点", "交货期", "交付标准"),
    "acceptance": ("验收标准", "验收合格", "验收期限", "验收方式"),
    "breach": ("违约金", "违约方", "承担违约责任"),
    "confidentiality": ("保密义务", "保密信息", "不得披露", "保密责任"),
    "data": ("个人信息", "数据安全", "数据处理者", "数据泄露"),
    "intellectual_property": ("知识产权归属", "著作权归", "专利权归", "成果归"),
    "dispute": ("人民法院管辖", "仲裁委员会", "向.*法院起诉", "争议由"),
}

# 条款标题行模式：覆盖"第X条"、"第X章"、"一、"、"（一）"、"1."、"1.1" 等编号形式。
# 捕获组 1 是编号前缀，用于判断一行是否是"新条款开始"（从而截断上一条款范围）。
_TITLE_PATTERN = re.compile(
    r"^\s*(第[一二三四五六七八九十百零\d]+条"
    r"|[一二三四五六七八九十]+[、.．]"
    r"|[（(][一二三四五六七八九十\d]+[）)]"
    r"|\d+(?:\.\d+)*[、.．]?)\s*"
)

# 关键词兜底时向后扩展的最大行数。取 4 是因为条款正文通常在 1—4 行内，
# 扩太多会把无关条款内容混进证据。
_FALLBACK_WINDOW = 4


def _line_is_title(text: str) -> bool:
    """判断一行是否为条款标题行。

    参数：
        text: 行文本。
    返回：
        命中编号模式且行长度较短（≤40 字）时为 ``True``。
    关联：
        被 :func:`_collect_title_lines` 与 :func:`_range_end` 调用。
    设计原因：
        正文里也可能出现"第3条"的引用（如"依据第3条约定"），
        限制行长度可排除大部分引用句，降低误判。
    """
    return bool(_TITLE_PATTERN.match(text)) and len(text) <= 40


def _collect_title_lines(document: DocumentText) -> list[int]:
    """收集全部条款标题行的索引。

    参数：
        document: 文档文本视图。
    返回：
        标题行的行索引列表（升序）。
    关联：
        被 :func:`_range_end` 调用，用于确定某条款的范围终点。
    """
    return [index for index, line in enumerate(document.lines) if _line_is_title(line.text)]


def _range_end(title_indices: list[int], start: int) -> int:
    """计算从某行开始到下一个标题行之前的结束索引（不含）。

    参数：
        title_indices: 全部标题行索引（升序）。
        start: 当前条款标题行索引。
    返回：
        结束索引；没有后续标题时返回一个足够大的值，由调用方按文档长度裁剪。
    关联：
        被 :func:`_extract_clause` 调用。
    """
    for index in title_indices:
        if index > start:
            return index
    return 10**9


def _extract_clause(
    document: DocumentText, clause_key: str, title_indices: list[int]
) -> ParsedField:
    """抽取单类条款。

    参数：
        document: 文档文本视图。
        clause_key: 条款键，见 :data:`app.domain.schemas.CLAUSE_LABELS`。
        title_indices: 预先计算的标题行索引，避免每类条款重复扫描。
    返回：
        条款字段；值为条款原文（截断到配置长度），未检出时值为 ``None``。
    关联：
        被 :func:`extract_clauses` 调用；结果供规则引擎做关键词匹配与数值抽取。
    实现要点：
        1. 先按标题关键词找标题行，命中则取"标题行 → 下一标题行"之间的内容；
        2. 标题未命中时，按内容关键词兜底，取命中行起若干行；
        3. 两者都未命中则记 ``not_found``，并说明已尝试的策略。
    """
    label = CLAUSE_LABELS[clause_key]

    # ---- 策略一：标题定位 ----
    for index, line in enumerate(document.lines):
        if not _line_is_title(line.text):
            continue
        if not any(keyword in line.text for keyword in _TITLE_KEYWORDS[clause_key]):
            continue
        end = min(_range_end(title_indices, index), len(document.lines))
        body_lines = [document.lines[i].text for i in range(index, end)]
        body = "\n".join(body_lines)
        return ParsedField(
            key=clause_key,
            label=label,
            value=body[: settings.clause_snippet_limit],
            evidence=FieldEvidence(
                snippet=snippet_around(body, line.text, settings.clause_snippet_limit),
                location=line.location,
                status=ExtractStatus.SUCCESS,
                detail=f"按条款标题定位，覆盖第{index + 1}行至第{end}行",
            ),
        )

    # ---- 策略二：内容关键词兜底 ----
    for index, line in enumerate(document.lines):
        for keyword in _CONTENT_KEYWORDS[clause_key]:
            # 部分关键词写成正则（如"向.*法院起诉"），统一用 search 兼容两种形式。
            if not re.search(keyword, line.text):
                continue
            end = min(index + _FALLBACK_WINDOW + 1, len(document.lines))
            body_lines = [document.lines[i].text for i in range(index, end)]
            body = "\n".join(body_lines)
            return ParsedField(
                key=clause_key,
                label=label,
                value=body[: settings.clause_snippet_limit],
                evidence=FieldEvidence(
                    snippet=snippet_around(body, keyword, settings.clause_snippet_limit),
                    location=line.location,
                    status=ExtractStatus.SUCCESS,
                    detail=f"未找到条款标题，按内容关键词「{keyword}」兜底定位",
                ),
            )

    return ParsedField(
        key=clause_key,
        label=label,
        value=None,
        evidence=FieldEvidence(
            snippet="",
            location="",
            status=ExtractStatus.NOT_FOUND,
            detail=(
                f"未找到条款标题（{'/'.join(_TITLE_KEYWORDS[clause_key])}），"
                f"内容关键词（{'/'.join(_CONTENT_KEYWORDS[clause_key])}）也未命中"
            ),
        ),
    )


def extract_clauses(document: DocumentText) -> dict[str, ParsedField]:
    """抽取全部 8 类条款。

    参数：
        document: 文档文本视图。
    返回：
        条款键到 :class:`ParsedField` 的映射，键固定为
        :data:`app.domain.schemas.CLAUSE_LABELS` 的 8 个键。
    关联：
        被 ``services/parse_service.py::parse_attachment`` 调用；
        结果写入 ``contract_parses.clause_info_json``，并被规则引擎消费。
    说明：
        与基础信息一致，返回值一定包含全部 8 个键，未检出时状态为 ``not_found``，
        该状态会驱动"条款缺失"类规则命中，因此不能省略键。
    """
    title_indices = _collect_title_lines(document)
    return {
        clause_key: _extract_clause(document, clause_key, title_indices)
        for clause_key in CLAUSE_LABELS
    }
