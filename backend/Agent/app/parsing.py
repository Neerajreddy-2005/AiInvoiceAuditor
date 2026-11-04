"""Raw invoice text -> structured fields (deterministic, no LLM).

Handles the label vocabularies, date formats and table layouts found in
the dataset (English, Spanish, German; pipe tables, whitespace tables
from OCR, and single-item key/value invoices).

Rules:
  * a value is only recorded if it is printed in the text; otherwise NULL
  * currency is never inferred from a country or vendor
  * anything labelled but not mapped to a column goes to `extras` (JSONB)
"""
import re
import unicodedata
from collections import Counter
from contextvars import ContextVar
from datetime import date
from decimal import Decimal, InvalidOperation
from difflib import get_close_matches

DEFAULT_SYMBOL_MAP = {"$": "USD", "€": "EUR", "₹": "INR", "£": "GBP"}
CURRENCY_CODES = {"USD", "EUR", "GBP", "INR", "VND", "JPY", "CHF", "AUD", "CAD", "CNY", "SGD", "AED"}
SYMBOLS = "$€£₹"

MONTHS = {
    # English
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9,
    "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
    # Spanish
    "enero": 1, "ene": 1, "febrero": 2, "marzo": 3, "abril": 4, "abr": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "ago": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12, "dic": 12,
    # German
    "januar": 1, "jänner": 1, "februar": 2, "märz": 3, "maerz": 3, "mrz": 3, "mai": 5, "juni": 6,
    "juli": 7, "oktober": 10, "okt": 10, "dezember": 12, "dez": 12,
}

I = re.IGNORECASE

# Header field rules, tried in order on each line. Order matters:
# "Invoice Date: ..." must be claimed by invoice_date before invoice_number.
FIELD_RULES = [
    ("invoice_date", re.compile(
        r"^(?:invoice\s*date|date\s*of\s*invoice|date|fecha(?:\s+de\s+(?:la\s+)?(?:factura|emisi[oó]n))?"
        r"|rechnungsdatum|datum)\s*[:.]?\s*(?P<v>.+)$", I)),
    ("invoice_number", re.compile(
        # [il1|] tolerates OCR confusions such as "lnvoice" / "InvoLce"
        r"^(?:[il1|]nvo[il1|]ce\s*(?:no|number|nr|num|#)?|factura\s*(?:no|n[º°o]|n[uú]m(?:ero)?|#)?"
        r"|rechnungs?\s*-?\s*(?:nummer|nr)|bill\s*(?:no|number))\s*[.:#]?\s*[:#]?\s*"
        r"(?P<v>[A-Z0-9][A-Z0-9\-/_.]*)\s*$", I)),
    ("po_number", re.compile(
        r"^(?:p\.?\s?o\.?(?:\s*(?:reference|ref|no|number|nr|#))?|purchase\s*order(?:\s*(?:no|number|ref))?"
        r"|referencia\s*de\s*pedido|n[º°o]?\s*(?:de\s*)?pedido|pedido|bestell(?:nummer|nr|ung))"
        r"\s*[.:#]?\s*[:#]?\s*(?P<v>[A-Z0-9][A-Z0-9\-/_]*)\s*$", I)),
    ("subtotal_amount", re.compile(
        r"^(?:sub\s*-?\s*total|zwischensumme|nettobetrag|base\s*imponible|net\s*amount)\s*[:]?\s*(?P<v>.+)$", I)),
    ("tax", re.compile(
        r"^(?:tax|vat|gst|iva|mwst|ust|igv)\b\.?\s*(?:\(\s*(?P<rate>\d+(?:[.,]\d+)?)\s*%\s*\))?"
        r"\s*[:]?\s*(?P<v>.+)$", I)),
    ("total_amount", re.compile(
        r"^(?P<label>total\s*amount\s*due|amount\s*due|grand\s*total|total\s*due|invoice\s*total"
        r"|total\s*amount|gesamtbetrag|rechnungsbetrag|gesamtsumme|importe\s*total|total\s*a\s*pagar|total)"
        r"\s*[:]?\s*(?P<v>.+)$", I)),
]

