"""基础信息字段抽取模块：从文档文本中提取 8 项合同基础信息。

职责：
- 抽取合同标题、合同编号、签约主体、对方名称、金额、币种、生效时间、到期时间；
- 为每个字段生成 :class:`app.domain.schemas.FieldEvidence`（原文片段 + 位置 + 提取状态）；
- 抽取失败时区分 ``failed``（找到候选但解析不出）与 ``not_found``（完全没找到）。

非职责：
- 不抽取条款（见 ``clause_extractor``）；
- 不判断字段值是否"合理"（如金额是否过大），那是规则层职责。

设计原因：
需求 2.4.5 要求每个字段保留原文片段、位置标识、提取状态，且"解析失败时要记录失败原因，
不允许只返回空结果"。因此本模块的核心不是"尽力猜出一个值"，而是
**"要么给出有出处的值，要么给出有原因的缺失"**——绝不填占位字符串。

抽取策略：
采用"标签正则优先 + 邻近行回退"两级策略：
1. 标签正则：匹配 ``合同编号：XXX`` 这类"标签 + 分隔符 + 值"的行内模式；
2. 邻近行回退：标签与值分处两行时（常见于表格化排版），在标签行之后若干行内找值。
两级都失败才记 ``not_found``，并在 ``detail`` 中说明尝试过的方式。

主要协作模块：
- ``services/parse_service.py`` 调用 :func:`extract_basic_info`；
- 结果被 ``review/rule_engine.py`` 的 missing / threshold 模式消费。
"""

from __future__ import annotations

import re

from app.config import settings
from app.domain.enums import ExtractStatus
from app.domain.schemas import BASIC_INFO_LABELS, DocumentText, FieldEvidence, ParsedField
from app.parser.text_utils import (
    extract_amount,
    normalize_for_match,
    snippet_around,
)

# 标签别名表：一个字段可能有多种叫法，按优先级排列。
# 定义原因：不同企业合同模板用词差异大（"合同编号"/"协议编号"/"编号"），
# 用别名表比写死单一标签更实用，且新增别名不影响其他逻辑。
_LABEL_ALIASES: dict[str, tuple[str, ...]] = {
    "contract_title": ("合同名称", "合同标题", "协议名称"),
    "contract_no": ("合同编号", "协议编号", "合同编码", "编号"),
    "our_party": ("甲方", "委托方", "采购方", "需方", "本公司"),
    "counterparty": ("乙方", "受托方", "供应方", "供方", "对方"),
    "amount": ("合同总金额", "合同金额", "总金额", "合同价款", "价款", "金额"),
    "currency": ("币种", "货币", "结算币种"),
    "effective_date": ("生效日期", "生效时间", "起始日期", "合同生效日"),
    "expire_date": ("到期日期", "终止日期", "失效日期", "截止日期", "合同到期日"),
}

# 标签与值之间的分隔符，供文档说明使用。
# 实际匹配在 :func:`_value_after_label` 与 :func:`_is_label_line` 中以字面量写死，
# 因为严格模式要求"标签在行首 + 紧跟分隔符"，与简单字符类语义不同，无法共用。
_SEPARATOR = r"[:：|｜]"

# 日期模式：支持 2024-01-01、2024/01/01、2024年1月1日、2024.01.01 四种常见写法。
_DATE_PATTERN = re.compile(
    r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})\s*日?"
)

