"""解析服务：编排文档解析并持久化解析结果。

职责：
- 对任务下的合同附件执行解析（下载 → 读取 → 字段抽取 → 条款抽取）；
- 把解析产物写入 ``contract_parses``（基础信息、条款、状态、失败原因、元信息）；
- 把解析异常转换为任务阻塞（需求 2.4.4 的四种情况）；
- 解析成功后把任务推进到 ``reviewing``。

非职责：
- 不做文件读取与字段抽取的具体实现（见 ``app.parser``）；
- 不执行规则（见 ``review_service``）。

设计原因：
1. **一次解析一条记录，追加不覆盖**。
   重试后成功时，失败历史仍需保留，否则无法回答"这份合同第一次为什么没解析成功"。
   因此本模块每次解析都 ``INSERT`` 新记录，读取时取该任务最新一条。
2. **多附件合并解析**。企业审批单常见"正文 + 附件清单"两个文件。
   本模块按附件顺序逐个解析，基础信息取"第一个成功提取到该字段的附件"，
   条款取"第一个检出该条款的附件"。这样即使某个附件是纯表格，
   也能被合并进整体结论，而不是只认第一个文件。
3. **异常分类精确到原因**。
   OCR 失败、内容为空、格式不支持、附件缺失是四种不同的阻塞原因，
   必须分别记录，因为它们的排查方向和修复动作完全不同。

主要协作模块：
- ``tools/contract_tools.py::parse_contract_document`` 是单附件解析入口；
- :func:`parse_task_documents` 是任务级编排，被演示脚本与工具编排调用；
- 结果被 ``review_service`` 读取（取最新一条 ``contract_parses``）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from app.db.base import session_scope
from app.db.models import ContractParse
from app.domain.enums import ExtractStatus, LogType, ParseStatus, TaskStatus
from app.domain.errors import (
    AttachmentMissingError,
    ContractReviewError,
    NotFoundError,
    UnsupportedDocumentError,
)
from app.domain.schemas import (
    DocumentText,
    FieldEvidence,
    ParseOutcome,
    ParsedField,
    TextLine,
)
from app.parser.clause_extractor import extract_clauses
from app.parser.document import read_document
from app.parser.field_extractor import extract_basic_info
from app.services import attachment_service
from app.services.log_service import append_log_in_session
from app.services.task_service import get_task, mark_blocked, mark_status


def _field_to_dict(field: ParsedField) -> dict:
    """把字段对象序列化为可存 JSON 的字典。

    参数：
        field: 解析出的字段。
    返回：
        含 ``label`` / ``value`` / ``snippet`` / ``location`` / ``status`` / ``detail``
        的字典，对应需求 2.4.5 要求的"值 + 原文片段 + 位置 + 提取状态"。
    关联：
        被 :func:`_persist_parse` 写入 ``basic_info_json`` 与 ``clause_info_json``。
    """
    return {
        "label": field.label,
        "value": field.value,
        "snippet": field.evidence.snippet,
        "location": field.evidence.location,
        "status": field.evidence.status.value,
        "detail": field.evidence.detail,
    }


def _parse_single_attachment(
    document_id: int, local_path: Path
) -> tuple[ParseOutcome, float]:
    """解析单个附件文件。

    参数：
        document_id: 附件主键，仅用于日志与异常消息。
        local_path: 本地文件路径。
    返回：
        ``(解析产物, 耗时秒数)``。
    关联：
        被 :func:`parse_contract_document` 与 :func:`parse_task_documents` 调用。
    异常：
        解析层抛出的 :class:`UnsupportedDocumentError`、``OcrUnavailableError``、
        ``DocumentEmptyError`` 原样向上传递，由调用方转成任务阻塞。
    """
    started = time.perf_counter()
    document: DocumentText = read_document(local_path)
    basic_info = extract_basic_info(document)
    clauses = extract_clauses(document)
    elapsed = time.perf_counter() - started

    # 解析状态：所有字段都成功为 success；有失败或未找到为 partial。
    # 注意 partial 仍会进入规则审查——"未找到"本身就是要审查的风险点
    # （例如没有保密条款），不能因为字段缺失就阻断流程。
    all_fields = list(basic_info.values()) + list(clauses.values())
    has_gap = any(
        field.evidence.status != ExtractStatus.SUCCESS for field in all_fields
    )
    status = ParseStatus.PARTIAL if has_gap else ParseStatus.SUCCESS

    outcome = ParseOutcome(
        status=status,
        basic_info=basic_info,
        clauses=clauses,
        document=document,
        error_message="",
        meta={
            "document_id": document_id,
            "page_count": document.page_count,
            "text_pages": list(document.text_pages),
            "ocr_pages": list(document.ocr_pages),
            "char_count": document.char_count,
            "elapsed_seconds": round(elapsed, 3),
        },
    )
    return outcome, elapsed


def _persist_parse(
    task_id: int, document_id: int, outcome: ParseOutcome
) -> int:
    """把解析产物写入 ``contract_parses``。

    参数：
        task_id: 任务主键。
        document_id: 附件主键。
        outcome: 解析产物。
    返回：
        新建解析记录的主键。
    关联：
        被 :func:`parse_contract_document` 与 :func:`parse_task_documents` 调用；
        ``review_service`` 通过 :func:`load_latest_parse` 读取。
    设计原因：
        必须把**原文行**一并存入 ``parse_meta_json``。原因是规则引擎的 keyword 模式
        需要在全文里检索（例如"自动续约"可能出现在任何条款中），
        而 ``basic_info_json``/``clause_info_json`` 只存了截断后的片段，
        不足以支撑全文检索。若不存原文，规则只能看到片段，会漏掉大量命中。
        存原文行而非单个大字符串，是为了保留页码与行号，证据位置才准确。
    """
    meta = dict(outcome.meta)
    # 原文行以 [页, 行, 文本] 三元组保存，比保存大段字符串更省空间且保留定位。
    meta["text_lines"] = [
        [line.page, line.line_no, line.text] for line in outcome.document.lines
    ]

    with session_scope() as db:
        record = ContractParse(
            task_id=task_id,
            attachment_id=document_id,
            basic_info_json=json.dumps(
                {key: _field_to_dict(field) for key, field in outcome.basic_info.items()},
                ensure_ascii=False,
            ),
            clause_info_json=json.dumps(
                {key: _field_to_dict(field) for key, field in outcome.clauses.items()},
                ensure_ascii=False,
            ),
            parse_status=outcome.status.value,
            parse_error=outcome.error_message,
            parse_meta_json=json.dumps(meta, ensure_ascii=False),
        )
        db.add(record)
        db.flush()
        return record.id


def _persist_parse_failure(
    task_id: int, document_id: int, error: ContractReviewError
) -> None:
    """记录一次失败的解析。

    参数：
        task_id: 任务主键。
        document_id: 附件主键。
        error: 触发失败的异常。
    返回：
        无。
    关联：
        被 :func:`parse_contract_document` 的异常分支调用。
    设计原因：
        需求 2.4.5 明确要求"解析失败时，要记录失败原因，不允许只返回空结果"。
        因此失败也必须写一条 ``contract_parses`` 记录（``parse_status=failed``），
        让人能在页面上看到失败原因，而不是只看到任务卡在 blocked。
    """
    with session_scope() as db:
        record = ContractParse(
            task_id=task_id,
            attachment_id=document_id,
            basic_info_json="{}",
            clause_info_json="{}",
            parse_status=ParseStatus.FAILED.value,
            parse_error=error.message,
            parse_meta_json=json.dumps({"error_reason": error.reason}, ensure_ascii=False),
        )
        db.add(record)


def parse_contract_document(document_id: int) -> dict:
    """解析单个附件（工具接口 ``parse_contract_document`` 的实现）。

    参数：
        document_id: 附件主键，即工具接口的 ``document_id``。
    返回：
        字典，含 ``document_id``、``task_id``、``parse_id``、``parse_status``、
        ``basic_info``、``clauses``、``meta``。
    关联：
        被 ``tools/contract_tools.py::parse_contract_document`` 调用。
    异常：
        附件不存在抛 :class:`NotFoundError`；
        文件未下载抛 :class:`AttachmentMissingError`；
        解析失败抛对应解析异常，同时任务进入 blocked 并记录失败解析。
    """
    attachment = attachment_service.get_attachment(document_id)
    task_id = attachment["task_id"]

    if attachment["download_status"] != "success" or not attachment["local_path"]:
        error = AttachmentMissingError(
            f"附件尚未成功下载，无法解析：document_id={document_id}，"
            f"文件名 {attachment['file_name']}，下载状态 {attachment['download_status']}"
        )
        mark_blocked(task_id, error)
        raise error

    local_path = Path(attachment["local_path"])
    if not local_path.exists():
        # 数据库有记录但磁盘文件丢失（例如被手工清理），同样属于附件缺失。
        error = AttachmentMissingError(
            f"附件本地文件不存在：{local_path}（document_id={document_id}）"
        )
        mark_blocked(task_id, error)
        raise error

    mark_status(task_id, TaskStatus.PARSING, f"开始解析附件 {attachment['file_name']}")

    try:
        outcome, elapsed = _parse_single_attachment(document_id, local_path)
    except ContractReviewError as exc:
        _persist_parse_failure(task_id, document_id, exc)
        mark_blocked(task_id, exc)
        raise

    parse_id = _persist_parse(task_id, document_id, outcome)

    with session_scope() as db:
        append_log_in_session(
            db, LogType.PARSE,
            f"解析完成：{attachment['file_name']}，状态 {outcome.status.value}，"
            f"共 {outcome.document.page_count} 页"
            f"（文本层 {len(outcome.document.text_pages)} 页、"
            f"OCR {len(outcome.document.ocr_pages)} 页），"
            f"有效字符 {outcome.document.char_count}，耗时 {elapsed:.2f}s",
            task_id=task_id,
        )

    # 解析成功，推进到 reviewing，等待规则审查。
    mark_status(task_id, TaskStatus.REVIEWING, "解析完成，等待规则审查")

    return {
        "document_id": document_id,
        "task_id": task_id,
        "parse_id": parse_id,
        "parse_status": outcome.status.value,
        "basic_info": {
            key: _field_to_dict(field) for key, field in outcome.basic_info.items()
        },
        "clauses": {
            key: _field_to_dict(field) for key, field in outcome.clauses.items()
        },
        "meta": outcome.meta,
    }


def _merge_field_maps(
    merged: dict[str, ParsedField], incoming: dict[str, ParsedField]
) -> None:
    """把新附件的字段合并进累计结果。

    参数：
        merged: 累计字段映射（原地修改）。
        incoming: 新附件的字段映射。
    返回：
        无。
    关联：
        被 :func:`parse_task_documents` 调用。
    合并规则：
        只在新字段"成功提取"且累计结果中该字段"尚未成功"时覆盖。
        这样第一个成功提取到的值会保留，避免后一个附件（可能是补充说明）
        覆盖掉正文里的权威字段。
    """
    for key, field in incoming.items():
        current = merged.get(key)
        if current is None:
            merged[key] = field
            continue
        if (
            field.evidence.status == ExtractStatus.SUCCESS
            and current.evidence.status != ExtractStatus.SUCCESS
        ):
            merged[key] = field


def parse_task_documents(task_id: int, allow_reprocess: bool = False) -> dict:
    """解析任务下的全部附件并合并结果。

    参数：
        task_id: 任务主键。
        allow_reprocess: 是否允许对已完成（``done``）的任务重新处理，默认 ``False``。
    返回：
        字典，含 ``task_id``、``parse_id``（合并记录）、``attachments``（逐附件明细）、
        ``parse_status``、``basic_info``、``clauses``、``meta``。
    关联：
        被演示脚本 ``scripts/demo_flow.py`` 与工具编排调用。
    异常：
        任一附件解析失败时任务已置 blocked，异常向上抛出；
        任务已完成且未显式允许重处理时抛 :class:`TaskStateError`。

    为什么默认拒绝对已完成任务重处理：
        已 ``done`` 的任务意味着"审查意见已写入审批评论区"。
        若允许直接重跑，会出现两个问题：
        1. **浪费**：扫描件 OCR 每页约 5 秒，重跑一次就多花一次；
        2. **误导**：任务状态会从 ``done`` 退回 ``parsing``，
           页面上看起来"已完成的任务又被打开了"，审批人会怀疑之前的意见是否作废。
        若确实需要重新审查（例如合同被换版），调用方应显式传
        ``allow_reprocess=True``，让这次重跑成为**有意的**动作而非误触。
    """
    task = get_task(task_id)

    if task["task_status"] == TaskStatus.DONE.value and not allow_reprocess:
        from app.domain.errors import TaskStateError

        raise TaskStateError(
            f"任务 {task_id}（审批编号 {task['approval_code']}）已完成并回写成功，"
            "如需重新审查请显式指定 allow_reprocess=True"
        )

    attachments = attachment_service.download_all_attachments(task_id)

    merged_basic: dict[str, ParsedField] = {}
    merged_clauses: dict[str, ParsedField] = {}
    merged_lines: list[TextLine] = []
    per_attachment: list[dict] = []
    total_pages = 0
    all_ocr_pages: dict[int, list[int]] = {}
    total_chars = 0
    total_elapsed = 0.0
    ocr_pages_union: list[int] = []
    text_pages_union: list[int] = []

    mark_status(task_id, TaskStatus.PARSING, f"开始解析 {len(attachments)} 个附件")

    for attachment in attachments:
        document_id = attachment["document_id"]
        local_path = Path(attachment["local_path"])

        try:
            outcome, elapsed = _parse_single_attachment(document_id, local_path)
        except ContractReviewError as exc:
            _persist_parse_failure(task_id, document_id, exc)
            mark_blocked(task_id, exc)
            raise

        _persist_parse(task_id, document_id, outcome)
        _merge_field_maps(merged_basic, outcome.basic_info)
        _merge_field_maps(merged_clauses, outcome.clauses)

        # 累积原文行。合并记录必须保留全部附件的原文，
        # 否则规则引擎的 keyword 全文检索只能看到字段片段，
        # 会漏掉"自动续约""数据出境"这类不落在任何已抽取字段里的风险表述。
        # 行号按"全局连续"重排，避免多个附件的第1页第1行相互覆盖导致定位歧义；
        # 同时在证据位置里保留原附件名，便于人工回到具体文件核对。
        for line in outcome.document.lines:
            merged_lines.append(
                TextLine(
                    page=line.page,
                    line_no=len(merged_lines) + 1,
                    text=f"[{attachment['file_name']}] {line.text}",
                )
            )

        total_pages += outcome.document.page_count
        total_chars += outcome.document.char_count
        total_elapsed += elapsed
        all_ocr_pages[document_id] = list(outcome.document.ocr_pages)
        ocr_pages_union.extend(outcome.document.ocr_pages)
        text_pages_union.extend(outcome.document.text_pages)

        per_attachment.append(
            {
                "document_id": document_id,
                "file_name": attachment["file_name"],
                "parse_status": outcome.status.value,
                "page_count": outcome.document.page_count,
                "text_pages": list(outcome.document.text_pages),
                "ocr_pages": list(outcome.document.ocr_pages),
                "char_count": outcome.document.char_count,
                "elapsed_seconds": round(elapsed, 3),
            }
        )

    # 合并结果再写一条记录，作为"任务级"的权威解析产物。
    # 这样 review_service 只需读取最新一条即可拿到全部字段与全文，
    # 不必自己再遍历多个附件记录做合并。
    merged_document = DocumentText(
        lines=tuple(merged_lines),
        page_count=total_pages,
        ocr_pages=tuple(sorted(set(ocr_pages_union))),
        text_pages=tuple(sorted(set(text_pages_union))),
    )
    has_gap = any(
        field.evidence.status != ExtractStatus.SUCCESS
        for field in list(merged_basic.values()) + list(merged_clauses.values())
    )
    merged_outcome = ParseOutcome(
        status=ParseStatus.PARTIAL if has_gap else ParseStatus.SUCCESS,
        basic_info=merged_basic,
        clauses=merged_clauses,
        document=merged_document,
        error_message="",
        meta={
            "merged": True,
            "attachment_count": len(attachments),
            "page_count": total_pages,
            "char_count": total_chars,
            "ocr_pages_by_attachment": all_ocr_pages,
            "elapsed_seconds": round(total_elapsed, 3),
        },
    )
    merged_parse_id = _persist_parse(task_id, attachments[0]["document_id"], merged_outcome)

    with session_scope() as db:
        append_log_in_session(
            db, LogType.PARSE,
            f"任务级解析完成：共 {len(attachments)} 个附件、{total_pages} 页、"
            f"{total_chars} 字符，状态 {merged_outcome.status.value}",
            task_id=task_id,
        )

    mark_status(task_id, TaskStatus.REVIEWING, "全部附件解析完成，等待规则审查")

    return {
        "task_id": task_id,
        "approval_code": task["approval_code"],
        "parse_id": merged_parse_id,
        "parse_status": merged_outcome.status.value,
        "attachments": per_attachment,
        "basic_info": {
            key: _field_to_dict(field) for key, field in merged_basic.items()
        },
        "clauses": {
            key: _field_to_dict(field) for key, field in merged_clauses.items()
        },
        "meta": merged_outcome.meta,
    }


def _dict_to_field(key: str, raw: dict) -> ParsedField:
    """把入库的字段字典还原为 :class:`ParsedField`。

    参数：
        key: 字段键。
        raw: 来自 ``basic_info_json`` 或 ``clause_info_json`` 的字典。
    返回：
        字段对象，证据三要素（片段、位置、状态）完整还原。
    关联：
        被 :func:`load_parse_outcome` 调用。
    """
    return ParsedField(
        key=key,
        label=raw.get("label", key),
        value=raw.get("value"),
        evidence=FieldEvidence(
            snippet=raw.get("snippet", ""),
            location=raw.get("location", ""),
            status=ExtractStatus(raw.get("status", ExtractStatus.NOT_FOUND.value)),
            detail=raw.get("detail", ""),
        ),
    )


def _latest_parse_record(db, task_id: int) -> ContractParse | None:
    """取出任务应当用于审查的解析记录。

    参数：
        db: 当前会话。
        task_id: 任务主键。
    返回：
        解析记录；无记录时返回 ``None``。
    关联：
        被 :func:`load_parse_outcome` 与 :func:`load_latest_parse` 调用。
    选择规则（按优先级）：
        1. **合并记录**（``parse_meta_json`` 中 ``merged=true``）——
           它汇总了全部附件的字段与全文，是审查唯一正确的输入；
        2. 若没有合并记录（只调用了单附件解析接口），退化为最新一条记录。

    为什么必须优先合并记录：
        多附件任务下，最后一次单附件解析的记录只含**那一个附件**的字段与原文。
        若审查直接取"最新一条"，会把另一个附件的内容全部丢掉，
        导致"保密条款缺失"之类的规则被误命中——那是数据丢失造成的假风险，
        比漏报更危险，因为它会让审批人去做无意义的确认。
    """
    merged = (
        db.query(ContractParse)
        .filter(
            ContractParse.task_id == task_id,
            ContractParse.parse_meta_json.like('%"merged": true%'),
        )
        .order_by(ContractParse.id.desc())
        .first()
    )
    if merged is not None:
        return merged
    return (
        db.query(ContractParse)
        .filter(ContractParse.task_id == task_id)
        .order_by(ContractParse.id.desc())
        .first()
    )


def load_parse_outcome(task_id: int) -> ParseOutcome:
    """读取任务应当用于审查的解析记录并还原为 :class:`ParseOutcome`。

    参数：
        task_id: 任务主键。
    返回：
        完整的解析产物，含字段、条款与原文行（用于全文检索）。
    关联：
        被 ``review_service.run_review`` 调用，是解析层与规则层之间的桥。
    异常：
        无解析记录时抛 :class:`NotFoundError`。
    设计原因：
        规则引擎设计为"纯逻辑、不碰数据库"，因此需要一次显式还原。
        把还原逻辑集中在此函数，避免规则服务里散落 JSON 解析代码。
        记录选择规则见 :func:`_latest_parse_record`。
    """
    with session_scope() as db:
        record = _latest_parse_record(db, task_id)
        if record is None:
            raise NotFoundError(f"任务 {task_id} 尚无解析记录，请先执行解析")

        basic_raw = json.loads(record.basic_info_json or "{}")
        clause_raw = json.loads(record.clause_info_json or "{}")
        meta = json.loads(record.parse_meta_json or "{}")

        basic_info = {key: _dict_to_field(key, raw) for key, raw in basic_raw.items()}
        clauses = {key: _dict_to_field(key, raw) for key, raw in clause_raw.items()}

        # 还原原文行。旧记录（未存 text_lines）退化为用各字段 snippet 拼接，
        # 保证向后兼容：历史数据仍可审查，只是全文检索范围变窄。
        raw_lines = meta.get("text_lines")
        if raw_lines:
            lines = tuple(
                TextLine(page=int(item[0]), line_no=int(item[1]), text=str(item[2]))
                for item in raw_lines
            )
        else:
            lines = _fallback_lines_from_fields(basic_info, clauses)

        document = DocumentText(
            lines=lines,
            page_count=int(meta.get("page_count", 1)),
            ocr_pages=tuple(meta.get("ocr_pages", [])),
            text_pages=tuple(meta.get("text_pages", [])),
        )

        return ParseOutcome(
            status=ParseStatus(record.parse_status),
            basic_info=basic_info,
            clauses=clauses,
            document=document,
            error_message=record.parse_error,
            meta=meta,
        )


def _fallback_lines_from_fields(
    basic_info: dict[str, ParsedField], clauses: dict[str, ParsedField]
) -> tuple[TextLine, ...]:
    """在缺少原文行时，用字段片段拼出可检索文本。

    参数：
        basic_info: 基础信息字段。
        clauses: 条款字段。
    返回：
        由片段构成的文本行元组。
    关联：
        被 :func:`load_parse_outcome` 在旧记录缺 ``text_lines`` 时调用。
    说明：
        这是降级路径：只能覆盖已抽取到的片段，全文检索会漏掉未抽取的内容。
        新写入的记录都会带 ``text_lines``，因此该分支只影响历史数据。
    """
    lines: list[TextLine] = []
    index = 0
    for field in list(basic_info.values()) + list(clauses.values()):
        text = field.value or field.evidence.snippet
        if not text:
            continue
        for piece in text.splitlines():
            piece = piece.strip()
            if not piece:
                continue
            index += 1
            lines.append(TextLine(page=1, line_no=index, text=piece))
    return tuple(lines)


def load_latest_parse(task_id: int) -> dict:
    """读取任务应当展示的解析记录（面向展示的字典形态）。

    参数：
        task_id: 任务主键。
    返回：
        字典，含 ``parse_id``、``parse_status``、``basic_info``、``clauses``、
        ``parse_error``、``meta``。
    关联：
        被调用端「解析结果模块」与 ``tools/contract_tools.py`` 使用；
        需要给规则引擎用的对象形态请用 :func:`load_parse_outcome`。
    异常：
        该任务尚无解析记录时抛 :class:`NotFoundError`。
    说明：
        记录选择规则与审查一致（优先合并记录），保证"页面看到的字段"
        与"规则判断依据的字段"完全同源——否则会出现
        "页面显示保密条款已抽取，但规则报缺失"这种无法解释的矛盾。
    """
    with session_scope() as db:
        record = _latest_parse_record(db, task_id)
        if record is None:
            raise NotFoundError(f"任务 {task_id} 尚无解析记录，请先执行解析")
        return {
            "parse_id": record.id,
            "task_id": task_id,
            "attachment_id": record.attachment_id,
            "parse_status": record.parse_status,
            "parse_error": record.parse_error,
            "basic_info": json.loads(record.basic_info_json or "{}"),
            "clauses": json.loads(record.clause_info_json or "{}"),
            "meta": json.loads(record.parse_meta_json or "{}"),
        }


def parse_status_of(task_id: int) -> dict | None:
    """查询任务的解析状态摘要（不加载完整字段）。

    参数：
        task_id: 任务主键。
    返回：
        含 ``parse_status``、``parse_error``、``meta`` 的字典；无记录返回 ``None``。
    关联：
        被调用端「实时任务区」与 ``tools/contract_tools.py`` 的状态查询使用，
        避免为了显示状态而加载整份 JSON。
    """
    with session_scope() as db:
        record = _latest_parse_record(db, task_id)
        if record is None:
            return None
        return {
            "parse_id": record.id,
            "parse_status": record.parse_status,
            "parse_error": record.parse_error,
            "meta": json.loads(record.parse_meta_json or "{}"),
            "created_at": record.created_at.isoformat(timespec="seconds"),
        }


def list_parse_history(task_id: int) -> list[dict]:
    """查询任务的解析历史（含失败记录）。

    参数：
        task_id: 任务主键。
    返回：
        解析记录摘要列表，按主键升序（时间正序）。
    关联：
        被调用端「解析结果模块」展示"重试前失败、重试后成功"的对比。
    """
    with session_scope() as db:
        rows = (
            db.query(ContractParse)
            .filter(ContractParse.task_id == task_id)
            .order_by(ContractParse.id.asc())
            .all()
        )
        return [
            {
                "parse_id": row.id,
                "attachment_id": row.attachment_id,
                "parse_status": row.parse_status,
                "parse_error": row.parse_error,
                "meta": json.loads(row.parse_meta_json or "{}"),
                "created_at": row.created_at.isoformat(timespec="seconds"),
            }
            for row in rows
        ]
