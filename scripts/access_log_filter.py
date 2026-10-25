"""访问日志过滤器：抑制浏览器扩展对本机端口探测产生的日志噪音。

职责：
- 在 ``uvicorn.access`` 日志器上挂一个过滤器，把「本系统不提供的路径」
  的访问记录降级丢弃，让 access log 只保留真实业务请求；
- 通过 ``scripts/serve.py`` 装配，不侵入 ``app/`` 业务代码。

非职责：
- 不改变任何 HTTP 响应（状态码与 body 仍由 ``app/main.py`` 的
  ``_absorb_probe`` 处理）；
- 不过滤真实业务请求的错误日志。

--------------------------------------------------------------------------
为什么需要这个模块（问题背景）
--------------------------------------------------------------------------
浏览器装了本机服务探测类插件（常见于本地大模型前端扩展）后，它会连锁扫描
本机常见端口，反复请求 ``/tools``、``/props``、``/v1/models`` 等路径。
日志会变成这样：

    INFO: 127.0.0.1:55645 - "GET /tools HTTP/1.1" 404 Not Found
    INFO: 127.0.0.1:63964 - "GET /build.json HTTP/1.1" 404 Not Found
    INFO: 127.0.0.1:60569 - "GET /props HTTP/1.1" 404 Not Found
    INFO: 127.0.0.1:60569 - "POST /tools HTTP/1.1" 405 Method Not Allowed
    ... 每秒数条，持续不断

这有两个实际危害：
1. **真正的错误被淹没**：业务接口真的 500 时，你得在几百行噪音里找；
2. **误判为故障**：看到满屏 404，第一反应是"路由写错了"，
   实际是别人的插件在探测——本项目开发过程中就因此排查过两次。

--------------------------------------------------------------------------
为什么在"日志层"过滤，而不是改 HTTP 响应
--------------------------------------------------------------------------
这两件事必须分开看：

- **HTTP 响应**要诚实：这些路径确实不存在，就该返回 404，
  且 body 必须是合法 JSON（否则客户端 ``res.json()`` 会抛
  ``Unexpected end of JSON input``）。这部分已由 ``app/main.py`` 处理。
- **日志**是给人看的：既然已经确认这些请求与我们无关，
  就没必要每条都记。日志的价值在于"异常可见"，不在于"全量留痕"。

如果为了日志干净而去改状态码（比如返回 200），就是拿错误信息换安静——
那是本末倒置，之前已经踩过一次（详见 README 缺陷 15）。

--------------------------------------------------------------------------
过滤规则的边界（重要）
--------------------------------------------------------------------------
只过滤 **路径在白名单内、且状态码为 404/405** 的访问记录，**不区分 HTTP 方法**。

为什么不按方法区分（初版判断错误，已修正）：
    初版只静默 GET/HEAD，理由是"POST 可能是真实接口误调用"。
    但实测发现：``POST /tools``、``POST /props`` 依然刷屏，问题只解决一半。
    更关键的是，那个理由本身站不住：
    - 本系统的真实接口全部挂在 ``/api/tools/*`` 下；
    - ``/tools``、``/props`` 这类顶层路径**不存在任何真实路由**；
    - 所以对这些路径发 POST，只可能是外部探测。
    而真正需要被看见的"接口误调用"（例如用 GET 调 ``/api/tools/download``），
    它的路径不在白名单内，仍会正常记录——这才是日志该保留的价值。

不满足条件就放行：
- 状态码不是 404/405 → 说明这个路径真的被某个路由处理了，必须留痕；
- 路径不在白名单 → 与本系统无关，照常记录。

这样即使将来把 ``/favicon.ico`` 做成了真实路由（返回 200），
它的访问记录也会自动出现在日志里，不会被这个过滤器吞掉。
"""

from __future__ import annotations

import logging

# 需要静默的路径。与 ``app.main._PROBE_PATHS`` 保持一致，
# 但这里刻意**不 import 那个常量**——原因：
#   本模块属于运维层（scripts/），而 _PROBE_PATHS 属于应用层（app/）。
#   运维脚本 import 应用内部常量会让依赖方向倒挂，
#   且应用层改常量时可能连带影响日志行为，不利于排查。
#   两处都写一份，配合 ``tests/test_web.py`` 断言两者一致即可。
_PROBE_PATHS: frozenset[str] = frozenset({
    "/tools",
    "/props",
    "/build.json",
    "/v1/models",
    "/sw.js",
    "/favicon.ico",
    "/robots.txt",
})

# 只有这些状态码会被静默。
# 一旦探测路径返回了别的状态码（例如将来变成真实路由返回 200），
# 记录会正常输出——这是有意的：那时候它就是"真实请求"了。
_SILENT_STATUS: frozenset[int] = frozenset({404, 405})

# 关于是否按 HTTP 方法区分（曾经按方法区分，后来修正）：
#
# 初版只静默 GET/HEAD，理由写的是"POST 可能是真实的接口误调用，值得看见"。
# 实测发现这个理由是错的：
#   - 本系统的真实接口全部挂在 ``/api/tools/*`` 下；
#   - ``/tools``、``/props`` 这类顶层路径**根本不存在任何真实路由**；
#   - 因此对这些路径发 POST，只可能是外部探测，不可能是真实业务调用。
# 结果是 ``POST /tools 405``、``POST /props 405`` 仍然刷屏，问题只解决了一半。
#
# 现在的判定改为「路径命中白名单 + 状态码是 404/405」，
# **不看方法**。理由：
#   1. 状态码 404/405 已经充分说明"这个路径/方法没有被本系统处理"；
#   2. 真实接口即便被误用错误方法（如 GET 调 POST 接口），
#      它的路径是 ``/api/tools/xxx``，不在白名单内，仍会正常记录——
#      这才是真正需要被看见的"接口误调用"。
#
# 保留 _SILENT_METHODS 常量只是为了在 verbose 模式里标注信息，
# 不再参与过滤判定。
_LOGGED_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})


