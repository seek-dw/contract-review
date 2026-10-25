"""数据库初始化脚本：建表并灌入种子规则。

职责：
- 按 ORM 模型创建全部业务表（幂等，重复执行安全）；
- 把 ``app.review.seed_rules`` 的 26 条种子规则写入 ``review_rules``，
  已存在的规则（按 ``rule_code`` 判断）跳过，不覆盖人工修改过的规则；
- 打印执行结果，供验收时核对。

非职责：
- 不生成示例合同（见 ``scripts/make_samples.py``）；
- 不清空已有数据（避免误删生产数据；如需重置请手工删除数据库文件）。

运行：
    .venv/Scripts/python.exe scripts/init_db.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.db.base import create_all, session_scope  # noqa: E402
from app.db.models import ALL_MODELS, ReviewRule  # noqa: E402
from app.review.seed_rules import seed_rule_rows  # noqa: E402


def init_database() -> dict:
    """建表并灌入种子规则。

    返回：
        统计字典：``tables``（表名列表）、``rules_created``（新增规则数）、
        ``rules_existing``（已存在规则数）、``db_path``。
    关联：
        被 :func:`main` 调用；也可被测试直接调用以准备干净数据库。
    副作用：
        在 ``settings.data_dir`` 下创建数据库文件与目录。
    """
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.attachment_dir.mkdir(parents=True, exist_ok=True)

    create_all()

    created = 0
    existing = 0
    with session_scope() as db:
        for row in seed_rule_rows():
            found = (
                db.query(ReviewRule)
                .filter(ReviewRule.rule_code == row["rule_code"])
                .one_or_none()
            )
            if found is not None:
                existing += 1
                continue
            db.add(ReviewRule(**row))
            created += 1

    return {
        "tables": [model.__tablename__ for model in ALL_MODELS],
        "rules_created": created,
        "rules_existing": existing,
        "db_path": str(settings.db_path),
    }


def main() -> None:
    """命令行入口：执行初始化并打印结果。

    返回：
        无。
    关联：
        直接运行本脚本时调用 :func:`init_database`。
    """
    print(f"数据库：{settings.db_url}")
    result = init_database()
    print(f"已建表（{len(result['tables'])} 张）：{', '.join(result['tables'])}")
    print(
        f"种子规则：新增 {result['rules_created']} 条，"
        f"已存在 {result['rules_existing']} 条"
    )
    print(f"数据库文件：{result['db_path']}")
    print("初始化完成。")


if __name__ == "__main__":
    main()
