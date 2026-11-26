"""Reading a laboratory's data file as one table: Excel workbooks (.xlsx) and CSV files of any common dialect.

``scale data add`` and ``scale data match`` read a file as a grid of cell texts (:func:`grid`), choose its header row
(given, or found by :func:`find_header_row`), and extract one table (:func:`extract`): the header, the data rows and
the same table written as ``scale_csv_v1`` (UTF-8, comma, double-quote escaping, one header row; :mod:`scale.datatable`)
— the canonical bytes evidence cells are named in. What was read (the format, the sheet, the header row, the range,
the delimiter, the encoding, the decimal mark) is the *recipe*; recorded with the table (``table.ingest``), it lets
``scale data verify`` extract the same bytes again from the original file.

Pure Python and the standard library only (``zipfile``, ``xml.etree``, ``csv``), so the same code runs in Pyodide;
no spreadsheet library is needed. What it reads of a workbook: the sheets, shared and inline strings, numbers,
booleans, error values, the cached values of formulas (a formula never calculated is refused, never read as a
blank), dates (by the cell's number format) and percentages (a cell shown as 60% is the text 60%, or, with
``percent: displayed``, the number 60: never 0.6 under a header in %). It does not evaluate formulas, merge cells or
read charts. A number is written as the shortest decimal that gives the stored double back (lossless).
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

from .util import ScaleError

XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CSV_MEDIA = "text/csv"
FORMATS = ("csv", "xlsx")
DELIMITERS = (",", ";", "\t", "|")
RECIPE_KEYS = ("format", "sheet", "header_row", "range", "delimiter", "encoding", "decimal", "percent")
MAX_UNZIPPED = 256 * 1024 * 1024                 # a workbook larger than this unzipped is refused (a zip bomb)
MAX_CELLS = 20_000_000                           # a sheet whose used area is larger is refused (a cell at XFD1048576)
_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
       "rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
_REF = re.compile(r"^([A-Z]{1,3})([0-9]+)$")
_RANGE = re.compile(r"^([A-Z]{1,3})([0-9]+):([A-Z]{1,3})([0-9]+)$")
_DECIMAL_COMMA = re.compile(r"^[+-]?(?:\d+,\d*|,\d+)(?:[eE][+-]?\d+)?$")
_NUMBERISH = re.compile(r"^[<>~≤≥]?\s*[+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:[eE][+-]?\d+)?\s*%?$")
# number formats that show a date or a time (ECMA-376 18.8.30, the built-in ids)
_DATE_IDS = set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) | set(range(50, 59))


def _fail(msg: str) -> None:
    raise ScaleError(msg)


def sniff(data: bytes) -> str:
    """The format of a file's bytes: ``xlsx`` (a zip holding a workbook) or ``csv``."""
    if data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if "xl/workbook.xml" in z.namelist():
                    return "xlsx"
        except zipfile.BadZipFile:
            pass
        _fail("a zip file that is no Excel workbook (.xlsx); export the sheet as CSV or save it as .xlsx")
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        _fail("an Excel 97-2003 workbook (.xls); save it as .xlsx or export the sheet as CSV")
    return "csv"