GENERIC_LABEL = re.compile(r"^(?P<k>[A-Za-zÀ-ÿ][\wÀ-ÿ .()/'#-]{1,40}?)\s*:{1,2}\s*(?P<v>\S.*)$")
PO_PATTERN = re.compile(r"\bPO[-\s]?(\d{3,})\b", I)

AMOUNT_RE = re.compile(
    r"^\s*(?:(?P<pre>[A-Z]{3}|[$€£₹])\s*)?(?P<num>-?\d[\d.,']*)\s*(?P<post>[A-Z]{3}|[$€£₹])?\s*$")

# Single-item invoices written as key/value pairs (e.g. INV_EN_002)
KV_ITEM_RULES = {
    "item_code": re.compile(r"^(?:item\s*code|sku|c[oó]digo|artikel(?:code|nummer|-nr))\s*[:]\s*(?P<v>.+)$", I),
    "description": re.compile(r"^(?:description|descripci[oó]n|beschreibung|bezeichnung|item)\s*[:]\s*(?P<v>.+)$", I),
    "quantity": re.compile(r"^(?:quantity|qty|cantidad|menge|anzahl)\s*[:]\s*(?P<v>.+)$", I),
    "unit_price": re.compile(r"^(?:unit\s*price|price|precio(?:\s*unitario)?|einzelpreis|preis)\s*[:]\s*(?P<v>.+)$", I),
    "line_total": re.compile(r"^(?:line\s*total|amount|importe|betrag|gesamtpreis)\s*[:]\s*(?P<v>.+)$", I),
}

HEADER_CELLS = {
    "item_code": {"item code", "code", "codigo", "artikelcode", "artikelnummer", "artikel-nr", "art.-nr",
                  "sku", "item no", "item #"},
    "description": {"description", "descripcion", "beschreibung", "bezeichnung", "item", "articulo",
                    "concepto", "leistung", "charge description"},
    "quantity": {"qty", "quantity", "cantidad", "cant", "menge", "anzahl"},
    "unit_price": {"unit price", "unit", "price", "precio", "precio unitario", "preis", "einzelpreis", "rate"},
    "line_total": {"total", "line total", "amount", "importe", "gesamt", "betrag", "gesamtpreis"},
}

MAX_ROW_CHARS = 500
MAX_LINE_CHARS = 2000
WS_ROW = re.compile(
    r"^(?:(?P<code>[A-Z]{2,}[-_]\d+)\s+)?(?P<desc>.*?[A-Za-zÀ-ÿ].*?)\s+(?P<qty>\d+(?:[.,]\d+)?)\s+"
    r"(?P<unit>[$€£₹]?\s?\d[\d.,]*)\s+(?P<total>[$€£₹]?\s?\d[\d.,]*)\s*$")


def _fold(text: str) -> str:
    """lowercase, strip accents: 'Descripción' -> 'descripcion'"""
    norm = unicodedata.normalize("NFKD", text)
    return "".join(c for c in norm if not unicodedata.combining(c)).lower().strip()


# ---------------------------------------------------------------- values

# Set while reading a document whose decimals use a comma: there, 1.500 means 1500.
_dot_groups_thousands: ContextVar[bool] = ContextVar("dot_groups_thousands", default=False)


def _uses_decimal_comma(text: str) -> bool:
    without_dates = re.sub(r"\b\d{1,2}\.\d{1,2}\.\d{2,4}\b", " ", text)
    return (re.search(r"\d,\d{1,2}(?!\d)", without_dates) is not None
            and re.search(r"\d\.\d{1,2}(?!\d)", without_dates) is None)


