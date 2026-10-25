"""种子规则模块：定义需求 2.4.6 要求的审查规则集合。

职责：
- 以纯数据形式声明 26 条种子规则，覆盖需求 2.4.6 明确列出的 11 类主题；
- 供 ``scripts/init_db.py`` 灌入 ``review_rules`` 表；
- 每条规则自带中文名称、风险等级、匹配模式、匹配文本、建议说明。

非职责：
- 不执行匹配（见 ``review/rule_engine.py``）；
- 不写数据库（由初始化脚本负责）。

设计原因：
需求 2.4.6 原文列出的 11 类主题是：
预付款比例、付款周期、自动续约、违约责任、管辖地、主体信息缺失、金额缺失、
保密缺失、数据处理、知识产权、验收标准缺失。

本模块严格按这 11 类组织，每类至少 1 条，并补充若干同族规则（如"付款周期过长"
与"付款条件不明"分开），总计 26 条。这样做的理由：
- **可验收**：11 类主题每类都能在规则表里找到对应条目，对照原文即可核对；
- **可解释**：一条规则只判断一件事，命中时审批人一眼知道在提示什么，
  不会出现"命中了一条包罗万象的规则但不知道具体问题在哪"。

风险等级设定原则：
- ``high``：直接造成资金或法律责任风险（预付款过高、违约金过低、管辖地不利、主体缺失）；
- ``medium``：需要确认但通常可通过谈判调整（付款周期偏长、验收标准模糊、续约自动）；
- ``low``：提示性事项（缺少兜底条款、表述不完整）。

主要协作模块：
- ``scripts/init_db.py`` 调用 :func:`seed_rule_rows` 生成待插入的行数据；
- ``services/review_service.py`` 从数据库读取后转成
  :class:`app.review.rule_engine.RuleSpec` 传给引擎。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.enums import MatchMode, RiskLevel, RuleStatus


@dataclass(frozen=True)
class SeedRule:
    """一条种子规则的定义。

    用途：以不可变数据描述规则，避免初始化脚本里散落大量字典字面量。
    字段与 ``review_rules`` 表一一对应，便于初始化时直接映射。
    """

    # 规则编码，业务唯一。命名规则：R{序号}_{主题英文}，便于排序与检索。
    rule_code: str
    # 规则名称（中文），直接展示给审批人。
    rule_name: str
    # 风险等级。
    risk_level: RiskLevel
    # 匹配模式。
    match_mode: MatchMode
    # 匹配文本，语义随模式变化。
    match_text: str
    # 建议说明：命中后给审批人的处理建议。
    suggestion_text: str
    # 求值优先级，数值小的先求值。按主题分组：1x 付款、2x 期限、3x 违约、4x 管辖、
    # 5x 主体与金额、6x 保密、7x 数据、8x 知识产权、9x 验收。
    priority: int
    # 结构化条件 JSON，供 threshold 模式使用。
    condition_json: str = "{}"
    # 启用状态，种子规则默认全部启用。
    rule_status: RuleStatus = RuleStatus.ENABLED


# 26 条种子规则。分组顺序与需求 2.4.6 列出的主题顺序一致，便于逐条对照验收。
SEED_RULES: tuple[SeedRule, ...] = (
    # ==================== 一、预付款比例 ====================
    SeedRule(
        rule_code="R101_ADVANCE_PAYMENT_HIGH",
        rule_name="预付款比例过高",
        risk_level=RiskLevel.HIGH,
        match_mode=MatchMode.THRESHOLD,
        match_text="预付款比例 > 30%",
        suggestion_text="建议将预付款比例压降至 30% 以内，或要求对方提供同等金额的履约保函。",
        priority=101,
        # 只在付款条款内抽百分比：全文抽会命中"违约金 30%"等无关数字。
        condition_json='{"scope": "payment", "extract": "percent", "operator": ">", "value": 30}',
    ),
    SeedRule(
        rule_code="R102_ADVANCE_PAYMENT_FULL",
        rule_name="要求全额预付",
        risk_level=RiskLevel.HIGH,
        match_mode=MatchMode.KEYWORD,
        match_text="全额预付|一次性支付全部|预付全部款项|款到发货",
        suggestion_text="全额预付将全部资金风险转移至我方，建议改为分期付款并与交付节点绑定。",
        priority=102,
        condition_json='{"scope": "payment"}',
    ),
    SeedRule(
        rule_code="R103_NO_ADVANCE_CLAUSE",
        rule_name="付款条款缺失",
        risk_level=RiskLevel.HIGH,
        match_mode=MatchMode.MISSING,
        match_text="payment",
        suggestion_text="合同缺少付款条款，无法确定付款节点与金额，建议补充完整付款安排。",
        priority=103,
    ),
    # ==================== 二、付款周期 ====================
    SeedRule(
        rule_code="R201_PAYMENT_PERIOD_LONG",
        rule_name="付款周期过长",
        risk_level=RiskLevel.MEDIUM,
        match_mode=MatchMode.THRESHOLD,
        match_text="付款周期 > 60 日",
        suggestion_text="付款周期超过 60 日将显著拉长回款周期，建议缩短至 30—60 日内并明确起算点。",
        priority=201,
        condition_json='{"scope": "payment", "extract": "day", "operator": ">", "value": 60}',
    ),
    SeedRule(
        rule_code="R202_PAYMENT_UNCLEAR",
        rule_name="付款条件表述不明",
        risk_level=RiskLevel.MEDIUM,
        match_mode=MatchMode.KEYWORD,
        match_text="另行协商|另行约定|视情况而定|届时确定",
        suggestion_text="付款条件使用「另行协商」等不确定表述，建议明确具体金额、节点与期限。",
        priority=202,
        condition_json='{"scope": "payment"}',
    ),
    # ==================== 三、自动续约 ====================
    SeedRule(
        rule_code="R301_AUTO_RENEWAL",
        rule_name="存在自动续约条款",
        risk_level=RiskLevel.MEDIUM,
        match_mode=MatchMode.KEYWORD,
        match_text="自动续约|自动顺延|自动延长|期满自动续期|默示续约",
        suggestion_text="自动续约条款可能导致合同在无明确审批的情况下延续，建议改为到期前书面确认续约。",
        priority=301,
        condition_json='{"scope": "full"}',
    ),
    SeedRule(
        rule_code="R302_NO_RENEWAL_NOTICE",
        rule_name="续约未约定提前通知期",
        risk_level=RiskLevel.LOW,
        match_mode=MatchMode.KEYWORD,
        match_text="自动续约|自动顺延|自动延长",
        suggestion_text="建议补充「任一方可在期满前 30 日书面通知不再续约」，避免被动续约。",
        priority=302,
        condition_json='{"scope": "full"}',
    ),
    # ==================== 四、违约责任 ====================
    SeedRule(
        rule_code="R401_PENALTY_RATIO_HIGH",
        rule_name="违约金比例过高",
        risk_level=RiskLevel.HIGH,
        match_mode=MatchMode.THRESHOLD,
        match_text="违约金比例 > 20%",
        suggestion_text="违约金比例超过合同金额 20% 可能被认定过高，建议调整至 20% 以内或与实际损失挂钩。",
        priority=401,
        condition_json='{"scope": "breach", "extract": "percent", "operator": ">", "value": 20}',
    ),
    SeedRule(
        rule_code="R402_PENALTY_ONE_SIDED",
        rule_name="违约责任可能单向",
        risk_level=RiskLevel.MEDIUM,
        match_mode=MatchMode.KEYWORD,
        match_text="乙方应承担全部|乙方承担一切|甲方不承担任何|乙方单独承担",
        suggestion_text="违约责任表述可能单向倾斜，建议核对是否对双方对等约定。",
        priority=402,
        condition_json='{"scope": "breach"}',
    ),
    SeedRule(
        rule_code="R403_NO_BREACH_CLAUSE",
        rule_name="违约条款缺失",
        risk_level=RiskLevel.HIGH,
        match_mode=MatchMode.MISSING,
        match_text="breach",
        suggestion_text="合同缺少违约责任条款，发生违约时缺少索赔依据，建议补充违约责任约定。",
        priority=403,
    ),
    # ==================== 五、管辖地 ====================
    SeedRule(
        rule_code="R501_JURISDICTION_COUNTERPARTY",
        rule_name="管辖地约定在对方所在地",
        risk_level=RiskLevel.HIGH,
        match_mode=MatchMode.KEYWORD,
        match_text="由乙方所在地|乙方所在地人民法院|乙方住所地法院|由供方所在地",
        suggestion_text="管辖地约定在对方所在地将增加我方维权成本，建议改为我方所在地或双方约定的中立地。",
        priority=501,
        condition_json='{"scope": "dispute"}',
    ),
    SeedRule(
        rule_code="R502_JURISDICTION_FOREIGN",
        rule_name="约定境外仲裁或境外法院",
        risk_level=RiskLevel.HIGH,
        match_mode=MatchMode.KEYWORD,
        match_text="境外仲裁|新加坡仲裁|香港国际仲裁|ICC 仲裁|外国法院",
        suggestion_text="约定境外争议解决机构将大幅提高争议成本，建议改为境内仲裁或法院管辖。",
        priority=502,
        condition_json='{"scope": "dispute"}',
    ),
    SeedRule(
        rule_code="R503_NO_DISPUTE_CLAUSE",
        rule_name="争议解决条款缺失",
        risk_level=RiskLevel.MEDIUM,
        match_mode=MatchMode.MISSING,
        match_text="dispute",
        suggestion_text="合同缺少争议解决条款，建议明确适用的争议解决方式与管辖机构。",
        priority=503,
    ),
    # ==================== 六、主体信息缺失 ====================
    SeedRule(
        rule_code="R601_NO_OUR_PARTY",
        rule_name="我方签约主体缺失",
        risk_level=RiskLevel.HIGH,
        match_mode=MatchMode.MISSING,
        match_text="our_party",
        suggestion_text="未能识别我方签约主体，请确认合同是否完整载明双方名称与统一社会信用代码。",
        priority=601,
    ),
    SeedRule(
        rule_code="R602_NO_COUNTERPARTY",
        rule_name="对方名称缺失",
        risk_level=RiskLevel.HIGH,
        match_mode=MatchMode.MISSING,
        match_text="counterparty",
        suggestion_text="未能识别对方名称，请确认合同相对方主体信息是否完整，避免签约主体不明。",
        priority=602,
    ),
    SeedRule(
        rule_code="R603_NO_CONTRACT_NO",
        rule_name="合同编号缺失",
        risk_level=RiskLevel.LOW,
        match_mode=MatchMode.MISSING,
        match_text="contract_no",
        suggestion_text="合同未载明编号，建议补充以便归档与后续追溯。",
        priority=603,
    ),
    # ==================== 七、金额缺失 ====================
    SeedRule(
        rule_code="R701_NO_AMOUNT",
        rule_name="合同金额缺失",
        risk_level=RiskLevel.HIGH,
        match_mode=MatchMode.MISSING,
        match_text="amount",
        suggestion_text="未能从合同中提取合同金额，请确认是否明确约定价款；金额不明将无法审核预算与付款。",
        priority=701,
    ),
    SeedRule(
        rule_code="R702_NO_CURRENCY",
        rule_name="币种未明确",
        risk_level=RiskLevel.MEDIUM,
        match_mode=MatchMode.MISSING,
        match_text="currency",
        suggestion_text="未能确认结算币种，涉外合同应明确币种及汇率确定方式，避免结算争议。",
        priority=702,
    ),
    # ==================== 八、保密缺失 ====================
    SeedRule(
        rule_code="R801_NO_CONFIDENTIALITY",
        rule_name="保密条款缺失",
        risk_level=RiskLevel.MEDIUM,
        match_mode=MatchMode.MISSING,
        match_text="confidentiality",
        suggestion_text="合同未检出保密条款，涉及商业秘密或客户数据时建议补充保密义务与保密期限。",
        priority=801,
    ),
    SeedRule(
        rule_code="R802_CONFIDENTIALITY_NO_TERM",
        rule_name="保密期限未约定",
        risk_level=RiskLevel.LOW,
        match_mode=MatchMode.KEYWORD,
        match_text="保密义务|保密责任|保密信息",
        suggestion_text="建议明确保密期限（如合同终止后 3 年），避免保密义务期限不明。",
        priority=802,
        condition_json='{"scope": "confidentiality"}',
    ),
    # ==================== 九、数据处理 ====================
    SeedRule(
        rule_code="R901_NO_DATA_CLAUSE",
        rule_name="数据条款缺失",
        risk_level=RiskLevel.MEDIUM,
        match_mode=MatchMode.MISSING,
        match_text="data",
        suggestion_text="合同未检出数据处理相关条款，涉及个人信息或客户数据时建议补充数据处理目的、范围与安全义务。",
        priority=901,
    ),
    SeedRule(
        rule_code="R902_DATA_CROSS_BORDER",
        rule_name="涉及数据出境",
        risk_level=RiskLevel.HIGH,
        match_mode=MatchMode.KEYWORD,
        match_text="数据出境|传输至境外|境外服务器|跨境传输",
        suggestion_text="合同涉及数据出境，需确认是否完成安全评估或标准合同备案等合规程序。",
        priority=902,
        condition_json='{"scope": "full"}',
    ),
    # ==================== 十、知识产权 ====================
    SeedRule(
        rule_code="R1001_NO_IP_CLAUSE",
        rule_name="知识产权条款缺失",
        risk_level=RiskLevel.MEDIUM,
        match_mode=MatchMode.MISSING,
        match_text="intellectual_property",
        suggestion_text="合同未检出知识产权归属约定，涉及定制开发或成果交付时建议明确成果权属。",
        priority=1001,
    ),
    SeedRule(
        rule_code="R1002_IP_ALL_TO_COUNTERPARTY",
        rule_name="知识产权全部归对方",
        risk_level=RiskLevel.HIGH,
        match_mode=MatchMode.KEYWORD,
        match_text="知识产权归乙方所有|成果归乙方|著作权归乙方|专利归乙方",
        suggestion_text="知识产权全部归对方可能导致我方付费却无法使用成果，建议改为共有或我方所有并授予对方使用许可。",
        priority=1002,
        condition_json='{"scope": "intellectual_property"}',
    ),
    # ==================== 十一、验收标准缺失 ====================
    SeedRule(
        rule_code="R1101_NO_ACCEPTANCE_CLAUSE",
        rule_name="验收条款缺失",
        risk_level=RiskLevel.MEDIUM,
        match_mode=MatchMode.MISSING,
        match_text="acceptance",
        suggestion_text="合同未检出验收条款，缺少验收标准将导致交付争议，建议补充验收标准、期限与不合格处理方式。",
        priority=1101,
    ),
    SeedRule(
        rule_code="R1102_ACCEPTANCE_VAGUE",
        rule_name="验收标准表述模糊",
        risk_level=RiskLevel.MEDIUM,
        match_mode=MatchMode.KEYWORD,
        match_text="验收标准由甲方确定|以甲方满意为准|验收合格后另行|视为验收合格",
        suggestion_text="验收标准过于模糊或存在默示验收，建议量化为可检验的指标并明确验收期限。",
        priority=1102,
        condition_json='{"scope": "acceptance"}',
    ),
)


def seed_rule_rows() -> list[dict]:
    """把种子规则转换为可直接插入数据库的行字典。

    返回：
        字典列表，键与 ``review_rules`` 表列名一致。
    关联：
        被 ``scripts/init_db.py`` 调用；插入时按 ``rule_code`` 判重，
        已存在则跳过，保证初始化脚本可重复执行。
    """
    return [
        {
            "rule_code": rule.rule_code,
            "rule_name": rule.rule_name,
            "risk_level": rule.risk_level.value,
            "rule_status": rule.rule_status.value,
            "match_mode": rule.match_mode.value,
            "match_text": rule.match_text,
            "suggestion_text": rule.suggestion_text,
            "priority": rule.priority,
            "condition_json": rule.condition_json,
        }
        for rule in SEED_RULES
    ]