# ------------------------------------------------------------------------------------------ xlsx
def _col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def col_letters(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


class _Workbook:
    def __init__(self, data: bytes):
        try:
            self.z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise ScaleError(f"not a readable .xlsx workbook ({exc})") from exc
        if sum(i.file_size for i in self.z.infolist()) > MAX_UNZIPPED:
            _fail(f"the workbook unzips to more than {MAX_UNZIPPED // 2**20} MiB; refused")
        wb = self._xml("xl/workbook.xml")
        pr = wb.find("m:workbookPr", _NS)
        self.date1904 = pr is not None and pr.get("date1904") in ("1", "true")
        rels = {r.get("Id"): r.get("Target") for r in self._xml("xl/_rels/workbook.xml.rels").findall("rel:Relationship", _NS)}
        self.sheets: list[tuple[str, str]] = []
        for s in wb.findall("m:sheets/m:sheet", _NS):
            target = rels.get(s.get(f"{{{_NS['r']}}}id"), "")
            target = target.lstrip("/")
            path = target if target.startswith("xl/") else f"xl/{target}"
            self.sheets.append((s.get("name") or "", path))
        self.strings = self._shared_strings()
        self.date_styles, self.percent_styles = self._styles()
        self.percent = None

    def _xml(self, name: str) -> ET.Element:
        try:
            return ET.fromstring(self.z.read(name))
        except KeyError as exc:
            raise ScaleError(f"the workbook has no {name}") from exc
        except ET.ParseError as exc:
            raise ScaleError(f"the workbook's {name} is not readable XML ({exc})") from exc

    @staticmethod
    def _text(si: ET.Element) -> str:
        """The text of a shared or inline string: its plain text or its runs joined (phonetic hints left out)."""
        t = si.find("m:t", _NS)
        if t is not None:
            return t.text or ""
        return "".join(x.text or "" for r in si.findall("m:r", _NS) for x in r.findall("m:t", _NS))

    def _shared_strings(self) -> list[str]:
        if "xl/sharedStrings.xml" not in self.z.namelist():
            return []
        return [self._text(si) for si in self._xml("xl/sharedStrings.xml").findall("m:si", _NS)]

    def _styles(self) -> tuple[set[int], set[int]]:
        """The indexes of the cell formats (cellXfs) that show a date or a time, and those that show a percentage."""
        if "xl/styles.xml" not in self.z.namelist():
            return set(), set()
        st = self._xml("xl/styles.xml")
        dates, percents = {}, {}
        for f in st.findall("m:numFmts/m:numFmt", _NS):
            code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", f.get("formatCode") or "").lower()
            fid = int(f.get("numFmtId", "0"))
            dates[fid] = bool(re.search(r"[dmyhs]", code)) and "general" not in code
            percents[fid] = "%" in code
        out_d, out_p = set(), set()
        for i, xf in enumerate(st.findall("m:cellXfs/m:xf", _NS)):
            fid = int(xf.get("numFmtId", "0"))
            if fid in _DATE_IDS or dates.get(fid):
                out_d.add(i)
            elif fid in (9, 10) or percents.get(fid):
                out_p.add(i)
        return out_d, out_p

    def sheet_names(self) -> list[str]:
        return [n for n, _ in self.sheets]

    def grid(self, sheet: str | None) -> tuple[str, list[list[str]]]:
        """(the sheet's name, its cells as texts: rows from row 1, columns from column A)."""
        if not self.sheets:
            _fail("the workbook has no sheets")
        if sheet is None:
            name, path = self.sheets[0]
        else:
            hit = [s for s in self.sheets if s[0] == sheet]
            if not hit:
                _fail(f"the workbook has no sheet {sheet!r} (its sheets: {', '.join(repr(n) for n in self.sheet_names())})")
            name, path = hit[0]
        root = self._xml(path)
        cells: dict[tuple[int, int], str] = {}
        r = -1
        for row in root.iterfind("m:sheetData/m:row", _NS):
            rr = row.get("r")                         # optional (ECMA-376): without it, the row after the last
            r = int(rr) - 1 if rr and rr.isdigit() and int(rr) > 0 else r + 1
            col = -1
            for c in row.iterfind("m:c", _NS):
                ref = c.get("r")
                if ref:
                    m = _REF.match(ref)
                    if not m or int(m.group(2)) - 1 != r:
                        _fail(f"sheet {name!r}: the cell reference {ref!r} is not one of row {r + 1}")
                    col = _col_index(m.group(1))
                else:
                    col += 1                          # optional too: the cell after the last
                    ref = f"{col_letters(col)}{r + 1}"
                text = self._cell(c, name, ref)
                if text != "":
                    if (r, col) in cells:
                        _fail(f"sheet {name!r}: the cell {ref} is written twice")
                    cells[(r, col)] = text
        if not cells:
            return name, []
        nrows = max(r for r, _ in cells) + 1
        ncols = max(c for _, c in cells) + 1
        if nrows * ncols > MAX_CELLS:
            _fail(f"sheet {name!r} spans {nrows} rows and {ncols} columns, more than {MAX_CELLS} cells; give input.range "
                  "or save the table on a sheet of its own")
        rows = [["" for _ in range(ncols)] for _ in range(nrows)]
        for (r, c), t in cells.items():
            rows[r][c] = t
        return name, rows

    def _cell(self, c: ET.Element, sheet: str, ref: str) -> str:
        t = c.get("t", "n")
        v = c.find("m:v", _NS)
        if t == "inlineStr":
            is_ = c.find("m:is", _NS)
            return self._text(is_) if is_ is not None else ""
        if v is None or v.text is None:
            if c.find("m:f", _NS) is not None:      # a formula never computed is no blank: refuse, never read as missing
                _fail(f"sheet {sheet!r}, cell {ref}: a formula without a computed value; open the workbook in a "
                      "spreadsheet program, let it calculate and save it")
            return ""
        raw = v.text
        if t == "s":
            try:
                return self.strings[int(raw)]
            except (ValueError, IndexError) as exc:
                raise ScaleError(f"sheet {sheet!r}, cell {ref}: a shared string the workbook does not hold") from exc
        if t in ("str", "e"):
            return raw
        if t == "b":
            return "true" if raw.strip() in ("1", "true") else "false"
        if t == "d":
            return raw
        # a number (t="n"): a date when its style shows one
        try:
            x = float(raw)
        except ValueError as exc:
            raise ScaleError(f"sheet {sheet!r}, cell {ref}: {raw!r} is not a number") from exc
        style = int(c.get("s", "0") or 0)
        if style in self.date_styles:
            return _serial_date(x, self.date1904)
        if style in self.percent_styles:
            # the sheet shows 60% and stores 0.6: the cell is read as it shows, with its % sign, so that a number is
            # never taken at a hundredth of its magnitude (input.percent: displayed reads it as the number shown)
            shown = number_text(_times_100(raw), float(_times_100(raw)))
            return shown if self.percent == "displayed" else f"{shown}%"
        return number_text(raw, x)


def _times_100(raw: str) -> str:
    from decimal import Decimal
    d = Decimal(raw.strip()) * 100
    t = format(d.normalize(), "f")
    return t if t not in ("-0",) else "0"


def number_text(raw: str, x: float) -> str:
    """The text of a stored number: an integer as written, else the shortest decimal that reads back as the stored
    double (lossless; a sum such as 0.1 + 0.2 is the double it stores, 0.30000000000000004)."""
    if re.fullmatch(r"[+-]?\d+", raw.strip()):
        return str(int(raw))
    s = repr(x)
    if s.endswith(".0"):
        s = s[:-2]
    return s


def _serial_date(x: float, date1904: bool) -> str:
    base = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30)
    d = base + timedelta(days=x)
    d = d.replace(microsecond=0) + timedelta(seconds=round(d.microsecond / 1e6))
    if x == int(x):
        return d.strftime("%Y-%m-%d")
    return d.strftime("%Y-%m-%dT%H:%M:%S")


