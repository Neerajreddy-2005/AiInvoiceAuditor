"""Validation agents.

Invoice Data Validation: completeness, data types, currency, and the
invoice's own arithmetic, against configs/rules.yaml. Uses the LLM only
to look for missing header fields, and keeps a recovered value only if
it is actually printed in the source text.

Business Validation: compares the invoice with ERP master data fetched
from the mock-erp HTTP API (never from the erp schema directly). Uses
the LLM only to match invoice lines that have no item code and no close
description match to a PO line.

Both return discrepancy dicts: one per mismatched field, with both values
and the deviation, and an action from validation_policies.
"""
import logging
import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher

from . import llm
from .parsing import parse_date, parse_number

log = logging.getLogger(__name__)

ACTION_SEVERITY = {"reject": "high", "manual_review": "medium", "info": "low"}
HEADER_FIELDS = ("invoice_number", "invoice_date", "vendor_name", "po_number", "currency",
                 "subtotal_amount", "tax_rate_pct", "tax_amount", "total_amount")
LINE_FIELDS = ("item_code", "description", "quantity", "unit_price", "line_total")
DECIMAL_FIELDS = {"subtotal_amount", "tax_rate_pct", "tax_amount", "total_amount",
                  "quantity", "unit_price", "line_total"}
LEGAL_SUFFIXES = {"ltd", "limited", "co", "inc", "gmbh", "sa", "pvt", "llc", "corp", "company",
                  "ag", "sl", "srl", "plc", "kg", "bv"}


# ---------------------------------------------------------------- helpers

def D(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def deviation_pct(actual: Decimal, reference: Decimal) -> Decimal | None:
    if reference == 0:
        return None if actual == 0 else Decimal("100")
    pct = (actual - reference) / abs(reference) * 100
    # audit.discrepancies.deviation_pct is NUMERIC(10,4).
    return max(-MAX_DEVIATION_PCT, min(MAX_DEVIATION_PCT, pct)).quantize(Decimal("0.0001"))


def _fmt(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value.normalize(), "f") if value == value.to_integral() else str(value)
    return str(value)


def policy(rules: dict, name: str) -> str:
    action = rules["validation_policies"].get(f"{name}_action", "manual_review")
    if action == "flag":  # legacy value from the original rules file
        action = "manual_review"
    if action not in ACTION_SEVERITY:
        raise ValueError(f"Unknown action {action!r} for policy {name}")
    return action


def discrepancy(check_type: str, field_name: str, *, action: str, message: str, line_number=None,
                invoice_value=None, erp_value=None, expected_value=None,
                deviation=None, tolerance=None) -> dict:
    return {
        "check_type": check_type,
        "field_name": field_name,
        "line_number": line_number,
        "invoice_value": _fmt(invoice_value),
        "erp_value": _fmt(erp_value),
        "expected_value": _fmt(expected_value),
        "deviation_pct": _fmt(deviation),
        "tolerance_pct": _fmt(tolerance),
        "severity": ACTION_SEVERITY[action],
        "action": action,
        "message": message,
    }


MAX_DEVIATION_PCT = Decimal("999999.9999")


def _fold(text: str) -> str:
    norm = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in norm if not unicodedata.combining(c)).lower()


def _tokens(text: str, drop: set[str] = frozenset()) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", _fold(text)) if t not in drop]


def name_similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a, LEGAL_SUFFIXES), _tokens(b, LEGAL_SUFFIXES)
    sa, sb = " ".join(ta), " ".join(tb)
    if not sa or not sb:
        return 0.0
    if sa == sb:
        return 1.0
    # A shorter name may omit words, but one shared word ("Global") proves nothing.
    shorter, longer = sorted((ta, tb), key=len)
    if len(shorter) >= 2 and set(shorter) <= set(longer):
        return 1.0
    return SequenceMatcher(None, sa, sb).ratio()


def description_similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    seq = SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
    containment = len(set(ta) & set(tb)) / min(len(set(ta)), len(set(tb)))
    return (seq + containment) / 2


# ---------------------------------------------------------------- data validation

def normalise_invoice_number(number: str | None) -> str:
    # Must match the SQL in repository.find_same_number.
    return re.sub(r"[^a-z0-9]", "", (number or "").lower())


