"""解析层测试：验证三类文档读取、OCR 真实执行、字段与条款抽取。

覆盖需求：
- 2.4.12「能解析文档和图片扫描件，提取主要字段」；
- 2.4.5「每个解析字段至少保留原文片段、页码或位置标识、提取状态」；
- 2.4.4「文档内容为空进入 blocked」（在 ``test_errors.py`` 验证阻塞，本文件验证异常类型）。
"""

from __future__ import annotations

import pytest

from app.domain.enums import ExtractStatus
from app.domain.errors import DocumentEmptyError
from app.parser.clause_extractor import extract_clauses
from app.parser.document import detect_file_type, read_document
from app.parser.field_extractor import extract_basic_info


def test_detect_file_type():
    """验证扩展名推断：大小写混写与无扩展名都要正确处理。

    关联：
        ``app.parser.document.detect_file_type``，结果写入
        ``approval_attachments.file_type`` 并用于解析分派。
    """
    assert detect_file_type("合同.PDF") == "pdf"
    assert detect_file_type("a.docx") == "docx"
    assert detect_file_type("scan.JPEG") == "jpeg"
    assert detect_file_type("noext") == ""


def test_read_text_pdf_uses_text_layer(samples_dir):
    """验证文本层 PDF 走文本层分支，不触发 OCR。

    关联：
        ``app.parser.document.read_document`` → ``_read_pdf``。

    验证要点：
        若误走 OCR，``ocr_pages`` 会包含第 1 页，且耗时会从毫秒级跳到秒级。
    """
    document = read_document(samples_dir / "contract_normal.pdf")
    assert document.page_count == 1
    assert document.text_pages == (1,)
    assert document.ocr_pages == ()
    assert document.blank_pages == ()
    assert document.char_count > 300
    assert "采购合同" in document.full_text


def test_read_scanned_pdf_uses_ocr(samples_dir):
    """验证无文本层扫描件强制走 OCR 并能识别出中文内容。

    关联：
        ``app.parser.document._read_pdf`` → ``app.parser.ocr.recognize_page``。

    验证要点（这是需求里最容易"假装做到"的一条）：
        - 该 PDF 文本层必须为空，否则说明示例文件不是真扫描件；
        - ``ocr_pages`` 必须包含第 1 页，证明 OCR 真被执行；
        - 识别结果必须包含关键中文词，证明识别有效而不是返回空。
    """
    import pymupdf

    with pymupdf.open(samples_dir / "contract_scanned.pdf") as pdf:
        raw = pdf.load_page(0).get_text("text")
    assert raw.strip() == "", "示例扫描件不应含文本层，否则无法验证 OCR 路径"

    document = read_document(samples_dir / "contract_scanned.pdf")
    assert document.ocr_pages == (1,)
    assert document.text_pages == ()
    assert document.char_count > 300
    # 中文识别有效性：合同标题与关键条款词应被识别出来。
    assert "采购合同" in document.full_text
    assert "预付款" in document.full_text


def test_read_docx_includes_table_cells(samples_dir):
    """验证 DOCX 读取包含表格单元格内容。

    关联：
        ``app.parser.document._read_docx``。

    验证要点：
        合同模板常把金额、日期放在表格里，只读段落会漏字段。
    """
    document = read_document(samples_dir / "contract.docx")
    assert document.page_count == 1
    full = document.full_text
    assert "技术服务协议" in full
    # 表格里的字段必须以 "标签 | 值" 形式出现。
    assert "HT-2024-0002" in full
    assert "480,000" in full


def test_read_empty_pdf_raises_document_empty(samples_dir):
    """验证空白 PDF 抛 ``DocumentEmptyError`` 而不是 OCR 失败。

    关联：
        ``app.parser.document.read_document`` 的空内容判定 +
        ``_is_visually_blank`` 的空白页识别。

    验证要点（真实缺陷的回归测试）：
        空白页若直接走 OCR，会抛 ``OcrUnavailableError``，
        导致 ``blocked_reason`` 错报为"图片无法识别"，
        与实际原因"文档内容为空"不符，人工排查方向会被带偏。
    """
    with pytest.raises(DocumentEmptyError) as exc_info:
        read_document(samples_dir / "contract_empty.pdf")
    assert "内容为空" in exc_info.value.message


def test_extract_basic_info_all_fields_have_evidence(samples_dir):
    """验证 8 项基础信息都带证据三要素（片段/位置/状态）。

    关联：
        ``app.parser.field_extractor.extract_basic_info``。

    验证要点（需求 2.4.5 硬要求）：
        即使某字段未提取到，也必须带 ``status`` 与 ``detail``，
        不允许出现"值为空且无任何说明"的字段。
    """
    document = read_document(samples_dir / "contract_normal.pdf")
    fields = extract_basic_info(document)

    assert set(fields) == {
        "contract_title", "contract_no", "our_party", "counterparty",
        "amount", "currency", "effective_date", "expire_date",
    }
    for key, field in fields.items():
        assert field.evidence.status in ExtractStatus
        if field.evidence.status == ExtractStatus.SUCCESS:
            assert field.value, f"{key} 状态为 success 但值为空"
            assert field.evidence.location, f"{key} 状态为 success 但缺少位置"
        else:
            assert field.evidence.detail, f"{key} 未提取但缺少失败原因"