def parse_number(raw: str) -> Decimal | None:
    s = raw.strip().replace("'", "").replace(" ", "")
    if not s or not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):   # 1.234,56
            s = s.replace(".", "").replace(",", ".")
        else:                             # 1,234.56
            s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        s = s.replace(",", ".") if len(parts) == 2 and len(parts[1]) in (1, 2) else s.replace(",", "")
    elif s.count(".") > 1 or (_dot_groups_thousands.get() and re.fullmatch(r"-?\d{1,3}\.\d{3}", s)):
        s = s.replace(".", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def parse_amount(raw: str, symbol_map: dict | None = None) -> tuple[Decimal | None, str | None]:
    """'$1,470.00' -> (Decimal('1470.00'), 'USD'). Rejects anything that is not just an amount."""
    symbol_map = symbol_map or DEFAULT_SYMBOL_MAP
    m = AMOUNT_RE.match(raw)
    if not m:
        return None, None
    currency = None
    for token in (m.group("pre"), m.group("post")):
        if not token:
            continue
        if token in symbol_map:
            currency = symbol_map[token]
        elif token.upper() in CURRENCY_CODES:
            currency = token.upper()
        else:
            return None, None  # e.g. "VAT 330,526.00" is not an amount
    return parse_number(m.group("num")), currency


def parse_quantity(raw: str) -> Decimal | None:
    m = re.search(r"-?\d[\d.,]*", raw)
    return parse_number(m.group(0)) if m else None


def _month(token: str) -> int | None:
    """Month name -> number; tolerates one OCR-misread letter ("MOY" -> May)."""
    token = token.lower()
    if token in MONTHS:
        return MONTHS[token]
    if len(token) < 3:
        return None
    close = get_close_matches(token, MONTHS.keys(), n=2, cutoff=0.6)
    candidates = {MONTHS[c] for c in close}
    return candidates.pop() if len(candidates) == 1 else None


def parse_date(raw: str, day_first: bool = True) -> date | None:
    v = re.sub(r"\s+", " ", raw).strip()[:MAX_LINE_CHARS]
    try:
        m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", v)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b", v)
        if m:
            a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            d, mo = (a, b) if day_first else (b, a)
            if mo > 12 and d <= 12:
                d, mo = mo, d
            return date(y + 2000 if y < 100 else y, mo, d)
        m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\.?[\s\-/]+(?:de\s+)?([A-Za-zÀ-ÿ]+)\.?[\s\-/,]+(?:de\s+)?(\d{4})\b", v, I)
        if m and _month(m.group(2)):
            return date(int(m.group(3)), _month(m.group(2)), int(m.group(1)))
        m = re.search(r"\b([A-Za-zÀ-ÿ]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", v, I)
        if m and _month(m.group(1)):
            return date(int(m.group(3)), _month(m.group(1)), int(m.group(2)))
    except ValueError:
        return None
    return None


def currency_hints(text: str, symbol_map: dict | None = None) -> Counter:
    symbol_map = symbol_map or DEFAULT_SYMBOL_MAP
    hints: Counter = Counter()
    for ch in text:
        if ch in symbol_map:
            hints[symbol_map[ch]] += 1
    for code in re.findall(r"\b([A-Z]{3})\b", text):
        if code in CURRENCY_CODES:
            hints[code] += 1
    return hints


def _s(value) -> str | None:
    """Values in workflow state are JSON-safe strings (checkpoint friendly)."""
    if value is None:
        return None
    return value.isoformat() if isinstance(value, date) else str(value)


# ---------------------------------------------------------------- line items

def _classify_header(cell: str) -> str | None:
    folded = _fold(cell)
    for field, names in HEADER_CELLS.items():
        if folded in names:
            return field
    return None


def _is_numeric_cell(cell: str) -> bool:
    return parse_amount(cell)[0] is not None


def _pipe_items(lines: list[str], symbol_map: dict) -> list[dict]:
    items: list[dict] = []
    columns: list[str | None] | None = None
    for line in lines:
        if line.count("|") < 2:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        numeric = sum(_is_numeric_cell(c) for c in cells)
        if numeric == 0:
            mapped = [_classify_header(c) for c in cells]
            if sum(m is not None for m in mapped) >= 2:
                columns = mapped
            continue
        if numeric < 2 or len(cells) < 3:
            continue
        cols = columns if columns and len(columns) == len(cells) else _positional_columns(cells)
        row = dict(zip(cols, cells))
        items.append(_make_item(row, line, symbol_map))
    return items


