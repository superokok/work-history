"""Turns the Edge-printed banner PDF into a print file: every glyph becomes a vector path
(no fonts needed at the print shop) and the page is scaled to the given size in millimetres.

Usage: python outline_pdf.py in.pdf out.pdf [width_mm height_mm]
Needs fontTools (MIT) and pypdf (BSD); this is a build tool for the banner only, not part of the app.
"""
import io
import re
import sys

from fontTools.pens.basePen import BasePen
from fontTools.ttLib import TTFont
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, FloatObject, NameObject

MM = 72 / 25.4
TOKEN = re.compile(rb"<[0-9A-Fa-f\s]*>|/[^\s/<>\[\]()]+|[-+]?(?:\d+\.?\d*|\.\d+)|[A-Za-z'\"*]+")


def num(x):
    s = f"{x:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


class PdfPen(BasePen):
    def __init__(self, glyphset):
        super().__init__(glyphset)
        self.ops = []

    def _moveTo(self, p):
        self.ops.append(f"{num(p[0])} {num(p[1])} m")

    def _lineTo(self, p):
        self.ops.append(f"{num(p[0])} {num(p[1])} l")

    def _curveToOne(self, p1, p2, p3):
        self.ops.append(" ".join(num(v) for v in (*p1, *p2, *p3)) + " c")

    def _closePath(self):
        self.ops.append("h")


class Font:
    """An embedded Type0/CIDFontType2 font (Identity-H, CIDToGIDMap Identity)."""

    def __init__(self, ref):
        font = ref.get_object()
        desc = font["/DescendantFonts"][0].get_object()
        if font.get("/Encoding") != "/Identity-H" or desc.get("/CIDToGIDMap", "/Identity") != "/Identity":
            raise SystemExit(f"unsupported font encoding in {font.get('/BaseFont')}")
        data = desc["/FontDescriptor"].get_object()["/FontFile2"].get_object().get_data()
        self.tt = TTFont(io.BytesIO(data))
        self.upm = self.tt["head"].unitsPerEm
        self.order = self.tt.getGlyphOrder()
        self.glyphs = self.tt.getGlyphSet()
        self.default_w = float(desc.get("/DW", 1000))
        self.widths = {}
        w = desc.get("/W", [])
        i = 0
        while i < len(w):
            first = int(w[i])
            if isinstance(w[i + 1], (list, ArrayObject)):
                for k, v in enumerate(w[i + 1]):
                    self.widths[first + k] = float(v)
                i += 2
            else:
                for c in range(first, int(w[i + 1]) + 1):
                    self.widths[c] = float(w[i + 2])
                i += 3
        self.cache = {}

    def path(self, gid):
        if gid not in self.cache:
            pen = PdfPen(self.glyphs)
            if gid < len(self.order):
                self.glyphs[self.order[gid]].draw(pen)
            self.cache[gid] = " ".join(pen.ops)
        return self.cache[gid]

    def width(self, gid):
        return self.widths.get(gid, self.default_w) / 1000


def mul(a, b):
    """PDF matrix product a x b for [a b c d e f] matrices."""
    return [a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5]]


def outline_text_block(block, fonts):
    """Replaces one BT..ET text object with filled glyph paths (keeps other ops in order)."""
    out, stack = [], []
    font, size, tm, tlm = None, 0.0, [1, 0, 0, 1, 0, 0], [1, 0, 0, 1, 0, 0]
    for tok in TOKEN.findall(block):
        if tok[:1] in b"/<" or re.fullmatch(rb"[-+]?(?:\d+\.?\d*|\.\d+)", tok):
            stack.append(tok)
            continue
        op = tok.decode()
        if op == "Tf":
            font, size = fonts[stack[0].decode()], float(stack[1])
        elif op == "Tm":
            tm = tlm = [float(v) for v in stack]
        elif op == "Td":
            tlm = mul([1, 0, 0, 1, float(stack[0]), float(stack[1])], tlm)
            tm = tlm
        elif op == "Tj":
            hexs = re.sub(rb"\s", b"", stack[0][1:-1])
            for i in range(0, len(hexs), 4):
                gid = int(hexs[i:i + 4], 16)
                path = font.path(gid)
                if path:
                    m = mul([size / font.upm, 0, 0, size / font.upm, 0, 0], tm)
                    out.append(f"q {' '.join(num(v) for v in m)} cm {path} f Q")
                tm = mul([1, 0, 0, 1, font.width(gid) * size, 0], tm)
        elif op in ("rg", "RG", "g", "G", "k", "K", "gs", "cs", "CS", "sc", "scn", "SC", "SCN"):
            out.append(" ".join(t.decode() for t in stack) + " " + op)
        elif op in ("Tc", "Tw", "Tz", "TL", "Ts", "Tr", "TJ", "T*", "'", '"'):
            raise SystemExit(f"text operator {op} is not handled; extend outline_pdf.py")
        else:
            raise SystemExit(f"unexpected operator {op} inside a text object")
        stack = []
    return "\n".join(out).encode()


def main():
    src, dst = sys.argv[1], sys.argv[2]
    w_mm, h_mm = (float(sys.argv[3]), float(sys.argv[4])) if len(sys.argv) > 4 else (None, None)
    reader = PdfReader(src)
    writer = PdfWriter()  # only what the pages still reference gets copied: no fonts, no tag tree
    for page in reader.pages:
        res = page["/Resources"]
        fonts = {name: Font(ref) for name, ref in res.get("/Font", {}).items()}
        data = page.get_contents().get_data()
        data = re.sub(rb"\bBT\b(.*?)\bET\b", lambda m: outline_text_block(m.group(1), fonts), data, flags=re.S)
        for _, xref in res.get("/XObject", {}).items():
            if b"BT" in xref.get_object().get_data():
                raise SystemExit("text inside a form XObject is not handled")
        box = page.mediabox
        if w_mm:
            k = w_mm * MM / float(box.width)
            if abs(h_mm * MM / float(box.height) - k) > 1e-3:
                raise SystemExit("target size has a different aspect ratio than the page")
            data = f"q {num(k)} 0 0 {num(k)} 0 0 cm\n".encode() + data + b"\nQ"
            # Page-level patterns live in default page space, so they need the same scale.
            for _, pref in res.get("/Pattern", {}).items():
                pat = pref.get_object()
                m = [float(v) for v in pat.get("/Matrix", [1, 0, 0, 1, 0, 0])]
                pat[NameObject("/Matrix")] = ArrayObject(FloatObject(round(v * k, 6)) for v in m)
            page.mediabox.upper_right = (float(box.width) * k, float(box.height) * k)
            for b in ("/CropBox", "/TrimBox", "/BleedBox", "/ArtBox"):
                if b in page:
                    del page[NameObject(b)]
        if "/Font" in res:
            del res[NameObject("/Font")]
        procset = res.get("/ProcSet")
        if procset is not None:
            res[NameObject("/ProcSet")] = ArrayObject(p for p in procset if p != "/Text")
        if "/StructParents" in page:
            del page[NameObject("/StructParents")]
        out = writer.add_page(page)
        stream = DecodedStreamObject()
        stream.set_data(data)
        out.replace_contents(stream)
        out.compress_content_streams()
    writer.add_metadata({"/Title": "WorkHistory X-banner"})
    with open(dst, "wb") as f:
        writer.write(f)


if __name__ == "__main__":
    main()
