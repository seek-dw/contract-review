"""领域枚举模块：定义全系统共享的状态与分类取值。

职责：
- 把"状态字符串"收敛为唯一来源，避免各层各写一份字面量导致比较失效；
- 枚举值直接以字符串保存进数据库，保证数据库内容可读、可人工排查；
- 提供 :func:`coerce` 系列工具，把外部系统传入的松散字符串安全转换为枚举。

非职责：
- 不做状态机校验（状态是否可迁移由 ``services/task_service.py`` 负责）；
- 不携带业务文案（中文展示名由 ``DISPLAY_NAMES`` 单独提供，供调用端渲染）。

主要协作模块：
- ``db/models.py`` 用枚举值作为列默认值与取值约束；
- ``services/*`` 用枚举做状态比较与流转；
- ``web/routes.py`` 用 ``DISPLAY_NAMES`` 渲染中文标签。
"""

from __future__ import annotations

from enum import StrEnum


class TaskStatus(StrEnum):
    """审批任务处理状态。

    用途：驱动整条审查流水线，决定某个任务当前应该执行解析、审查还是等待人工处理。
    取值来源：需求原文 2.4.4「任务状态至少包含 pending、parsing、reviewing、blocked、done」。
    """

    # 已从审批系统拉取，尚未开始解析。初始状态。
    PENDING = "pending"
    # 正在下载附件并解析文档（含 OCR）。
    PARSING = "parsing"
    # 解析完成，正在执行规则审查或已生成结果等待回写。
    REVIEWING = "reviewing"
    # 处理受阻，需要人工介入（附件缺失、OCR 失败、内容为空、接口异常、回写失败）。
    BLOCKED = "blocked"
    # 审查结果已保存且评论已成功回写，闭环完成。
    DONE = "done"


class WriteStatus(StrEnum):
    """审批评论回写状态。

    用途：保证回写幂等——只有 ``NOT_WRITTEN``/``FAILED`` 才允许发起新的写回，
    避免重复往审批系统评论区刷同一份意见。
    取值来源：需求原文 2.4.4「回写状态至少包含 not_written、writing、success、failed」。
    """

    # 从未尝试回写，或任务重试后重置为该状态。
    NOT_WRITTEN = "not_written"
    # 正在调用外部审批系统写评论，作为并发互斥标记使用。
    WRITING = "writing"
    # 外部系统确认写入成功。
    SUCCESS = "success"
    # 写入失败（超时、接口报错、外部拒绝），任务应进入 blocked。
    FAILED = "failed"