def sheets(data: bytes) -> list[str]:
    """The sheet names of a workbook, in its order."""
    return _Workbook(data).sheet_names()


# ------------------------------------------------------------------------------------------ csv
def detect_encoding(data: bytes) -> str:
    """The encoding of a text file: by its byte-order mark, else UTF-8 when it decodes, else Windows-1252."""
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return "utf-16"
    try:
        data.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        data.decode("cp1252")
        return "cp1252"
    except UnicodeDecodeError:
        return "latin-1"


def _decode(data: bytes, encoding: str) -> str:
    try:
        return data.decode(encoding)
    except (UnicodeDecodeError, LookupError) as exc:
        raise ScaleError(f"the file is not readable as {encoding} ({exc})") from exc


def _rows(text: str, delimiter: str) -> list[list[str]]:
    try:
        return list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, quotechar='"', doublequote=True,
                               strict=True))
    except csv.Error as exc:
        raise ScaleError(f"not readable as CSV with the delimiter {delimiter!r} ({exc})") from exc


def detect_delimiter(text: str, header_row: int | None = None) -> str:
    """The delimiter that splits the most lines (from the header row on) into one width of two or more fields; ties go
    to the earlier of , ; tab |."""
    lines = text.splitlines()[:200]
    if header_row:
        lines = lines[header_row - 1:]
    best, best_score = ",", (-1, -1)
    for d in DELIMITERS:
        try:
            rows = [r for r in csv.reader(lines, delimiter=d) if any(x.strip() for x in r)]
        except csv.Error:
            continue
        widths: dict[int, int] = {}
        for r in rows:
            widths[len(r)] = widths.get(len(r), 0) + 1
        if not widths:
            continue
        width, count = max(widths.items(), key=lambda kv: (kv[1], kv[0]))
        score = (count if width >= 2 else 0, width)
        if score > best_score:
            best, best_score = d, score
    return best