def _positional_columns(cells: list[str]) -> list[str | None]:
    if len(cells) >= 5:
        return ["item_code", "description", "quantity", "unit_price", "line_total"] + [None] * (len(cells) - 5)
    if len(cells) == 4:
        return ["description", "quantity", "unit_price", "line_total"]
    return ["description", "quantity", "line_total"]


def _make_item(row: dict, raw_line: str, symbol_map: dict) -> dict:
    code = (row.get("item_code") or "").strip() or None
    return {
        "item_code": code,
        "description": (row.get("description") or "").strip() or None,
        "quantity": _s(parse_quantity(row["quantity"])) if row.get("quantity") else None,
        "unit_price": _s(parse_amount(row["unit_price"], symbol_map)[0]) if row.get("unit_price") else None,
        "line_total": _s(parse_amount(row["line_total"], symbol_map)[0]) if row.get("line_total") else None,
        "raw_line": raw_line.strip(),
    }


def _whitespace_items(lines: list[str], symbol_map: dict) -> list[dict]:
    """Column tables without separators (typical OCR output)."""
    items: list[dict] = []
    in_table = False
    for line in lines:
        folded = _fold(line)
        words = set(re.findall(r"[a-z]+", folded))
        if not in_table:
            has_qty = bool(words & HEADER_CELLS["quantity"])
            has_money = bool(words & {"total", "unit", "price", "precio", "preis", "amount", "importe", "gesamt"})
            if has_qty and has_money and "|" not in line:
                in_table = True
            continue
        if any(rule.match(line) for name, rule in FIELD_RULES if name in ("subtotal_amount", "tax", "total_amount")):
            break
        # WS_ROW backtracks quadratically, and no real table row is this long.
        m = WS_ROW.match(line.strip()) if len(line) <= MAX_ROW_CHARS else None
        if m:
            items.append(_make_item({"item_code": m.group("code"), "description": m.group("desc"),
                                     "quantity": m.group("qty"), "unit_price": m.group("unit"),
                                     "line_total": m.group("total")}, line, symbol_map))
    return items


def _key_value_item(lines: list[str], symbol_map: dict) -> tuple[dict | None, set[int]]:
    found: dict[str, str] = {}
    used: set[int] = set()
    for idx, line in enumerate(lines):
        for field, rule in KV_ITEM_RULES.items():
            m = rule.match(line)
            if m and field not in found:
                found[field] = m.group("v")
                used.add(idx)
                break
    if not {"description", "quantity", "unit_price"} <= found.keys():
        return None, set()
    item = _make_item(found, "; ".join(lines[i] for i in sorted(used)), symbol_map)
    return item, used


# ---------------------------------------------------------------- invoice

def parse_invoice(text: str, symbol_map: dict | None = None) -> dict:
    """Returns {"fields": {...}, "line_items": [...], "extras": {...}}; all values are strings or None."""
    number_style = _dot_groups_thousands.set(_uses_decimal_comma(text))
    try:
        return _read_invoice(text, symbol_map)
    finally:
        _dot_groups_thousands.reset(number_style)


