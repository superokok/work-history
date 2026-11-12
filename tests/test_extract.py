"""Text from documents, for diffs: HWP 5.0 (OLE compound file) built here byte by byte."""

import struct
import unittest
import zlib

from workhistory import extract

END, FREE, FATSECT = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD
SECTOR = 512


def _entry(name: str, kind: int, child=FREE, right=FREE, start=END, size=0) -> bytes:
    raw = name.encode("utf-16le") + b"\x00\x00"
    e = bytearray(128)
    e[:len(raw)] = raw
    struct.pack_into("<HBB", e, 64, len(raw), kind, 1)
    struct.pack_into("<III", e, 68, FREE, right, child)
    struct.pack_into("<IQ", e, 116, start, size)
    return bytes(e)


def ole(streams: list[tuple[str, bytes]]) -> bytes:
    """A compound file with the streams FileHeader and BodyText/Section0..n (each 4096 bytes or more,
    so no mini stream is needed): enough for `extract.hwp_text`."""
    fat, data, start = [FATSECT, END], b"", 2
    starts = []
    for _, body in streams:
        body = body + b"\x00" * (-len(body) % SECTOR)
        n = len(body) // SECTOR
        starts.append(start)
        fat += [start + k + 1 for k in range(n - 1)] + [END]
        data += body
        start += n
    header_name, *sections = [n for n, _ in streams]
    dirs = [_entry("Root Entry", 5, child=1),
            _entry(header_name, 2, right=2, start=starts[0], size=len(streams[0][1])),
            _entry("BodyText", 1, child=3)]
    for i, name in enumerate(sections):
        dirs.append(_entry(name.split("/")[-1], 2, right=4 + i if i + 1 < len(sections) else FREE,
                           start=starts[i + 1], size=len(streams[i + 1][1])))
    dir_bytes = b"".join(dirs)
    dir_bytes += b"\x00" * (-len(dir_bytes) % SECTOR)
    assert len(dir_bytes) == SECTOR and len(fat) <= SECTOR // 4
    header = bytearray(SECTOR)
    header[:8] = bytes.fromhex("d0cf11e0a1b11ae1")
    struct.pack_into("<HHHHH", header, 0x18, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<IIIIIIII", header, 0x2C, 1, 1, 0, 4096, END, 0, END, 0)
    struct.pack_into("<109I", header, 0x4C, 0, *([FREE] * 108))
    fat_bytes = struct.pack("<%dI" % (SECTOR // 4), *(fat + [FREE] * (SECTOR // 4 - len(fat))))
    return bytes(header) + fat_bytes + dir_bytes + data


def para(text: str) -> bytes:
    body = text.encode("utf-16le")
    return struct.pack("<I", 67 | (len(body) << 20)) + body  # HWPTAG_PARA_TEXT


def hwp(paragraphs: list[str], compressed: bool = True) -> bytes:
    section = b"".join(para(p) for p in paragraphs)
    # Another record (a long one: size 0xFFF, then the real size), so the stream is a regular one.
    filler = struct.pack("<II", 66 | (0xFFF << 20), 4096) + b"\x00" * 4096
    section += filler
    if compressed:
        c = zlib.compressobj(9, zlib.DEFLATED, -15)
        section = c.compress(section) + c.flush()
        section += b"\x00" * max(0, 4096 - len(section))
    header = b"HWP Document File".ljust(32, b"\x00") + struct.pack("<II", 0x05000000, 1 if compressed else 0)
    return ole([("FileHeader", header.ljust(4096, b"\x00")), ("BodyText/Section0", section)])


def zipped(parts: dict[str, str]) -> bytes:
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, xml in parts.items():
            z.writestr(name, xml)
    return buf.getvalue()


class TextTests(unittest.TestCase):
    def test_hwpx_paragraphs_and_table_cells_once_each(self):
        hp = 'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"'
        section = (f'<hs:sec xmlns:hs="x" {hp}><hp:p><hp:run><hp:t>검토 보고서</hp:t></hp:run></hp:p>'
                   '<hp:p><hp:run><hp:tbl><hp:tr><hp:tc><hp:subList><hp:p><hp:run><hp:t>항목</hp:t></hp:run></hp:p>'
                   '</hp:subList></hp:tc></hp:tr></hp:tbl><hp:t>표 아래 설명</hp:t></hp:run></hp:p></hs:sec>')
        data = zipped({"Contents/section0.xml": section, "Contents/section1.xml": section.replace("검토", "둘째")})
        self.assertEqual(extract.extract_text("문서.hwpx", data).splitlines(),
                         ["검토 보고서", "표 아래 설명", "항목", "둘째 보고서", "표 아래 설명", "항목"])

    def test_utf16_text_with_a_byte_order_mark(self):
        self.assertEqual(extract.extract_text("메모.txt", "\ufeff한글 메모\r\n".encode("utf-16-le")), "한글 메모\r\n")
        self.assertEqual(extract.extract_text("a.txt", "\ufeffbig".encode("utf-16-be")), "big")

    def test_never_text(self):
        self.assertIsNone(extract.extract_text("scan.pdf", b"%PDF-1.4 BT (hello) Tj ET"))  # looks like text
        self.assertIsNone(extract.extract_text("a.txt", extract.LARGE_FILE_MARKER + b"size=9\n"))
        self.assertIsNone(extract.extract_text("old.xls", b"plain bytes"))


NS = ('xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')


def workbook(amount: str, day: str, sheets: int = 2) -> bytes:
    """Two named tabs (plus empty ones), shared strings, a gap column, a formula, a date and a boolean."""
    tabs = ["예산", "일정"] + [f"빈{i}" for i in range(3, sheets + 1)]
    parts = {
        "xl/workbook.xml": f"<workbook {NS}><sheets>" + "".join(
            f'<sheet name="{n}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, n in enumerate(tabs)) + "</sheets></workbook>",
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(f'<Relationship Id="rId{i + 1}" Target="worksheets/sheet{i + 1}.xml"/>' for i in range(len(tabs)))
            + "</Relationships>",
        "xl/sharedStrings.xml": f"<sst {NS}><si><t>항목</t></si><si><t>금액</t></si>"
            "<si><r><t>서</t></r><r><t>버</t></r><rPh><t>サーバ</t></rPh></si><si><t>비고</t></si></sst>",
        "xl/styles.xml": f'<styleSheet {NS}><numFmts><numFmt numFmtId="176" formatCode="yyyy&quot;년&quot; m&quot;월&quot;"/>'
            '</numFmts><cellXfs><xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="176"/><xf numFmtId="4"/></cellXfs></styleSheet>',
        "xl/worksheets/sheet1.xml": f"<worksheet {NS}><sheetData>"
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="D1" t="s"><v>3</v></c></row>'
            f'<row r="3"><c r="A3" t="s"><v>2</v></c><c r="B3" s="3"><v>{amount}</v></c>'
            '<c r="D3" t="inlineStr"><is><t>메모</t></is></c></row>'
            f'<row r="4"><c r="A4" t="str"><v>합계</v></c><c r="B4"><f>SUM(B3:B3)</f><v>{amount}</v></c></row>'
            "</sheetData></worksheet>",
        "xl/worksheets/sheet2.xml": f"<worksheet {NS}><sheetData><row r=\"1\">"
            f'<c r="A1" s="1"><v>{day}</v></c><c r="B1" s="2"><v>45962</v></c><c r="C1" t="b"><v>1</v></c>'
            '<c r="D1" s="1"><v>45972.5</v></c></row></sheetData></worksheet>',
    }
    for i in range(3, len(tabs) + 1):
        parts[f"xl/worksheets/sheet{i}.xml"] = f"<worksheet {NS}><sheetData/></worksheet>"
    return zipped(parts)


class XlsxTests(unittest.TestCase):
    def test_sheets_rows_columns_dates_and_formulas(self):
        self.assertEqual(extract.extract_text("예산.xlsx", workbook("1200", "45972")).splitlines(), [
            "[예산]",
            "1행: 항목 | 금액 |  | 비고",
            "3행: 서버 | 1200 |  | 메모",
            "4행: 합계 | 1200 (=SUM(B3:B3))",
            "[일정]",
            "1행: 2025-11-11 | 2025-11-01 | TRUE | 2025-11-11 12:00",
        ])

    def test_a_changed_cell_is_one_changed_line(self):
        import difflib
        old = extract.extract_text("a.xlsx", workbook("1200", "45972")).splitlines()
        new = extract.extract_text("a.xlsx", workbook("1500", "45972")).splitlines()
        changed = [l for l in difflib.unified_diff(old, new, lineterm="", n=0) if l[:1] in "+-" and l[:3] not in ("+++", "---")]
        self.assertEqual(changed, ["-3행: 서버 | 1200 |  | 메모", "-4행: 합계 | 1200 (=SUM(B3:B3))",
                                   "+3행: 서버 | 1500 |  | 메모", "+4행: 합계 | 1500 (=SUM(B3:B3))"])

    def test_tab_order_with_more_than_ten_sheets(self):
        names = [l for l in extract.extract_text("a.xlsx", workbook("1", "1", sheets=12)).splitlines() if l.startswith("[")]
        self.assertEqual(names, ["[예산]", "[일정]"] + [f"[빈{i}]" for i in range(3, 13)])


class HwpTests(unittest.TestCase):
    def test_paragraphs_of_a_compressed_hwp(self):
        data = hwp(["민원 조회 개선 검토보고서", "1. 현황: 평균 응답 3.2초"])
        self.assertEqual(extract.extract_text("검토보고서.hwp", data), "민원 조회 개선 검토보고서\n1. 현황: 평균 응답 3.2초")

    def test_uncompressed_hwp(self):
        self.assertEqual(extract.extract_text("a.HWP", hwp(["표 없는 문서"], compressed=False)), "표 없는 문서")

    def test_not_an_hwp(self):
        self.assertIsNone(extract.extract_text("x.hwp", b"not an ole file at all"))


if __name__ == "__main__":
    unittest.main()
