"""数据库连接模块：创建引擎、会话工厂与声明基类。

职责：
- 依据 ``app.config.settings.db_url`` 创建 SQLAlchemy 引擎；
- 提供 ``session_scope()`` 上下文管理器，统一处理提交与回滚；
- 提供 ``init_engine()`` 与 ``reset_engine()``，使测试可以把数据库指向临时文件。

非职责：
- 不定义业务表（见 ``db/models.py``）；
- 不做业务事务编排（由 ``services/*`` 在需要的地方开启 ``session_scope``）。

设计原因：
- 使用同步 SQLAlchemy 2.x 而非异步：本系统是工具服务，单次审查的耗时集中在 OCR
  （秒级）与外部接口，数据库压力极小，同步会话能显著降低代码复杂度；
- SQLite 需要显式开启外键约束与 ``check_same_thread=False``，否则并发写入报错；
- 会话不设 ``expire_on_commit=False`` 以外的花活，保持提交后对象可读即可。

主要协作模块：
- ``services/*`` 通过 :func:`session_scope` 获得会话；
- ``scripts/init_db.py`` 调用 :func:`init_engine` 与 :func:`create_all`；
- 测试通过环境变量 ``CR_DB_URL`` 指向临时库后调用 :func:`reset_engine`。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    """全部 ORM 模型的声明基类。

    用途：SQLAlchemy 2.x 的声明式基类，集中登记所有表定义。
    协作对象：``db/models.py`` 中的各模型继承本类；``create_all`` 通过本类的
    ``metadata`` 建表。
    """


# 模块级引擎与会话工厂。初始为 None，由 :func:`init_engine` 或首次 :func:`get_engine`
# 创建。做成懒加载是为了让测试能在导入模块后替换数据库地址。
_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    """为每个 SQLite 连接开启外键约束。

    参数：
        dbapi_connection: DBAPI 原始连接，由 SQLAlchemy 事件机制传入。
        _connection_record: 连接池记录，本函数不使用。
    返回：
        无。
    关联：
        仅由 :func:`create_engine_for_url` 在建立 SQLite 引擎时注册。
    设计原因：
        SQLite 默认关闭外键，若不开启，``ON DELETE CASCADE`` 不会生效，
        会导致会话/任务删除后残留孤儿记录。
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_engine_for_url(url: str) -> Engine:
    """按连接串创建引擎。

    参数：
        url: SQLAlchemy 连接串，例如 ``sqlite+pysqlite:///D:/data/app.db``。
    返回：
        配置好的引擎实例。
    关联：
        被 :func:`init_engine` 调用；测试可单独调用以验证建表语句。
    副作用：
        SQLite 场景下注册外键开关事件。
    """
    is_sqlite = url.startswith("sqlite")
    connect_args = {"check_same_thread": False} if is_sqlite else {}
    engine = create_engine(url, connect_args=connect_args, future=True)
    if is_sqlite:
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def init_engine(url: str | None = None) -> Engine:
    """初始化（或重建）全局引擎与会话工厂。

    参数：
        url: 可选连接串；缺省使用 ``settings.db_url``。
    返回：
        新建的引擎。
    关联：
        被 ``scripts/init_db.py``、``app/main.py`` 启动钩子与测试夹具调用；
        初始化后 :func:`session_scope` 才能正常工作。
    """
    global _engine, _session_factory
    _engine = create_engine_for_url(url or settings.db_url)
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def reset_engine() -> None:
    """销毁当前引擎并释放连接池。

    返回：
        无。
    关联：
        测试在切换数据库地址后调用；生产代码不在运行期调用。
    """
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def get_engine() -> Engine:
    """获取当前引擎，未初始化时自动按配置初始化。

    返回：
        全局引擎实例。
    关联：
        被 :func:`create_all` 与 :func:`session_scope` 调用。
    """
    if _engine is None:
        return init_engine()
    return _engine


def create_all() -> None:
    """按模型定义创建所有尚不存在的表。

    返回：
        无。
    关联：
        被 ``scripts/init_db.py`` 与测试夹具调用；不删除已有表，重复调用安全。
    """
    Base.metadata.create_all(bind=get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    """提供一个自动提交/回滚的会话上下文。

    返回：
        生成器，产出 :class:`Session`。
    关联：
        服务层每个业务操作包一层 ``with session_scope() as db:``；
        正常退出提交，异常时回滚并原样抛出，保证异常不被吞掉。
    """
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None  # 上一步保证已初始化，此处仅为类型收窄
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