def same_vendor(a: str | None, b: str | None, rules: dict) -> bool:
    """A missing name can't rule out a match, and a legal-suffix variant is still the same vendor."""
    return not a or not b or name_similarity(a, b) >= float(rules["tolerances"]["vendor_name_min_similarity"])


def _outside_tolerance(actual: Decimal, expected: Decimal, percent: Decimal, rules: dict) -> bool:
    """Tolerances absorb rounding, so the percentage is also capped at a fixed amount of money."""
    dev = deviation_pct(actual, expected)
    limit = Decimal(str(rules["tolerances"]["rounding_absolute_limit"]))
    return dev is not None and (abs(dev) > percent or abs(actual - expected) > limit)


def validate_invoice_data(invoice: dict, line_items: list[dict], rules: dict,
                          duplicates: list[dict] | None = None, po_repeats: list[dict] | None = None) -> dict:
    found: list[dict] = []
    missing: list[str] = []
    required = rules["required_fields"]
    tol = rules["tolerances"]
    allow_code_from_po = rules["validation_policies"].get("allow_item_code_from_po_match", False)

    # completeness
    for field in required["header"]:
        if invoice.get(field) in (None, ""):
            missing.append(field)
            found.append(discrepancy("completeness", field, action=policy(rules, "missing_field"),
                                     message=f"Required field '{field}' is missing from the invoice"))
    if not line_items:
        missing.append("line_items")
        found.append(discrepancy("completeness", "line_items", action=policy(rules, "missing_field"),
                                 message="No line items could be read from the invoice"))
    for item in line_items:
        n = item["line_number"]
        for field in required["line_item"]:
            if item.get(field) in (None, ""):
                missing.append(f"line_items[{n}].{field}")
                if field == "item_code" and allow_code_from_po:
                    action, note = "info", " (will be matched to the PO by description)"
                else:
                    action, note = policy(rules, "missing_field"), ""
                found.append(discrepancy("completeness", field, line_number=n, action=action,
                                         message=f"Line {n}: required field '{field}' is missing{note}"))

    # data types
    for field in DECIMAL_FIELDS & set(invoice):
        if invoice.get(field) not in (None, "") and D(invoice[field]) is None:
            found.append(discrepancy("data_type", field, action=policy(rules, "missing_field"),
                                     invoice_value=invoice[field], message=f"'{field}' is not a valid number"))
    if invoice.get("invoice_date"):
        try:
            date.fromisoformat(str(invoice["invoice_date"]))
        except ValueError:
            found.append(discrepancy("data_type", "invoice_date", action=policy(rules, "missing_field"),
                                     invoice_value=invoice["invoice_date"], message="Invoice date is not a valid date"))
    for item in line_items:
        for field in ("quantity", "unit_price", "line_total"):
            if item.get(field) not in (None, "") and D(item[field]) is None:
                found.append(discrepancy("data_type", field, line_number=item["line_number"],
                                         action=policy(rules, "missing_field"), invoice_value=item[field],
                                         message=f"Line {item['line_number']}: '{field}' is not a valid number"))

    # currency
    currency = invoice.get("currency")
    if currency and currency.upper() not in rules["accepted_currencies"]:
        found.append(discrepancy("currency", "currency", action=policy(rules, "invalid_currency"),
                                 invoice_value=currency, expected_value=", ".join(rules["accepted_currencies"]),
                                 message=f"Currency {currency} is not an accepted currency"))

    # arithmetic
    line_total_tol = Decimal(str(tol["line_total_difference_percent"]))
    total_tol = Decimal(str(tol["total_difference_percent"]))
    tax_tol = Decimal(str(tol["tax_difference_percent"]))

    for item in line_items:
        qty, price, total = D(item.get("quantity")), D(item.get("unit_price")), D(item.get("line_total"))
        if None in (qty, price, total):
            continue
        expected = (qty * price).quantize(Decimal("0.01"))
        dev = deviation_pct(total, expected)
        if _outside_tolerance(total, expected, line_total_tol, rules):
            found.append(discrepancy(
                "arithmetic", "line_total", line_number=item["line_number"],
                action=policy(rules, "total_mismatch"), invoice_value=total, expected_value=expected,
                deviation=dev, tolerance=line_total_tol,
                message=f"Line {item['line_number']}: {qty} x {price} = {expected}, invoice says {total}"))

    line_sum = None
    if line_items and all(D(i.get("line_total")) is not None for i in line_items):
        line_sum = sum(D(i["line_total"]) for i in line_items)
    subtotal, tax = D(invoice.get("subtotal_amount")), D(invoice.get("tax_amount"))
    total, rate = D(invoice.get("total_amount")), D(invoice.get("tax_rate_pct"))

    def _check(field, actual, expected, tolerance, message):
        if _outside_tolerance(actual, expected, tolerance, rules):
            found.append(discrepancy("arithmetic", field, action=policy(rules, "total_mismatch"),
                                     invoice_value=actual, expected_value=expected,
                                     deviation=deviation_pct(actual, expected),
                                     tolerance=tolerance, message=message))

    if line_sum is not None and subtotal is not None:
        _check("subtotal_amount", subtotal, line_sum, total_tol,
               f"Line totals add up to {line_sum}, invoice subtotal is {subtotal}")
    # Without a printed subtotal, the line totals are the net amount.
    net = subtotal if subtotal is not None else line_sum
    if net is not None and tax is not None and total is not None:
        _check("total_amount", total, net + tax, tax_tol,
               f"Net {net} + tax {tax} = {net + tax}, invoice total is {total}")
    elif net is not None and tax is None and total is not None:
        _check("total_amount", total, net, total_tol,
               f"No tax stated: net {net} should equal total {total}")
    if net is not None and tax is not None and rate is not None:
        expected_tax = (net * rate / 100).quantize(Decimal("0.01"))
        _check("tax_amount", tax, expected_tax, tax_tol,
               f"{rate}% of {net} = {expected_tax}, invoice tax is {tax}")
    elif tax is not None and rate is None:
        found.append(discrepancy("arithmetic", "tax_amount", action=policy(rules, "total_mismatch"),
                                 invoice_value=tax, message="Tax is stated without a rate, so it cannot be verified"))

    # duplicates (same vendor + invoice number already audited under another file)
    for dup in duplicates or []:
        found.append(discrepancy(
            "duplicate", "invoice_number", action=policy(rules, "duplicate_invoice"),
            invoice_value=invoice.get("invoice_number"),
            message=f"Invoice number already audited from file {dup['file_name']} "
                    f"(recommendation: {dup.get('recommendation') or 'n/a'})"))

    # repeat billing (a different invoice number against a PO that was already invoiced)
    if po_repeats:
        files = ", ".join(r["file_name"] for r in po_repeats[:5])
        found.append(discrepancy(
            "duplicate", "po_number", action=policy(rules, "repeat_po_billing"), invoice_value=invoice.get("po_number"),
            message=f"PO {invoice.get('po_number')} was already invoiced by {files}: check this isn't billed twice"))

    return {"discrepancies": found, "missing_fields": missing}


