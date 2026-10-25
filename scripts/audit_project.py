"""项目全量静态审计脚本。

用途：找出"测试跑通但设计有问题"的隐患。覆盖：
1. 未使用的导入（代码腐化信号）
2. 裸 except / 过宽异常捕获（可能吞掉真实故障）
3. 硬编码路径（换机器就崩）
4. 函数过长（可维护性）
5. 缺失 docstring（交接成本）
6. 规则误报率（用演示案例做交叉验证）
7. 硬编码的固定值（如过期日期、魔法数字）

运行：
    uv run --python 3.12 python scripts/audit_project.py
"""

from __future__ import annotations

import ast
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"


def audit_unused_imports() -> list[str]:
    """检查 app/ 下未使用的导入。

    返回：
        问题描述列表。
    关联：
        代码腐化指标——无用的导入会让读者误以为有依赖关系。
    """
    issues: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        # 收集所有 Name / Attribute 使用
        used: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                used.add(node.attr)
            # 字符串注解里也可能用到
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", node.value):
                    used.add(token)

        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                # 跳过 __future__ 导入：它们改变编译行为，不需要被"使用"
                if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                    continue
                for alias in node.names:
                    name = alias.asname or alias.name.split(".")[0]
                    if name == "*":
                        continue
                    if name not in used:
                        issues.append(f"{path.relative_to(ROOT)}:{node.lineno} 未使用: {name}")
    return issues


def audit_broad_except() -> list[str]:
    """检查过宽的异常捕获。

    返回：
        问题描述列表。

    说明：
        ``except Exception`` 本身不必然是错的（如 OCR 引擎故障需要统一转换），
        但**必须**满足其一：① 重新抛出；② 记录日志；③ 明确注释说明为何吞掉。
        否则就是静默失败，比崩溃更难排查。
    """
    issues: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines):
            if not re.search(r"except\s+Exception", line):
                continue
            # 看后续 6 行内是否有 raise / 日志 / 注释说明
            window = "\n".join(lines[idx : idx + 6])
            has_raise = "raise" in window
            has_log = any(k in window for k in ("traceback", "append_log", "print_exc"))
            has_comment = "#" in line
            if not (has_raise or has_log or has_comment):
                issues.append(
                    f"{path.relative_to(ROOT)}:{idx + 1} 宽泛捕获且无处理说明"
                )
    return issues


def audit_hardcoded_paths() -> list[str]:
    """检查硬编码的绝对路径。

    返回：
        问题描述列表。

    说明：
        ``D:\\...`` 或 ``C:\\...`` 这类绝对路径写死在代码里，
        换机器或换目录就崩。应当从配置或相对路径推导。

    例外：
        审计与安全测试脚本里**刻意**包含恶意路径作为攻击载荷
        （如 ``scripts/audit_security.py`` 的 ``C:\\Windows\\evil.pdf``），
        那是测试数据不是配置依赖，不应误报。
        因此跳过文件名含 ``audit_`` 或 ``test_`` 的脚本。
    """
    issues: list[str] = []
    pattern = re.compile(r'["\'][A-Za-z]:[\\/]')
    for path in sorted(list(APP.rglob("*.py")) + list((ROOT / "scripts").rglob("*.py"))):
        # 跳过审计/测试脚本：它们包含刻意的恶意路径作为载荷
        if path.name.startswith(("audit_", "test_")):
            continue
        for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # 跳过注释行
            if line.strip().startswith("#"):
                continue
            if pattern.search(line):
                issues.append(f"{path.relative_to(ROOT)}:{idx} 硬编码路径: {line.strip()[:70]}")
    return issues


def audit_long_functions(threshold: int = 80) -> list[str]:
    """检查过长的函数。

    参数：
        threshold: 行数阈值，超过即报告。
    返回：
        问题描述列表。

    说明：
        函数超过 80 行通常意味着职责不单一。这里只作提示，
        因为有些检查函数（如陷阱规则）确实需要较长篇幅的注释。
    """
    issues: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                length = (node.end_lineno or node.lineno) - node.lineno
                if length > threshold:
                    issues.append(
                        f"{path.relative_to(ROOT)}:{node.lineno} {node.name} ({length} 行)"
                    )
    return issues


def audit_docstring_coverage() -> list[str]:
    """检查 docstring 覆盖率。

    返回：
        缺失 docstring 的类/函数列表。
    关联：
        用户明确要求"每个函数都要写功能注释"，这是硬性验收项。
    """
    issues: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if not ast.get_docstring(tree):
            issues.append(f"{path.relative_to(ROOT)} 缺少模块 docstring")
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if not ast.get_docstring(node):
                    issues.append(
                        f"{path.relative_to(ROOT)}:{node.lineno} {node.name} 缺少 docstring"
                    )
    return issues


