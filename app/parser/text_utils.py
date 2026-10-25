"""文本工具模块：提供抽取层共用的文本归一化与数值解析纯函数。

职责：
- 统一处理全角/半角、空白、标点差异，使关键词匹配与数值抽取不受排版影响；
- 从中文合同语句中解析金额、百分比、天数、日期等确定性数值；
- 提供"截取关键词周围片段"的能力，用于生成命中证据。

非职责：
- 不做语义理解（不判断"这条条款是否合理"）；
- 不访问文件与数据库。

设计原因：
中文合同里同一含义有大量写法差异（``60%``/``百分之六十``/``60 ％``；``5 个工作日``/
``五个工作日``）。把归一化集中到本模块，可以让字段抽取、条款抽取、规则引擎三处
使用完全一致的文本视图，避免"规则匹配不到但人工能看见"的诡异情况。

主要协作模块：
- ``parser/field_extractor.py``、``parser/clause_extractor.py`` 调用归一化与数值解析；
- ``review/rule_engine.py`` 调用 :func:`normalize_for_match` 与 :func:`snippet_around`。

关于中文数字：
只支持合同常见的一至十、百、千、万，且仅在紧跟量词（工作日/日/天/个月/年/元）时转换。
这不是通用中文数字解析器，刻意保持窄范围以免误伤（例如"第一条款"不应变成数字）。
"""

from __future__ import annotations

import re

# 中文数字到阿拉伯数字的映射。仅覆盖合同高频出现的个位与十位组合。
_CN_DIGITS: dict[str, int] = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}

# 量词集合：只有中文数字后面紧跟这些量词时才做转换，避免误伤序数词。
_CN_MEASURE_WORDS = "个|日|天|月|年|元|万|千|百|次|份|项"

# 全角字符到半角字符的转换表（数字、字母、常用标点）。
# 用 str.translate 一次性完成，比逐个 replace 快且不会互相干扰。
_FULLWIDTH_MAP = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}
_FULLWIDTH_MAP[0x3000] = 0x20  # 全角空格
_FULLWIDTH_MAP[0xFF05] = 0x25  # ％ → %

# 匹配"中文数字 + 量词"的模式，例如"五个工作日""三十日"。
_CN_NUMBER_PATTERN = re.compile(rf"([零一二两三四五六七八九十百千]+)\s*({_CN_MEASURE_WORDS})")

# 匹配阿拉伯数字 + 可选单位的百分比，例如"60%""60 ％""百分之60"。
_PERCENT_PATTERN = re.compile(r"(?:百分之\s*)?(\d+(?:\.\d+)?)\s*%")

# 匹配金额：数字 + 可选千分位 + 可选"元/万元/亿元"单位。
_AMOUNT_PATTERN = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(亿元|万元|亿|万|元|人民币|RMB|CNY)?", re.IGNORECASE
)

# 匹配"X 个工作日""X 日""X 天"等天数表达。
#
# 【重要】天数抽取的难点是"日"这个字既表示天数又表示日期：
#   "2024 年 12 月 31 日前完成交付" 里的 "31 日" 是日期
#   "7 日内完成验收" 里的 "7 日" 是天数
# 最初的实现直接匹配 `(\d+)\s*日`，把日期里的 31 当成了 31 天，
# 导致验收期限从 7 日变成 31 日，跨期风险规则算出错误结论。
#
# 解决方案分两层（顺序不能反）：
#   第一层：:func:`extract_day_count` 先用 `_DATE_INLINE_PATTERN`
#           把完整日期整段剔除（"2024 年 12 月 31 日" → " "）。
#   第二层：本正则只处理"剔除日期后"的文本，
#           因此不需要用变长后行断言去排除"X 月"——
#           那样写 Python 也不支持（look-behind requires fixed-width）。
#
# 保留的 `(?!\s*前)` 用于排除"31 日前"这类残留表述
# （例如日期被部分匹配后留下的片段）。
_DAY_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*"
    r"(?:个)?\s*"
    r"(?:工作日|日历日|日(?!\s*前)|天)"
)