def recover_missing_fields(text: str, missing: list[str], symbol_map: dict) -> dict:
    """Ask the LLM for missing header fields; keep only values printed in the text."""
    wanted = [f for f in missing if f in ("invoice_number", "invoice_date", "vendor_name",
                                          "currency", "total_amount", "po_number")]
    if not wanted or not text.strip():
        return {"accepted": {}, "rejected": {}}
    result = llm.complete_json(llm.prompt_messages(
        "field_recovery", missing_fields=", ".join(wanted), text=llm.truncate(text)))
    proposed = result.get("fields") or {}
    accepted, rejected = {}, {}
    for field in wanted:
        value = proposed.get(field)
        if value in (None, "", "null"):
            continue
        value = str(value).strip()
        if _printed_in_text(field, value, text, symbol_map):
            accepted[field] = value
        else:
            rejected[field] = value
    if rejected:
        log.info("Discarded LLM-proposed values not found in the text: %s", rejected)
    return {"accepted": accepted, "rejected": rejected}


def _printed_in_text(field: str, value: str, text: str, symbol_map: dict) -> bool:
    folded_text = _fold(text)
    if field in ("invoice_number", "po_number", "vendor_name"):
        return _fold(value) in folded_text
    if field == "currency":
        code = value.upper()
        symbols = [s for s, c in symbol_map.items() if c == code]
        return re.search(rf"\b{re.escape(code)}\b", text) is not None or any(s in text for s in symbols)
    if field == "total_amount":
        target = parse_number(value)
        numbers = {parse_number(n) for n in re.findall(r"\d[\d.,]*", text)}
        return target is not None and target in numbers
    if field == "invoice_date":
        try:
            target = date.fromisoformat(value)
        except ValueError:
            return False
        return any(parse_date(line) == target for line in text.splitlines())
    return False


