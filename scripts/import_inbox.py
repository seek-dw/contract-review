"""把 inbox 目录里的合同注册进模拟审批系统。

============================================================================
用途
============================================================================
本系统没有"上传"功能，因为设计上合同是从**审批系统**拉取的，不是用户传的
（需求 2.4.3：``list_pending_contract_approvals`` 是七个工具的第一个，
定位就是"审批系统触发待办拉取"）。

但演示环境没有真实 OA，所以提供这个脚本作为"模拟 OA 提交审批"的等价动作：
把你手上的合同放进 ``inbox/``，脚本把它注册成一条待审记录，
之后在页面上点"拉取待办"就能看到它，点"一键处理"就能审。

============================================================================
用法
============================================================================
1. 把合同文件（PDF / DOCX / 图片）拷进 ``inbox/`` 目录
2. 运行：

       uv run --python 3.12 python scripts/import_inbox.py

3. 打开页面 → 点「拉取待处理审批单」→ 看到新合同 → 点「查看详情」→「一键处理」

支持的格式：``.pdf`` ``.docx`` ``.png`` ``.jpg`` ``.jpeg`` ``.bmp`` ``.tif`` ``.tiff``

============================================================================
设计说明
============================================================================
- **幂等**：同一个文件重复导入不会产生重复审批单。用文件名做去重键，
  已存在则跳过（并提示）。
- **不改动原文件**：只在 JSON 里记一条指向 inbox 文件的路径，
  下载时复制到 ``data/attachments/``。原文件保持不动。
- **审批编号规则**：``INBOX-<文件名主干>``，便于一眼看出是本地导入的。
- **申请信息占位**：真实 OA 会有申请人、部门、金额等表单数据。
  这里填"本地导入"，并在表单里注明，避免误以为是真实审批数据。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

# 允许脚本直接运行时 import app 包。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402

# 支持的合同格式。与 ``app.parser.document`` 的读取能力保持一致。
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}

# inbox 目录。放在项目根下，与 samples/ 平级。
INBOX_DIR = settings.base_dir / "inbox"


def _iter_contract_files() -> list[Path]:
    """列出 inbox 里所有受支持的合同文件。

    返回：
        文件路径列表，按名称排序。空目录返回空列表。
    关联：
        被 :func:`main` 调用。
    """
    if not INBOX_DIR.exists():
        return []
    files = [
        p
        for p in sorted(INBOX_DIR.iterdir())
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
    ]
    return files


def _approval_code_for(path: Path, existing: list[dict]) -> str:
    """为合同文件生成审批编号。

    参数：
        path: 合同文件路径。
        existing: 已有的审批单列表，用于避免编号冲突。
    返回：
        形如 ``INBOX-001`` 的纯 ASCII 编号。
    关联：
        被 :func:`register_file` 调用；作为去重键。

    【重要】编号必须是纯 ASCII，不能含中文。
        最初设计是 ``INBOX-<文件名>``，实测发现两个问题：
        1. **URL 双重编码**：中文编号放进表单提交会被再编码一次，
           服务端收到的不是原始编号，查不到记录
           （``INBOX-测试采购合同`` → ``INBOX-%25E6%25B5%258B...``）；
        2. 真实审批系统的编号本来就是 ASCII（如 ``HT-2024-0001``），
           用中文编号不符合惯例，也不便口头沟通。

        现在改为顺序编号 ``INBOX-001``，中文文件名放进
        ``approval_title`` 字段（那是给人看的，不参与路由）。
    """
    # 找出已用的 INBOX 编号，取最大序号 +1
    used_numbers: list[int] = []
    for item in existing:
        code = item.get("approval_code", "")
        if code.startswith("INBOX-"):
            tail = code[len("INBOX-") :]
            if tail.isdigit():
                used_numbers.append(int(tail))
    next_no = max(used_numbers, default=0) + 1
    return f"INBOX-{next_no:03d}"


def register_file(path: Path, store: dict) -> str:
    """把单个文件注册成一条审批单记录。

    参数：
        path: 合同文件路径。
        store: 模拟审批系统的完整存储字典（原地修改）。
    返回：
        ``"created"`` 表示新建，``"exists"`` 表示已存在跳过。
    关联：
        被 :func:`main` 调用；结果写回 ``data/mock_oa/approvals.json``。

    去重方式：
        用**文件名**判断是否已导入（而不是编号）。
        理由：编号是顺序生成的，每次导入都会变；文件名才是稳定的身份标识。
        同一份文件重复导入应当跳过，否则待办列表会堆满重复项。
    """
    approvals = store.setdefault("approvals", [])

    # 幂等：按文件名去重
    for item in approvals:
        for att in item.get("attachments", []):
            if att.get("file_name") == path.name:
                return "exists"

    code = _approval_code_for(path, approvals)
    now = datetime.now()
    approvals.append(
        {
            "approval_code": code,
            # 中文文件名放在标题里：这是给人看的字段，不参与 URL 路由
            "approval_title": path.stem,
            "applicant_name": "本地导入",
            "applied_at": now.isoformat(timespec="seconds"),
            "approval_status": "pending",
            "form_data": {
                "来源": "inbox 本地导入",
                "文件名": path.name,
                "文件大小": f"{path.stat().st_size} 字节",
                "说明": "非真实审批数据，用于本地合同试审",
            },
            "attachments": [
                {
                    "external_attachment_id": f"ATT-{code}",
                    "file_name": path.name,
                    "file_size": path.stat().st_size,
                    "file_kind": "contract",
                    # 指向 inbox 原文件。下载时由网关复制，不移动原文件。
                    "source_path": str(path.resolve()),
                }
            ],
            "comments": [],
        }
    )
    return "created"


def main() -> None:
    """导入 inbox 目录下的全部合同。

    返回：
        无。
    关联：
        直接运行本脚本时调用。

    副作用：
        修改 ``data/mock_oa/approvals.json``（新增审批单记录）。
        不移动、不修改 inbox 里的原文件。
    """
    print("=" * 70)
    print("把 inbox 目录里的合同注册进模拟审批系统")
    print("=" * 70)
    print(f"目录：{INBOX_DIR}")

    files = _iter_contract_files()
    if not files:
        print()
        print("✗ inbox 目录里没有可识别的合同文件。")
        print(f"  支持的格式：{' '.join(sorted(SUPPORTED_SUFFIXES))}")
        print(f"  请把合同文件拷进：{INBOX_DIR}")
        return

    print(f"发现 {len(files)} 个文件")
    print()

    store_path = settings.mock_oa_store
    if not store_path.exists():
        print(f"✗ 模拟审批系统数据文件不存在：{store_path}")
        print("  请先运行：uv run --python 3.12 python scripts/make_samples.py")
        return

    store = json.loads(store_path.read_text(encoding="utf-8"))

    created = 0
    existed = 0
    for path in files:
        result = register_file(path, store)
        if result == "created":
            created += 1
            # 取刚写入的那条记录，打印它的真实编号
            code = next(
                (
                    a["approval_code"]
                    for a in store["approvals"]
                    if a["approval_title"] == path.stem
                ),
                "?",
            )
            print(f"  ✓ 新增：{path.name}")
            print(f"      审批编号：{code}（列表里按此编号查找）")
        else:
            existed += 1
            print(f"  · 已导入过，跳过：{path.name}")

    store_path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print()
    print(f"完成：新增 {created} 条，跳过 {existed} 条")
    print()
    print("下一步：")
    print("  1. 启动服务：uv run --python 3.12 python scripts/serve.py")
    print("  2. 打开 http://127.0.0.1:8099")
    print("  3. 点「拉取待处理审批单」")
    print("  4. 在列表里找到你的合同，点「查看详情」→「一键处理」")


if __name__ == "__main__":
    main()