def _read_invoice(text: str, symbol_map: dict | None) -> dict:
    symbol_map = symbol_map or DEFAULT_SYMBOL_MAP
    # The field rules backtrack badly on long runs of spaces, so collapse them and cap each line first.
    lines = [re.sub(r"\s+", " ", ln).strip()[:MAX_LINE_CHARS] for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and not re.fullmatch(r"[-=_*\s]+", ln)]

    fields: dict = {k: None for k in ("invoice_number", "invoice_date", "vendor_name", "po_number", "currency",
                                      "subtotal_amount", "tax_rate_pct", "tax_amount", "total_amount")}
    extras: dict = {}
    consumed: set[int] = set()
    total_candidates: list[tuple[int, int, Decimal, str | None]] = []  # (priority, index, amount, currency)
    amount_currencies: list[str] = []

    for idx, line in enumerate(lines):
        if "|" in line:
            continue
        for name, rule in FIELD_RULES:
            m = rule.match(line)
            if not m:
                continue
            value = m.group("v").strip()
            if name == "invoice_date":
                parsed = parse_date(value)
                if parsed and fields["invoice_date"] is None:
                    fields["invoice_date"] = parsed.isoformat()
                    consumed.add(idx)
                    break
            elif name in ("invoice_number", "po_number"):
                if re.search(r"\d", value) and fields[name] is None:
                    fields[name] = value.upper() if name == "po_number" else value
                    consumed.add(idx)
                    break
            elif name == "subtotal_amount":
                amount, cur = parse_amount(value, symbol_map)
                if amount is not None:
                    fields["subtotal_amount"] = _s(amount)
                    if cur:
                        amount_currencies.append(cur)
                    consumed.add(idx)
                    break
            elif name == "tax":
                amount, cur = parse_amount(value, symbol_map)
                if amount is not None:
                    fields["tax_amount"] = _s(amount)
                    if m.group("rate"):
                        fields["tax_rate_pct"] = _s(parse_number(m.group("rate")))
                    if cur:
                        amount_currencies.append(cur)
                    consumed.add(idx)
                    break
            elif name == "total_amount":
                amount, cur = parse_amount(value, symbol_map)
                if amount is not None:
                    priority = 1 if _fold(m.group("label")) == "total" else 2
                    total_candidates.append((priority, idx, amount, cur))
                    consumed.add(idx)
                    break

    if total_candidates:
        _, _, amount, cur = max(total_candidates, key=lambda c: (c[0], c[1]))
        fields["total_amount"] = _s(amount)
        if cur:
            amount_currencies.insert(0, cur)

    if fields["po_number"] is None:
        m = PO_PATTERN.search(text)
        if m:
            fields["po_number"] = f"PO-{m.group(1)}"
            extras["po_number_source"] = "pattern match in text (no PO label)"

    # Currency: only from symbols/codes actually printed. Prefer the total's.
    if amount_currencies:
        fields["currency"] = amount_currencies[0]
    else:
        hints = currency_hints(text, symbol_map)
        if hints:
            fields["currency"] = hints.most_common(1)[0][0]
            if len(hints) > 1:
                extras["currency_hints"] = dict(hints)

    # Vendor: the first unlabeled line containing words (letterhead).
    for idx, line in enumerate(lines):
        if idx in consumed or ":" in line or "|" in line:
            continue
        if len(re.findall(r"[A-Za-zÀ-ÿ]", line)) >= 3:
            fields["vendor_name"] = line
            consumed.add(idx)
            address = []
            for follow in lines[idx + 1: idx + 4]:
                if ":" in follow or "|" in follow or any(r.match(follow) for _, r in FIELD_RULES):
                    break
                address.append(follow)
            if address:
                extras["vendor_address"] = ", ".join(address)
            break

    # Line items: pipe table, else whitespace table, else key/value single item.
    items = _pipe_items(lines, symbol_map)
    if not items:
        items = _whitespace_items(lines, symbol_map)
    if not items:
        item, used = _key_value_item(lines, symbol_map)
        if item:
            consumed |= used
            if item["line_total"] is None and fields["subtotal_amount"] is not None:
                item["line_total"] = fields["subtotal_amount"]
                extras["derived_fields"] = ["line_items[1].line_total taken from subtotal (single-item invoice)"]
            items = [item]
    for number, item in enumerate(items, start=1):
        item["line_number"] = number

    # Every other "Label: value" line is kept as a variable extra.
    other = {}
    for idx, line in enumerate(lines):
        if idx in consumed or "|" in line:
            continue
        m = GENERIC_LABEL.match(line)
        if m:
            other[m.group("k").strip()] = m.group("v").strip()
    if other:
        extras["other_labeled_fields"] = other

    return {"fields": fields, "line_items": items, "extras": extras}
