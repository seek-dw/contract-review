"""示例合同生成脚本：产出演示与测试所需的合同文件。

职责：
- 生成三份示例合同，覆盖本系统必须支持的三类输入形态：
  1. ``contract_normal.pdf``：纯文本层 PDF（可复制文字，模拟电子版合同）；
  2. ``contract_scanned.pdf``：扫描件 PDF（页面只有图片，无文本层，必须走 OCR）；
  3. ``contract.docx``：Word 合同（含表格，模拟带表格的模板）；
- 同步生成模拟审批系统的数据文件 ``data/mock_oa/approvals.json``，
  其中包含正常合同、缺附件合同、空文档合同三类审批单，用于演示正常链路与阻塞链路。

非职责：
- 不参与线上审查链路（本脚本只在初始化与演示时运行）；
- 不生成"完美合同"，刻意保留若干风险点，使规则引擎能命中。

设计原因：
需求 2.4.12 要求"能解析文档和图片扫描件，提取主要字段"。
若示例只有文本 PDF，扫描件能力就无法被真实验证。因此这里用 PyMuPDF 把文字渲染成图片
再嵌入 PDF，得到一份**真正没有文本层**的扫描件——这样 OCR 路径是被强制走到的，
而不是靠 mock 假装。

主要协作模块：
- ``scripts/init_demo.py`` 调用 :func:`generate_all`；
- 生成的 ``approvals.json`` 被 ``app.approval.mock_gateway`` 读取。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

# 允许脚本直接运行时 import app 包。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.pdfbase import pdfmetrics  # noqa: E402
from reportlab.pdfbase.cidfonts import UnicodeCIDFont  # noqa: E402
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer  # noqa: E402

from app.config import settings  # noqa: E402

# 中文字体注册名。reportlab 内置 STSong-Light 支持中文，无需外部字体文件。
_CN_FONT = "STSong-Light"


def _register_font() -> None:
    """注册中文字体，供 reportlab 渲染中文。

    返回：
        无。
    关联：
        被 :func:`generate_normal_pdf` 与 :func:`generate_scanned_pdf` 调用。
    """
    pdfmetrics.registerFont(UnicodeCIDFont(_CN_FONT))


def _normal_contract_paragraphs() -> list[str]:
    """返回"正常合同"的段落文本。

    返回：
        段落字符串列表。
    关联：
        被 :func:`generate_normal_pdf` 与 :func:`generate_scanned_pdf` 共用，
        保证两份示例内容一致，便于对比"文本层解析"与"OCR 解析"的结果差异。
    说明：
        文本刻意包含若干风险点，使规则引擎有命中：
        - 预付款比例 60%（偏高）；
        - 付款周期 90 日（偏长）；
        - 违约金比例 30%（偏高）；
        - 争议由乙方所在地法院管辖（对我方不利）；
        - 无保密条款、无数据条款（缺失）。
    """
    return [
        "采购合同",
        "合同编号：HT-2024-0001",
        "甲方：星海智能科技有限公司",
        "乙方：云帆数据服务有限公司",
        "第一条 合同标的",
        "乙方向甲方提供数据治理平台软件及实施服务，具体范围见附件一。",
        "第二条 合同金额与付款方式",
        "本合同总金额为人民币 1,200,000 元（大写：壹佰贰拾万元整）。",
        "本合同签订后 5 个工作日内，甲方支付合同总金额 60% 作为预付款。",
        "剩余款项在验收合格后 90 日内支付。",
        "第三条 交付与验收",
        "乙方应于 2024 年 12 月 31 日前完成全部交付。",
        "甲方应在收到交付物后 7 日内完成验收，验收标准详见附件二。",
        "第四条 违约责任",
        "任何一方违约，应向对方支付合同总金额 30% 的违约金。",
        "逾期交付的，每逾期一日按合同总金额的 0.5% 支付违约金。",
        "第五条 知识产权",
        "本项目形成的定制开发成果知识产权归甲方所有，乙方保留其原有技术成果的权利。",
        "第六条 争议解决",
        "因本合同产生的争议，双方应友好协商；协商不成的，由乙方所在地人民法院管辖。",
        "第七条 其他",
        "本合同一式两份，双方各执一份，自双方盖章之日起生效，有效期至 2025 年 12 月 31 日。",
    ]


def generate_normal_pdf(out_path: Path) -> None:
    """生成带文本层的正常合同 PDF。

    参数：
        out_path: 输出文件路径。
    返回：
        无。
    关联：
        被 :func:`generate_all` 调用；产物供 ``app.parser.document._read_pdf``
        走文本层分支。
    副作用：
        写入 PDF 文件。
    """
    _register_font()
    doc = SimpleDocTemplate(
        str(out_path), pagesize=A4,
        leftMargin=25 * mm, rightMargin=25 * mm, topMargin=25 * mm, bottomMargin=25 * mm,
    )
    title_style = ParagraphStyle(
        "title", fontName=_CN_FONT, fontSize=18, leading=26, spaceAfter=10, alignment=1
    )
    body_style = ParagraphStyle("body", fontName=_CN_FONT, fontSize=11, leading=20)

    story = []
    for index, text in enumerate(_normal_contract_paragraphs()):
        # 首行作为文档大标题居中，其余按正文排版。
        style = title_style if index == 0 else body_style
        story.append(Paragraph(text, style))
        if index == 0:
            story.append(Spacer(1, 8))
    doc.build(story)


def generate_scanned_pdf(out_path: Path, work_dir: Path) -> None:
    """生成无文本层的扫描件 PDF。

    参数：
        out_path: 输出 PDF 路径。
        work_dir: 中间图片存放目录。
    返回：
        无。
    关联：
        被 :func:`generate_all` 调用；产物强制 ``app.parser.document._read_pdf``
        走 OCR 分支。
    副作用：
        写入若干 PNG 中间文件与最终 PDF。
    实现方式：
        先用 reportlab 渲染一份带文字的 PDF，再用 PyMuPDF 把它逐页渲染成图片，
        最后把图片重新组装成新 PDF——新 PDF 每页只有图片，没有文本层，
        因此解析时必然触发 OCR，这正是"扫描件"的真实形态。
    """
    _register_font()
    temp_pdf = work_dir / "_scanned_source.pdf"
    doc = SimpleDocTemplate(
        str(temp_pdf), pagesize=A4,
        leftMargin=25 * mm, rightMargin=25 * mm, topMargin=25 * mm, bottomMargin=25 * mm,
    )
    title_style = ParagraphStyle(
        "title", fontName=_CN_FONT, fontSize=18, leading=26, spaceAfter=10, alignment=1
    )
    body_style = ParagraphStyle("body", fontName=_CN_FONT, fontSize=12, leading=24)

    story = []
    for index, text in enumerate(_normal_contract_paragraphs()):
        style = title_style if index == 0 else body_style
        story.append(Paragraph(text, style))
    doc.build(story)

    image_paths: list[Path] = []
    with pymupdf.open(temp_pdf) as source:
        for index in range(source.page_count):
            page = source.load_page(index)
            # 200 DPI 与线上解析配置一致，确保示例文件与真实处理条件相同。
            pixmap = page.get_pixmap(dpi=200)
            image_path = work_dir / f"_scanned_{index + 1}.png"
            pixmap.save(str(image_path))
            image_paths.append(image_path)

    with pymupdf.open() as target:
        for image_path in image_paths:
            page = target.new_page(width=A4[0], height=A4[1])
            page.insert_image(page.rect, filename=str(image_path))
        target.save(str(out_path), deflate=True)

    # 清理中间产物，只保留最终扫描件 PDF。
    temp_pdf.unlink(missing_ok=True)
    for image_path in image_paths:
        image_path.unlink(missing_ok=True)


def generate_docx(out_path: Path) -> None:
    """生成带表格的 Word 合同。

    参数：
        out_path: 输出文件路径。
    返回：
        无。
    关联：
        被 :func:`generate_all` 调用；产物供 ``app.parser.document._read_docx`` 解析，
        表格内容用于验证"表格里的字段也能抽到"。
    副作用：
        写入 DOCX 文件。
    """
    from docx import Document as DocxDocument

    docx = DocxDocument()
    docx.add_heading("技术服务协议", level=0)

    # 用表格承载关键字段，模拟企业模板的常见排版（标签与值分列）。
    table = docx.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    for label, value in (
        ("协议编号", "HT-2024-0002"),
        ("甲方", "星海智能科技有限公司"),
        ("乙方", "启明云计算有限公司"),
        ("合同金额", "人民币 480,000 元"),
        ("生效日期", "2024年3月1日"),
        ("到期日期", "2025年2月28日"),
    ):
        row = table.add_row().cells
        row[0].text = label
        row[1].text = value

    docx.add_paragraph("")
    for text in (
        "第一条 服务内容",
        "乙方向甲方提供云资源运维服务，服务范围见附件。",
        "第二条 付款方式",
        "甲方应在服务开始前支付合同总金额 40% 作为预付款，其余按季度结算，每季度结束后 30 日内支付。",
        "第三条 交付与验收",
        "乙方按月提交服务报告，甲方应在收到报告后 10 日内完成验收。",
        "第四条 保密条款",
        "双方对在合作过程中知悉的对方商业秘密负有保密义务，保密期限为合同终止后三年。",
        "第五条 数据与个人信息",
        "乙方在处理甲方数据时应遵守数据安全相关法律法规，不得将数据用于本合同以外的目的。",
        "第六条 违约责任",
        "乙方逾期提供服务的，每逾期一日按合同总金额的 0.3% 支付违约金。",
        "第七条 争议解决",
        "因本协议产生的争议，由甲方所在地人民法院管辖。",
    ):
        docx.add_paragraph(text)

    docx.save(str(out_path))


def _iso(dt: datetime) -> str:
    """把时间格式化为 ISO 字符串。

    参数：
        dt: 时间对象。
    返回：
        ISO 8601 字符串（秒级精度）。
    关联：
        被 :func:`build_mock_store` 用于生成审批单的申请时间。
    """
    return dt.isoformat(timespec="seconds")


def build_mock_store(store_path: Path, sample_dir: Path) -> None:
    """构造模拟审批系统的数据文件。

    参数：
        store_path: 输出 JSON 路径。
        sample_dir: 示例合同所在目录，用于填写附件源路径。
    返回：
        无。
    关联：
        被 :func:`generate_all` 调用；产物被 ``app.approval.mock_gateway`` 读取。
    副作用：
        写入 JSON 文件。
    设计原因：
        刻意构造三类审批单，覆盖需求 2.4.4 要求的阻塞分支：
        - ``HT-2024-0001``：正常合同（文本 PDF + 扫描件 PDF），走完整闭环；
        - ``HT-2024-0003``：附件源文件缺失，触发 ``attachment_missing`` → blocked；
        - ``HT-2024-0004``：空白文档，触发 ``document_empty`` → blocked。
        这样演示脚本可以在一次运行里同时展示"成功链路"与"阻塞 + 重试链路"。
    """
    now = datetime.now()
    approvals = [
        {
            "approval_code": "HT-2024-0001",
            "approval_title": "数据治理平台采购合同",
            "applicant_name": "李静",
            "applied_at": _iso(now.replace(hour=9, minute=15, second=0, microsecond=0)),
            "approval_status": "pending",
            "form_data": {
                "申请部门": "技术中心",
                "合同类型": "采购合同",
                "金额": "1200000",
                "币种": "CNY",
                "对方单位": "云帆数据服务有限公司",
            },
            "attachments": [
                {
                    "external_attachment_id": "ATT-0001",
                    "file_name": "contract_normal.pdf",
                    "file_size": (sample_dir / "contract_normal.pdf").stat().st_size
                    if (sample_dir / "contract_normal.pdf").exists() else 0,
                    "file_kind": "contract",
                    "source_path": str(sample_dir / "contract_normal.pdf"),
                },
                {
                    "external_attachment_id": "ATT-0002",
                    "file_name": "contract_scanned.pdf",
                    "file_size": (sample_dir / "contract_scanned.pdf").stat().st_size
                    if (sample_dir / "contract_scanned.pdf").exists() else 0,
                    "file_kind": "contract",
                    "source_path": str(sample_dir / "contract_scanned.pdf"),
                },
            ],
            "comments": [],
        },
        {
            "approval_code": "HT-2024-0002",
            "approval_title": "云资源运维技术服务协议",
            "applicant_name": "王磊",
            "applied_at": _iso(now.replace(hour=10, minute=30, second=0, microsecond=0)),
            "approval_status": "pending",
            "form_data": {
                "申请部门": "运维部",
                "合同类型": "技术服务协议",
                "金额": "480000",
                "币种": "CNY",
                "对方单位": "启明云计算有限公司",
            },
            "attachments": [
                {
                    "external_attachment_id": "ATT-0003",
                    "file_name": "contract.docx",
                    "file_size": (sample_dir / "contract.docx").stat().st_size
                    if (sample_dir / "contract.docx").exists() else 0,
                    "file_kind": "contract",
                    "source_path": str(sample_dir / "contract.docx"),
                }
            ],
            "comments": [],
        },
        {
            "approval_code": "HT-2024-0003",
            "approval_title": "办公设备采购合同（附件缺失示例）",
            "applicant_name": "赵敏",
            "applied_at": _iso(now.replace(hour=11, minute=5, second=0, microsecond=0)),
            "approval_status": "pending",
            "form_data": {
                "申请部门": "行政部",
                "合同类型": "采购合同",
                "金额": "86000",
                "币种": "CNY",
                "对方单位": "宏达办公设备有限公司",
            },
            "attachments": [
                {
                    "external_attachment_id": "ATT-0004",
                    "file_name": "contract_missing.pdf",
                    # 源文件刻意不存在：模拟"审批系统记录有附件但文件取不到"。
                    "file_size": 0,
                    "file_kind": "contract",
                    "source_path": str(sample_dir / "contract_missing.pdf"),
                }
            ],
            "comments": [],
        },
        {
            "approval_code": "HT-2024-0004",
            "approval_title": "咨询服务合同（空文档示例）",
            "applicant_name": "陈刚",
            "applied_at": _iso(now.replace(hour=13, minute=40, second=0, microsecond=0)),
            "approval_status": "pending",
            "form_data": {
                "申请部门": "战略部",
                "合同类型": "咨询合同",
                "金额": "200000",
                "币种": "CNY",
                "对方单位": "远景咨询有限公司",
            },
            "attachments": [
                {
                    "external_attachment_id": "ATT-0005",
                    "file_name": "contract_empty.pdf",
                    "file_size": (sample_dir / "contract_empty.pdf").stat().st_size
                    if (sample_dir / "contract_empty.pdf").exists() else 0,
                    "file_kind": "contract",
                    "source_path": str(sample_dir / "contract_empty.pdf"),
                }
            ],
            "comments": [],
        },
    ]

    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(
        json.dumps({"approvals": approvals}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def generate_empty_pdf(out_path: Path) -> None:
    """生成一份没有任何文字内容的空白 PDF。

    参数：
        out_path: 输出路径。
    返回：
        无。
    关联：
        被 :func:`generate_all` 调用；用于验证"文档内容为空 → blocked"分支。
    副作用：
        写入 PDF 文件。
    """
    with pymupdf.open() as doc:
        doc.new_page(width=A4[0], height=A4[1])
        doc.save(str(out_path))


def generate_all() -> None:
    """生成全部示例文件与模拟审批系统数据。

    返回：
        无。
    关联：
        被 ``scripts/init_demo.py`` 调用，也可直接运行本脚本。
    副作用：
        在 ``settings.sample_dir`` 写入示例合同，在 ``settings.mock_oa_store``
        写入模拟审批数据；覆盖同名文件。
    """
    sample_dir = settings.sample_dir
    sample_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1/5] 生成文本层合同 PDF → {sample_dir / 'contract_normal.pdf'}")
    generate_normal_pdf(sample_dir / "contract_normal.pdf")

    print(f"[2/5] 生成扫描件合同 PDF → {sample_dir / 'contract_scanned.pdf'}")
    generate_scanned_pdf(sample_dir / "contract_scanned.pdf", sample_dir)

    print(f"[3/5] 生成 Word 合同 → {sample_dir / 'contract.docx'}")
    generate_docx(sample_dir / "contract.docx")

    print(f"[4/5] 生成空白合同 PDF → {sample_dir / 'contract_empty.pdf'}")
    generate_empty_pdf(sample_dir / "contract_empty.pdf")

    print(f"[5/5] 生成模拟审批数据 → {settings.mock_oa_store}")
    build_mock_store(settings.mock_oa_store, sample_dir)

    print("完成。示例文件清单：")
    for path in sorted(sample_dir.glob("contract*")):
        print(f"  {path.name}  {path.stat().st_size} bytes")


if __name__ == "__main__":
    generate_all()
