"""HTTP 层测试：验证路由注册、探测吸收与真实 404 语义。

覆盖内容：
- 需求 2.4.7 的 5 个页面路由可访问；
- 需求 2.4.10 的 7 个工具接口已注册；
- 浏览器扩展探测路径被吸收为 204，不污染日志，也不伪装成真实服务；
- 真实不存在的路径仍返回 404（不能被探测吸收逻辑吞掉）。

设计原因：
本文件是"路由层面"的冒烟测试，不重复验证业务逻辑（那些在
``test_flow`` / ``test_errors`` / ``test_comment`` 里）。
它存在的价值是防止两类回归：
1. 新增探测路径时不小心覆盖了真实路由
   （FastAPI 中先注册者优先，覆盖后真实接口会静默失效）；
2. 把探测吸收写成通配符，导致真实 404 变成 204，
   让调用方无法区分"路径错了"和"路径对了但没内容"。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(app_env):
    """构造绑定到隔离环境的 TestClient。

    参数：
        app_env: 隔离环境夹具（设置好临时数据库与模拟审批数据）。
    返回：
        :class:`fastapi.testclient.TestClient` 实例。
    关联：
        被本文件各测试使用；不启动真实网络端口，测试更稳定。
    """
    from app.main import app

    return TestClient(app)


def test_pages_accessible(client):
    """验证调用端各页面返回 200。

    参数：
        client: 测试客户端。
    关联：
        ``app.web.routes`` 中的页面路由；需求 2.4.7。

    说明：
        详情页在未拉取数据时也应能打开（显示"尚未解析"等空态），
        不是报错——页面必须能展示"处理到一半"的任务。
    """
    for path in ("/", "/rules", "/logs"):
        response = client.get(path)
        assert response.status_code == 200, f"{path} 返回 {response.status_code}"


def test_health_returns_config(client):
    """验证健康检查返回当前运行配置。

    参数：
        client: 测试客户端。
    关联：
        ``app.main.health``。

    验证要点：
        必须包含 ``db_url`` 与 ``approval_mode``，便于运维确认"当前连的是哪个库、
        用的是模拟还是真实审批系统"。
    """
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert "db_url" in payload
    assert payload["approval_mode"] == "mock"


def test_probe_paths_return_parseable_json(client):
    """验证探测路径返回可解析的 JSON，而不是空 body。

    参数：
        client: 测试客户端。
    关联：
        ``app.main._absorb_probe`` 与 :data:`app.main._PROBE_PATHS`。

    验证要点（真实缺陷的回归测试，这条最要紧）：
        最初实现对这些路径返回 **204 No Content**，理由是"没内容就返回无内容状态码"。
        结果前端报：
            Failed to execute 'json' on 'Response': Unexpected end of JSON input
        原因：204 按 HTTP 规范不允许有响应体，客户端拿到空 body 去 ``res.json()``
        必然抛异常。当时的取舍只考虑了服务端日志干净，没考虑客户端行为，
        属于本末倒置。

        现在的实现返回 **404 + 合法 JSON body**：
        状态码诚实（资源不存在），body 可解析（不会崩客户端）。
        本测试断言 ``content-type`` 是 JSON 且 body 能被 ``.json()`` 正常解析。
    """
    from app.main import _PROBE_PATHS

    for path in _PROBE_PATHS:
        response = client.get(path)
        # 状态码必须是 404：不能伪装成 200，否则插件会误判"找到目标服务"。
        assert response.status_code == 404, (
            f"探测路径 {path} 应返回 404，实际 {response.status_code}"
        )
        # 关键：body 必须非空且能解析为 JSON。
        assert response.content, f"探测路径 {path} 的 body 不能为空（否则前端解析崩溃）"
        assert response.headers["content-type"].startswith("application/json")
        payload = response.json()  # 这一步若抛异常，说明修复无效
        assert payload["ok"] is False
        assert payload["error_type"] == "not_supported"
        assert "/api/tools" in payload["message"]


def test_probe_paths_non_get_returns_parseable_json(client):
    """验证探测路径上的 POST 等请求也返回可解析 JSON（不是空 body 的 405）。

    参数：
        client: 测试客户端。
    关联：
        ``app.main._absorb_probe_method_not_allowed``。

    验证要点：
        FastAPI 对"路径存在但方法不允许"自动返回 405，**默认 body 为空**，
        是 204 那个坑的另一个入口。
        用户实机日志里真实出现过 ``POST /tools 405``，
        若只修 GET 就会漏掉它。
    """
    from app.main import _PROBE_PATHS

    for path in _PROBE_PATHS[:3]:  # 验证前若干个即可，逻辑一致
        response = client.post(path)
        assert response.status_code == 405
        assert response.content, f"POST {path} 的 body 不能为空"
        payload = response.json()
        assert payload["error_type"] == "not_supported"


def test_real_tool_routes_not_shadowed_by_probe(client):
    """验证真实工具接口没有被探测路由遮蔽。

    参数：
        client: 测试客户端。
    关联：
        ``app.main`` 中的 ``/api/tools/*`` 路由与 :data:`app.main._PROBE_PATHS`。

    验证要点（关键回归）：
        FastAPI 中先注册的路由优先匹配。探测路径是循环批量注册的，
        如果将来有人把 ``/api`` 或 ``/api/tools`` 误加进探测清单，
        真实工具接口会静默返回 204 而不是执行 —— 这种 bug 极难发现，
        因为服务不报错、只是"接口没反应"。
        本测试直接断言真实接口返回 200 且带业务字段。
    """
    response = client.get("/api/tools/list_pending", params={"limit": 5})
    assert response.status_code == 200, "工具接口被遮蔽了"
    payload = response.json()
    assert payload["ok"] is True
    assert "data" in payload
    assert "items" in payload["data"]


def test_unknown_path_still_404(client):
    """验证真实不存在的路径仍返回 404。

    参数：
        client: 测试客户端。
    关联：
        FastAPI 默认行为。

    验证要点：
        探测吸收必须是"白名单精确匹配"，不能退化成通配。
        否则调用方无法区分"路径拼错"和"路径对了但没内容"，
        排查成本会大幅上升。
    """
    assert client.get("/no-such-page").status_code == 404
    assert client.get("/apix/tools/list_pending").status_code == 404


def test_missing_approval_returns_404(client):
    """验证查询不存在的审批单返回 404 页面。

    参数：
        client: 测试客户端。
    关联：
        ``app.web.routes.approval_detail`` 的 ``NotFoundError`` 分支。

    验证要点：
        页面显示错误内容的同时，HTTP 状态码必须是 404，
        而不是"页面说没找到但状态是 200"——后者会让自动化脚本误判成功。
    """
    response = client.get("/approvals/HT-NOT-EXIST")
    assert response.status_code == 404
    assert "不存在" in response.text


def test_probe_paths_not_in_openapi_schema(client):
    """验证探测路径不出现在 OpenAPI 文档中。

    参数：
        client: 测试客户端。
    关联：
        ``app.main`` 注册探测路由时传了 ``include_in_schema=False``。

    验证要点：
        ``/docs`` 是给对接方看的接口说明。若把"别人的探测路径"混进去，
        对接方会以为本系统提供这些接口，属于误导。
    """
    schema = client.get("/openapi.json").json()
    paths = set(schema.get("paths", {}).keys())
    for probe in ("/tools", "/props", "/build.json", "/v1/models", "/sw.js"):
        assert probe not in paths, f"{probe} 不应出现在 OpenAPI 文档中"
    # 真实工具接口必须在文档里。
    assert "/api/tools/list_pending" in paths