# 无显式标签时的日期回退模式。
# 定义原因：真实合同常用"有效期至 2025 年 12 月 31 日""自 2024 年 3 月 1 日起生效"
# 这类表述，而不会写"到期日期：xxx"。若只认标签，这两项必然抽不到，
# 导致"到期时间缺失"这类规则被误命中——那是解析能力不足造成的假风险。
# 因此为每个日期字段补充语义模式，模式内部再取日期。
_FALLBACK_DATE_PATTERNS: dict[str, tuple[str, ...]] = {
    "effective_date": (
        r"有效期自\s*(?:{date})",
        r"自\s*(?:{date})\s*起(?:生效|执行|履行)",
        r"于\s*(?:{date})\s*(?:生效|开始)",
    ),
    "expire_date": (
        r"有效期至\s*(?:{date})",
        r"有效期截止(?:至|到)?\s*(?:{date})",
        r"至\s*(?:{date})\s*(?:止|截止|届满)",
        # 量词的花括号必须写成 ``{{0,20}}``：这些模板会经过
        # ``str.format(date=...)``，未转义的 ``{0,20}`` 会被当成名为 ``0,20``
        # 的替换字段并抛 KeyError，导致整个解析中断。
        # 实测踩过：真实政府采购合同既没有"到期日期："标签，也没有"有效期至"
        # 表述，一路落到本模式时直接崩溃。
        r"合同期限[^。；\n]{{0,20}}?至\s*(?:{date})",
    ),
}

# 供上面的模式拼接使用的日期子模式（不含捕获组，避免分组冲突）。
_DATE_INLINE = r"\d{4}\s*[-/年.]\s*\d{1,2}\s*[-/月.]\s*\d{1,2}\s*日?"

# 币种关键词到标准代码的映射。只映射合同高频币种，未命中时保留原文。
_CURRENCY_TOKENS: tuple[tuple[str, str], ...] = (
    ("人民币", "CNY"),
    ("RMB", "CNY"),
    ("CNY", "CNY"),
    ("美元", "USD"),
    ("USD", "USD"),
    ("欧元", "EUR"),
    ("EUR", "EUR"),
    ("港币", "HKD"),
    ("港元", "HKD"),
    ("HKD", "HKD"),
    ("日元", "JPY"),
    ("JPY", "JPY"),
)

# 邻近行回退时向下搜索的最大行数。取 3 是因为表格化排版中标签与值通常相邻，
# 搜索过远会把无关内容误当字段值。
_NEIGHBOR_WINDOW = 3


def _make_field(
    key: str, value: str | None, snippet: str, location: str, status: ExtractStatus, detail: str = ""
) -> ParsedField:
    """构造一个字段结果，统一补齐标签与证据对象。

    参数：
        key: 字段键，见 :data:`app.domain.schemas.BASIC_INFO_LABELS`。
        value: 解析出的值；失败或未找到时为 ``None``。
        snippet: 原文片段，不允许改写。
        location: 位置标识。
        status: 提取状态。
        detail: 失败原因，成功时为空串。
    返回：
        :class:`ParsedField`。
    关联：
        被本模块所有 ``_extract_*`` 函数调用，确保字段结构一致。
    """
    return ParsedField(
        key=key,
        label=BASIC_INFO_LABELS.get(key, key),
        value=value,
        evidence=FieldEvidence(
            snippet=snippet[: settings.clause_snippet_limit],
            location=location,
            status=status,
            detail=detail,
        ),
    )


def _find_label_line(document: DocumentText, aliases: tuple[str, ...]) -> tuple[int, str] | None:
    """在文档中定位第一个出现指定标签的行。

    参数：
        document: 文档文本视图。
        aliases: 标签别名元组，按优先级排列。
    返回：
        ``(行索引, 行文本)``；未找到返回 ``None``。
    关联：
        被 :func:`_extract_by_label` 与 :func:`_extract_date` 调用。
    设计原因：
        返回行索引而非行对象，是为了让调用方能在邻近行窗口内继续搜索。

    两级匹配（这是本函数的核心，避免把正文误判为标签行）：
        1. **严格模式**：标签位于行首（允许带条款编号前缀），且紧跟分隔符
           ``: ： | ｜``。例如 ``甲方 | 星海智能科技有限公司``、
           ``合同编号：HT-2024-0001``。
        2. **宽松模式**：仅在严格模式全部落空时启用，按子串包含匹配。
           用于 ``本合同总金额为人民币 1,200,000 元`` 这类标签嵌在句中的写法。

    为什么必须先严格后宽松：
        合同正文常出现"乙方向甲方提供…"。若直接用子串匹配，"甲方"会命中这一行，
        导致签约主体被抽成正文片段（真实测试中已复现该错误）。
        严格模式要求标签后紧跟分隔符，正文句子中的"甲方提供"不满足，因此被排除。
    """
    # ---- 第一轮：严格模式（标签在行首 + 紧跟分隔符）----
    for alias in aliases:
        pattern = re.compile(
            # 可选的行首编号前缀：第X条 / 一、 / (一) / 1. / 1.1
            r"^\s*(?:第[一二三四五六七八九十百零\d]+条[、.．]?\s*"
            r"|[一二三四五六七八九十]+[、.．]\s*"
            r"|[（(][一二三四五六七八九十\d]+[）)]\s*"
            r"|\d+(?:\.\d+)*[、.．]?\s*)?"
            + re.escape(alias)
            + r"\s*[:：|｜]"
        )
        for index, line in enumerate(document.lines):
            if pattern.search(line.text):
                return index, line.text

    # ---- 第二轮：宽松模式（标签出现在行内任意位置）----
    for alias in aliases:
        for index, line in enumerate(document.lines):
            if alias in line.text:
                return index, line.text
    return None