# ---------------------------------------------------------------- business validation

def _match_lines(line_items: list[dict], po_lines: list[dict], rules: dict, use_llm: bool) -> list[dict]:
    """Returns one entry per invoice line: {invoice_line, po_line (or None), matched_by, score}."""
    min_sim = float(rules["tolerances"]["line_description_min_similarity"])
    taken: set[int] = set()
    matches: dict[int, dict] = {}
    by_code = {pl["item_code"].upper(): pl for pl in po_lines}

    for item in line_items:
        code = (item.get("item_code") or "").upper()
        if code and code in by_code and by_code[code]["line_number"] not in taken:
            pl = by_code[code]
            taken.add(pl["line_number"])
            matches[item["line_number"]] = {"po_line": pl, "matched_by": "item_code", "score": 1.0}

    for item in line_items:
        if item["line_number"] in matches or not item.get("description"):
            continue
        scored = sorted(((description_similarity(item["description"], pl["description"]), pl)
                         for pl in po_lines if pl["line_number"] not in taken),
                        key=lambda s: s[0], reverse=True)
        if scored and scored[0][0] >= min_sim:
            score, pl = scored[0]
            taken.add(pl["line_number"])
            matches[item["line_number"]] = {"po_line": pl, "matched_by": "description", "score": round(score, 3)}

    unmatched = [i for i in line_items if i["line_number"] not in matches and i.get("description")]
    available = [pl for pl in po_lines if pl["line_number"] not in taken]
    if use_llm and unmatched and available:
        try:
            matches.update(_llm_match(unmatched, available, taken))
        except llm.LLMError as exc:
            log.warning("LLM line matching unavailable: %s", exc)

    return [{"invoice_line": item, **matches.get(item["line_number"],
                                                 {"po_line": None, "matched_by": None, "score": 0.0})}
            for item in line_items]


def _llm_match(unmatched: list[dict], available: list[dict], taken: set[int]) -> dict[int, dict]:
    inv_text = "\n".join(f"{i['line_number']}. {i.get('description')} (qty {i.get('quantity')}, "
                         f"unit price {i.get('unit_price')})" for i in unmatched)
    po_text = "\n".join(f"{p['line_number']}. {p['item_code']} {p['description']} (qty {p['quantity']}, "
                        f"unit price {p['unit_price']})" for p in available)
    result = llm.complete_json(llm.prompt_messages("line_matching", invoice_lines=inv_text, po_lines=po_text))
    by_number = {p["line_number"]: p for p in available}
    wanted = {i["line_number"] for i in unmatched}
    out: dict[int, dict] = {}
    matches = result.get("matches")
    for m in matches if isinstance(matches, list) else []:
        try:
            inv_n, conf = int(m["invoice_line"]), float(m.get("confidence", 0))
            po_n = None if m.get("po_line") is None else int(m["po_line"])
        except (AttributeError, KeyError, OverflowError, TypeError, ValueError):
            continue
        if po_n is None or not conf >= 0.7 or conf > 1 or inv_n not in wanted or inv_n in out:
            continue
        if po_n in by_number and po_n not in taken:
            taken.add(po_n)
            out[inv_n] = {"po_line": by_number[po_n], "matched_by": "llm", "score": conf}
    return out


