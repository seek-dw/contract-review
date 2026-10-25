"""测试共享夹具：临时数据库、模拟审批系统、示例合同。

职责：
- 为每个测试模块提供隔离的数据库与附件目录，避免测试相互污染；
- 按需生成示例合同文件（首次生成后复用，避免每个测试都跑一遍 reportlab）；
- 提供"已拉取任务"的便捷夹具，减少各测试重复写准备代码。

非职责：
- 不做断言（断言在各 ``test_*.py`` 中）。

设计原因：
1. **每个测试函数用独立数据库**。审查链路会写多张表，若共用数据库，
   上一个测试的 ``rule_hits`` 会干扰下一个测试的命中计数。
2. **审批系统用 JSON 文件而不是 mock 对象**。
   这样"回写后能读出来"这类验收点走的是与生产完全相同的代码路径
   （``MockApprovalGateway``），而不是测试专用替身。
3. **示例合同只在会话首次需要时生成**。OCR 与 reportlab 都有开销，
   重复生成会显著拖慢测试。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# 让测试能 import app 包（项目根目录加入 sys.path）。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 会话级临时根目录：存放本次测试运行生成的所有示例合同。
_SESSION_TMP = Path(tempfile.mkdtemp(prefix="cr_tests_"))


@pytest.fixture(scope="session")
def samples_dir() -> Path:
    """生成并返回示例合同目录（会话级，只生成一次）。

    返回：
        含 ``contract_normal.pdf``、``contract_scanned.pdf``、``contract.docx``、
        ``contract_empty.pdf`` 的目录路径。
    关联：
        调用 ``scripts.make_samples.generate_all`` 的各个生成函数；
        被 :func:`app_env` 用于构造模拟审批数据。
    """
    target = _SESSION_TMP / "samples"
    target.mkdir(parents=True, exist_ok=True)

    from scripts.make_samples import (
        generate_docx,
        generate_empty_pdf,
        generate_normal_pdf,
        generate_scanned_pdf,
    )

    if not (target / "contract_normal.pdf").exists():
        generate_normal_pdf(target / "contract_normal.pdf")
    if not (target / "contract_scanned.pdf").exists():
        generate_scanned_pdf(target / "contract_scanned.pdf", target)
    if not (target / "contract.docx").exists():
        generate_docx(target / "contract.docx")
    if not (target / "contract_empty.pdf").exists():
        generate_empty_pdf(target / "contract_empty.pdf")
    return target


@pytest.fixture()
def app_env(tmp_path, monkeypatch, samples_dir):
    """为单个测试准备隔离的运行环境（数据库 + 附件目录 + 模拟审批系统）。

    参数：
        tmp_path: pytest 提供的临时目录（每个测试独立）。
        monkeypatch: pytest 提供的环境变量补丁工具。
        samples_dir: 会话级示例合同目录。
    返回：
        字典，含 ``data_dir``、``store_path``、``db_url``。
    关联：
        测试在调用任何 ``app.*`` 服务前必须先使用本夹具，
        因为它会重置全局引擎（``db.base.reset_engine``）并重新绑定配置。
    副作用：
        设置 ``CR_*`` 环境变量、重置 SQLAlchemy 引擎、建表、灌种子规则。
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    store_path = data_dir / "mock_oa" / "approvals.json"
    db_path = data_dir / "test.db"

    # 用 override_settings 就地替换全局配置，而不是重载模块。
    #
    # 为什么不能重载模块（开发中踩过的坑，值得记下来）：
    #   importlib.reload(app.db.base) 会创建一个**新的** Base 类，
    #   而 app.db.models 里的模型仍继承旧 Base。
    #   于是 Base.metadata.create_all() 用的是新 Base 的空 metadata，
    #   结果是"建表没报错，但表根本不存在"，后续任何查询都报 no such table。
    #   就地替换 settings 对象则没有这个问题：所有模块通过模块属性访问 settings，
    #   拿到的是同一个新对象，且 ORM 类、引擎、会话工厂都不需要重建。
    from app.config import override_settings

    override_settings(
        data_dir=data_dir,
        attachment_dir=data_dir / "attachments",
        sample_dir=samples_dir,
        db_url=f"sqlite+pysqlite:///{db_path.as_posix()}",
        approval_mode="mock",
        mock_oa_store=store_path,
    )

    # 按新 URL 重建引擎（reset 后 init，确保不残留旧连接池）。
    from app.db.base import init_engine, reset_engine

    reset_engine()
    init_engine(f"sqlite+pysqlite:///{db_path.as_posix()}")

    # 构造模拟审批数据（含正常、缺附件、空文档三类审批单）。
    from scripts.make_samples import build_mock_store

    build_mock_store(store_path, samples_dir)

    # 建表 + 灌种子规则。
    from app.db.base import create_all, session_scope
    from app.db.models import ReviewRule
    from app.review.seed_rules import seed_rule_rows

    create_all()
    with session_scope() as db:
        for row in seed_rule_rows():
            db.add(ReviewRule(**row))

    yield {
        "data_dir": data_dir,
        "store_path": store_path,
        "db_url": str(db_path),
        # 同时暴露配置对象，供测试确认"写入的路径"与"网关读取的路径"一致。
        # 早期版本只返回 store_path，测试把新增审批单写进去后网关读不到，
        # 排查了半天才发现是路径不一致——把它显式暴露出来便于断言。
        "settings": __import__("app.config", fromlist=["settings"]).settings,
    }

    # 清理：销毁引擎，释放文件句柄（Windows 上不释放会导致临时目录删不掉）。
    reset_engine()
    from app.approval.factory import set_gateway_override

    set_gateway_override(None)