# 匹配"X 个月""X 月""X 年"等周期表达。
_MONTH_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(?:个)?\s*(?:月|年)")

# 日期模式（与 field_extractor 中的定义保持一致）。
# 用于在抽天数前先把日期从文本中剔除，避免"12 月 31 日"污染天数抽取。
_DATE_INLINE_PATTERN = re.compile(
    r"\d{4}\s*[-/年.]\s*\d{1,2}\s*[-/月.]\s*\d{1,2}\s*日?"
)


def to_halfwidth(text: str) -> str:
    """把全角字符转换为半角字符。

    参数：
        text: 原始文本。
    返回：
        全角转半角后的文本；中文汉字不受影响（它们不在 0xFF01—0xFF5E 区间）。
    关联：
        被 :func:`normalize_for_match` 调用，是归一化第一步。
    """
    return text.translate(_FULLWIDTH_MAP)


def cn_number_to_int(token: str) -> int | None:
    """把中文数字字符串转换为整数。

    参数：
        token: 形如 ``五``、``三十``、``一百二十`` 的中文数字串。
    返回：
        对应整数；无法解析时返回 ``None``。
    关联：
        被 :func:`normalize_cn_numbers` 调用。
    设计原因：
        只处理"十/百/千"作为位权的简单组合，不处理"万"以上（合同期限极少超过万年）。
    """
    if not token:
        return None
    total = 0
    section = 0
    number = 0
    for ch in token:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch == "十":
            section += (number or 1) * 10
            number = 0
        elif ch == "百":
            section += (number or 1) * 100
            number = 0
        elif ch == "千":
            section += (number or 1) * 1000
            number = 0
        else:
            return None
    return total + section + number


def normalize_cn_numbers(text: str) -> str:
    """把紧跟量词的中文数字替换为阿拉伯数字。

    参数：
        text: 原始文本。
    返回：
        替换后的文本，例如 ``五个工作日`` → ``5 个工作日``。
    关联：
        被 :func:`normalize_for_match` 调用；结果供 :func:`extract_day_count` 等使用。
    设计原因：
        合同常用"三十日"而非"30 日"表述。若不转换，数值抽取会漏掉大量真实条款。
        限定必须紧跟量词，避免把"第一条"变成"第1条"从而干扰条款标题定位。
    """

    def _replace(match: re.Match[str]) -> str:
        """把单个"中文数字 + 量词"匹配替换为阿拉伯数字。

        参数：
            match: 正则匹配对象，第 1 组为中文数字，第 2 组为量词。
        返回：
            替换后的字符串；中文数字无法解析时返回原匹配文本（不改写）。
        关联：
            作为 ``re.sub`` 的回调被 :func:`normalize_cn_numbers` 使用。
        设计原因：
            无法解析时保留原文而非丢弃，保证归一化是"尽力而为"而不是"破坏性改写"——
            丢弃会让条款原文出现空洞，人工核对时会以为合同缺字。
        """
        value = cn_number_to_int(match.group(1))
        if value is None:
            return match.group(0)
        return f"{value} {match.group(2)}"

    return _CN_NUMBER_PATTERN.sub(_replace, text)


def normalize_for_match(text: str) -> str:
    """生成用于匹配与抽取的归一化文本。

    参数：
        text: 原始文本（可含全角字符、中文数字、多余空白）。
    返回：
        归一化文本：全角转半角、中文数字转阿拉伯数字、连续空白折叠为单个空格。
    关联：
        被字段抽取、条款抽取、规则引擎共同调用，保证三处看到同一文本视图。
    注意：
        归一化文本只用于"匹配与数值抽取"，展示给用户的证据片段必须取自原文，
        否则会篡改合同原文，违反"证据不可改写"的要求。
    """
    normalized = to_halfwidth(text)
    normalized = normalize_cn_numbers(normalized)
    normalized = re.sub(r"[ \t\u00a0]+", " ", normalized)
    return normalized.strip()