# ------------------------------------------------------------------------------------------ the grid
def grid(data: bytes, opts: dict | None = None) -> dict:
    """The file as a grid of cell texts, with what was resolved to read it: ``{format, rows, sheet?, sheets?,
    encoding?, delimiter?, media_type}`` (rows: lists of texts, ragged rows padded to one width)."""
    opts = dict(opts or {})
    fmt = opts.get("format") or sniff(data)
    if fmt not in FORMATS:
        _fail(f"input.format is one of {list(FORMATS)}")
    if fmt == "xlsx":
        wb = _Workbook(data)
        wb.percent = opts.get("percent")
        name, rows = wb.grid(opts.get("sheet"))
        out = {"format": "xlsx", "sheet": name, "sheets": wb.sheet_names(), "rows": rows, "media_type": XLSX_MEDIA}
    else:
        if opts.get("sheet"):
            _fail("input.sheet goes with a workbook (.xlsx); this file is CSV")
        enc = opts.get("encoding") or detect_encoding(data)
        text = _decode(data, enc)
        if text.startswith("﻿"):
            text = text[1:]
        delim = opts.get("delimiter") or detect_delimiter(text, opts.get("header_row"))
        if len(delim) != 1:
            _fail("input.delimiter is one character")
        rows = _rows(text, delim)
        out = {"format": "csv", "encoding": enc, "delimiter": delim, "rows": rows, "media_type": CSV_MEDIA}
    width = max((len(r) for r in out["rows"]), default=0)
    out["rows"] = [r + [""] * (width - len(r)) for r in out["rows"]]
    return out


def _is_number(text: str) -> bool:
    return bool(_NUMBERISH.match(text.strip())) if text.strip() else False


def find_header_row(rows: list[list[str]]) -> tuple[int | None, list[int]]:
    """(the 1-based header row, every candidate): the first row of two or more non-empty cells, none a number, followed
    by a row with at least one number and no more columns than it names. None when no row qualifies."""
    cands = []
    for i, row in enumerate(rows[:40]):
        filled = [x for x in row if x.strip()]
        if len(filled) < 2 or any(_is_number(x) for x in filled):
            continue
        last = max(j for j, x in enumerate(row) if x.strip())
        nxt = next((r for r in rows[i + 1:] if any(x.strip() for x in r)), None)
        if nxt is None or not any(_is_number(x) for x in nxt):
            continue
        if any(x.strip() for x in nxt[last + 1:]):
            continue
        cands.append(i + 1)
    return (cands[0] if cands else None), cands


def parse_range(rng: str) -> tuple[int, int, int, int]:
    """An A1 range (``B3:H20``) as zero-based (first row, first column, last row, last column)."""
    m = _RANGE.match(rng.strip().upper()) if isinstance(rng, str) else None
    if not m:
        _fail(f"input.range {rng!r} is an A1 range such as B3:H20")
    r0, c0, r1, c1 = int(m.group(2)) - 1, _col_index(m.group(1)), int(m.group(4)) - 1, _col_index(m.group(3))
    if r1 < r0 or c1 < c0:
        _fail(f"input.range {rng!r} ends before it starts")
    return r0, c0, r1, c1


def _decimal(text: str) -> str:
    return text.replace(",", ".") if _DECIMAL_COMMA.match(text) else text