class RiskLevel(StrEnum):
    """风险等级。

    用途：既是单条规则的严重度，也是任务整体风险结论。
    取值来源：需求原文 2.4.11「总风险等级：低、中、高」。
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RuleStatus(StrEnum):
    """审查规则启用状态。

    用途：规则管理支持停用而不删除，保留历史命中记录的可追溯性。
    """

    ENABLED = "enabled"
    DISABLED = "disabled"


class MatchMode(StrEnum):
    """规则匹配模式。

    用途：决定规则如何求值，是规则引擎的分支开关。
    - ``KEYWORD``：关键词或正则出现在指定范围内即命中；
    - ``MISSING``：指定字段/条款应存在但缺失时命中；
    - ``THRESHOLD``：从原文抽出数值后与阈值比较，超出即命中。
    """

    KEYWORD = "keyword"
    MISSING = "missing"
    THRESHOLD = "threshold"


class ParseStatus(StrEnum):
    """文档解析状态。

    用途：区分"解析成功但字段有缺失"与"解析过程本身失败"，
    后者必须记录 ``parse_error`` 并让任务进入 blocked。
    """

    SUCCESS = "success"
    FAILED = "failed"
    # 解析成功但部分字段未能提取，仍可进入规则审查（缺失类规则会命中）。
    PARTIAL = "partial"


class ExtractStatus(StrEnum):
    """单个字段的提取状态。

    用途：需求 2.4.5 要求每个解析字段保留"提取状态"。字段没抽到不等于合同没写，
    必须让审核人看到是"未提取到"而不是被误导为"合同中不存在"。
    """

    # 明确提取到值。
    SUCCESS = "success"
    # 定位到了候选文本但无法解析成目标类型（如金额不是数字）。
    FAILED = "failed"
    # 全文未找到任何候选文本。
    NOT_FOUND = "not_found"


class HitStatus(StrEnum):
    """规则命中记录状态。

    用途：人工可以确认或忽略某条命中，保留原始命中记录而不删除。
    """

    ACTIVE = "active"
    IGNORED = "ignored"
    CONFIRMED = "confirmed"


class DownloadStatus(StrEnum):
    """附件下载状态。

    用途：需求 2.4.4 要求附件缺失进入 blocked。附件记录必须先落库再下载，
    这样下载失败也有记录可查、可重试。
    """

    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"


class LogLevel(StrEnum):
    """日志级别。

    用途：全链路日志分级，便于演示时按级别过滤与人工排查。
    """

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class LogType(StrEnum):
    """日志类型（按流水线阶段划分）。

    用途：需求 2.4.8 要求日志模块记录全链路，按阶段打标后可按阶段回放一次任务的处理轨迹。
    """

    PULL = "pull"
    DOWNLOAD = "download"
    PARSE = "parse"
    REVIEW = "review"
    RESULT = "result"
    COMMENT = "comment"
    RETRY = "retry"


# 风险等级由低到高的排序权重。用于把多条命中的等级汇总为整体等级。
# 定义在此处而非规则引擎内部，是为了让调用端也能按同一权重排序展示。
RISK_ORDER: dict[str, int] = {
    RiskLevel.LOW.value: 1,
    RiskLevel.MEDIUM.value: 2,
    RiskLevel.HIGH.value: 3,
}

# 状态与枚举的中文展示名。仅供调用端页面渲染，不参与任何业务判断，
# 因此不放进数据库，避免多语言/改文案时需要迁移数据。
DISPLAY_NAMES: dict[str, str] = {
    TaskStatus.PENDING.value: "待解析",
    TaskStatus.PARSING.value: "解析中",
    TaskStatus.REVIEWING.value: "审查中",
    TaskStatus.BLOCKED.value: "已阻塞",
    TaskStatus.DONE.value: "已完成",
    WriteStatus.NOT_WRITTEN.value: "未回写",
    WriteStatus.WRITING.value: "回写中",
    WriteStatus.SUCCESS.value: "回写成功",
    WriteStatus.FAILED.value: "回写失败",
    RiskLevel.LOW.value: "低",
    RiskLevel.MEDIUM.value: "中",
    RiskLevel.HIGH.value: "高",
    RuleStatus.ENABLED.value: "启用",
    RuleStatus.DISABLED.value: "停用",
    MatchMode.KEYWORD.value: "关键词匹配",
    MatchMode.MISSING.value: "缺失检测",
    MatchMode.THRESHOLD.value: "阈值比较",
    ParseStatus.SUCCESS.value: "解析成功",
    ParseStatus.FAILED.value: "解析失败",
    ParseStatus.PARTIAL.value: "部分成功",
    ExtractStatus.SUCCESS.value: "已提取",
    ExtractStatus.FAILED.value: "提取失败",
    ExtractStatus.NOT_FOUND.value: "未找到",
    HitStatus.ACTIVE.value: "有效",
    HitStatus.IGNORED.value: "已忽略",
    HitStatus.CONFIRMED.value: "已确认",
    DownloadStatus.PENDING.value: "待下载",
    DownloadStatus.SUCCESS.value: "下载成功",
    DownloadStatus.FAILED.value: "下载失败",
}


def display_name(value: str | None) -> str:
    """把枚举值转换为中文展示名。

    参数：
        value: 数据库或接口中的状态字符串，允许为 ``None``。
    返回：
        中文展示名；未登记的取值原样返回，``None`` 返回空字符串。
    关联：
        被 ``web/routes.py`` 与模板过滤器调用，纯展示用途，不影响业务判断。
    """
    if value is None:
        return ""
    return DISPLAY_NAMES.get(value, value)


def max_risk_level(levels: list[str]) -> RiskLevel:
    """从多条规则风险等级中取最高等级。

    参数：
        levels: 风险等级字符串列表，允许为空或含未知值。
    返回：
        最高风险等级；列表为空或全为未知值时返回 ``RiskLevel.LOW``。
    关联：
        被 ``review/rule_engine.py`` 用于汇总整体风险，被 ``services/result_service.py``
        用于二次确认汇总结果，二者共用同一权重口径避免结论不一致。
    """
    best = RiskLevel.LOW
    best_weight = 0
    for level in levels:
        weight = RISK_ORDER.get(level, 0)
        if weight > best_weight:
            best = RiskLevel(level)
            best_weight = weight
    return best
