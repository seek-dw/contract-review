"""文档读取模块：把合同文件转成带位置信息的文本视图。

职责：
- 按扩展名分派读取策略：PDF（文本层 + 按页 OCR 回退）、DOCX、图片；
- 判定每一页属于"文本层页"还是"扫描页"，并记录 OCR 页列表；
- 产出 :class:`app.domain.schemas.DocumentText`，供字段抽取与规则引擎消费。

非职责：
- 不做字段与条款抽取（见 ``field_extractor`` / ``clause_extractor``）；
- 不判断文档内容是否符合业务要求（那是规则层的事）。

设计原因：
需求 2.4.5 要求"每个解析字段保留原文片段、页码或位置标识"，
因此读取阶段就必须保留行级位置，不能只返回一大段纯文本。
同时需求 2.4.4 要求区分"图片无法识别"与"内容为空"，
所以这里对每页做显式判定并记录元信息，而不是笼统地"读不出文字就报错"。

主要协作模块：
- ``services/parse_service.py`` 调用 :func:`read_document`；
- :func:`read_document` 在扫描页上调用 ``parser/ocr.py::recognize_page``。

支持的格式：
- ``.pdf``：PyMuPDF 读取；页面文本层字符数低于阈值时按扫描页处理；
- ``.docx``：python-docx 读取段落与表格文本；
- ``.png`` / ``.jpg`` / ``.jpeg`` / ``.bmp`` / ``.tif`` / ``.tiff``：直接走 OCR。

不支持的格式（如 ``.xlsx``、``.zip``）抛 :class:`UnsupportedDocumentError`，
以便服务层把原因写进 ``blocked_detail`` 而不是笼统报"解析失败"。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pymupdf
from PIL import Image

from app.config import settings
from app.domain.errors import DocumentEmptyError, UnsupportedDocumentError
from app.domain.schemas import DocumentText, TextLine
from app.parser.ocr import recognize_page
from app.parser.text_utils import collapse_blank_lines

# 扩展名到读取策略的映射。集中定义便于新增格式时只改一处。
_PDF_SUFFIXES = {".pdf"}
_DOCX_SUFFIXES = {".docx"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}


def detect_file_type(file_name: str) -> str:
    """从文件名推断文件类型标识。

    参数：
        file_name: 文件名（含扩展名）。
    返回：
        小写扩展名去掉点号，如 ``pdf`` / ``docx`` / ``png``；无扩展名时返回空串。
    关联：
        被 ``services/attachment_service.py`` 写入 ``approval_attachments.file_type``，
        也被 :func:`read_document` 用于分派读取策略。
    """
    return Path(file_name).suffix.lower().lstrip(".")


def read_document(file_path: Path) -> DocumentText:
    """读取合同文件，返回带位置信息的文本视图。

    参数：
        file_path: 本地文件绝对路径。
    返回：
        :class:`DocumentText`，包含全部文本行、页数、OCR 页列表、文本层页列表。
    关联：
        被 ``services/parse_service.py::parse_attachment`` 调用；
        内部按扩展名分派到 :func:`_read_pdf`、:func:`_read_docx`、:func:`_read_image`。
    异常：
        - 文件不存在：``FileNotFoundError``（调用方应已保证文件存在，属编程错误）；
        - 格式不支持：:class:`UnsupportedDocumentError`；
        - 全文无有效字符：:class:`DocumentEmptyError`。
    """
    suffix = file_path.suffix.lower()

    if suffix in _PDF_SUFFIXES:
        document = _read_pdf(file_path)
    elif suffix in _DOCX_SUFFIXES:
        document = _read_docx(file_path)
    elif suffix in _IMAGE_SUFFIXES:
        document = _read_image(file_path)
    else:
        raise UnsupportedDocumentError(
            f"不支持的文档格式 {suffix or '(无扩展名)'}，"
            "当前支持 PDF / DOCX / PNG / JPG / BMP / TIF"
        )

    # 全文有效字符不足时判定为"文档内容为空"。
    # 这是需求 2.4.4 要求进入 blocked 的四种情况之一，必须与"OCR 失败"区分开：
    # 空文档说明文件本身没有可审查内容，OCR 失败说明技术上没读出来。
    if document.char_count < settings.empty_document_char_threshold:
        blank_note = (
            f"（其中视觉空白页 {list(document.blank_pages)}）"
            if document.blank_pages
            else ""
        )
        raise DocumentEmptyError(
            f"文档内容为空：共 {document.page_count} 页{blank_note}，"
            f"有效字符仅 {document.char_count} 个"
            f"（阈值 {settings.empty_document_char_threshold}）"
        )
    return document


def _read_pdf(file_path: Path) -> DocumentText:
    """读取 PDF，逐页判断使用文本层还是 OCR。

    参数：
        file_path: PDF 文件路径。
    返回：
        :class:`DocumentText`。
    关联：
        被 :func:`read_document` 调用；扫描页交给 ``parser/ocr.py::recognize_page``。
    副作用：
        对扫描页会在临时目录生成页面图片，读取完成后删除。
    设计原因：
        企业合同常见"正文电子版 + 盖章页扫描件"混合形态，
        因此必须按页判定，不能整份文件一刀切。

    关于"空白页"的处理：
        空白页渲染成图片后 OCR 必然识别不到文字，会抛 ``OcrUnavailableError``。
        但"整份文档是空白"和"扫描件看不清"是两种完全不同的情况，排查方向也不同：
        前者是文件给错了，后者是图像质量问题。
        因此这里先判断页面是否**视觉空白**（像素几乎全白），
        视觉空白页直接跳过 OCR、记为空页；只有非空白的页面 OCR 失败才算识别失败。
        这样最终 :func:`read_document` 才能正确判定为 ``DocumentEmptyError``。
    """
    lines: list[TextLine] = []
    ocr_pages: list[int] = []
    text_pages: list[int] = []
    blank_pages: list[int] = []
    temp_dir = Path(tempfile.mkdtemp(prefix="contract_pages_"))

    try:
        with pymupdf.open(file_path) as pdf:
            page_count = pdf.page_count
            for index in range(page_count):
                page = pdf.load_page(index)
                page_no = index + 1
                raw_text = page.get_text("text") or ""
                page_lines = collapse_blank_lines(raw_text.splitlines())

                # 文本层字符数（不含空白）达到阈值即认为该页有可用文本层。
                text_char_count = sum(1 for ch in raw_text if not ch.isspace())
                if text_char_count >= settings.scanned_page_char_threshold:
                    text_pages.append(page_no)
                    for line_no, text in enumerate(page_lines, start=1):
                        lines.append(TextLine(page=page_no, line_no=line_no, text=text))
                    continue

                # 文本层不足：先渲染成图片，再判断是空白页还是扫描页。
                image_path = temp_dir / f"page_{page_no}.png"
                # dpi 由配置控制：200 DPI 是中文合同的识别质量与耗时平衡点。
                pixmap = page.get_pixmap(dpi=settings.ocr_dpi)
                pixmap.save(str(image_path))

                if _is_visually_blank(image_path):
                    # 视觉空白页：不是 OCR 失败，而是这页本来就没有内容。
                    blank_pages.append(page_no)
                    continue

                ocr_pages.append(page_no)
                ocr_lines = recognize_page(image_path, page_no)
                for line_no, text in enumerate(ocr_lines, start=1):
                    lines.append(TextLine(page=page_no, line_no=line_no, text=text))
    finally:
        # 临时页面图片用完即删，避免长期占用磁盘。
        for leftover in temp_dir.glob("*.png"):
            leftover.unlink(missing_ok=True)
        temp_dir.rmdir()

    return DocumentText(
        lines=tuple(lines),
        page_count=page_count,
        ocr_pages=tuple(ocr_pages),
        text_pages=tuple(text_pages),
        blank_pages=tuple(blank_pages),
    )


def _is_visually_blank(image_path: Path, white_ratio: float = 0.995) -> bool:
    """判断页面图片是否为视觉空白（几乎全白）。

    参数：
        image_path: 页面渲染图路径。
        white_ratio: 判定为空白所需的"近白像素"占比阈值。
    返回：
        近白像素占比达到阈值时返回 ``True``。
    关联：
        被 :func:`_read_pdf` 在文本层不足的页面上调用，
        用于区分"空白页"（→ 内容为空）与"扫描件"（→ 走 OCR）。
    设计原因：
        扫描件即使整页文字，也会有大量白底；但文字像素会把占比拉低。
        实测中文合同扫描页的近白像素占比在 0.90—0.97 之间，
        纯空白页接近 1.0，因此取 0.995 作为分界，既能放过正常扫描件，
        又能识别真正的空白页。
    """
    with Image.open(image_path) as image:
        # 转灰度后统计：像素值 > 240 视为"近白"。
        # 用 240 而非 255，是因为 JPEG/PNG 压缩会让纯白区域出现轻微噪点。
        gray = image.convert("L")
        histogram = gray.histogram()
        total = sum(histogram)
        if total == 0:
            return True
        near_white = sum(histogram[241:])
        return (near_white / total) >= white_ratio


def _read_docx(file_path: Path) -> DocumentText:
    """读取 DOCX 的段落与表格文本。

    参数：
        file_path: DOCX 文件路径。
    返回：
        :class:`DocumentText`；DOCX 无真实页码概念，统一记为第 1 页。
    关联：
        被 :func:`read_document` 调用。
    设计原因：
        表格里常放金额、期限等关键信息，只读段落会漏字段，因此表格单元格也必须抽取。
        行号按"段落 + 表格行"的阅读顺序连续编号，保证定位稳定。
    """
    from docx import Document as DocxDocument

    docx = DocxDocument(str(file_path))
    lines: list[TextLine] = []
    line_no = 0

    for paragraph in docx.paragraphs:
        text = (paragraph.text or "").strip()
        if not text:
            continue
        line_no += 1
        lines.append(TextLine(page=1, line_no=line_no, text=text))

    for table in docx.tables:
        for row in table.rows:
            # 一行表格拼成一行文本，用 " | " 分隔单元格，保留列间边界便于后续正则定位。
            cells = [(cell.text or "").strip() for cell in row.cells]
            row_text = " | ".join(cell for cell in cells if cell)
            if not row_text:
                continue
            line_no += 1
            lines.append(TextLine(page=1, line_no=line_no, text=row_text))

    return DocumentText(
        lines=tuple(lines), page_count=1, ocr_pages=(), text_pages=(1,)
    )


def _read_image(file_path: Path) -> DocumentText:
    """读取单张图片，全部文本来自 OCR。

    参数：
        file_path: 图片文件路径。
    返回：
        :class:`DocumentText`；图片记为第 1 页，且归入 OCR 页列表。
    关联：
        被 :func:`read_document` 调用；实际识别交给 ``parser/ocr.py::recognize_page``。
    """
    ocr_lines = recognize_page(file_path, 1)
    lines = tuple(
        TextLine(page=1, line_no=index, text=text)
        for index, text in enumerate(ocr_lines, start=1)
    )
    return DocumentText(lines=lines, page_count=1, ocr_pages=(1,), text_pages=())