def extract(data: bytes, opts: dict | None = None, where: str = "the file") -> dict:
    """One table of a file: ``{header, body, canonical (scale_csv_v1 bytes), recipe, media_type, identity, rows_used,
    ignored_after}``. ``opts`` (an input block): format, sheet, header_row (1-based; default 1), range (A1, a
    workbook's or a CSV's grid), delimiter, encoding, decimal (``,`` reads 12,5 as 12.5). The data rows run from the row
    after the header to the row before the first empty one (or the range's end); rows after that empty row are counted
    in ``ignored_after``, never read. ``identity`` is true when the canonical bytes are the file's own."""
    opts = dict(opts or {})
    unknown = sorted(set(opts) - set(RECIPE_KEYS))
    if unknown:
        _fail(f"input has unknown keys {unknown} (allowed: {list(RECIPE_KEYS)})")
    if opts.get("percent") not in (None, "displayed"):
        _fail("input.percent is displayed (a cell shown as 60% is read as 60; without it, as the text 60%)")
    if opts.get("decimal") not in (None, ".", ","):
        _fail("input.decimal is . or ,")
    hr = opts.get("header_row", 1)
    if not isinstance(hr, int) or isinstance(hr, bool) or hr < 1:
        _fail("input.header_row is the header's row number, from 1")
    g = grid(data, opts)
    rows = g["rows"]
    c0, c_end = 0, None
    r_end = len(rows) - 1
    if opts.get("range"):
        r0, c0, r1, c1 = parse_range(opts["range"])
        if hr - 1 < r0 or hr - 1 > r1:
            _fail(f"{where}: header_row {hr} lies outside the range {opts['range']}")
        r_end, c_end = min(r1, len(rows) - 1), c1
    if hr > len(rows):
        _fail(f"{where}: header_row {hr}, and the {'sheet' if g['format'] == 'xlsx' else 'file'} has {len(rows)} rows")
    cut = (lambda r: r[c0:c_end + 1]) if c_end is not None else (lambda r: r[c0:])
    head = cut(rows[hr - 1])
    named = [j for j, x in enumerate(head) if x != ""]
    if not named:
        _fail(f"{where}: row {hr} is empty; give the header's row (input.header_row)")
    width = named[-1] + 1
    header = head[:width]
    empty = [col_letters(c0 + j) for j, x in enumerate(header) if x.strip() == ""]
    if empty:
        _fail(f"{where}: the header (row {hr}) has no name in column {', '.join(empty)}; name it, or give input.range")
    dup = sorted({h for h in header if header.count(h) > 1})
    if dup:
        _fail(f"{where}: the header (row {hr}) names {', '.join(map(repr, dup))} more than once")
    body, last, ignored = [], hr, 0
    for i in range(hr, r_end + 1):
        r = cut(rows[i])
        if not any(x.strip() for x in r):
            ignored = sum(1 for k in range(i + 1, r_end + 1) if any(x.strip() for x in cut(rows[k])))
            break
        extra = [col_letters(c0 + j) for j, x in enumerate(r[width:], start=width) if x.strip()]
        if extra:
            _fail(f"{where}: row {i + 1} holds data in column {', '.join(extra)}, beyond the header; name the column "
                  "or give input.range")
        cells = r[:width] + [""] * (width - len(r[:width]))
        if opts.get("decimal") == ",":
            cells = [_decimal(x) for x in cells]
        body.append(cells)
        last = i + 1
    if not body:
        _fail(f"{where}: no data rows after the header (row {hr})")
    canonical = canonical_csv(header, body)
    recipe = {"format": g["format"], "header_row": hr}
    if g["format"] == "xlsx":
        recipe["sheet"] = g["sheet"]
    else:
        recipe["encoding"], recipe["delimiter"] = g["encoding"], g["delimiter"]
    if opts.get("range"):
        recipe["range"] = opts["range"].strip().upper()
    if opts.get("decimal"):
        recipe["decimal"] = opts["decimal"]
    if opts.get("percent"):
        recipe["percent"] = opts["percent"]
    used = f"{col_letters(c0)}{hr}:{col_letters(c0 + width - 1)}{last}"
    return {"header": header, "body": body, "canonical": canonical, "recipe": recipe, "media_type": g["media_type"],
            "identity": canonical == data, "rows_used": used, "ignored_after": ignored,
            **({"sheets": g["sheets"]} if "sheets" in g else {})}