def audit_rule_false_positive_rate() -> tuple[Counter, Counter, list[str]]:
    """用演示案例交叉验证每条规则的命中分布。

    返回：
        ``(命中次数, 案例总数, 问题列表)``。

    设计原因：
        这是本审计最重要的部分。**误报是规则系统最大的杀手**——
        一条规则如果在每个案例里都命中，等于没有信息量，只会让用户关掉系统。
        本项目在开发中已经踩过 T009 误报率 100% 的坑。
    """
    from app.review.trap_rules import run_trap_checks
    from scripts.demo_trap_value import CASES as TRAP_CASES

    hits_counter: Counter = Counter()
    for case in TRAP_CASES:
        for hit in run_trap_checks(case["outcome"]):
            hits_counter[hit.rule_code] += 1

    total = len(TRAP_CASES)
    issues: list[str] = []
    for code, count in hits_counter.items():
        rate = count / total
        # 命中率超过 50% 视为过宽（该规则几乎对所有样本都报）
        if rate > 0.5:
            issues.append(f"{code} 命中率 {count}/{total}（{rate:.0%}），过宽")
    return hits_counter, Counter({"total": total}), issues


def audit_expired_date_dependency() -> list[str]:
    """检查规则是否依赖"当前日期"。

    返回：
        问题描述列表。

    说明：
        依赖 ``date.today()`` 的规则会随时间改变行为——
        今天能过的测试，明年可能失败。这类规则要么标注、
        要么改为可配置基准日。
    """
    issues: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "date.today()" in line or "datetime.now()" in line:
                issues.append(f"{path.relative_to(ROOT)}:{idx} {line.strip()[:60]}")
    return issues


def main() -> None:
    """执行全部审计并打印分级结论。

    返回：
        无。
    """
    print("=" * 78)
    print("合同审批审查系统 —— 全量静态审计")
    print("=" * 78)

    blocking: list[str] = []
    warnings: list[str] = []

    print("\n[1] 未使用导入")
    r = audit_unused_imports()
    print(f"    {len(r)} 项")
    for x in r[:10]:
        print(f"      {x}")
    if r:
        warnings.extend(r)

    print("\n[2] 宽泛异常捕获（无处理说明）")
    r = audit_broad_except()
    print(f"    {len(r)} 项")
    for x in r:
        print(f"      {x}")
    blocking.extend(r)

    print("\n[3] 硬编码绝对路径")
    r = audit_hardcoded_paths()
    print(f"    {len(r)} 项")
    for x in r[:10]:
        print(f"      {x}")
    blocking.extend(r)

    print("\n[4] 超长函数（>80 行）")
    r = audit_long_functions()
    print(f"    {len(r)} 项")
    for x in r[:8]:
        print(f"      {x}")
    warnings.extend(r)

    print("\n[5] docstring 覆盖率")
    r = audit_docstring_coverage()
    print(f"    缺失 {len(r)} 项")
    for x in r[:10]:
        print(f"      {x}")
    blocking.extend(r)

    print("\n[6] 规则误报率（用 12 个演示案例交叉验证）")
    counter, totals, r = audit_rule_false_positive_rate()
    total = totals["total"]
    print(f"    案例数 {total}，命中分布：")
    for code, count in counter.most_common():
        flag = "  ← 过宽" if count / total > 0.5 else ""
        print(f"      {code}: {count}/{total}{flag}")
    not_hit = [
        c
        for c in ("T001","T002","T003","T004","T005","T006","T007","T008","T009","T010","T011","T012")
        if c not in counter
    ]
    if not_hit:
        print(f"    从未命中（需检查是否失效）: {not_hit}")
        warnings.extend([f"{c} 在演示案例中从未命中" for c in not_hit])
    blocking.extend(r)

    print("\n[7] 依赖当前日期的代码")
    r = audit_expired_date_dependency()
    print(f"    {len(r)} 项")
    for x in r:
        print(f"      {x}")
    warnings.extend(r)

    print("\n" + "=" * 78)
    print("审计结论")
    print("=" * 78)
    print(f"  阻塞项：{len(blocking)}")
    print(f"  告警项：{len(warnings)}")
    if blocking:
        print("\n  【阻塞项】必须修复：")
        for x in blocking:
            print(f"    ✗ {x}")
    if not blocking:
        print("\n  ✓ 无阻塞项")
    print("\n  注：告警项不必然要修，但需逐条判断影响。")


if __name__ == "__main__":
    main()
