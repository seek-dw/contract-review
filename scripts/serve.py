"""启动脚本：装配访问日志过滤器后启动 uvicorn。

职责：
- 在 uvicorn 启动前把 :class:`scripts.access_log_filter.ProbeNoiseFilter`
  挂到 ``uvicorn.access`` 上，抑制浏览器扩展探测产生的日志噪音；
- 透传常规的 host / port / reload 参数；
- 退出时打印一行统计，说明"静默了多少条探测请求"。

非职责：
- 不改变任何 HTTP 行为（路由、状态码、响应体仍由 ``app/main.py`` 决定）。

--------------------------------------------------------------------------
为什么默认端口是 8099 而不是 8080（重要，务必先读）
--------------------------------------------------------------------------
**8080 会被浏览器里已注册的 Service Worker 劫持。**

现象：
    启动服务后，浏览器打开 http://127.0.0.1:8080 ，看到的**不是本系统页面**，
    而是某个别的应用的界面（一个聊天框，写着 "Hello there / Type a message..."），
    并且那个界面里报错 "Server unavailable"。

为什么会出现这种"串台"：
    1. 本机之前访问过某个本地大模型前端（或类似工具），它在
       ``127.0.0.1:8080`` 这个源（origin）上注册了 Service Worker；
    2. Service Worker 是**按 origin 注册**的，且一旦注册就长期驻留，
       即使那个应用已经关掉、甚至卸载，它仍然活着；
    3. 它接管了这个 origin 下所有页面导航请求（fetch 事件），
       直接返回它自己缓存的界面文件，**根本不发到我们的服务器**；
    4. 与此同时，那个界面会去调 ``/v1/models``、``/props`` 等接口，
       请求真正打到我们的服务，我们返回 404 + JSON，
       于是那个界面显示 "Server unavailable" + 我们那句
       "本服务不提供该接口…"。

    这就解释了为什么服务端日志一切正常（我们的页面确实是 200），
    但浏览器里永远看到同一个别人的界面。

**关键判断方法**：
    用 ``curl http://127.0.0.1:端口/`` 看返回的 HTML。
    - 如果 ``<title>`` 是本系统的标题 → 服务正常，问题在浏览器端；
    - 如果浏览器显示的和 curl 拿到的完全不同 → 就是 Service Worker 劫持。
    这一条能避免大量"以为是代码 bug"的无效排查。

**解决办法（按推荐顺序）**：
    1. **换端口（最简单）**：本脚本默认已改为 8099，直接用默认值即可；
    2. 在 Chrome 里注销那个 Service Worker：
       打开 ``chrome://serviceworker-internals/``，
       找到 ``http://127.0.0.1:8080`` 那条，点 Unregister；
    3. 用无痕窗口打开（无痕窗口不继承已注册的 SW）。

    注意：只"清浏览器缓存"通常无效——Service Worker 注册独立于 HTTP 缓存。

--------------------------------------------------------------------------
为什么需要一个包装脚本，而不是直接 ``uvicorn app.main:app``
--------------------------------------------------------------------------
过滤器必须在 uvicorn **创建日志配置之后、开始接收请求之前** 挂上。
如果写成 ``app/main.py`` 里的启动钩子，会有一个时序问题：
lifespan 触发时 access logger 可能已被 uvicorn 重建配置，过滤器被冲掉。
在启动脚本里、调用 ``uvicorn.run()`` 之前安装，时序是确定的。

另外这样做也让 ``app/`` 保持纯粹：应用代码不需要知道"日志要不要过滤"这种
运维决策。运维行为留在运维脚本里。

--------------------------------------------------------------------------
用法
--------------------------------------------------------------------------
    # 默认：端口 8099，静默探测日志
    uv run --python 3.12 python scripts/serve.py

    # 指定端口
    uv run --python 3.12 python scripts/serve.py --port 9000

    # 排查用：不静默，但在每条探测日志前标 [已静默]，确认判定是否正确
    uv run --python 3.12 python scripts/serve.py --verbose-probe

    # 开发热重载
    uv run --python 3.12 python scripts/serve.py --reload
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 允许脚本直接运行时 import app 包与 scripts 包。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402

from scripts.access_log_filter import install  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。

    参数：
        argv: 参数列表；``None`` 时读取 ``sys.argv``。
    返回：
        解析后的参数命名空间。
    关联：
        被 :func:`main` 调用。
    """
    parser = argparse.ArgumentParser(
        description="启动合同审批审查系统（带探测日志过滤）",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="监听地址，默认 127.0.0.1",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8099,
        help="监听端口，默认 8099。"
        "刻意避开 8080：该端口常被本地大模型前端等工具占用，"
        "其 Service Worker 会拦截 127.0.0.1:8080 的页面导航，"
        "导致浏览器打开后看到的是别的应用的界面而不是本系统。详见模块顶部说明。",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="开启热重载（开发用；注意：重载子进程不会继承过滤器，"
        "排查日志问题时请关掉它）",
    )
    parser.add_argument(
        "--verbose-probe",
        action="store_true",
        help="不静默探测日志，改为在记录前标注 [已静默]，用于确认过滤规则",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="日志级别，默认 info",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """启动服务。

    参数：
        argv: 命令行参数；``None`` 时读取 ``sys.argv``。
    返回：
        无。
    关联：
        调用 :func:`scripts.access_log_filter.install` 后启动 uvicorn；
        uvicorn 退出时打印静默统计。

    说明：
        ``--reload`` 模式下 uvicorn 会 fork 子进程处理请求，
        子进程会重新初始化日志配置，父进程挂的过滤器不会生效。
        因此这里给出明确提示，避免"开了 reload 却发现过滤不起作用"的困惑。
    """
    args = parse_args(argv)

    probe_filter = install(verbose=args.verbose_probe)

    print(f"启动：http://{args.host}:{args.port}")
    print(
        "访问日志过滤："
        + ("已开启（探测请求会标注 [已静默]）" if args.verbose_probe else "已开启（探测请求不记录）")
    )
    if args.reload:
        print(
            "⚠ 已开启 --reload：重载子进程不继承过滤器，"
            "探测日志可能仍会出现。排查日志问题时请去掉 --reload。"
        )

    try:
        uvicorn.run(
            "app.main:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
            log_level=args.log_level,
        )
    finally:
        # 退出时报告统计：让人确认过滤器确实工作了，
        # 而不是怀疑"日志系统是不是坏了、怎么一条都没有"。
        if probe_filter.suppressed:
            print(f"本次运行共静默 {probe_filter.suppressed} 条探测请求日志。")


if __name__ == "__main__":
    main()
