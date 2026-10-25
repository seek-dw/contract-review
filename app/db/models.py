"""ORM 模型模块：定义需求 2.4.9 要求的 8 张业务表。

职责：
- 把需求原文的字段逐项落地为 SQLAlchemy 2.x 映射类；
- 声明唯一约束、索引、外键级联，保证去重、去孤儿、可回查；
- 为每个字段写清用途与"为什么这样定义"。

非职责：
- 不写业务查询语句（分散在 ``services/*``，便于就近理解上下文）；
- 不写状态流转校验（见 ``services/task_service.py``）。

设计原则：
1. **原文优先**：字段名与需求原文保持一致，便于对照验收；确需补充的字段在注释中标「扩展」。
2. **快照留痕**：``rule_hits`` 保存命中时的规则名称/等级/建议快照。规则是可变配置，
   若只存 ``rule_id``，日后改规则会让历史命中结论失真，无法满足"命中结果可回查"。
3. **JSON 列用 Text**：SQLite 对 JSON 支持有限且不利于跨库迁移，统一用 Text 存
   JSON 字符串，由服务层用 ``json.dumps/loads`` 转换，并在注释中写明结构。
4. **时间戳统一 UTC**：``created_at``/``updated_at`` 存 UTC 时间，
   避免服务器时区变化导致时间线错乱。

主要协作模块：
- ``db/base.py`` 提供 :class:`Base` 与建表能力；
- ``services/task_service.py`` 写 ``approval_tasks``；
- ``services/attachment_service.py`` 写 ``approval_attachments``；
- ``services/parse_service.py`` 写 ``contract_parses``；
- ``services/review_service.py`` 写 ``rule_hits``；
- ``services/result_service.py`` 写 ``review_results``；
- ``services/comment_service.py`` 写 ``comment_logs``；
- ``services/log_service.py`` 写 ``task_logs``；
- ``review/seed_rules.py`` 初始化 ``review_rules``。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.domain.enums import (
    DownloadStatus,
    HitStatus,
    LogLevel,
    MatchMode,
    ParseStatus,
    RiskLevel,
    RuleStatus,
    TaskStatus,
    WriteStatus,
)


def utc_now() -> datetime:
    """返回当前 UTC 时间（不带时区信息，便于 SQLite 存储与比较）。

    返回：
        当前 UTC ``datetime``。
    关联：
        作为各表 ``created_at``/``updated_at`` 的默认值函数；服务层更新记录时
        显式调用它刷新 ``updated_at``。
    """
    return datetime.now(UTC).replace(tzinfo=None)


class ApprovalTask(Base):
    """审批任务表：一份待审查合同对应一条任务。

    用途：审查流水线的主表，串起附件、解析、命中、结果、回写与日志。
    生命周期：拉取时创建（``pending``），随解析/审查推进，最终 ``done`` 或 ``blocked``。
    协作对象：``ApprovalAttachment``、``ContractParse``、``RuleHit``、``ReviewResult``、
    ``CommentLog``、``TaskLog`` 均以本表 ``id`` 为外键。

    去重约束：``approval_code`` 建唯一索引。需求 2.4.4 要求"每个审批单按唯一业务标识去重，
    重复拉取时只更新已有记录，不重复创建新任务"，唯一索引是这条规则的最后一道保障——
    即使服务层判重逻辑出现并发竞争，数据库也会拒绝重复插入。
    """

    __tablename__ = "approval_tasks"

    # 主键，自增整数。选整数而非 UUID：单库单实例场景下自增主键更省空间，
    # 且便于演示时按创建顺序阅读。
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 审批单业务唯一标识，来自审批系统的 instance_id / 审批编号。
    # 定义原因：它是"去重"的依据，也是调用外部接口的入参，必须有唯一约束。
    approval_code: Mapped[str] = mapped_column(String(128), nullable=False)

    # 审批单标题，通常即合同名称。用于待办列表展示。
    approval_title: Mapped[str] = mapped_column(String(512), nullable=False, default="")

    # 申请人姓名。需求 2.4.3 要求待办列表返回"申请人"。
    applicant_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    # 任务处理状态，取值见 :class:`TaskStatus`。
    # 定义原因：驱动流水线分支，需求 2.4.4 强制的五态。
    task_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=TaskStatus.PENDING.value
    )

    # 评论回写状态，取值见 :class:`WriteStatus`。
    # 定义原因：需求 2.4.4 要求回写状态独立于任务状态，因为审查完成与回写成功是两个里程碑。
    write_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=WriteStatus.NOT_WRITTEN.value
    )

    # ---- 以下为扩展字段，原文未列出但被其他条款间接要求 ----

    # 申请时间。需求 2.4.3 要求待办列表返回"申请时间"，
    # 该时间由审批系统给出，与本地 created_at（拉取时间）语义不同，必须分开存。
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 附件数量。需求 2.4.3 要求待办列表返回"附件数量"。
    # 冗余存储而非每次 count 查询：待办列表是高频展示页，避免 N+1 查询。
    attachment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 阻塞原因代码，取自 ``app.domain.errors`` 中各异常的 ``reason``。
    # 需求 2.4.4 要求 blocked 任务可人工重试，必须先能说明"为什么被阻塞"。
    blocked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 阻塞明细说明（中文），包含具体页码、文件名等人工排查所需信息。
    blocked_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 重试目标状态（``parsing`` 或 ``reviewing``）。
    # 定义原因：不同阻塞原因的重试起点不同——解析失败要重新解析，回写失败只需重新回写。
    # 把该意图在阻塞时就记录下，重试逻辑无需重新推断。
    retry_target: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # 拉取时间（本地时间）。
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)

    # 最后更新时间，任何字段变更时由服务层显式刷新。
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now, onupdate=utc_now
    )

    # 关联集合。cascade="all, delete-orphan" 保证删除任务时子记录一并删除，
    # 避免残留孤儿数据影响统计与回查。
    attachments: Mapped[list[ApprovalAttachment]] = relationship(
        back_populates="task", cascade="all, delete-orphan", passive_deletes=True
    )
    parses: Mapped[list[ContractParse]] = relationship(
        back_populates="task", cascade="all, delete-orphan", passive_deletes=True
    )
    rule_hits: Mapped[list[RuleHit]] = relationship(
        back_populates="task", cascade="all, delete-orphan", passive_deletes=True
    )
    results: Mapped[list[ReviewResult]] = relationship(
        back_populates="task", cascade="all, delete-orphan", passive_deletes=True
    )
    comment_logs: Mapped[list[CommentLog]] = relationship(
        back_populates="task", cascade="all, delete-orphan", passive_deletes=True
    )
    task_logs: Mapped[list[TaskLog]] = relationship(
        back_populates="task", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        # 业务唯一标识唯一索引：去重的数据库级保障，同时加速按审批编号查询。
        UniqueConstraint("approval_code", name="uq_approval_tasks_code"),
        # 待办列表按状态 + 创建时间排序，建立复合索引避免全表扫描。
        Index("ix_approval_tasks_status_created", "task_status", "created_at"),
    )


class ApprovalAttachment(Base):
    """审批附件表：审批单下的合同文件及其本地副本信息。

    用途：记录每次附件下载的结果，是解析工具的输入（``document_id`` 即本表主键）。
    生命周期：拉取时按审批系统给出的附件清单预建记录（``pending``），
    下载成功后补全路径与校验信息（``success``），失败则记 ``failed`` 并说明原因。
    协作对象：``ApprovalTask`` 为父；``ContractParse`` 通过 ``attachment_id`` 回指本表。

    设计原因：需求 2.4.4 要求"附件缺失"进入 blocked。若只在下载成功后落库，
    下载失败就没有任何记录，既无法展示"缺了哪个附件"，也无法在重试时复用同一记录。
    """

    __tablename__ = "approval_attachments"

    # 主键，同时作为工具接口 ``parse_contract_document(document_id)`` 的入参。
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 所属任务。
    task_id: Mapped[int] = mapped_column(
        ForeignKey("approval_tasks.id", ondelete="CASCADE"), nullable=False
    )

    # 文件名（含扩展名），用于识别格式与展示。
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)

    # 文件类型：``pdf`` / ``docx`` / ``png`` / ``jpg`` 等，取自扩展名小写形式。
    # 定义原因：解析器按类型分派读取策略，避免运行时反复推断。
    file_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    # 本地绝对路径。未下载成功时为空字符串，不允许存相对路径——
    # 相对路径依赖进程工作目录，重启后可能失效。
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False, default="")

    # 下载状态，取值见 :class:`DownloadStatus`。
    download_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=DownloadStatus.PENDING.value
    )

    # 下载时间。
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)

    # ---- 扩展字段 ----

    # 外部附件编号。重试下载时用它向审批系统重新请求同一附件，
    # 与本地主键分开是因为本地 id 无法反查外部系统。
    external_attachment_id: Mapped[str] = mapped_column(
        String(128), nullable=False, default=""
    )

    # 文件字节数。需求 2.4.3 要求下载工具返回"校验信息"，大小是最基础的校验项。
    file_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 文件 SHA256。用于验证本地副本与外部系统一致，也便于人工确认没有重复下载。
    checksum: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    # 下载失败原因（中文）。与 download_status 配合，说明"为什么失败"。
    download_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 父任务反向引用。
    task: Mapped[ApprovalTask] = relationship(back_populates="attachments")

    __table_args__ = (
        # 同一任务内外部附件编号唯一：防止重复拉取时把同一附件插成两条。
        UniqueConstraint(
            "task_id", "external_attachment_id", name="uq_attachment_task_external"
        ),
    )


class ContractParse(Base):
    """合同解析表：一次解析的完整产物。

    用途：保存结构化字段与条款抽取结果，是规则审查的输入，也是法务查看证据的来源。
    生命周期：每次解析追加一条新记录（保留历史），读取时取该任务最新一条。
    协作对象：``ApprovalTask`` 为父；``services/review_service.py`` 读取最新记录。

    设计原因：需求 2.4.5 要求"解析失败时，要记录失败原因，不允许只返回空结果"，
    因此 ``parse_status`` 与 ``parse_error`` 是必填语义；采用"追加新记录"而非原地更新，
    是为了让"重试后成功"的场景同时保留失败历史，便于排查文件质量问题。
    """

    __tablename__ = "contract_parses"

    # 主键。
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 所属任务。
    task_id: Mapped[int] = mapped_column(
        ForeignKey("approval_tasks.id", ondelete="CASCADE"), nullable=False
    )

    # 基础信息 JSON。结构：``{字段键: {"label","value","snippet","location","status","detail"}}``。
    # 定义原因：字段集合会随业务扩展，用 JSON 避免每次加字段都要改表结构。
    basic_info_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    # 条款信息 JSON，结构同 ``basic_info_json``，键取自 8 类条款。
    clause_info_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    # 解析状态，取值见 :class:`ParseStatus`。
    # 默认 success 而非某个"未解析"值：本表只在解析动作结束后写入，
    # 未解析状态由 ``approval_tasks.task_status`` 表达，此处不重复建模。
    parse_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ParseStatus.SUCCESS.value
    )

    # 解析失败原因（中文）。成功时为空字符串；失败时必须写明原因，不允许留空。
    parse_error: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # 解析时间。
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)

    # ---- 扩展字段 ----

    # 解析来源附件，便于回溯"这份结果是从哪个文件解析出来的"。
    attachment_id: Mapped[int | None] = mapped_column(
        ForeignKey("approval_attachments.id", ondelete="SET NULL"), nullable=True
    )

    # 解析元信息 JSON：页数、OCR 页列表、文本层页列表、耗时、字符数。
    # 定义原因：需求 2.4.4 要求区分"图片无法识别"与"内容为空"，
    # 排查这两类问题必须知道哪些页走了 OCR、识别出多少字符。
    parse_meta_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    # 父任务反向引用。
    task: Mapped[ApprovalTask] = relationship(back_populates="parses")

    __table_args__ = (
        # 读取"最新一次解析"按 task_id + id 倒序，建立复合索引。
        Index("ix_contract_parses_task_id", "task_id", "id"),
    )


class ReviewRule(Base):
    """审查规则表：可配置的合同风险规则。

    用途：规则引擎的配置来源，支持启停用、优先级、三种匹配模式。
    生命周期：由种子脚本初始化，管理员通过规则管理接口维护。
    协作对象：``RuleHit`` 以本表 ``id`` 为外键；``review/rule_engine.py`` 读取启用规则。

    设计原因：需求 2.4.6 明确列出 8 个必需字段，本表全部保留原名；
    额外增加 ``priority`` 与 ``condition_json``，原因是：
    - ``priority``：同类规则（如多条金额类规则）需要稳定的求值顺序，否则命中顺序随
      插入顺序变化，结论展示不稳定；
    - ``condition_json``：threshold 型规则需要结构化阈值（字段、运算符、比较值），
      仅靠 ``match_text`` 字符串无法可靠解析。
    """

    __tablename__ = "review_rules"

    # 主键。
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 规则编码，业务唯一（如 ``R001_ADVANCE_PAYMENT_RATIO``），便于日志与文档引用。
    rule_code: Mapped[str] = mapped_column(String(64), nullable=False)

    # 规则名称（中文），直接展示给审批人。
    rule_name: Mapped[str] = mapped_column(String(256), nullable=False)

    # 风险等级，取值见 :class:`RiskLevel`。命中该规则时采用此等级。
    risk_level: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RiskLevel.MEDIUM.value
    )

    # 启用状态，取值见 :class:`RuleStatus`。停用的规则不参与审查，但保留历史命中。
    rule_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RuleStatus.ENABLED.value
    )

    # 匹配模式，取值见 :class:`MatchMode`：keyword / missing / threshold。
    match_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default=MatchMode.KEYWORD.value
    )

    # 匹配文本。语义随 ``match_mode`` 变化：
    # - keyword：以 ``|`` 分隔的关键词或正则集合；
    # - missing：目标字段键或条款键（如 ``confidentiality``）；
    # - threshold：给人看的阈值描述（如 ``预付款比例>30%``），实际比较用 condition_json。
    match_text: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # 建议说明：命中后给审批人的处理建议，写入回写文本。
    suggestion_text: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # 最后更新时间。规则可改，必须留痕。
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now, onupdate=utc_now
    )

    # ---- 扩展字段 ----

    # 求值优先级，数值小的先求值。默认 100，便于在中间插入新规则。
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)

    # 结构化条件 JSON，供 threshold 模式使用。
    # 结构：``{"scope":"payment","extract":"percent","operator":">","value":30}``
    # - scope：在哪个条款/全文范围抽取；
    # - extract：抽取目标类型（percent / amount / days）；
    # - operator：比较运算符（> >= < <= == !=）；
    # - value：比较值。
    condition_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    # 规则创建时间。原文未要求，但规则管理页需要展示"何时新增"，便于审计。
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)

    __table_args__ = (
        # 规则编码唯一：避免同一编码出现两条定义，导致日志引用歧义。
        UniqueConstraint("rule_code", name="uq_review_rules_code"),
        # 审查时按"启用 + 优先级"过滤排序，建立复合索引。
        Index("ix_review_rules_status_priority", "rule_status", "priority"),
    )


class RuleHit(Base):
    """规则命中表：某条规则在某个任务上的命中记录。

    用途：需求 2.4.6 要求的"命中结果"落库位置，含证据文本与证据位置。
    生命周期：每次审查先清理该任务的旧命中再重新写入（避免重复审查导致命中翻倍），
    人工确认/忽略只改 ``hit_status``，不删除记录。
    协作对象：``ApprovalTask`` 与 ``ReviewRule`` 为双父。

    设计原因：除 ``rule_id`` 外额外保存规则快照字段（见下），
    因为规则是可编辑配置，历史命中必须能还原"当时命中的是哪条规则、什么等级、什么建议"。
    """

    __tablename__ = "rule_hits"

    # 主键。
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 所属任务。
    task_id: Mapped[int] = mapped_column(
        ForeignKey("approval_tasks.id", ondelete="CASCADE"), nullable=False
    )

    # 命中的规则主键。规则被删除时置空（SET NULL），但快照字段仍在，历史不丢。
    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("review_rules.id", ondelete="SET NULL"), nullable=True
    )

    # 命中证据：从合同原文摘取的片段，原样保存不改写，供法务核对。
    evidence_text: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # 证据位置，形如 ``第2页第15行``；全文检索命中时为 ``全文检索``。
    evidence_position: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    # 命中状态，取值见 :class:`HitStatus`：active / ignored / confirmed。
    hit_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=HitStatus.ACTIVE.value
    )

    # 命中时间。
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)

    # ---- 扩展字段：规则快照 ----

    # 命中时的规则编码快照。
    rule_code_snapshot: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # 命中时的规则名称快照，直接用于结果展示，避免关联查询与规则改名导致的展示漂移。
    rule_name_snapshot: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    # 命中时的风险等级快照。
    risk_level_snapshot: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    # 命中时的建议说明快照。
    suggestion_snapshot: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # 父任务反向引用。
    task: Mapped[ApprovalTask] = relationship(back_populates="rule_hits")

    __table_args__ = (
        # 按任务查询命中列表是最高频操作。
        Index("ix_rule_hits_task_id", "task_id", "id"),
    )


class ReviewResult(Base):
    """审查结果表：一次审查的最终结论。

    用途：需求 2.4.11 规定的输出落库位置，也是回写工具 ``write_approval_comment``
    的入参来源（``review_id`` 即本表主键）。
    生命周期：每次审查追加一条；回写成功后任务置 ``done``。
    协作对象：``ApprovalTask`` 为父；``services/result_service.py`` 写入，
    ``services/comment_service.py`` 读取。
    """

    __tablename__ = "review_results"

    # 主键，同时是 ``write_approval_comment(instance_id, review_id)`` 的入参。
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 所属任务。
    task_id: Mapped[int] = mapped_column(
        ForeignKey("approval_tasks.id", ondelete="CASCADE"), nullable=False
    )

    # 总风险等级，取值见 :class:`RiskLevel`。
    overall_risk_level: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RiskLevel.LOW.value
    )

    # 中文摘要：对整体合同风险的总结。
    summary_text: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # 审批关注点 JSON 数组，元素为中文字符串。
    focus_points_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    # 最终写入审批评论区的文本。单独存一份的原因：
    # 回写内容会随模板调整变化，存下"实际写出去的内容"才能与外部系统对账。
    comment_text: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # 生成时间。
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)

    # 父任务反向引用。
    task: Mapped[ApprovalTask] = relationship(back_populates="results")

    __table_args__ = (
        # 取"最新结果"按 task_id + id 倒序。
        Index("ix_review_results_task_id", "task_id", "id"),
    )


class CommentLog(Base):
    """评论回写日志表：每次回写尝试的完整记录。

    用途：需求 2.4.4 要求回写状态可查，需求 2.4.8 要求回写模块保存回写日志。
    生命周期：每次调用回写追加一条，成功与失败都记，形成重试历史。
    协作对象：``ApprovalTask`` 为父；``services/comment_service.py`` 写入。

    设计原因：与 ``approval_tasks.write_status`` 的分工是——
    任务表存"当前状态"（单值），本表存"全部尝试历史"（多值）。二者不可互相替代。
    """

    __tablename__ = "comment_logs"

    # 主键。
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 所属任务。
    task_id: Mapped[int] = mapped_column(
        ForeignKey("approval_tasks.id", ondelete="CASCADE"), nullable=False
    )

    # 本次尝试的回写状态，取值见 :class:`WriteStatus`。
    write_status: Mapped[str] = mapped_column(String(32), nullable=False)

    # 外部系统返回的原始响应文本（或错误说明），用于排查对接问题。
    write_response_text: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # 尝试时间。
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)

    # ---- 扩展字段 ----

    # 本次回写对应的结果记录，便于从回写日志直接跳到结论内容。
    review_result_id: Mapped[int | None] = mapped_column(
        ForeignKey("review_results.id", ondelete="SET NULL"), nullable=True
    )

    # 父任务反向引用。
    task: Mapped[ApprovalTask] = relationship(back_populates="comment_logs")

    __table_args__ = (Index("ix_comment_logs_task_id", "task_id", "id"),)


class TaskLog(Base):
    """任务日志表：全链路处理日志。

    用途：需求 2.4.8 要求日志模块记录全链路日志和错误信息；
    演示时可据此回放一个任务从拉取到回写的完整轨迹。
    生命周期：流水线各阶段追加写入，只增不改。
    协作对象：``services/log_service.py`` 是唯一写入方。
    """

    __tablename__ = "task_logs"

    # 主键。
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 所属任务。允许为空：拉取阶段可能还没有任务记录（例如拉取接口整体失败）。
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("approval_tasks.id", ondelete="CASCADE"), nullable=True
    )

    # 日志级别，取值见 :class:`LogLevel`。
    log_level: Mapped[str] = mapped_column(
        String(16), nullable=False, default=LogLevel.INFO.value
    )

    # 日志类型（阶段），取值见 :class:`LogType`。
    log_type: Mapped[str] = mapped_column(String(16), nullable=False, default="")

    # 日志内容（中文描述，含关键参数）。
    log_content: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # 记录时间。
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)

    # 父任务反向引用。
    task: Mapped[ApprovalTask | None] = relationship(back_populates="task_logs")

    __table_args__ = (
        # 按任务 + 时间正序回放时间线。
        Index("ix_task_logs_task_id", "task_id", "id"),
    )


# 供 ``scripts/init_db.py`` 与测试引用的表清单，避免各处重复列举。
ALL_MODELS: tuple[type[Base], ...] = (
    ApprovalTask,
    ApprovalAttachment,
    ContractParse,
    ReviewRule,
    RuleHit,
    ReviewResult,
    CommentLog,
    TaskLog,
)