def _is_label_line(text: str, aliases: tuple[str, ...]) -> bool:
    """判断一行是否以某个标签开头（严格模式）。

    参数：
        text: 行文本。
        aliases: 标签别名元组。
    返回：
        标签位于行首且紧跟分隔符时为 ``True``。
    关联：
        被 :func:`_extract_by_label` 的邻近行搜索用于跳过"另一个标签行"，
        避免把 ``乙方：XXX`` 当成 ``甲方`` 的值。
    设计原因：
        与 :func:`_find_label_line` 的严格模式使用同一判定口径，
        否则会出现"定位用严格、跳过用宽松"的不一致，导致漏值或错值。
    """
    for alias in aliases:
        pattern = re.compile(
            r"^\s*(?:第[一二三四五六七八九十百零\d]+条[、.．]?\s*"
            r"|[一二三四五六七八九十]+[、.．]\s*"
            r"|[（(][一二三四五六七八九十\d]+[）)]\s*"
            r"|\d+(?:\.\d+)*[、.．]?\s*)?"
            + re.escape(alias)
            + r"\s*[:：|｜]"
        )
        if pattern.search(text):
            return True
    return False


def _value_after_label(line_text: str, alias: str) -> str:
    """从"标签 + 分隔符 + 值"的行内文本中截取值部分。

    参数：
        line_text: 整行原文。
        alias: 命中的标签。
    返回：
        标签之后的值文本（已 strip）；标签后无内容时返回空串。
    关联：
        被 :func:`_extract_by_label` 调用。
    注意：
        这里不做归一化，保证证据片段与合同原文一致。
        分隔符同时支持 ``: ： | ｜``，因为 DOCX 表格拼接后的行形如
        ``甲方 | 星海智能科技有限公司``，竖线是列边界。
    """
    pattern = re.compile(re.escape(alias) + r"\s*[:：|｜]\s*(.+)$")
    match = pattern.search(line_text)
    if not match:
        return ""
    return match.group(1).strip()


def _extract_by_label(
    document: DocumentText, key: str
) -> tuple[str, str, str] | None:
    """按标签抽取一个文本型字段（标题、编号、主体、对方）。

    参数：
        document: 文档文本视图。
        key: 字段键。
    返回：
        ``(值, 原文片段, 位置)``；未找到或值为空时返回 ``None``。
    关联：
        被 :func:`extract_basic_info` 调用，是四个文本型字段的统一实现。
    设计原因：
        四个字段的抽取逻辑完全相同（找标签、取值、截证据），
        用一个函数处理避免四份重复代码；差异只在别名表里。
    """
    aliases = _LABEL_ALIASES[key]
    found = _find_label_line(document, aliases)
    if found is None:
        return None
    index, line_text = found
    line = document.lines[index]

    # 第一级：标签与值在同一行。
    for alias in aliases:
        if alias not in line_text:
            continue
        value = _value_after_label(line_text, alias)
        if value:
            return value, snippet_around(line_text, alias), line.location

        # 第二级：标签独占一行，值在后续邻近行。
        for offset in range(1, _NEIGHBOR_WINDOW + 1):
            if index + offset >= len(document.lines):
                break
            neighbor = document.lines[index + offset]
            candidate = neighbor.text.strip()
            # 跳过任何"其他标签行"，避免把"乙方：XXX"当成"甲方"的值。
            # 用 _is_label_line 统一判定口径（与定位逻辑一致）。
            if any(
                _is_label_line(candidate, other_aliases)
                for other_aliases in _LABEL_ALIASES.values()
            ):
                break
            if candidate:
                return candidate, snippet_around(candidate, ""), neighbor.location
    return None