@pytest.fixture()
def pulled(app_env):
    """准备已拉取待办的环境。

    参数：
        app_env: 隔离环境夹具。
    返回：
        ``(pull_result, tasks_by_code)``：拉取统计与按审批编号索引的任务字典。
    关联：
        调用 ``app.tools.contract_tools.list_pending_contract_approvals``。
    """
    from app.tools import contract_tools

    result = contract_tools.list_pending_contract_approvals(limit=20)
    tasks = {t["approval_code"]: t for t in contract_tools.task_service.list_tasks(limit=100)}
    return result, tasks


@pytest.fixture()
def failing_gateway():
    """构造一个"所有外部调用都失败"的网关替身。

    返回：
        网关实例，``list_pending`` 等方法抛 :class:`ApprovalGatewayError`。
    关联：
        被 ``test_errors.py`` 用于验证"接口调用失败"分支。
    设计原因：
        需求 2.4.4 要求接口失败进入 blocked，这条分支无法靠 mock 网关的自然行为触发
        （mock 网关读写本地文件，不会失败），必须注入替身。
    """
    from app.approval.gateway import ApprovalGateway
    from app.domain.errors import ApprovalGatewayError

    class _FailingGateway(ApprovalGateway):
        """所有方法都抛接口异常的网关，用于验证失败分支。"""

        def list_pending(self, limit: int):
            """模拟待办接口超时。"""
            raise ApprovalGatewayError("模拟故障：待办接口超时")

        def get_approval(self, instance_id: str):
            """模拟详情接口失败。"""
            raise ApprovalGatewayError("模拟故障：详情接口不可用")

        def download_attachment(self, instance_id, external_attachment_id, file_name):
            """模拟附件下载失败。"""
            raise ApprovalGatewayError("模拟故障：附件下载连接被重置")

        def write_comment(self, instance_id: str, comment_text: str):
            """模拟评论写入超时。"""
            raise ApprovalGatewayError("模拟故障：评论接口超时")

        def get_comments(self, instance_id: str):
            """模拟评论读取失败。"""
            raise ApprovalGatewayError("模拟故障：评论读取失败")

    return _FailingGateway()


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """测试会话结束时清理临时目录。

    参数：
        session: pytest 会话对象（未使用）。
        exitstatus: 退出码（未使用）。
    返回：
        无。
    关联：
        由 pytest 自动调用；Windows 上若引擎未释放会清理失败，此处容错忽略。
    """
    shutil.rmtree(_SESSION_TMP, ignore_errors=True)
