"""访问日志过滤器测试：验证探测噪音被静默、真实日志不被误吞。

覆盖内容：
- 探测路径的 404/405 记录被丢弃（含 POST，不区分方法）；
- 真实业务请求（200/303/500）的记录必须保留；
- 白名单路径但返回非 404/405 时保留（模拟"将来变成真实路由"）；
- 非白名单路径的 404 保留（这是真实的路径写错，必须看见）；
- 记录结构异常时放行（宁可多打日志，不吞真实记录）；
- 过滤器的路径清单与 ``app.main._PROBE_PATHS`` 保持一致。

设计原因：
本文件是"运维层"的测试，与 ``test_web.py``（HTTP 行为）互补：
- ``test_web.py`` 保证**响应**正确（404 + 合法 JSON）；
- 本文件保证**日志**正确（该静的静，该响的响）。

两者必须都测，因为它们是两个独立的失败面：
响应错了 → 前端崩；日志错了 → 真正的故障被淹没。
本项目两个坑都踩过（见 README 缺陷 15、16）。
"""

from __future__ import annotations

import logging

import pytest

from scripts.access_log_filter import ProbeNoiseFilter, install

# uvicorn 的 access 记录格式：'%s - "%s %s HTTP/%s" %d'
# args 依次是 (客户端地址, 方法, 带查询串的路径, HTTP 版本, 状态码)
_LOG_FORMAT = '%s - "%s %s HTTP/%s" %d'


def _make_record(
    method: str, path: str, status: int, client: str = "127.0.0.1:12345"
) -> logging.LogRecord:
    """构造一条与 uvicorn access 结构相同的日志记录。

    参数：
        method: HTTP 方法。
        path: 带查询串的路径。
        status: 状态码。
        client: 客户端地址。
    返回：
        :class:`logging.LogRecord` 实例。
    关联：
        被本文件各测试用于精确构造输入，不依赖真实 uvicorn 运行。
    """
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=_LOG_FORMAT,
        args=(client, method, path, "1.1", status),
        exc_info=None,
    )


@pytest.fixture()
def probe_filter() -> ProbeNoiseFilter:
    """构造一个默认（非 verbose）的过滤器。

    返回：
        :class:`ProbeNoiseFilter` 实例。
    关联：
        被本文件各测试使用。
    """
    return ProbeNoiseFilter(verbose=False)


def test_probe_get_requests_are_suppressed(probe_filter):
    """验证探测路径的 GET 404 记录被丢弃。

    参数：
        probe_filter: 过滤器实例。
    关联：
        ``ProbeNoiseFilter.filter``。

    验证要点：
        这是最基础的一条——用户日志里刷屏的就是这类记录。
    """
    for path in ("/tools", "/props", "/build.json", "/v1/models", "/sw.js"):
        record = _make_record("GET", path, 404)
        assert probe_filter.filter(record) is False, f"{path} 应被静默"


def test_probe_post_requests_are_suppressed(probe_filter):
    """验证探测路径的 POST 405 记录也被丢弃（不区分方法）。

    参数：
        probe_filter: 过滤器实例。

    验证要点（真实缺陷的回归测试）：
        初版只静默 GET/HEAD，理由是"POST 可能是真实接口误调用"。
        但用户实机日志里 ``POST /tools 405``、``POST /props 405`` 仍在刷屏，
        问题只解决一半。
        更关键的是那个理由站不住：真实接口都在 ``/api/tools/*`` 下，
        顶层 ``/tools`` 不存在任何真实路由，对它发 POST 只可能是探测。
    """
    for path in ("/tools", "/props", "/v1/models"):
        record = _make_record("POST", path, 405)
        assert probe_filter.filter(record) is False, f"POST {path} 应被静默"


def test_probe_path_with_query_string_suppressed(probe_filter):
    """验证带查询串的探测路径也能被正确识别。

    参数：
        probe_filter: 过滤器实例。

    验证要点：
        实机日志里出现过 ``/props?autoload=false``。
        uvicorn 记录的是带查询串的路径，过滤器必须先切掉 ``?`` 再比对，
        否则这类记录会漏过。
    """
    record = _make_record("GET", "/props?autoload=false", 404)
    assert probe_filter.filter(record) is False


def test_real_business_requests_are_kept(probe_filter):
    """验证真实业务请求的记录不被吞掉。

    参数：
        probe_filter: 过滤器实例。

    验证要点（最关键的一条）：
        过滤器的目的是"去噪"，绝不能变成"把真实日志也吞了"。
        一旦吞掉真实日志，故障就完全不可见，比刷屏更危险。
    """
    cases = [
        ("GET", "/", 200),
        ("GET", "/rules", 200),
        ("GET", "/logs", 200),
        ("GET", "/health", 200),
        ("GET", "/approvals/HT-2024-0001", 200),
        ("POST", "/actions/pull", 303),
        ("POST", "/actions/run", 303),
        ("GET", "/api/tools/list_pending?limit=5", 200),
        ("POST", "/api/tools/download", 200),
    ]
    for method, path, status in cases:
        record = _make_record(method, path, status)
        assert probe_filter.filter(record) is True, f"{method} {path} 不应被静默"