def _normalize_path(raw: str) -> str:
    """把 access log 里的路径去掉查询串。

    参数：
        raw: 形如 ``/props?autoload=false`` 的原始路径。
    返回：
        去掉 ``?`` 之后部分的路径。
    关联：
        被 :meth:`ProbeNoiseFilter.filter` 调用。

    说明：
        uvicorn 记录的是 ``get_path_with_query_string`` 的结果，
        带查询串。而我们的白名单是不带查询串的路径，
        因此必须先切掉 ``?`` 之后再比对。
    """
    return raw.split("?", 1)[0]


class ProbeNoiseFilter(logging.Filter):
    """丢弃浏览器扩展探测产生的访问日志。

    用途：挂在 ``uvicorn.access`` 上，让 access log 只保留真实业务请求。
    生命周期：由 ``scripts/serve.py`` 在启动 uvicorn 前创建并挂载，常驻进程。
    协作对象：``uvicorn.access`` 日志器。

    工作原理：
        uvicorn 的 access 记录格式为
        ``'%s - "%s %s HTTP/%s" %d'``，对应 args 是
        ``(client_addr, method, path_with_query, http_version, status_code)``。
        过滤器从这个元组里取方法、路径、状态码做判断，命中白名单则返回 False
        （logging 的 Filter 返回 False 表示丢弃该记录）。
    """

    def __init__(self, verbose: bool = False) -> None:
        """初始化过滤器。

        参数：
            verbose: 为 ``True`` 时不丢弃，只在记录前打印一行提示。
                用于排查"某个路径为什么没日志"时临时打开。
        关联：
            由 ``scripts/serve.py`` 按命令行参数构造。
        """
        super().__init__(name="probe-noise-filter")
        self.verbose = verbose
        # 被丢弃的条数。用于退出时打印一行统计，
        # 让人知道"过滤器工作了"，而不是怀疑日志系统坏了。
        self.suppressed = 0

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        """判断是否保留这条日志记录。

        参数：
            record: 日志记录对象。
        返回：
            ``True`` 表示保留输出，``False`` 表示丢弃。
        关联：
            由 logging 框架在每次 ``uvicorn.access`` 记录时自动调用。

        判定条件（三者同时满足才静默）：
            1. 路径在白名单内（本系统不提供这些路径）；
            2. 状态码是 404 或 405（说明确实没被任何路由处理）；
            3. 不区分 HTTP 方法——理由见 ``_LOGGED_METHODS`` 上方的说明。
        """
        args = record.args
        # 结构不符时一律放行：宁可多打日志，也不要因为解析失败而吞掉真实记录。
        if not isinstance(args, tuple) or len(args) < 5:
            return True

        path = _normalize_path(str(args[2]))
        status = args[4]

        if path in _PROBE_PATHS and status in _SILENT_STATUS:
            self.suppressed += 1
            if self.verbose:
                # verbose 模式只提示不丢弃，便于确认过滤器判定是否正确。
                #
                # 【踩坑记录 —— 这里有两个坑，都记下来】
                #
                # 坑 1：以为改 record.msg 就能改输出
                #   最初写 `record.msg = "[已静默] " + str(record.msg)`，
                #   实测输出毫无变化。原因：
                #     uvicorn 的 access formatter 是 AccessFormatter，
                #     它的格式串是
                #       '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'
                #     —— **完全不用 %(message)s**。
                #     它直接在 format() 里读取 record.args[0] / args[1..3] / args[4]。
                #     所以改 msg 对 access 日志没有任何作用。
                #   （对比：默认 formatter 用的是 %(message)s，改 msg 才有效。
                #     两种 formatter 行为不同，是这次踩坑的根本原因。）
                #
                # 坑 2：占位符与 args 数量必须严格对应
                #   若把 msg 改成多一个 %s 的格式串而 args 仍是 5 个，
                #   getMessage() 会抛格式化异常，这条日志反而丢失——比不打标记更糟。
                #
                # 现在的做法：**只改 record.args**，把客户端地址前面加标记。
                # AccessFormatter 会把它渲染进 %(client_addr)s，因此输出形如：
                #   INFO: [已静默]127.0.0.1:xxxx - "GET /tools HTTP/1.1" 404
                # 不动 msg，不引入新占位符，格式化始终成立。
                args_list = list(record.args)
                args_list[0] = "[已静默]" + str(args_list[0])
                record.args = tuple(args_list)
                return True
            return False
        return True


def install(verbose: bool = False) -> ProbeNoiseFilter:
    """把过滤器安装到 uvicorn 的 access 日志器上。

    参数：
        verbose: 是否使用"只提示不丢弃"模式。
    返回：
        安装好的 :class:`ProbeNoiseFilter` 实例，供调用方在退出时读取
        ``suppressed`` 统计。
    关联：
        被 ``scripts/serve.py`` 调用；测试可直接调用本函数验证过滤行为。

    说明：
        重复调用不会叠加多个过滤器——先移除同名旧过滤器再添加，
        避免在测试或热重载场景下重复过滤。
    """
    access_logger = logging.getLogger("uvicorn.access")
    for existing in list(access_logger.filters):
        if existing.name == "probe-noise-filter":
            access_logger.removeFilter(existing)

    probe_filter = ProbeNoiseFilter(verbose=verbose)
    access_logger.addFilter(probe_filter)
    return probe_filter