def _check_against_po_value(invoice: dict, line_items: list[dict], matches: list[dict], rules: dict) -> list[dict]:
    """The invoice's money can't exceed what the PO allows, even if each small step looks like rounding."""
    if not matches or any(m["po_line"] is None or D(m["po_line"]["quantity"]) is None
                          or D(m["po_line"]["unit_price"]) is None for m in matches):
        return []  # an unmatched line is already flagged, and leaves no PO value to compare with
    tol = rules["tolerances"]
    price_factor = 1 + Decimal(str(tol["price_difference_percent"])) / 100
    limit = Decimal(str(tol["rounding_absolute_limit"]))
    allowed_net = sum(D(m["po_line"]["quantity"]) * D(m["po_line"]["unit_price"]) * price_factor for m in matches)
    subtotal, total, rate = (D(invoice.get(f)) for f in ("subtotal_amount", "total_amount", "tax_rate_pct"))
    found = []
    if subtotal is not None and subtotal > allowed_net + limit:
        found.append(discrepancy(
            "erp_price", "subtotal_amount", action=policy(rules, "price_mismatch"), invoice_value=subtotal,
            expected_value=allowed_net.quantize(Decimal("0.01")),
            message=f"Subtotal {_fmt(subtotal)} is above the {allowed_net:.2f} the PO allows, including price tolerance"))
    if total is not None:
        allowed_total = allowed_net * (1 + (rate or Decimal(0)) / 100)
        if total > allowed_total + limit:
            found.append(discrepancy(
                "erp_price", "total_amount", action=policy(rules, "price_mismatch"), invoice_value=total,
                expected_value=allowed_total.quantize(Decimal("0.01")),
                message=f"Total {_fmt(total)} is above the {allowed_total:.2f} the PO allows, including price tolerance and tax"))
    return found


