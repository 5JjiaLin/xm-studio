from lib.script.source_loader.docx import DocxExtractor


def test_docx_extracts_paragraphs(docx_factory):
    src = docx_factory(["第一段：开篇。", "第二段：转折。"])
    result = DocxExtractor().extract(src)
    assert "第一段：开篇。" in result.text
    assert "第二段：转折。" in result.text
    assert result.used_encoding is None
    assert result.chapter_count == 0


def test_docx_falls_back_to_mammoth(monkeypatch, docx_factory):
    """docx2txt 抛错后仍保留正文的字面符号。"""
    original = "甲：保留 **暗号** 与 __标记__，以及 `门牌` #3。"
    src = docx_factory([original])

    import lib.script.source_loader.docx as mod

    def _boom(_path):
        raise RuntimeError("simulated docx2txt failure")

    monkeypatch.setattr(mod.docx2txt, "process", _boom)
    result = DocxExtractor().extract(src)
    assert result.text.strip() == original


def test_docx_preserves_table_and_paragraph_order(tmp_path):
    import pytest

    from lib.script.source_loader.errors import CorruptFileError

    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph("第一集 门外")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "甲：谁在门外？"
    table.cell(0, 1).text = "乙：不要开门。"
    document.add_paragraph("门把手缓缓转动。")
    path = tmp_path / "中文剧本.docx"
    document.save(path)
    text = DocxExtractor().extract(path).text
    assert [
        text.index(value) for value in ["第一集 门外", "甲：谁在门外？", "乙：不要开门。", "门把手缓缓转动。"]
    ] == sorted(text.index(value) for value in ["第一集 门外", "甲：谁在门外？", "乙：不要开门。", "门把手缓缓转动。"])
    path.write_bytes(b"corrupt docx")
    with pytest.raises(CorruptFileError):
        DocxExtractor().extract(path)
