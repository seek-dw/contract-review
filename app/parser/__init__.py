"""文档解析层包：把合同文件转成结构化字段与条款。

包含：
- ``document``：读取 PDF/DOCX/图片，产出带位置信息的 :class:`DocumentText`；
- ``ocr``：RapidOCR 封装，负责扫描件识别与失败判定；
- ``field_extractor``：8 项基础信息字段抽取；
- ``clause_extractor``：8 类条款抽取；
- ``text_utils``：文本归一化、数值解析等纯函数工具。

设计原则：
1. 本层只做"理解文档"，不做数据库读写，不抛业务状态异常（除解析类异常）；
2. 所有抽取结果必须带 :class:`app.domain.schemas.FieldEvidence`，禁止只返回值；
3. 抽取失败要区分"未找到"与"找到了但解析失败"，不允许统一报空。
"""