def validate_business(invoice: dict, line_items: list[dict], rules: dict, erp, use_llm: bool = True) -> dict:
    found: list[dict] = []
    tol = rules["tolerances"]
    result = {"discrepancies": found, "vendor_id": None, "po": None, "vendor": None, "line_matches": []}

    po_number = invoice.get("po_number")
    if not po_number:
        found.append(discrepancy("erp_po", "po_number", action=policy(rules, "po_not_found"),
                                 message="No purchase order reference on the invoice; ERP checks could not run"))
        return result
    po = erp.get_po(po_number)
    if po is None:
        found.append(discrepancy("erp_po", "po_number", action=policy(rules, "po_not_found"),
                                 invoice_value=po_number, message=f"PO {po_number} does not exist in the ERP"))
        return result
    result["po"] = po
    result["vendor_id"] = po["vendor_id"]

    vendor = erp.get_vendor(po["vendor_id"])
    result["vendor"] = vendor
    if vendor and invoice.get("vendor_name"):
        similarity = name_similarity(invoice["vendor_name"], vendor["vendor_name"])
        if similarity < float(tol["vendor_name_min_similarity"]):
            found.append(discrepancy(
                "erp_vendor", "vendor_name", action=policy(rules, "vendor_mismatch"),
                invoice_value=invoice["vendor_name"], erp_value=vendor["vendor_name"],
                message=f"Invoice vendor does not match the PO's vendor {vendor['vendor_id']} "
                        f"(similarity {similarity:.2f})"))

    po_currency = po["currency"]
    inv_currency = (invoice.get("currency") or "").upper() or None
    if inv_currency and inv_currency != po_currency:
        found.append(discrepancy("erp_currency", "currency", action=policy(rules, "currency_mismatch"),
                                 invoice_value=inv_currency, erp_value=po_currency,
                                 message=f"Invoice currency {inv_currency} differs from PO currency {po_currency}"))
    elif not inv_currency:
        found.append(discrepancy("erp_currency", "currency", action="info", erp_value=po_currency,
                                 message=f"Currency is not printed on the invoice; the PO is in {po_currency}"))

    price_tol = Decimal(str(tol["price_difference_percent"]))
    qty_tol = Decimal(str(tol["quantity_difference_percent"]))
    matches = _match_lines(line_items, po["line_items"], rules, use_llm)
    result["line_matches"] = [{"invoice_line": m["invoice_line"]["line_number"],
                               "po_line": m["po_line"]["line_number"] if m["po_line"] else None,
                               "po_item_code": m["po_line"]["item_code"] if m["po_line"] else None,
                               "matched_by": m["matched_by"], "score": m["score"]} for m in matches]

    sku_cache: dict[str, dict | None] = {}
    inv_rate = D(invoice.get("tax_rate_pct"))
    rate_flagged: set[str] = set()

    for m in matches:
        item, pl = m["invoice_line"], m["po_line"]
        n = item["line_number"]
        if pl is None:
            found.append(discrepancy(
                "erp_line", "line_item", line_number=n, action=policy(rules, "unmatched_line"),
                invoice_value=item.get("item_code") or item.get("description"),
                message=f"Line {n} ({item.get('item_code') or item.get('description')}) is not on PO {po['po_number']}"))
            continue
        if item.get("item_code") and item["item_code"].upper() != pl["item_code"].upper():
            found.append(discrepancy(
                "erp_line", "item_code", line_number=n, action=policy(rules, "unmatched_line"),
                invoice_value=item["item_code"], erp_value=pl["item_code"],
                message=f"Line {n}: item code differs from the matching PO line"))
        if m["matched_by"] == "llm":
            # The AI's match is only a suggestion: invoice text can influence it, so a person confirms it.
            found.append(discrepancy(
                "erp_line", "line_item", line_number=n, action=policy(rules, "unmatched_line"),
                invoice_value=item.get("description"), erp_value=pl["item_code"],
                message=f"Line {n} ({item.get('description')}): the AI suggested PO line {pl['line_number']} "
                        f"({pl['item_code']}), but the descriptions differ. Please confirm"))

        qty, po_qty = D(item.get("quantity")), D(pl["quantity"])
        if qty is not None and po_qty is not None:
            dev = deviation_pct(qty, po_qty)
            if dev is not None and abs(dev) > qty_tol:
                found.append(discrepancy(
                    "erp_quantity", "quantity", line_number=n, action=policy(rules, "quantity_mismatch"),
                    invoice_value=qty, erp_value=po_qty, deviation=dev, tolerance=qty_tol,
                    message=f"Line {n} ({pl['item_code']}): invoiced quantity {_fmt(qty)}, PO quantity {_fmt(po_qty)}"))

        price, po_price = D(item.get("unit_price")), D(pl["unit_price"])
        if price is not None and po_price is not None:
            dev = deviation_pct(price, po_price)
            if dev is not None and abs(dev) > price_tol:
                found.append(discrepancy(
                    "erp_price", "unit_price", line_number=n, action=policy(rules, "price_mismatch"),
                    invoice_value=price, erp_value=po_price, deviation=dev, tolerance=price_tol,
                    message=f"Line {n} ({pl['item_code']}): unit price {_fmt(price)} vs PO {_fmt(po_price)} "
                            f"({dev:+.1f}%, tolerance ±{_fmt(price_tol)}%)"))

        code = pl["item_code"]
        if code not in sku_cache:
            sku_cache[code] = erp.get_sku(code)
        sku = sku_cache[code]
        if sku is None:
            found.append(discrepancy("erp_sku", "item_code", line_number=n, action=policy(rules, "unknown_sku"),
                                     invoice_value=code, message=f"SKU {code} is not in the SKU master"))
        elif inv_rate is not None and code not in rate_flagged:
            gst = D(sku["gst_rate"])
            if gst is not None and abs(inv_rate - gst) > Decimal("0.01"):
                rate_flagged.add(code)
                found.append(discrepancy(
                    "erp_tax", "tax_rate_pct", line_number=n, action=policy(rules, "tax_rate_mismatch"),
                    invoice_value=inv_rate, erp_value=gst,
                    message=f"Invoice tax rate {_fmt(inv_rate)}% differs from SKU {code} rate {_fmt(gst)}%"))

    matched_po = {m["po_line"]["line_number"] for m in matches if m["po_line"]}
    for pl in po["line_items"]:
        if pl["line_number"] not in matched_po:
            found.append(discrepancy(
                "erp_line", "line_item", action=policy(rules, "uninvoiced_po_line"), erp_value=pl["item_code"],
                message=f"PO line {pl['line_number']} ({pl['item_code']} {pl['description']}) was not invoiced"))

    found.extend(_check_against_po_value(invoice, line_items, matches, rules))
    return result
