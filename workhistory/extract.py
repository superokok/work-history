"""Plain text from files, for diffs and AI context (standard library only).

Supports text/code files, HWP 5.0 (OLE compound file), and Office Open XML
(.docx, .xlsx, .pptx). Returns None for anything that is not text.
"""

import io
import re
import struct
import zipfile
import zlib
from typing import Optional
from xml.etree import ElementTree

MAX_TEXT_CHARS = 2_000_000
# Unpacked size allowed per document: a small zip or HWP can expand to gigabytes (decompression bomb).
MAX_UNPACKED_BYTES = 50 * 1024 * 1024
_OLE_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
_ENDOFCHAIN, _FREESECT = 0xFFFFFFFE, 0xFFFFFFFF


def decode_text(data: bytes) -> Optional[str]:
    """UTF-8 (with/without BOM) or CP949 text; None if it looks binary."""
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
    try:
        if lower.endswith(".hwp"):
            text = hwp_text(data)
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
            root = ElementTree.fromstring(reader.read(part))
            for para in root.iter():
                if _local(para.tag) != paragraph_tag:
                    continue
                text = "".join(t.text or "" for t in para.iter() if _local(t.tag) == "t")
                if text.strip():
                    out.append(text)
    return "\n".join(out)


def _xlsx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        reader = _Parts(z)
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ElementTree.fromstring(reader.read("xl/sharedStrings.xml")):
                shared.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))
        out = []
        sheets = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        for sheet in sheets:
            out.append(f"[{sheet.rsplit('/', 1)[-1][:-4]}]")
            for row in ElementTree.fromstring(reader.read(sheet)).iter():
                if _local(row.tag) != "row":
                    continue
                cells = []
                for c in row:
                    if _local(c.tag) != "c":
                        continue
                    value = next((v.text for v in c if _local(v.tag) == "v"), None)
                    inline = "".join(t.text or "" for t in c.iter() if _local(t.tag) == "t")
                    if c.get("t") == "s" and value is not None and value.isdigit() and int(value) < len(shared):
                        cells.append(shared[int(value)])
                    elif inline:
                        cells.append(inline)
                    elif value is not None:
                        cells.append(value)
                if cells:
                    out.append("\t".join(cells))
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