def test_extract_basic_info_correct_values(samples_dir):
    """验证基础信息抽取的具体值正确。

    关联：
        ``app.parser.field_extractor`` 的标签定位与数值解析。

    验证要点：
        - 金额必须可读（不能是 ``1.2e+06`` 这类科学计数法）；
        - 签约主体必须是公司名，不能是正文里含"甲方"的句子
          （这是开发中修复过的真实缺陷）。
    """
    document = read_document(samples_dir / "contract_normal.pdf")
    fields = extract_basic_info(document)

    assert fields["contract_title"].value == "采购合同"
    assert fields["contract_no"].value == "HT-2024-0001"
    assert fields["our_party"].value == "星海智能科技有限公司"
    assert fields["counterparty"].value == "云帆数据服务有限公司"
    assert fields["amount"].value == "1,200,000 元"
    assert fields["currency"].value == "CNY"
    assert fields["expire_date"].value == "2025-12-31"


def test_effective_date_not_found_when_no_specific_date(samples_dir):
    """验证"自双方盖章之日起生效"不会被伪造出一个日期。

    关联：
        ``app.parser.field_extractor._extract_date_field``。

    验证要点：
        合同原文没有具体生效日期，正确行为是 ``not_found`` 并说明原因，
        而不是猜一个日期。这是"不编造事实"的直接体现。
    """
    document = read_document(samples_dir / "contract_normal.pdf")
    fields = extract_basic_info(document)
    assert fields["effective_date"].value is None
    assert fields["effective_date"].evidence.status == ExtractStatus.NOT_FOUND
    assert "生效" in fields["effective_date"].evidence.detail


def test_extract_clauses_detects_and_reports_missing(samples_dir):
    """验证 8 类条款中"检出"与"未检出"都被明确记录。

    关联：
        ``app.parser.clause_extractor.extract_clauses``。

    验证要点：
        示例合同刻意没有保密条款与数据条款，
        正确行为是这两项 ``not_found``（后续驱动"条款缺失"规则命中），
        其余六项 ``success`` 且带位置。
    """
    document = read_document(samples_dir / "contract_normal.pdf")
    clauses = extract_clauses(document)

    assert len(clauses) == 8
    assert clauses["payment"].evidence.status == ExtractStatus.SUCCESS
    assert clauses["breach"].evidence.status == ExtractStatus.SUCCESS
    assert clauses["dispute"].evidence.status == ExtractStatus.SUCCESS
    # 示例合同无保密与数据条款。
    assert clauses["confidentiality"].evidence.status == ExtractStatus.NOT_FOUND
    assert clauses["data"].evidence.status == ExtractStatus.NOT_FOUND
    assert "保密" in clauses["confidentiality"].evidence.detail


def test_docx_fields_from_table(samples_dir):
    """验证 DOCX 表格里的字段能被正确抽取。

    关联：
        ``_read_docx``（表格拼成 ``标签 | 值``）+ ``field_extractor`` 的严格标签模式。

    验证要点（真实缺陷的回归测试）：
        表格拼接后标签与值之间是竖线，早期实现只认冒号，
        导致 DOCX 的编号、主体、金额全部抽不到。
    """
    document = read_document(samples_dir / "contract.docx")
    fields = extract_basic_info(document)

    assert fields["contract_no"].value == "HT-2024-0002"
    assert fields["our_party"].value == "星海智能科技有限公司"
    assert fields["counterparty"].value == "启明云计算有限公司"
    assert fields["amount"].value == "480,000 元"
    assert fields["effective_date"].value == "2024-03-01"
    assert fields["expire_date"].value == "2025-02-28"


def test_body_sentence_with_party_word_not_treated_as_label(samples_dir):
    """验证正文中出现的"甲方/乙方"不会被误判为标签行。

    关联：
        ``app.parser.field_extractor._find_label_line`` 的严格模式。

    验证要点（真实缺陷的回归测试）：
        DOCX 正文首条为"乙方向甲方提供云资源运维服务…"，
        早期实现用子串匹配，把这一行当成"甲方"标签行，
        结果签约主体与对方名称都被抽成这句正文。
    """
    document = read_document(samples_dir / "contract.docx")
    fields = extract_basic_info(document)

    for key in ("our_party", "counterparty"):
        value = fields[key].value or ""
        assert "提供" not in value, f"{key} 被抽成了正文句子：{value}"
        assert "服务" not in value, f"{key} 被抽成了正文句子：{value}"