def canonical_csv(header: list[str], body: list[list[str]]) -> bytes:
    """A table written as scale_csv_v1: UTF-8, comma, double quotes where needed, one line per row ending in LF."""
    buf = io.StringIO(newline="")
    w = csv.writer(buf, delimiter=",", quotechar='"', doublequote=True, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    w.writerow(header)
    for r in body:
        w.writerow(r)                               # a lone empty field is written "" (never an empty line)
    return buf.getvalue().encode("utf-8")


def recipe_strings(recipe: dict) -> dict[str, str]:
    """A recipe as the strings ``table.ingest`` holds (``input_<key>``)."""
    return {f"input_{k}": str(v) for k, v in recipe.items()}


def recipe_from_ingest(ingest: dict) -> dict:
    """The recipe ``table.ingest`` records, back as input options."""
    out = {}
    for k in RECIPE_KEYS:
        v = ingest.get(f"input_{k}")
        if isinstance(v, str):
            out[k] = int(v) if k == "header_row" else v
    return out


# ------------------------------------------------------------------------------------------ a minimal writer
def write_xlsx(sheets: list[tuple[str, list[list]]], date_cells: set[tuple[str, int, int]] | None = None) -> bytes:
    """A small, deterministic .xlsx workbook (for synthetic examples and tests): each sheet's rows, a ``str`` as a
    shared string, an ``int`` or ``float`` as a number, ``None`` or ``""`` as no cell; ``date_cells`` (the sheet's
    name, the zero-based row and column) are numbers shown as dates. Fixed timestamps: the same rows give the same
    bytes."""
    from xml.sax.saxutils import escape
    date_cells = date_cells or set()
    strings: list[str] = []
    index: dict[str, int] = {}
    sheet_xml = []
    for name, rows in sheets:
        out = []
        for i, row in enumerate(rows):
            cells = []
            for j, v in enumerate(row):
                if v is None or v == "":
                    continue
                ref = f"{col_letters(j)}{i + 1}"
                if isinstance(v, bool):
                    cells.append(f'<c r="{ref}" t="b"><v>{int(v)}</v></c>')
                elif isinstance(v, (int, float)):
                    style = ' s="1"' if (name, i, j) in date_cells else ""
                    cells.append(f'<c r="{ref}"{style}><v>{v!r}</v></c>')
                else:
                    if v not in index:
                        index[v] = len(strings)
                        strings.append(v)
                    cells.append(f'<c r="{ref}" t="s"><v>{index[v]}</v></c>')
            if cells:
                out.append(f'<row r="{i + 1}">{"".join(cells)}</row>')
        sheet_xml.append('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                         f'<worksheet xmlns="{_NS["m"]}"><sheetData>{"".join(out)}</sheetData></worksheet>')
    head = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    od = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    sml = "application/vnd.openxmlformats-officedocument.spreadsheetml"
    n = len(sheets)
    files = {
        "[Content_Types].xml": head + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/xl/workbook.xml" ContentType="{sml}.sheet.main+xml"/>'
        + "".join(f'<Override PartName="/xl/worksheets/sheet{k + 1}.xml" ContentType="{sml}.worksheet+xml"/>'
                  for k in range(n))
        + f'<Override PartName="/xl/sharedStrings.xml" ContentType="{sml}.sharedStrings+xml"/>'
        f'<Override PartName="/xl/styles.xml" ContentType="{sml}.styles+xml"/></Types>',
        "_rels/.rels": head + f'<Relationships xmlns="{_NS["rel"]}"><Relationship Id="rId1" '
        f'Type="{od}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": head + f'<workbook xmlns="{_NS["m"]}" xmlns:r="{_NS["r"]}"><sheets>'
        + "".join(f'<sheet name="{escape(nm, {chr(34): "&quot;"})}" sheetId="{k + 1}" r:id="rId{k + 1}"/>'
                  for k, (nm, _) in enumerate(sheets)) + "</sheets></workbook>",
        "xl/_rels/workbook.xml.rels": head + f'<Relationships xmlns="{_NS["rel"]}">'
        + "".join(f'<Relationship Id="rId{k + 1}" Type="{od}/worksheet" Target="worksheets/sheet{k + 1}.xml"/>'
                  for k in range(n))
        + f'<Relationship Id="rId{n + 1}" Type="{od}/sharedStrings" Target="sharedStrings.xml"/>'
        f'<Relationship Id="rId{n + 2}" Type="{od}/styles" Target="styles.xml"/></Relationships>',
        "xl/sharedStrings.xml": head + f'<sst xmlns="{_NS["m"]}" count="{len(strings)}" uniqueCount="{len(strings)}">'
        + "".join(f'<si><t xml:space="preserve">{escape(s)}</t></si>' for s in strings) + "</sst>",
        "xl/styles.xml": head + f'<styleSheet xmlns="{_NS["m"]}"><numFmts count="1"><numFmt numFmtId="164" '
        'formatCode="yyyy-mm-dd hh:mm"/></numFmts><cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="164" '
        'applyNumberFormat="1"/></cellXfs></styleSheet>',
    }
    for k, x in enumerate(sheet_xml):
        files[f"xl/worksheets/sheet{k + 1}.xml"] = x
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, files[name].encode("utf-8"))
    return buf.getvalue()
