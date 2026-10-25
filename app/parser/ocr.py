"""OCR 模块：封装 RapidOCR，把扫描页图片转成文本行。

职责：
- 惰性初始化 OCR 引擎（首次调用才加载模型，避免进程启动即占用内存）；
- 把图片识别为带位置信息的文本行；
- 判定"识别不可用"（引擎异常、全部置信度过低、无任何文本），并抛
  :class:`app.domain.errors.OcrUnavailableError`。

非职责：
- 不负责把 PDF 页面渲染成图片（见 ``parser/document.py``）；
- 不负责字段抽取。

设计原因：
需求 2.4.4 明确要求"图片无法识别"进入 blocked。因此 OCR 必须能明确区分三种情况：
1. 识别成功（有文本且置信度达标）；
2. 识别不到文字（空白页 / 图片无内容）；
3. 引擎本身不可用（模型缺失、图片损坏）。
只有把 2、3 区分开，``blocked_detail`` 才能给出可操作的排查方向。

主要协作模块：
- ``parser/document.py`` 在页面文本层字符数不足时调用 :func:`recognize_page`；
- 配置来自 ``settings.ocr_dpi``（渲染由 document 负责）与
  ``settings.ocr_min_confidence``。
"""

from __future__ import annotations

from pathlib import Path

from app.config import settings
from app.domain.errors import OcrUnavailableError
from app.parser.text_utils import collapse_blank_lines

# 模块级引擎缓存。RapidOCR 初始化约 0.7 秒（含模型加载），
# 单次审查可能有多页需要识别，缓存可避免每页重复加载。
_engine = None


def get_engine():
    """获取（必要时创建）OCR 引擎单例。

    返回：
        ``RapidOCR`` 实例。
    关联：
        被 :func:`recognize_page` 调用；测试可用 :func:`reset_engine` 清理缓存。
    异常：
        依赖未安装或模型加载失败时抛 :class:`OcrUnavailableError`，
        消息中说明需要安装的包名，便于部署排查。
    """
    global _engine
    if _engine is not None:
        return _engine
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:
        raise OcrUnavailableError(
            "OCR 依赖未安装：请执行 pip install rapidocr-onnxruntime"
        ) from exc
    try:
        _engine = RapidOCR()
    except Exception as exc:  # 模型文件损坏、onnxruntime 版本不匹配等
        raise OcrUnavailableError(f"OCR 引擎初始化失败：{exc}") from exc
    return _engine


def reset_engine() -> None:
    """清除引擎缓存。

    返回：
        无。
    关联：
        测试在切换配置或验证初始化失败分支后调用，避免状态串扰。
    """
    global _engine
    _engine = None


def recognize_page(image_path: Path, page_no: int) -> list[str]:
    """识别单页图片中的文本行。

    参数：
        image_path: 已渲染好的页面图片路径。
        page_no: 页码（从 1 开始），仅用于异常消息，便于定位是哪一页失败。
    返回：
        识别出的文本行列表（已过滤空白行），按 OCR 输出的阅读顺序排列。
    关联：
        被 ``parser/document.py::_read_pdf`` 与 ``_read_image`` 调用；
        返回值由后者组装成 :class:`app.domain.schemas.TextLine`。
    异常：
        引擎不可用、图片无法读取、识别结果全为空或全部低于置信度阈值时抛
        :class:`OcrUnavailableError`，消息中包含页码与判定原因。
    副作用：
        首次调用会加载 OCR 模型（约数十 MB），耗时数秒。
    """
    engine = get_engine()

    try:
        result, _elapsed = engine(str(image_path))
    except Exception as exc:
        raise OcrUnavailableError(f"第{page_no}页 OCR 执行失败：{exc}") from exc

    if not result:
        raise OcrUnavailableError(
            f"第{page_no}页未识别到任何文字，可能是空白页或图片无有效内容"
        )

    # RapidOCR 返回结构：[[四点坐标, 文本, 置信度字符串], ...]。
    # 置信度是字符串形式，需要转 float 才能比较。
    kept: list[str] = []
    max_confidence = 0.0
    for item in result:
        if len(item) < 3:
            continue
        text = str(item[1]).strip()
        try:
            confidence = float(item[2])
        except (TypeError, ValueError):
            confidence = 0.0
        max_confidence = max(max_confidence, confidence)
        if confidence >= settings.ocr_min_confidence and text:
            kept.append(text)

    if not kept:
        raise OcrUnavailableError(
            f"第{page_no}页识别结果全部低于置信度阈值 "
            f"{settings.ocr_min_confidence}（最高 {max_confidence:.2f}），"
            "疑似图片模糊或非文字内容"
        )

    lines = collapse_blank_lines(kept)
    if not lines:
        raise OcrUnavailableError(f"第{page_no}页识别结果为空行")
    return lines