def _format_amount(value: float, unit: str) -> str:
    """把金额数值与单位格式化为可读文本。

    参数：
        value: 金额数值（已去掉千分位）。
        unit: 单位词，如 ``元`` / ``万元``；可能为空串。
    返回：
        可读金额，如 ``1200000 元``；整数不带小数点，非整数保留两位小数。
    关联：
        被 :func:`_extract_amount_field` 调用。
    设计原因：
        Python 的 ``:g`` 格式化会把 1200000 输出成 ``1.2e+06``，
        这对合同审查完全不可读，也容易被误认为金额是 1.2。
        因此整数走 ``:,`` 千分位格式，非整数保留两位小数。
    """
    if value == int(value):
        return f"{int(value):,} {unit}".strip()
    return f"{value:,.2f} {unit}".strip()


def _extract_amount_field(document: DocumentText) -> ParsedField:
    """抽取合同金额字段。

    参数：
        document: 文档文本视图。
    返回：
        金额字段；值为数值与单位的可读组合（如 ``1,200,000 元``）。
    关联：
        被 :func:`extract_basic_info` 调用；数值本身由规则引擎从原文重新解析，
        本字段只负责"给人和规则一个可读值 + 出处"。
    设计原因：
        金额既可能是"合同总金额：1,200,000 元"，也可能是"总价为人民币壹佰贰拾万元整"。
        前者可用数字模式抽取，后者（大写金额）超出确定性解析范围，
        此时记 ``failed`` 并保留原文，让人工判断，而不是猜一个数。
    """
    found = _find_label_line(document, _LABEL_ALIASES["amount"])
    if found is None:
        return _make_field(
            "amount", None, "", "", ExtractStatus.NOT_FOUND,
            "全文未找到金额相关标签（合同总金额/合同金额/价款/金额）",
        )

    index, line_text = found
    line = document.lines[index]
    normalized = normalize_for_match(line_text)
    parsed = extract_amount(normalized)

    # 标签行内没有数字时，向邻近行找（表格化排版常见）。
    if parsed is None:
        for offset in range(1, _NEIGHBOR_WINDOW + 1):
            if index + offset >= len(document.lines):
                break
            neighbor = document.lines[index + offset]
            neighbor_parsed = extract_amount(normalize_for_match(neighbor.text))
            if neighbor_parsed is not None:
                value, unit = neighbor_parsed
                return _make_field(
                    "amount",
                    _format_amount(value, unit),
                    snippet_around(neighbor.text, ""),
                    neighbor.location,
                    ExtractStatus.SUCCESS,
                )

    if parsed is None:
        return _make_field(
            "amount", None, snippet_around(line_text, "金额"), line.location,
            ExtractStatus.FAILED,
            "定位到金额标签但无法解析出数字（可能为大写金额表述，需人工确认）",
        )

    value, unit = parsed
    return _make_field(
        "amount",
        _format_amount(value, unit),
        snippet_around(line_text, "金额"),
        line.location,
        ExtractStatus.SUCCESS,
    )


