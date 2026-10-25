# 贡献指南

## 环境准备

```bash
uv venv --python 3.12
uv pip install -r requirements.txt

uv run --python 3.12 python scripts/make_samples.py
uv run --python 3.12 python scripts/init_db.py
```

## 提交前必须跑通的检查

```bash
# 1. 全量测试（141 项）
uv run --python 3.12 python -m pytest

# 2. 静态审计（未使用导入 / 宽泛异常 / 硬编码路径 / docstring 覆盖率）
uv run --python 3.12 python scripts/audit_project.py

# 3. 安全边界（路径穿越 / SQL 注入 / 极端输入 / 并发互斥）
uv run --python 3.12 python scripts/audit_security.py

# 4. 需求逐条核对
uv run --python 3.12 python scripts/audit_spec_24.py
```

四项全绿才能提交。审计脚本报阻塞项时**不要绕过**——那些是真实缺陷。

## 代码规范

### 注释要求

本项目的注释标准比一般项目严格，因为审查规则需要可答辩：

- **每个模块**：顶部写职责、在架构中的位置、与其他模块的依赖关系
- **每个类**：说明用途、为什么这样定义
- **每个函数**：说明参数、返回值、与其他函数的关联
- **每个字段**：说明用途、为什么定义

```python
def run_deep_checks(outcome: ParseOutcome) -> list[RuleHitView]:
    """执行全部跨条款深层检查。

    参数：
        outcome: 解析产物。
    返回：
        命中列表，按风险降序。
    关联：
        被 ``services/review_service.run_review`` 调用，
        与浅层规则、陷阱规则的命中合并。
    """
```

### 注释写"为什么"，不写"是什么"

```python
# ✗ 差的注释：重复代码字面意思
i += 1  # i 加 1

# ✓ 好的注释：解释设计决策与踩过的坑
# 必须用 eol=lf：Windows 上开发的 CRLF 文件进仓库后，
# 整个仓库会显示"每行都改了"，diff 不可读。
```

### 分层依赖方向

```
web → tools → services → (parser | review | approval | db) → domain
```

**禁止反向依赖**。`domain` 层不允许 import 任何上层模块。

## 新增审查规则

这是最常改的部分，步骤固定：

### 1. 确定规则属于哪一层

| 层 | 适用场景 | 文件 |
| --- | --- | --- |
| 浅层 | 单条款内能找到的特征（如"预付款 60%"） | `app/review/seed_rules.py` |
| 深层 | 需要跨条款运算（如付款账期 vs 到期日） | `app/review/deep_rules.py` |
| 陷阱 | 有明确法条依据的合同陷阱 | `app/review/trap_rules.py` |

### 2. 陷阱规则必须绑定法条

```python
return TrapFinding(
    code="T0XX",
    name="规则名称",
    risk=RiskLevel.HIGH,
    evidence=f"命中的事实……",
    position=_pos(outcome, "payment"),
    # 必填：法条原文，不是"相关法律"这种模糊表述
    legal_basis="《民法典》第五百八十六条：……",
    suggestion="处理建议……",
)
```

`test_all_traps_have_legal_basis` 会强制校验这一点。

### 3. 必须配正反用例

```python
def test_t0xx_detected():
    """正向：条件成立时必须命中。"""
    ...

def test_t0xx_not_triggered():
    """反向：只差一个条件时必须不命中。"""
    ...
```

**反向用例比正向更重要**——它防止规则变成"见谁都报"的噪音源。

### 4. 检查误报率

```bash
uv run --python 3.12 python scripts/audit_project.py
```

看 `[6] 规则误报率` 一节。命中率超过 50% 视为过宽，需要收窄条件。

> 本项目踩过这个坑：T009 初版在 12 个演示案例中 **100% 命中**，
> 收窄后变成 **0% 命中**（完全失效）。两个极端都是错的。

## 已知局限

改代码前先看 README 的「当前局限」一节，避免重复踩坑：

- 阈值规则取数可能有歧义（条款里多个百分比时只取第一个）
- 中文大写金额（"壹佰贰拾万元整"）尚未支持
- 版式差异大的合同抽不到字段
- `date.today()` 依赖：D003 规则随时间改变行为

## 提交信息格式

```
<类型>: <简短描述>

<详细说明（可选）>
```

类型：`feat` / `fix` / `docs` / `test` / `refactor` / `chore`

示例：

```
fix: 修复中文审批编号被 URL 双重编码导致查不到记录

审批编号改为纯 ASCII（INBOX-001），中文文件名放入 approval_title 字段。
根因：中文编号放进表单提交时被再编码一次，服务端收到的不是原始值。
```