def extract_percent(text: str) -> float | None:
    """从文本中提取第一个百分比数值。

    参数：
        text: 已归一化的文本。
    返回：
        百分比数值（``60%`` 返回 ``60.0``）；未找到返回 ``None``。
    关联：
        被 :func:`app.parser.field_extractor` 与规则引擎 threshold 模式调用。
    """
    match = _PERCENT_PATTERN.search(text)
    if not match:
        return None
    return float(match.group(1))


def extract_amount(text: str) -> tuple[float, str] | None:
    """从文本中提取第一个金额及其单位。

    参数：
        text: 已归一化的文本。
    返回：
        ``(数值, 单位)``；单位为原文出现的单位词（``元``/``万元``/``亿元``），
        未出现单位时为空字符串。未找到数字时返回 ``None``。
    关联：
        被字段抽取模块用于解析合同金额字段。
    设计原因：
        金额单位影响数量级（1 万元 = 10000 元），必须与数值一起返回，
        由调用方决定是否换算，避免在解析层隐式改写数值。
    """
    match = _AMOUNT_PATTERN.search(text)
    if not match:
        return None
    raw = match.group(1).replace(",", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    return value, (match.group(2) or "")


def extract_day_count(text: str) -> float | None:
    """从文本中提取第一个天数（工作日/日历日/日/天）。

    参数：
        text: 已归一化的文本。
    返回：
        天数值；未找到返回 ``None``。
    关联：
        被条款抽取与规则引擎用于判断付款周期、交付期限、验收期限是否过长。

    处理顺序（关键）：
        1. **先把日期整段剔除**，再抽天数。
        2. 原因：合同里"2024 年 12 月 31 日前完成交付"与"7 日内完成验收"
           常出现在同一句/同一段。若不先剔除日期，
           正则会把"31 日"当成"31 天"，导致天数严重失真。
           实测该缺陷让验收期限从 7 日变成 31 日，
           进而使跨期风险规则算出错误结论。

    例：
        >>> extract_day_count("2024 年 12 月 31 日前完成交付, 7 日内验收")
        7.0
    """
    # 第一步：剔除日期，避免日期中的"日"被当成天数
    cleaned = _DATE_INLINE_PATTERN.sub(" ", text)

    # 第二步：在剔除后的文本里找天数
    match = _DAY_PATTERN.search(cleaned)
    if not match:
        return None
    return float(match.group(1))


def extract_month_count(text: str) -> float | None:
    """从文本中提取第一个月数或年数（统一折算为月）。

    参数：
        text: 已归一化的文本。
    返回：
        折算后的月数（``1 年`` → ``12.0``）；未找到返回 ``None``。
    关联：
        被规则引擎用于判断自动续约周期等场景。
    """
    match = _MONTH_PATTERN.search(text)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(0)
    return value * 12 if "年" in unit else value


def snippet_around(text: str, keyword: str, limit: int = 120) -> str:
    """截取关键词周围的文本片段作为证据。

    参数：
        text: 原文（不是归一化文本，证据必须保留原文形态）。
        keyword: 关键词；为空或未找到时返回文本开头片段。
        limit: 片段最大字符数，超出时以省略号标记截断。
    返回：
        证据片段字符串。
    关联：
        被规则引擎生成 ``evidence_text``、被字段抽取生成 ``snippet``。
    设计原因：
        证据要能让人一眼看到上下文，同时不能让单条证据撑爆回写评论，因此做定长截断。
    """
    if not text:
        return ""
    position = text.find(keyword) if keyword else -1
    if position < 0:
        clipped = text[:limit]
        return clipped + ("…" if len(text) > limit else "")
    start = max(0, position - limit // 3)
    end = min(len(text), start + limit)
    fragment = text[start:end]
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{fragment}{suffix}"


def collapse_blank_lines(lines: list[str]) -> list[str]:
    """过滤空白行并清理首尾空白。

    参数：
        lines: 原始行列表。
    返回：
        非空行列表，每行已 strip。
    关联：
        被 ``parser/document.py`` 在构建 :class:`app.domain.schemas.DocumentText` 时使用。
    """
    return [line.strip() for line in lines if line and line.strip()]