def _extract_currency_field(document: DocumentText) -> ParsedField:
    """抽取币种字段。

    参数：
        document: 文档文本视图。
    返回：
        币种字段；值优先取显式币种标签，其次从金额行推断。
    关联：
        被 :func:`extract_basic_info` 调用。
    设计原因：
        多数合同不单独写"币种"，而是把"人民币"写在金额里。
        因此先找显式标签，找不到再从金额行推断，推断成功时在 ``detail`` 中注明来源，
        让审核人知道这是推断值而非明示值。
    """
    found = _find_label_line(document, _LABEL_ALIASES["currency"])
    if found is not None:
        index, line_text = found
        line = document.lines[index]
        for alias in _LABEL_ALIASES["currency"]:
            if alias in line_text:
                value = _value_after_label(line_text, alias)
                if value:
                    return _make_field(
                        "currency", value, snippet_around(line_text, alias),
                        line.location, ExtractStatus.SUCCESS,
                    )

    # 回退：从金额行里找币种关键词。
    amount_found = _find_label_line(document, _LABEL_ALIASES["amount"])
    if amount_found is not None:
        index, line_text = amount_found
        line = document.lines[index]
        for token, code in _CURRENCY_TOKENS:
            if token in line_text:
                return _make_field(
                    "currency", code, snippet_around(line_text, token),
                    line.location, ExtractStatus.SUCCESS,
                    f"未找到显式币种标签，从金额行推断为 {code}",
                )

    return _make_field(
        "currency", None, "", "", ExtractStatus.NOT_FOUND,
        "未找到币种标签，金额行中也未出现币种关键词",
    )


def _extract_date_field(document: DocumentText, key: str) -> ParsedField:
    """抽取生效日期或到期日期。

    参数：
        document: 文档文本视图。
        key: ``effective_date`` 或 ``expire_date``。
    返回：
        日期字段，值格式统一为 ``YYYY-MM-DD``。
    关联：
        被 :func:`extract_basic_info` 调用。
    设计原因：
        日期统一格式化后，规则引擎做"生效晚于到期"这类比较才有意义；
        但证据片段保留原文，让人能看到合同里原本怎么写的。
    """
    aliases = _LABEL_ALIASES[key]
    found = _find_label_line(document, aliases)

    # ---- 第一优先级：显式标签（如"到期日期：2025-12-31"）----
    if found is not None:
        index, line_text = found
        line = document.lines[index]
        match = _DATE_PATTERN.search(line_text)

        # 标签行无日期时向邻近行找（表格化排版常见）。
        if match is None:
            for offset in range(1, _NEIGHBOR_WINDOW + 1):
                if index + offset >= len(document.lines):
                    break
                neighbor = document.lines[index + offset]
                neighbor_match = _DATE_PATTERN.search(neighbor.text)
                if neighbor_match is not None:
                    year, month, day = neighbor_match.groups()
                    return _make_field(
                        key,
                        f"{int(year):04d}-{int(month):02d}-{int(day):02d}",
                        snippet_around(neighbor.text, ""),
                        neighbor.location,
                        ExtractStatus.SUCCESS,
                    )

        if match is None:
            return _make_field(
                key, None, snippet_around(line_text, aliases[0]), line.location,
                ExtractStatus.FAILED,
                "定位到日期标签但未能解析出完整年月日",
            )

        year, month, day = match.groups()
        return _make_field(
            key,
            f"{int(year):04d}-{int(month):02d}-{int(day):02d}",
            snippet_around(line_text, aliases[0]),
            line.location,
            ExtractStatus.SUCCESS,
        )

    # ---- 第二优先级：语义回退模式（如"有效期至 2025 年 12 月 31 日"）----
    # 触发条件：合同没有写"到期日期：xxx"这类标签，但用自然语言表达了同一含义。
    # 若不回退，会误报"到期时间缺失"——那是解析能力问题，不是合同风险。
    for pattern_template in _FALLBACK_DATE_PATTERNS[key]:
        pattern = re.compile(pattern_template.format(date=_DATE_INLINE))
        for line in document.lines:
            match = pattern.search(line.text)
            if match is None:
                continue
            date_match = _DATE_PATTERN.search(match.group(0))
            if date_match is None:
                continue
            year, month, day = date_match.groups()
            return _make_field(
                key,
                f"{int(year):04d}-{int(month):02d}-{int(day):02d}",
                snippet_around(line.text, match.group(0)),
                line.location,
                ExtractStatus.SUCCESS,
                f"未找到日期标签，按语义模式「{pattern_template}」回退定位",
            )

    return _make_field(
        key, None, "", "", ExtractStatus.NOT_FOUND,
        f"全文未找到日期标签（{'/'.join(aliases)}），"
        f"也未匹配到语义表达（有效期至/有效期自/起生效等）",
    )


