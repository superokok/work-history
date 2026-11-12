"""Plain text from files, for diffs and AI context (standard library only).

Supports text/code files (UTF-8, CP949, UTF-16 with a byte order mark), HWP 5.0
(OLE compound file), HWPX and Office Open XML (.docx, .xlsx, .pptx). Returns None
for anything that is not text: images, PDF, old binary Office files, and the
stand-in kept for a file too big to store (`LARGE_FILE_MARKER`).
"""

import io
import re
import struct
import zipfile
import zlib
from datetime import datetime, timedelta
from typing import Optional
from xml.etree import ElementTree

MAX_TEXT_CHARS = 2_000_000
# Unpacked size allowed per document: a small zip or HWP can expand to gigabytes (decompression bomb).
MAX_UNPACKED_BYTES = 50 * 1024 * 1024
_OLE_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
# What the snapshot keeps instead of a file over its size limit (snapshots.py): size and time only.
LARGE_FILE_MARKER = b"WORKHISTORY-LARGE-FILE\n"
# Never read as text: binary formats whose bytes can still look like text (a simple PDF does).
NOT_TEXT = (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tif", ".tiff",
            ".doc", ".xls", ".ppt", ".hwt", ".psd", ".ai", ".eps")
_ENDOFCHAIN, _FREESECT = 0xFFFFFFFE, 0xFFFFFFFF


