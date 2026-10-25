"""领域数据结构模块：定义解析结果、审查结果与证据定位的纯数据模型。

职责：
- 定义"文档解析产物"（:class:`DocumentText`、:class:`ParsedField`、:class:`ParseOutcome`）
  与"规则审查产物"（:class:`RuleHitView`、:class:`ReviewOutcome`）；
- 所有结构都是不可变或纯数据对象，不依赖数据库、不依赖 FastAPI，可被单元测试直接构造。

非职责：
- 不负责持久化（由 ``services/*_service.py`` 把结构映射到 ORM 模型）；
- 不做字段提取逻辑（由 ``parser/field_extractor.py`` 与 ``parser/clause_extractor.py``）。

设计原因：
需求 2.4.5 要求"每个解析字段至少保留原文片段、页码或位置标识、提取状态"。
把这三要素收进 :class:`FieldEvidence`，避免在每个字段上重复三个平行属性；
需求 2.4.6 要求命中结果含"规则名称、风险等级、命中证据、证据位置、建议说明"，
统一由 :class:`RuleHitView` 承载，规则引擎与结果服务共用同一结构，杜绝口径漂移。

主要协作模块：
- ``parser/document.py`` 产出 :class:`DocumentText`；
- ``parser/field_extractor.py``、``parser/clause_extractor.py`` 产出 :class:`ParsedField`；
- ``services/parse_service.py`` 组装 :class:`ParseOutcome` 并写入 ``contract_parses``；
- ``review/rule_engine.py`` 消费 :class:`ParseOutcome`，产出 :class:`ReviewOutcome`；
- ``services/result_service.py`` 把 :class:`ReviewOutcome` 转成回写文本与 ``review_results`` 记录。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.enums import ExtractStatus, ParseStatus, RiskLevel

# 条款类型到中文名称的映射。
# 定义在此处而不是枚举，是因为条款类型只在解析与展示中使用，不参与状态判断，
# 用常量字典比枚举更轻，且便于调用端直接遍历生成表格行。
CLAUSE_LABELS: dict[str, str] = {
    "payment": "付款条款",
    "delivery": "交付条款",
    "acceptance": "验收条款",
    "breach": "违约条款",
    "confidentiality": "保密条款",
    "data": "数据条款",
    "intellectual_property": "知识产权条款",
    "dispute": "争议解决条款",
}

# 基础信息字段到中文名称的映射，用途同 CLAUSE_LABELS。
BASIC_INFO_LABELS: dict[str, str] = {
    "contract_title": "合同标题",
    "contract_no": "合同编号",
    "our_party": "签约主体（我方）",
    "counterparty": "对方名称",
    "amount": "合同金额",
    "currency": "币种",
    "effective_date": "生效时间",
    "expire_date": "到期时间",
}


@dataclass(frozen=True)
class TextLine:
    """文档中的一行文本及其位置。

    用途：所有定位信息的最小单位。字段抽取与规则命中都靠它回指原文。
    定义原因：需求 2.4.5 要求"页码或位置标识"，行级定位比整页定位更利于法务核对。
    """

    # 页码，从 1 开始；DOCX 与图片没有真实页码，统一记为 1。
    page: int
    # 该页内的行号，从 1 开始，用于同页内排序与展示。
    line_no: int
    # 行文本内容，已做首尾空白清理。
    text: str

    @property
    def location(self) -> str:
        """返回人类可读的位置标识。

        返回：
            形如 ``第2页第15行`` 的字符串。
        关联：
            被 :class:`FieldEvidence` 与 ``services/result_service.py`` 用于展示与入库。
        """
        return f"第{self.page}页第{self.line_no}行"


@dataclass(frozen=True)
class DocumentText:
    """整份文档的文本视图。

    用途：解析层与抽取层之间的唯一通道；抽取层只读本对象，不接触文件。
    定义原因：把"文件怎么读"和"字段怎么抽"隔开，使抽取逻辑可对纯文本做单元测试。
    """

    # 全部文本行，按阅读顺序排列。
    lines: tuple[TextLine, ...]
    # 文档总页数。
    page_count: int
    # 走 OCR 的页号列表（升序）。用于向审核人说明"哪些页是识别出来的"。
    ocr_pages: tuple[int, ...]
    # 文本层可用（非扫描件）的页号列表。
    text_pages: tuple[int, ...]
    # 视觉空白页的页号列表（渲染后几乎全白，既无文本层也无有效图像内容）。
    # 定义原因：需求 2.4.4 要求区分"图片无法识别"与"文档内容为空"。
    # 空白页属于后者——不是识别技术失败，而是文件本身没内容。
    # 没有这个字段，空白 PDF 会被 OCR 报成"无法识别"，导致 blocked_reason 错误。
    blank_pages: tuple[int, ...] = ()

    @property
    def full_text(self) -> str:
        """返回全文（行之间以换行连接）。

        返回：
            整份文档的纯文本，供关键词检索使用。
        关联：
            被 ``review/rule_engine.py`` 的关键词模式与 ``parser/clause_extractor.py`` 使用。
        """
        return "\n".join(line.text for line in self.lines)

    @property
    def char_count(self) -> int:
        """返回全文有效字符数（不计空白）。

        返回：
            去掉所有空白后的字符数量。
        关联：
            被 ``services/parse_service.py`` 判定"文档内容为空"时使用。
        """
        return sum(1 for ch in self.full_text if not ch.isspace())


@dataclass(frozen=True)
class FieldEvidence:
    """单个字段的证据与定位信息。

    用途：需求 2.4.5 强制要求的三要素载体——原文片段、位置标识、提取状态。
    定义原因：把三者绑成一个不可分对象，避免出现"有值没位置"或"有状态没证据"的残缺记录。
    """

    # 原文片段：命中的原始文本（可能截断），用于人工核对，不允许改写。
    snippet: str
    # 位置标识，来自 :attr:`TextLine.location`；未定位到时为空字符串。
    location: str
    # 提取状态，说明该字段是"提取到""提取失败"还是"未找到"。
    status: ExtractStatus
    # 提取失败或未找到时的具体原因，供人工排查；成功时为空字符串。
    detail: str = ""


@dataclass(frozen=True)
class ParsedField:
    """一个结构化字段的解析结果。

    用途：统一承载基础信息字段与条款字段，使入库与展示代码只有一套。
    定义原因：需求 2.4.5 对基础信息与条款提出同样的证据要求，
    用同一结构可避免两套相似代码各自演化。
    """

    # 字段键，取自 :data:`BASIC_INFO_LABELS` 或 :data:`CLAUSE_LABELS` 的键。
    key: str
    # 字段中文名，直接来自上面的标签表，保证页面展示一致。
    label: str
    # 解析出的值：基础信息为字符串，条款为条款原文（可能截断）。
    # 提取失败或未找到时为 None，绝不填占位字符串，避免下游把占位当真实值。
    value: str | None
    # 证据与定位。
    evidence: FieldEvidence


@dataclass(frozen=True)
class ParseOutcome:
    """一次文档解析的完整产物。

    用途：``services/parse_service.py`` 的返回值，同时是规则引擎的输入。
    生命周期：单次解析内构造一次，随 ``contract_parses`` 记录持久化后不再变化。
    """

    # 解析状态：成功 / 部分成功 / 失败。
    status: ParseStatus
    # 基础信息字段（8 项，键见 BASIC_INFO_LABELS）。
    basic_info: dict[str, ParsedField]
    # 条款字段（8 类，键见 CLAUSE_LABELS）。
    clauses: dict[str, ParsedField]
    # 文档文本视图，规则引擎需要全文检索。
    document: DocumentText
    # 解析失败原因；成功时为空字符串。
    error_message: str = ""
    # 附加元信息：OCR 页数、耗时等，写入 ``contract_parses.parse_meta_json`` 便于排查。
    meta: dict[str, object] = field(default_factory=dict)

    def basic_value(self, key: str) -> str | None:
        """读取基础信息字段的原始值。

        参数：
            key: 字段键，见 :data:`BASIC_INFO_LABELS`。
        返回：
            字段值；字段不存在或未提取到时返回 ``None``。
        关联：
            被 ``review/rule_engine.py`` 的 missing / threshold 模式调用。
        """
        item = self.basic_info.get(key)
        return item.value if item else None

    def clause_value(self, key: str) -> str | None:
        """读取条款字段的原文。

        参数：
            key: 条款键，见 :data:`CLAUSE_LABELS`。
        返回：
            条款原文；条款不存在或未检出时返回 ``None``。
        关联：
            被 ``review/rule_engine.py`` 定位条款范围后做关键词匹配。
        """
        item = self.clauses.get(key)
        return item.value if item else None

    def clause_evidence(self, key: str) -> FieldEvidence | None:
        """读取条款字段的证据对象。

        参数：
            key: 条款键。
        返回：
            证据对象；条款不存在时返回 ``None``。
        关联：
            被 ``review/rule_engine.py`` 生成命中证据位置时调用。
        """
        item = self.clauses.get(key)
        return item.evidence if item else None

    def has_basic(self, key: str) -> bool:
        """判断基础信息字段是否成功提取到非空值。

        参数：
            key: 字段键。
        返回：
            提取状态为 success 且值非空时为 ``True``。
        关联：
            被 ``review/rule_engine.py`` 的 missing 模式直接使用。
        """
        item = self.basic_info.get(key)
        return bool(item and item.value)


@dataclass(frozen=True)
class RuleHitView:
    """一条规则命中的完整视图。

    用途：需求 2.4.6 要求命中结果包含"规则名称、风险等级、命中证据、证据位置、建议说明"，
    本结构就是这五项的唯一载体，规则引擎产出、结果服务消费、调用端展示都用它。
    """

    # 规则数据库主键，用于回查规则定义。
    # 允许为 None：深层规则（D 开头）与陷阱规则（T 开头）不在数据库里，
    # 是代码内置的检查逻辑，没有对应记录。
    # 【重要】不能用 -1 之类的哨兵值：``rule_hits.rule_id`` 是指向
    # ``review_rules.id`` 的外键，写 -1 会触发 FOREIGN KEY constraint failed
    # （实测踩过，导致 15 项测试失败）。None 才能被外键正确接受。
    rule_id: int | None
    # 规则编码，业务唯一，便于人工在规则表里定位。
    rule_code: str
    # 规则名称。
    rule_name: str
    # 该规则的风险等级。
    risk_level: RiskLevel
    # 命中证据：从合同原文摘取的片段，不允许改写。
    evidence_text: str
    # 证据位置，形如 ``第2页第15行`` 或 ``全文检索``。
    evidence_position: str
    # 建议说明：给审批人看的处理建议。
    suggestion_text: str


@dataclass(frozen=True)
class ReviewOutcome:
    """一次规则审查的完整结论。

    用途：需求 2.4.11 规定的五项输出（总风险等级、命中列表、中文摘要、审批关注点、
    回写内容）中，前四项由本结构承载，回写内容由 ``services/result_service.py`` 生成。
    """

    # 总风险等级：所有命中规则中的最高等级；无命中为 low。
    overall_risk_level: RiskLevel
    # 命中规则列表，按风险等级从高到低排序，便于审批人先看重点。
    hits: tuple[RuleHitView, ...]
    # 中文摘要：对整体合同风险的总结。
    summary_text: str
    # 审批关注点：审批人需要重点确认的事项列表。
    focus_points: tuple[str, ...]
    # 参与本次审查的启用规则数量，用于说明审查覆盖面。
    evaluated_rule_count: int
