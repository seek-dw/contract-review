"""安全边界验证脚本：检查路径穿越、异常输入等攻击面。

用途：全量审计的一部分。测试通过不代表系统安全——
需要主动构造恶意输入验证防护是否生效。

运行：
    uv run --python 3.12 python scripts/audit_security.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.attachment_service import _safe_file_name, _safe_target_path


def test_path_traversal() -> int:
    """验证路径穿越攻击被防护。

    返回：
        失败项数。
    关联：
        ``app/services/attachment_service._safe_file_name`` 与
        ``_safe_target_path``。

    攻击场景：
        外部审批系统返回的文件名可能是恶意构造的：
        ``../../../etc/passwd``、``C:\\Windows\\evil.pdf`` 等。
        若不处理，文件会被写到系统目录。
    """
    attacks = [
        "../../../etc/passwd",
        "..\\..\\..\\windows\\system32\\evil.dll",
        "C:\\Windows\\evil.pdf",
        "/etc/shadow",
        "\\\\server\\share\\x.pdf",
        "",
        ".",
        "..",
        "a" * 300 + ".pdf",
        "合同\u0000.pdf",  # 空字节注入
        "con.pdf",         # Windows 保留名
        "a/b/c.pdf",
    ]

    failed = 0
    print("  [文件名清洗]")
    for name in attacks:
        try:
            cleaned = _safe_file_name(name)
            has_sep = "/" in cleaned or "\\" in cleaned
            has_traversal = ".." in cleaned
            too_long = len(cleaned) > 120
            if has_sep or has_traversal or too_long:
                print(f"    ✗ 未清洗干净: {name[:30]!r} → {cleaned[:40]!r}")
                failed += 1
            else:
                print(f"    ✓ {name[:28]!r:32} → {cleaned[:36]!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"    ✗ 抛异常 {name[:28]!r}: {exc}")
            failed += 1

    print("\n  [落盘路径越界]")
    for code in ["../../../evil", "..\\..\\evil", "normal-code"]:
        try:
            path = _safe_target_path(code, 1, "x.pdf")
            # 必须位于 attachments 根目录下
            from app.config import settings

            root = settings.attachment_dir.resolve()
            if root in path.parents:
                print(f"    ✓ {code[:24]!r:28} → 落在根目录内")
            else:
                print(f"    ✗ 越界！{code[:24]!r} → {path}")
                failed += 1
        except ValueError as exc:
            print(f"    ✓ 正确拒绝越界: {code[:24]!r} ({str(exc)[:40]})")
        except Exception as exc:  # noqa: BLE001
            print(f"    ✗ 意外异常 {code[:24]!r}: {type(exc).__name__}: {exc}")
            failed += 1

    return failed


def test_extreme_inputs() -> int:
    """验证极端输入不会导致崩溃。

    返回：
        失败项数。
    关联：
        ``app/parser/text_utils`` 的数值抽取函数。
    """
    from app.parser.text_utils import (
        extract_amount,
        extract_day_count,
        extract_percent,
        normalize_for_match,
    )

    cases = [
        ("空字符串", ""),
        ("纯空白", "   \t\n  "),
        ("无数字", "本合同没有任何数字"),
        ("超长重复", "100% " * 500),
        ("全角数字", "６０％"),
        ("嵌套中文数字", "第一百二十三万"),
        ("异常符号", "%%%####@@@"),
    ]

    failed = 0
    print("  [极端文本输入]")
    for label, text in cases:
        try:
            normalized = normalize_for_match(text)
            result = (
                extract_percent(normalized),
                extract_day_count(normalized),
                extract_amount(normalized),
            )
            print(f"    ✓ {label:10} ({len(text):5} 字符) → {result}")
        except Exception as exc:  # noqa: BLE001
            print(f"    ✗ {label}: {type(exc).__name__}: {exc}")
            failed += 1

    return failed


def test_sql_injection() -> int:
    """验证审批编号里的 SQL 特殊字符不会造成注入。

    返回：
        失败项数。
    关联：
        ``app/services/task_service`` 的查询逻辑。

    说明：
        本项目用 SQLAlchemy 参数化查询，理论上安全。
        但要验证**实际行为**，而不是假设。
    """
    from app.db.base import session_scope
    from app.db.models import ApprovalTask

    payloads = [
        "'; DROP TABLE approval_tasks; --",
        "' OR '1'='1",
        "1 UNION SELECT * FROM users",
    ]

    failed = 0
    print("  [SQL 注入]")
    try:
        with session_scope() as db:
            for payload in payloads:
                rows = (
                    db.query(ApprovalTask)
                    .filter(ApprovalTask.approval_code == payload)
                    .all()
                )
                if rows:
                    print(f"    ✗ 注入可能成功: {payload[:30]!r} 返回 {len(rows)} 行")
                    failed += 1
                else:
                    print(f"    ✓ 被当作普通字符串: {payload[:30]!r}")
            # 确认表还在
            count = db.query(ApprovalTask).count()
            print(f"    ✓ 表未被破坏，仍有 {count} 条记录")
    except Exception as exc:  # noqa: BLE001
        print(f"    ✗ 查询异常（可能已注入）: {type(exc).__name__}: {exc}")
        failed += 1

    return failed


def test_concurrent_write_guard() -> int:
    """验证并发回写的互斥保护。

    返回：
        失败项数。
    关联：
        ``app/services/comment_service._claim_write``。

    说明：
        这是需求里最容易被忽略的一条：两个请求同时回写会产生两条评论。
        实现用条件更新（UPDATE ... WHERE write_status IN (...)）抢占，
        只有 rowcount==1 的请求能继续。
    """
    from app.db.base import session_scope
    from app.db.models import ApprovalTask
    from app.domain.enums import WriteStatus
    from app.services.comment_service import _claim_write

    failed = 0
    print("  [并发回写互斥]")

    # 找一个任务
    with session_scope() as db:
        task = db.query(ApprovalTask).first()
        if task is None:
            print("    · 无任务可测，跳过")
            return 0
        task_id = task.id
        original = task.write_status
        task.write_status = WriteStatus.NOT_WRITTEN.value

    # 第一次抢占应成功
    first = _claim_write(task_id)
    # 第二次抢占应失败（状态已是 writing）
    second = _claim_write(task_id)

    if first and not second:
        print(f"    ✓ 第一次抢占成功={first}，第二次被拒={not second}")
    else:
        print(f"    ✗ 互斥失效: first={first}, second={second}")
        failed += 1

    # 还原
    with session_scope() as db:
        task = db.get(ApprovalTask, task_id)
        task.write_status = original

    return failed


def main() -> None:
    """执行全部安全边界验证。

    返回：
        无。
    """
    print("=" * 78)
    print("安全与边界验证")
    print("=" * 78)

    total_failed = 0

    print("\n[1] 路径穿越防护")
    total_failed += test_path_traversal()

    print("\n[2] 极端输入")
    total_failed += test_extreme_inputs()

    print("\n[3] SQL 注入")
    total_failed += test_sql_injection()

    print("\n[4] 并发互斥")
    total_failed += test_concurrent_write_guard()

    print("\n" + "=" * 78)
    if total_failed == 0:
        print("✓ 全部通过，未发现安全漏洞")
    else:
        print(f"✗ 发现 {total_failed} 项问题，需修复")
    print("=" * 78)


if __name__ == "__main__":
    main()