def decode_text(data: bytes) -> Optional[str]:
    """UTF-8 (with/without BOM), UTF-16 with a BOM, or CP949 text; None if it looks binary."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):  # UTF-16 (Notepad's "유니코드"): full of zero bytes
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            return None
    if b"\x00" in data[:8000]:
        return None
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def extract_text(name: str, data: bytes) -> Optional[str]:
    lower = name.lower()
    if data.startswith(LARGE_FILE_MARKER) or lower.endswith(NOT_TEXT):
        return None
    try:
        if lower.endswith(".hwp"):
            text = hwp_text(data)
        elif lower.endswith(".hwpx"):
            text = _ooxml_text(data, r"Contents/section\d+\.xml$", "p")
        elif lower.endswith(".docx"):
            text = _ooxml_text(data, r"word/document\.xml$", "p")
        elif lower.endswith(".pptx"):
            text = _ooxml_text(data, r"ppt/slides/slide\d+\.xml$", "p")
        elif lower.endswith(".xlsx"):
            text = _xlsx_text(data)
        else:
            text = decode_text(data)
    except Exception:  # corrupt or unsupported document: treat as binary
        return None
    return text[:MAX_TEXT_CHARS] if text is not None else None


# ---------------------------------------------------------------- Office Open XML

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class _Parts:
    """Reads zip members within one MAX_UNPACKED_BYTES budget for the whole document."""

    def __init__(self, z: zipfile.ZipFile):
        self.z, self.left = z, MAX_UNPACKED_BYTES

    def read(self, name: str) -> bytes:
        size = self.z.getinfo(name).file_size
        if size > self.left:
            raise ValueError("document too large when unpacked")
        self.left -= size
        with self.z.open(name) as f:
            return f.read(size)  # never more than the declared (and budgeted) size


def _ooxml_text(data: bytes, part_pattern: str, paragraph_tag: str) -> str:
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        reader = _Parts(z)
        parts = sorted((n for n in z.namelist() if re.search(part_pattern, n)),
                       key=lambda n: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", n)])
        for part in parts:
            out += _paragraphs(ElementTree.fromstring(reader.read(part)), paragraph_tag)
    return "\n".join(out)


def _paragraphs(root: ElementTree.Element, paragraph_tag: str) -> list[str]:
    """The text runs (`t`) of each paragraph, a paragraph inside another (a table cell, a text box)
    counted as its own line, not again as part of the outer one."""
    paragraphs: list[list[str]] = []

    def walk(el: ElementTree.Element, current: Optional[list[str]]) -> None:
        tag = _local(el.tag)
        if tag == paragraph_tag:
            current = []
            paragraphs.append(current)
        elif tag == "t" and current is not None:
            current.append("".join(el.itertext()))
            return
        for child in el:
            walk(child, current)

    walk(root, None)
    return [text for text in ("".join(p) for p in paragraphs) if text.strip()]


# Built-in number formats that show a date or a time (ECMA-376 18.8.30).
_DATE_FORMATS = set(range(14, 23)) | {45, 46, 47}
_EXCEL_EPOCH = datetime(1899, 12, 30)


def _is_date_format(code: str) -> bool:
    """A custom number format that shows a date or time: y, m, d, h or s outside quoted text
    and [...] parts ('yyyy"년" m"월" d"일"', '[$-412]yyyy-mm-dd')."""
    bare = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", code)
    return bool(re.search(r"[ymdhs]", bare, re.I)) and not re.fullmatch(r"(?i)\s*general\s*", bare)


def _excel_date(value: str) -> str:
    serial = float(value)
    moment = _EXCEL_EPOCH + timedelta(days=serial)
    if serial == int(serial):
        return moment.strftime("%Y-%m-%d")
    return moment.strftime("%Y-%m-%d %H:%M" if serial >= 1 else "%H:%M")


def _column(ref: str) -> int:
    """0-based column of a cell reference ("B3" -> 1, "AA10" -> 26)."""
    n = 0
    for ch in re.match(r"[A-Z]*", ref.upper()).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _xlsx_sheets(z: zipfile.ZipFile, reader: "_Parts") -> list[tuple[str, str]]:
    """(sheet name, part) in the order of the workbook's tabs."""
    names = z.namelist()
    try:
        book = ElementTree.fromstring(reader.read("xl/workbook.xml"))
        rels = ElementTree.fromstring(reader.read("xl/_rels/workbook.xml.rels"))
        target = {r.get("Id"): r.get("Target", "") for r in rels}
        out = []
        for sheet in (e for e in book.iter() if _local(e.tag) == "sheet"):
            rid = next((v for k, v in sheet.attrib.items() if _local(k) == "id"), None)
            path = target.get(rid, "").lstrip("/")
            path = path if path.startswith("xl/") else "xl/" + path
            if path in names:
                out.append((sheet.get("name") or path.rsplit("/", 1)[-1][:-4], path))
        if out:
            return out
    except (KeyError, ElementTree.ParseError):
        pass
    sheets = [n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)]
    sheets.sort(key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
    return [(n.rsplit("/", 1)[-1][:-4], n) for n in sheets]


def _rich_text(el: ElementTree.Element) -> str:
    """A shared or inline string: its text and runs, not the phonetic guide (rPh) some files carry."""
    parts = []
    for child in el:
        tag = _local(child.tag)
        if tag == "t":
            parts.append(child.text or "")
        elif tag == "r":
            parts += [t.text or "" for t in child if _local(t.tag) == "t"]
    return "".join(parts)


def _xlsx_text(data: bytes) -> str:
    """Each sheet by its tab name, then one line per row: its row number and its cells in column
    order ("3행: 서버 | 1200 |  | 메모"); dates as dates, formulas next to their value."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        reader = _Parts(z)
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            shared = [_rich_text(si) for si in ElementTree.fromstring(reader.read("xl/sharedStrings.xml"))]
        date_styles: set[int] = set()
        if "xl/styles.xml" in names:
            styles = ElementTree.fromstring(reader.read("xl/styles.xml"))
            custom = {int(f.get("numFmtId", -1)): f.get("formatCode", "")
                      for f in styles.iter() if _local(f.tag) == "numFmt"}
            xfs = next((e for e in styles.iter() if _local(e.tag) == "cellXfs"), None)
            for i, xf in enumerate(xfs if xfs is not None else []):
                fmt = int(xf.get("numFmtId", 0))
                if fmt in _DATE_FORMATS or (fmt in custom and _is_date_format(custom[fmt])):
                    date_styles.add(i)
        out = []
        for name, part in _xlsx_sheets(z, reader):
            out.append(f"[{name}]")
            for row in ElementTree.fromstring(reader.read(part)).iter():
                if _local(row.tag) != "row":
                    continue
                cells: dict[int, str] = {}
                for i, c in enumerate(e for e in row if _local(e.tag) == "c"):
                    col = _column(c.get("r", "")) if c.get("r") else i
                    value = next((v.text for v in c if _local(v.tag) == "v"), None)
                    formula = next((f.text for f in c if _local(f.tag) == "f" and f.text), None)
                    kind = c.get("t", "n")
                    if kind == "s" and value is not None and value.isdigit() and int(value) < len(shared):
                        text = shared[int(value)]
                    elif kind == "inlineStr":
                        text = "".join(_rich_text(e) for e in c if _local(e.tag) == "is")
                    elif kind == "b" and value is not None:
                        text = "TRUE" if value == "1" else "FALSE"
                    elif kind == "n" and value is not None and int(c.get("s", 0)) in date_styles:
                        try:
                            text = _excel_date(value)
                        except (ValueError, OverflowError):
                            text = value
                    else:
                        text = value or ""
                    if formula:
                        text = f"{text} (={formula})" if text else f"(={formula})"
                    if text:
                        cells[max(col, 0)] = text
                if cells:
                    line = " | ".join(cells.get(k, "") for k in range(max(cells) + 1))
                    out.append(f"{row.get('r') or len(out)}행: {line}")
    return "\n".join(out)


# ---------------------------------------------------------------- HWP 5.0

def _ole_streams(data: bytes) -> dict[str, bytes]:
    if data[:8] != _OLE_MAGIC:
        raise ValueError("not an OLE compound file")
    sec_shift, mini_shift = struct.unpack_from("<HH", data, 0x1E)
    ssz, mssz = 1 << sec_shift, 1 << mini_shift
    n_fat, first_dir, _, mini_cutoff, first_minifat, _, first_difat, n_difat = \
        struct.unpack_from("<IIIIIIII", data, 0x2C)
    difat = list(struct.unpack_from("<109I", data, 0x4C))

    def sector(i: int) -> bytes:
        return data[(i + 1) * ssz:(i + 2) * ssz]

    d = first_difat
    while d not in (_ENDOFCHAIN, _FREESECT) and n_difat:
        values = struct.unpack("<%dI" % (ssz // 4), sector(d))
        difat += values[:-1]
        d, n_difat = values[-1], n_difat - 1
    fat: list[int] = []
    for s in difat[:n_fat]:
        fat += struct.unpack("<%dI" % (ssz // 4), sector(s))

    def chain(start: int, table: list[int]) -> list[int]:
        out = []
        while start not in (_ENDOFCHAIN, _FREESECT) and start < len(table) and len(out) <= len(table):
            out.append(start)
            start = table[start]
        return out

    dir_bytes = b"".join(sector(s) for s in chain(first_dir, fat))
    entries = []
    for off in range(0, len(dir_bytes) - 127, 128):
        e = dir_bytes[off:off + 128]
        name_len = struct.unpack_from("<H", e, 64)[0]
        left, right, child = struct.unpack_from("<III", e, 68)
        start, size = struct.unpack_from("<IQ", e, 116)
        entries.append({"name": e[:max(name_len - 2, 0)].decode("utf-16le", "replace"), "type": e[66],
                        "left": left, "right": right, "child": child, "start": start, "size": size})
    root = entries[0]
    ministream = b"".join(sector(s) for s in chain(root["start"], fat))[:root["size"]]
    minifat: list[int] = []
    for s in chain(first_minifat, fat):
        minifat += struct.unpack("<%dI" % (ssz // 4), sector(s))

    def read(e: dict) -> bytes:
        if e["size"] < mini_cutoff:
            return b"".join(ministream[m * mssz:(m + 1) * mssz] for m in chain(e["start"], minifat))[:e["size"]]
        return b"".join(sector(s) for s in chain(e["start"], fat))[:e["size"]]

    streams: dict[str, bytes] = {}
    seen: set[int] = set()

    def walk(idx: int, prefix: str) -> None:
        if idx == _FREESECT or idx >= len(entries) or idx in seen:
            return
        seen.add(idx)
        e = entries[idx]
        walk(e["left"], prefix)
        path = prefix + e["name"]
        if e["type"] == 2:
            streams[path] = read(e)
        elif e["type"] == 1:
            walk(e["child"], path + "/")
        walk(e["right"], prefix)

    walk(root["child"], "")
    return streams


def _hwp_paragraphs(stream: bytes) -> list[str]:
    i, out = 0, []
    while i + 4 <= len(stream):
        header = struct.unpack_from("<I", stream, i)[0]
        tag, size = header & 0x3FF, (header >> 20) & 0xFFF
        i += 4
        if size == 0xFFF:
            size = struct.unpack_from("<I", stream, i)[0]
            i += 4
        body = stream[i:i + size]
        i += size
        if tag != 67:  # HWPTAG_PARA_TEXT
            continue
        chars = struct.unpack("<%dH" % (len(body) // 2), body[:len(body) // 2 * 2])
        j, text = 0, []
        while j < len(chars):
            c = chars[j]
            if c >= 32:
                text.append(chr(c))
                j += 1
            elif c in (0, 10, 13) or 24 <= c <= 31:
                if c == 10:
                    text.append("\n")
                j += 1
            else:  # inline/extended control characters occupy 8 code units
                if c == 9:
                    text.append("\t")
                j += 8
        line = "".join(text).rstrip()
        if line.strip():
            out.append(line)
    return out


def hwp_text(data: bytes) -> str:
    streams = _ole_streams(data)
    header = streams.get("FileHeader", b"")
    compressed = len(header) >= 40 and bool(struct.unpack_from("<I", header, 36)[0] & 1)
    sections = sorted((p for p in streams if p.startswith("BodyText/Section")),
                      key=lambda p: int(p.rsplit("Section", 1)[1] or 0))
    lines: list[str] = []
    left = MAX_UNPACKED_BYTES
    for p in sections:
        raw = streams[p]
        if compressed:
            raw = zlib.decompressobj(-15).decompress(raw, left + 1)
            if len(raw) > left:
                raise ValueError("document too large when unpacked")
        left -= len(raw)
        lines += _hwp_paragraphs(raw)
    return "\n".join(lines)