def test_unknown_path_404_is_kept(probe_filter):
    """验证非白名单路径的 404 记录被保留。

    参数：
        probe_filter: 过滤器实例。

    验证要点：
        路径写错（例如把 ``/api`` 写成 ``/apix``）是真实问题，
        必须出现在日志里。过滤器只认白名单，不做通配。
    """
    for path in ("/no-such-page", "/apix/tools/list_pending", "/admin"):
        record = _make_record("GET", path, 404)
        assert probe_filter.filter(record) is True, f"{path} 的 404 应保留"


def test_probe_path_with_non_404_status_is_kept(probe_filter):
    """验证白名单路径返回非 404/405 时记录被保留。

    参数：
        probe_filter: 过滤器实例。

    验证要点（面向未来）：
        如果将来把 ``/favicon.ico`` 做成了真实路由（返回 200），
        它的访问记录必须出现在日志里，而不是继续被静默。
        判定条件里带上状态码就是为了这个：路径相同但语义变了，
        行为也要跟着变。
    """
    record = _make_record("GET", "/favicon.ico", 200)
    assert probe_filter.filter(record) is True

    record_500 = _make_record("GET", "/tools", 500)
    assert probe_filter.filter(record_500) is True


def test_malformed_record_is_kept(probe_filter):
    """验证记录结构异常时放行。

    参数：
        probe_filter: 过滤器实例。

    验证要点：
        如果 uvicorn 改了日志格式（args 不再是 5 元组），
        过滤器必须放行而不是崩溃或静默丢弃。
        "宁可多打日志，不要吞真实记录"是这里的最高原则。
    """
    bad = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="some other format",
        args=("only", "two"),
        exc_info=None,
    )
    assert probe_filter.filter(bad) is True


def test_suppressed_counter(probe_filter):
    """验证静默计数被正确累加。

    参数：
        probe_filter: 过滤器实例。

    验证要点：
        ``scripts/serve.py`` 退出时依赖这个计数打印
        "本次运行共静默 N 条"——让人确认过滤器确实工作了，
        而不是怀疑日志系统坏了。
    """
    assert probe_filter.suppressed == 0
    probe_filter.filter(_make_record("GET", "/tools", 404))
    probe_filter.filter(_make_record("POST", "/props", 405))
    probe_filter.filter(_make_record("GET", "/", 200))  # 这条不计数
    assert probe_filter.suppressed == 2


def test_verbose_mode_keeps_record_with_marker():
    """验证 verbose 模式只标注不丢弃。

    返回：
        无。
    关联：
        ``ProbeNoiseFilter.__init__(verbose=True)``。

    验证要点（真实踩坑的回归测试）：
        标记必须加在 ``record.args[0]``（客户端地址）上，**不能改 record.msg**。
        原因：uvicorn 的 access formatter 是 ``AccessFormatter``，它的格式串是
            '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'
        —— 完全不用 ``%(message)s``，而是直接读 ``record.args``。
        因此改 msg 对 access 日志毫无作用（实测输出完全没变）。

        本测试断言 args[0] 带上了标记，且原 msg 与 args 长度都没变
        （保证格式化不会因占位符数量不匹配而抛异常）。
    """
    verbose_filter = ProbeNoiseFilter(verbose=True)
    record = _make_record("GET", "/tools", 404)
    original_msg = record.msg
    original_argc = len(record.args)

    assert verbose_filter.filter(record) is True

    assert str(record.args[0]).startswith("[已静默]"), "标记应加在 args[0]"
    # 其余字段原样保留，保证 AccessFormatter 能正常渲染
    assert record.args[1] == "GET"
    assert record.args[2] == "/tools"
    assert record.args[4] == 404
    # msg 与参数个数都不动：否则 getMessage() 会抛格式化异常，日志反而丢失
    assert record.msg == original_msg
    assert len(record.args) == original_argc
    assert verbose_filter.suppressed == 1


def test_install_is_idempotent():
    """验证重复安装不会叠加多个过滤器。

    返回：
        无。
    关联：
        ``scripts.access_log_filter.install``。

    验证要点：
        测试或热重载场景下可能多次调用 ``install``。
        若叠加多个过滤器，``suppressed`` 统计会翻倍，且性能略降。
    """
    access_logger = logging.getLogger("uvicorn.access")
    before = len([f for f in access_logger.filters if f.name == "probe-noise-filter"])

    install()
    install()
    install()

    after = [f for f in access_logger.filters if f.name == "probe-noise-filter"]
    assert len(after) == 1, "同名过滤器应只存在一个"
    assert before in (0, 1)

    # 清理，避免影响其他测试的日志行为
    for f in after:
        access_logger.removeFilter(f)


def test_probe_paths_match_app_layer():
    """验证运维层的路径清单与应用层保持一致。

    返回：
        无。
    关联：
        ``scripts.access_log_filter._PROBE_PATHS`` 与
        ``app.main._PROBE_PATHS``。

    验证要点（防漂移）：
        这两份清单是刻意重复的（运维层不 import 应用层，避免依赖倒挂）。
        代价是可能不同步：应用层新增了探测路径，但日志层没跟上，
        结果就是"响应处理了，日志还在刷"。
        本测试就是那个同步检查点。
    """
    from app.main import _PROBE_PATHS as app_paths
    from scripts.access_log_filter import _PROBE_PATHS as log_paths

    assert set(app_paths) == set(log_paths), (
        "应用层与日志层的探测路径清单不一致。"
        f"仅在应用层：{set(app_paths) - set(log_paths)}；"
        f"仅在日志层：{set(log_paths) - set(app_paths)}"
    )
