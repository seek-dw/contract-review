"""配置模块：集中声明运行期配置项、默认值与路径常量。

职责：
- 定义项目根目录、数据目录、样例目录、模板目录等路径；
- 通过环境变量覆盖默认值，使同一份代码可在演示环境与接入环境切换；
- 对外暴露唯一只读配置对象 ``settings``，全项目只从这里取配置。

非职责：
- 不读取数据库中的审查规则、审批接入凭据等业务数据（它们属于业务数据，由数据库与
  外部系统维护）；
- 不对配置值做深度合法性校验，配置错误由首次使用该配置的模块显式报错。

主要协作模块：
- ``app.db.base`` 使用 ``settings.db_url`` 创建引擎；
- ``app.approval.factory`` 使用 ``settings.approval_mode`` 选择审批网关实现；
- ``app.parser.document`` 与 ``app.parser.ocr`` 使用 OCR 与文本判定阈值；
- ``app.services.comment_service`` 使用 ``settings.comment_timeout_seconds``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# 项目根目录：本文件位于 <root>/app/config.py，因此上溯两级。
BASE_DIR = Path(__file__).resolve().parent.parent


def _env_str(name: str, default: str) -> str:
    """读取字符串型环境变量。

    参数：
        name: 环境变量名，统一使用 ``CR_`` 前缀。
        default: 未设置时返回的默认值。
    返回：
        环境变量值或默认值。
    关联：
        仅被 :func:`load_settings` 调用，不直接参与业务逻辑。
    """
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    """读取整型环境变量，非法内容时回退默认值。

    参数：
        name: 环境变量名。
        default: 未设置或无法解析为整数时返回的默认值。
    返回：
        解析后的整数。
    关联：
        仅被 :func:`load_settings` 调用。
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    """读取浮点型环境变量，非法内容时回退默认值。

    参数：
        name: 环境变量名。
        default: 未设置或无法解析为浮点数时返回的默认值。
    返回：
        解析后的浮点数。
    关联：
        仅被 :func:`load_settings` 调用。
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return float(raw)


@dataclass
class Settings:
    """系统运行期配置快照。

    用途：把散落在各模块的路径与阈值集中到一个对象，避免模块各自拼路径。
    生命周期：进程启动时由 :func:`load_settings` 构造一次；
    测试可通过 :func:`override_settings` 就地修改字段（见该函数说明）。
    协作对象：数据库、审批网关工厂、解析器、回写服务均从本对象读取参数。

    为什么不是 frozen：
        各模块普遍使用 ``from app.config import settings`` 的形式，这会在导入时
        把名字绑定到**当时那个对象**。若测试用"替换全局对象"的方式改配置，
        这些模块仍持有旧对象，出现"配置改了但服务读不到"的诡异现象
        （开发中真实踩过：测试写入模拟审批数据后，网关读的是另一个文件）。
        因此改为可变 dataclass，让 :func:`override_settings` **原地修改字段**，
        所有持有引用的模块都能看到新值，无需重载任何模块。
    """    # ---- 路径配置 ----
    # 项目根目录，用于拼装相对路径，避免依赖进程当前工作目录。
    base_dir: Path
    # 运行期数据目录：存放 SQLite 数据库、附件副本、模拟审批系统存储。
    data_dir: Path
    # 示例合同目录：演示脚本从这里取合同文件。
    sample_dir: Path
    # 附件落盘根目录：下载的合同附件按审批编号分目录保存，便于人工核对。
    attachment_dir: Path
    # HTML 模板目录，供 Jinja2 加载调用端页面。
    template_dir: Path
    # 静态资源目录，供 FastAPI 挂载 CSS。
    static_dir: Path

    # ---- 数据库配置 ----
    # SQLAlchemy 连接串；默认使用项目内的 SQLite 文件，便于零依赖演示。
    db_url: str

    # ---- 审批接入配置 ----
    # 审批网关模式：mock=本地模拟审批系统（读 JSON 存储）；http=真实审批系统 HTTP 接口。
    approval_mode: str
    # HTTP 模式下的审批系统根地址，如 https://oa.example.com。
    approval_base_url: str
    # HTTP 模式下的访问令牌，由审批系统颁发；演示环境留空。
    approval_token: str
    # 模拟审批系统的数据文件，模拟网关直接读写该文件以模拟外部系统状态。
    mock_oa_store: Path
    # 调用外部审批接口的超时时间（秒），超时按"接口调用失败"处理并进入 blocked。
    approval_timeout_seconds: float

    # ---- 解析配置 ----
    # 扫描件渲染分辨率：数值越高 OCR 越准但越慢，200 DPI 是中文合同的平衡点。
    ocr_dpi: int
    # OCR 文本块置信度下限，低于该值的识别结果被视为噪声丢弃。
    ocr_min_confidence: float
    # 页面文本层字符数低于该值时判定为扫描页，改走 OCR。
    scanned_page_char_threshold: int
    # 整份文档有效字符数低于该值时判定为"文档内容为空"，任务进入 blocked。
    empty_document_char_threshold: int
    # 单个条款原文片段的截断长度，避免条款 JSON 膨胀。
    clause_snippet_limit: int

    # ---- 回写配置 ----
    # 回写审批评论的超时时间（秒），超时记为 failed 并让任务进入 blocked。
    comment_timeout_seconds: float

    @property
    def db_path(self) -> Path:
        """返回 SQLite 数据库文件路径。

        返回：
            数据库文件路径；当 ``db_url`` 不是 SQLite 时该值无意义，仅供初始化脚本建目录。
        关联：
            被 ``scripts/init_db.py`` 与 ``app.db.base.init_engine`` 用于确保父目录存在。
        """
        return self.data_dir / "contract_review.db"


def load_settings() -> Settings:
    """按环境变量构造配置快照。

    返回：
        新的 :class:`Settings` 实例。
    关联：
        模块底部调用一次得到 ``settings``；测试可用环境变量重定向数据库与数据目录。
    """
    data_dir = Path(_env_str("CR_DATA_DIR", str(BASE_DIR / "data"))).resolve()
    return Settings(
        base_dir=BASE_DIR,
        data_dir=data_dir,
        sample_dir=Path(_env_str("CR_SAMPLE_DIR", str(BASE_DIR / "samples"))).resolve(),
        attachment_dir=data_dir / "attachments",
        template_dir=BASE_DIR / "app" / "web" / "templates",
        static_dir=BASE_DIR / "app" / "web" / "static",
        db_url=_env_str("CR_DB_URL", f"sqlite+pysqlite:///{(data_dir / 'contract_review.db').as_posix()}"),
        approval_mode=_env_str("CR_APPROVAL_MODE", "mock").strip().lower(),
        approval_base_url=_env_str("CR_APPROVAL_BASE_URL", "http://127.0.0.1:8100").rstrip("/"),
        approval_token=_env_str("CR_APPROVAL_TOKEN", ""),
        mock_oa_store=Path(
            _env_str("CR_MOCK_OA_STORE", str(data_dir / "mock_oa" / "approvals.json"))
        ).resolve(),
        approval_timeout_seconds=_env_float("CR_APPROVAL_TIMEOUT_SECONDS", 10.0),
        ocr_dpi=_env_int("CR_OCR_DPI", 200),
        ocr_min_confidence=_env_float("CR_OCR_MIN_CONFIDENCE", 0.5),
        scanned_page_char_threshold=_env_int("CR_SCANNED_PAGE_CHAR_THRESHOLD", 30),
        empty_document_char_threshold=_env_int("CR_EMPTY_DOCUMENT_CHAR_THRESHOLD", 20),
        clause_snippet_limit=_env_int("CR_CLAUSE_SNIPPET_LIMIT", 600),
        comment_timeout_seconds=_env_float("CR_COMMENT_TIMEOUT_SECONDS", 10.0),
    )


# 全局唯一配置对象。业务模块只读取它，不在运行期修改，避免配置漂移。
settings = load_settings()


def override_settings(**changes) -> Settings:
    """在运行期就地修改全局配置（仅供测试使用）。

    参数：
        **changes: 要覆盖的字段名与新值，例如 ``db_url=...``、``data_dir=Path(...)``。
    返回：
        全局 ``settings`` 对象本身（已修改）。
    关联：
        被 ``tests/conftest.py`` 调用，把数据库与目录指向临时位置。
    设计原因：
        最初的做法是重载 ``app.config`` 模块，但重载会产生新的模块对象：
        ``app.db.models`` 里的模型仍继承旧的 ``Base``，而 ``create_all`` 用的是
        新 ``Base`` 的空 metadata，导致"建表成功但表不存在"这种极难排查的问题。

        改成"替换全局对象"也不行：各模块用的是 ``from app.config import settings``，
        名字在导入时就绑定了旧对象，替换全局名字对它们无效。

        最终方案是**原地修改字段**——所有模块持有的是同一个对象引用，
        无论用哪种导入方式都能读到新值，且无需重载、无需重建 ORM 类。
        代价是 Settings 不能再是 frozen，但换来的是可测试性，值得。
    """
    for key, value in changes.items():
        if not hasattr(settings, key):
            raise AttributeError(f"未知的配置项：{key}")
        setattr(settings, key, value)
    return settings