def _extract_title_field(document: DocumentText) -> ParsedField:
    """抽取合同标题。

    参数：
        document: 文档文本视图。
    返回：
        标题字段；优先取"合同名称"标签，其次取首行含"合同/协议"的短行。
    关联：
        被 :func:`extract_basic_info` 调用。
    设计原因：
        合同标题常常是文档首行大字标题而非"合同名称：XXX"格式。
        因此增加"首行含合同/协议且长度适中"的回退规则，
        长度上限避免把包含"合同"字样的正文长句误当标题。
    """
    found = _extract_by_label(document, "contract_title")
    if found is not None:
        value, snippet, location = found
        return _make_field("contract_title", value, snippet, location, ExtractStatus.SUCCESS)

    for line in document.lines[:5]:
        text = line.text.strip()
        if ("合同" in text or "协议" in text) and len(text) <= 40:
            return _make_field(
                "contract_title", text, snippet_around(text, ""), line.location,
                ExtractStatus.SUCCESS,
                "未找到标题标签，取文档首部含「合同/协议」的短行作为标题",
            )

    return _make_field(
        "contract_title", None, "", "", ExtractStatus.NOT_FOUND,
        "未找到标题标签，文档首部也没有可作为标题的短行",
    )


def extract_basic_info(document: DocumentText) -> dict[str, ParsedField]:
    """抽取全部 8 项基础信息字段。

    参数：
        document: 文档文本视图。
    返回：
        字段键到 :class:`ParsedField` 的映射，键固定为
        :data:`app.domain.schemas.BASIC_INFO_LABELS` 的 8 个键。
    关联：
        被 ``services/parse_service.py::parse_attachment`` 调用；
        结果写入 ``contract_parses.basic_info_json``，并被规则引擎消费。
    说明：
        返回值一定包含全部 8 个键，即使某个字段未找到也会带 ``not_found`` 状态，
        保证下游无需处理"键缺失"这种额外分支。
    """
    fields: dict[str, ParsedField] = {
        "contract_title": _extract_title_field(document),
        "contract_no": _extract_text_field(document, "contract_no"),
        "our_party": _extract_text_field(document, "our_party"),
        "counterparty": _extract_text_field(document, "counterparty"),
        "amount": _extract_amount_field(document),
        "currency": _extract_currency_field(document),
        "effective_date": _extract_date_field(document, "effective_date"),
        "expire_date": _extract_date_field(document, "expire_date"),
    }
    return fields


def _extract_text_field(document: DocumentText, key: str) -> ParsedField:
    """抽取一个纯文本型字段（编号、签约主体、对方名称）。

    参数：
        document: 文档文本视图。
        key: 字段键。
    返回：
        字段结果；未找到时状态为 ``not_found``。
    关联：
        被 :func:`extract_basic_info` 调用，内部复用 :func:`_extract_by_label`。
    设计原因：
        单独封装一层是为了给失败情形补上统一的中文原因说明，
        与 :func:`_extract_title_field` 的"无标签回退"策略区分开。
    """
    found = _extract_by_label(document, key)
    if found is None:
        aliases = "/".join(_LABEL_ALIASES[key])
        return _make_field(
            key, None, "", "", ExtractStatus.NOT_FOUND, f"全文未找到标签（{aliases}）"
        )
    value, snippet, location = found
    return _make_field(key, value, snippet, location, ExtractStatus.SUCCESS)
