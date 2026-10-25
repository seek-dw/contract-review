"""审批网关工厂：按配置返回具体网关实现。

职责：
- 读取 ``settings.approval_mode``，返回 ``mock`` 或 ``http`` 网关；
- 集中校验模式取值，配置写错时立即报错，而不是运行到一半才失败。

非职责：
- 不缓存网关实例（网关无状态，每次创建成本可忽略，且便于测试注入不同配置）。

主要协作模块：
- ``services/*`` 通过 :func:`get_gateway` 获取网关，不直接 import 具体实现类；
- 测试通过 :func:`set_gateway_override` 注入替身，验证失败分支。

设计原因：
需求要求"演示"与"真实接入"明确区分。工厂是唯一知道"当前用哪个实现"的地方，
其余代码只依赖接口，这样切换接入方式不需要改动业务逻辑。
"""

from __future__ import annotations

from app.approval.gateway import ApprovalGateway
from app.approval.http_gateway import HttpApprovalGateway
from app.approval.mock_gateway import MockApprovalGateway
from app.config import settings

# 测试注入的网关替身。非 None 时 :func:`get_gateway` 优先返回它。
# 定义原因：测试需要模拟"接口超时""返回结构错误"等场景，
# 若只能靠改环境变量，无法在同一进程内对不同用例使用不同替身。
_override: ApprovalGateway | None = None


def set_gateway_override(gateway: ApprovalGateway | None) -> None:
    """设置或清除网关替身。

    参数：
        gateway: 替身实例；传 ``None`` 清除，恢复按配置创建。
    返回：
        无。
    关联：
        仅供测试与演示脚本使用；测试夹具在 ``setup`` 中设置、``teardown`` 中清除。
    """
    global _override
    _override = gateway


def get_gateway() -> ApprovalGateway:
    """按配置获取审批网关实例。

    返回：
        网关实现；存在测试替身时返回替身。
    关联：
        被 ``services/task_service.py``、``services/attachment_service.py``、
        ``services/comment_service.py`` 调用。
    异常：
        ``settings.approval_mode`` 不是 ``mock``/``http`` 时抛 ``ValueError``，
        这是配置错误，不应被业务异常体系掩盖。
    """
    if _override is not None:
        return _override

    mode = settings.approval_mode
    if mode == "mock":
        return MockApprovalGateway(settings.mock_oa_store)
    if mode == "http":
        return HttpApprovalGateway(
            base_url=settings.approval_base_url,
            token=settings.approval_token,
            timeout_seconds=settings.approval_timeout_seconds,
        )
    raise ValueError(
        f"未知的审批接入模式 CR_APPROVAL_MODE={mode!r}，可选值：mock / http"
    )
