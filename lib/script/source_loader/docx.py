"""DOCX 抽取：docx2txt 主路径 + mammoth 兜底。"""

import logging
import re
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

import docx2txt
import mammoth

from .base import ExtractedText
from .errors import CorruptFileError

logger = logging.getLogger(__name__)
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_SPEAKER_CELL = re.compile(
    r"(?:[\u4e00-\u9fff·]{1,8}|[A-Z][A-Z .'-]{0,30})(?:\s*[（(][^\n()（）]+[）)])?[:：]?"
)
_TABLE_LABELS = frozenset({"人物", "角色", "说话人", "场景", "镜头", "动作", "台词", "对白", "内容", "时间", "备注"})
_SPEAKER_HEADERS = {"人物", "角色", "说话人", "speaker", "character"}
_DIALOGUE_HEADERS = {"台词", "对白", "dialogue", "speech"}


class _DocumentTree(ElementTree.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise CorruptFileError(filename="document.xml", reason="DOCX 正文不支持 DTD")


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    separators = {_W + "tab": "\t", _W + "br": "\n", _W + "cr": "\n"}
    return "".join(
        (node.text or "") if node.tag == _W + "t" else separators.get(node.tag, "")
        for node in paragraph.iter()
    )


def _table_text(table: ElementTree.Element) -> list[str]:
    rows = []
    dialogue_table = False
    for row in table.findall(_W + "tr"):
        cells = ["\n".join(_paragraph_text(p) for p in cell.iter(_W + "p")).strip() for cell in row.findall(_W + "tc")]
        if len(cells) == 2:
            cue, dialogue = cells
            if cue.casefold() in _SPEAKER_HEADERS and dialogue.casefold() in _DIALOGUE_HEADERS:
                dialogue_table = True
                continue
            if (
                dialogue and (dialogue_table or cue.endswith((":", "：")))
                and _SPEAKER_CELL.fullmatch(cue) and cue.rstrip(":：") not in _TABLE_LABELS
                and not re.match(r"^[^\s:：]{1,16}[:：]", dialogue)
            ):
                # Keep line breaks within the utterance as explicit continuations.
                rows.append(cue.rstrip(":：") + "：" + dialogue.replace("\n", "\n\t"))
                continue
        rows.extend(cells)
    return rows


def _extract_table_document(path: Path) -> str | None:
    with ZipFile(path) as package:
        xml = package.read("word/document.xml")
    parser = ElementTree.XMLParser(target=_DocumentTree())
    body = ElementTree.fromstring(xml, parser=parser).find(_W + "body")
    if body is None or body.find(_W + "tbl") is None:
        return None
    blocks = []
    for item in body:
        if item.tag == _W + "tbl":
            blocks.extend(_table_text(item))
        else:
            blocks.extend(_paragraph_text(p) for p in item.iter(_W + "p"))
    return "\n\n".join(blocks).strip()


class DocxExtractor:
    def extract(self, path: Path) -> ExtractedText:
        try:
            table_text = _extract_table_document(path)
            if table_text:
                return ExtractedText(text=table_text, used_encoding=None, chapter_count=0)
            text = docx2txt.process(str(path)) or ""
            if text.strip():
                return ExtractedText(text=text, used_encoding=None, chapter_count=0)
            logger.warning("docx2txt 返回空文本，回退到 mammoth: %s", path.name)
        except CorruptFileError:
            raise
        except Exception as exc:
            logger.warning("docx2txt 抽取失败 (%s)，回退到 mammoth: %s", exc, path.name)

        try:
            with path.open("rb") as fh:
                result = mammoth.extract_raw_text(fh)
            raw_text = result.value or ""
            if not raw_text.strip():
                raise CorruptFileError(filename=path.name, reason="DOCX 抽取结果为空")
            return ExtractedText(text=raw_text, used_encoding=None, chapter_count=0)
        except CorruptFileError:
            raise
        except Exception as exc:
            raise CorruptFileError(filename=path.name, reason=f"DOCX 解析失败: {exc}") from exc
