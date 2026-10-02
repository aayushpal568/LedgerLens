import re
from .similarity import normalize

SUPPORTED_EXTENSIONS = ["pdf", "jpg", "jpeg", "png", "tiff", "tif", "docx", "xlsx", "csv"]

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_MONTH_RE = re.compile(r"\b(" + "|".join(MONTHS.keys()) + r")\b")

# Body-text year candidates, evaluated in order of reliability:
#   1. ISO-style date whose leading group is the year (2024-01-15, 2024/1/5)
#   2. US/EU-style date whose trailing group is the year (01/15/2024, 15-01-2024)
#   3. A bare four-digit year that is NOT an identifier fragment.
# The identifier guard in (3) rejects serial/reference numbers such as
# "S-2001", "MP-5001" or "INV-1001" (a letter immediately followed by a hyphen
# right before the digits) as well as a leading digit that makes the run part of
# a larger number, so an invoice serial is never mistaken for a document year.
_ISO_DATE_YEAR_RE = re.compile(r"(?<!\d)(?P<y>(?:19|20)\d{2})[-/]\d{1,2}[-/]\d{1,2}(?!\d)")
_TRAILING_DATE_YEAR_RE = re.compile(r"(?<!\d)\d{1,2}[-/]\d{1,2}[-/](?P<y>(?:19|20)\d{2})(?!\d)")
_BARE_YEAR_RE = re.compile(r"(?<!\d)(?<![A-Za-z]-)(?P<y>(?:19|20)\d{2})(?!\d)")
_BODY_YEAR_RES = (_ISO_DATE_YEAR_RE, _TRAILING_DATE_YEAR_RE, _BARE_YEAR_RE)


def _year_from_body(body: str):
    """Best-effort document year from body text, avoiding identifier false years."""
    for rx in _BODY_YEAR_RES:
        m = rx.search(body)
        if m:
            return int(m.group("y"))
    return None


def detect_period(name: str, text: str):
    """Return {'year': int|None, 'month': int|None} from filename + text.

    Prefer a year in the filename because accounting documents commonly include
    comparative prior-year figures in their body text. When falling back to the
    body, prefer an explicit calendar date and ignore identifier/reference-number
    fragments (e.g. invoice serial "S-2001") that merely resemble a year.
    """
    filename = (name or "").lower()
    body = (text or "")[:2000].lower()
    year = None
    fn_year = _YEAR_RE.search(filename)
    if fn_year:
        year = int(fn_year.group(0))
    else:
        year = _year_from_body(body)
    month = None
    haystack = f"{filename} {body}"
    mm = _MONTH_RE.search(haystack)
    if mm:
        month = MONTHS[mm.group(1)]
    else:
        # numeric month like 2024-03 / 03-2024
        num = re.search(r"\b(0[1-9]|1[0-2])[-_/](19|20)\d{2}\b|\b(19|20)\d{2}[-_/](0[1-9]|1[0-2])\b", haystack)
        if num:
            g = num.group(0)
            parts = re.split(r"[-_/]", g)
            for p in parts:
                if len(p) <= 2:
                    month = int(p)
    return {"year": year, "month": month}


def match_item(item: dict, norm_name: str, text: str) -> bool:
    """Does a file (by normalized name + extracted text) satisfy a checklist item?"""
    keywords = [normalize(item["name"])] + [normalize(a) for a in item.get("aliases", [])]
    rule = item.get("rule") or {}
    vendor = rule.get("vendor")
    if vendor:
        keywords.append(normalize(vendor))
    text_norm = normalize(text[:2000])
    for kw in keywords:
        if not kw:
            continue
        if kw in norm_name or kw in text_norm:
            return True
    return False


def default_templates():
    """Starter checklist templates keyed by common client types."""
    return [
        {
            "name": "Individual Tax Return",
            "client_type": "Individual",
            "items": [
                {"name": "W-2 Wage Statement", "aliases": ["w2", "w-2", "wage statement"], "allowed_types": ["PDF", "JPG", "PNG"], "rule": {"requires_period": True}},
                {"name": "1099 Form", "aliases": ["1099", "1099-int", "1099-div", "1099-misc"], "allowed_types": ["PDF"], "rule": {"requires_period": True}},
                {"name": "Bank Statement", "aliases": ["bank stmt", "statement", "checking"], "allowed_types": ["PDF", "CSV"], "rule": {"requires_period": True}},
                {"name": "Mortgage Interest 1098", "aliases": ["1098", "mortgage interest"], "allowed_types": ["PDF"], "rule": {}},
                {"name": "Prior Year Return", "aliases": ["prior return", "last year return"], "allowed_types": ["PDF"], "rule": {}},
            ],
        },
        {
            "name": "Small Business Package",
            "client_type": "Small Business",
            "items": [
                {"name": "Profit and Loss", "aliases": ["p&l", "pnl", "income statement"], "allowed_types": ["PDF", "XLSX", "CSV"], "rule": {"requires_period": True}},
                {"name": "Balance Sheet", "aliases": ["balance sheet"], "allowed_types": ["PDF", "XLSX"], "rule": {"requires_period": True}},
                {"name": "Bank Statement", "aliases": ["bank stmt", "statement"], "allowed_types": ["PDF", "CSV"], "rule": {"requires_period": True}},
                {"name": "Payroll Report", "aliases": ["payroll", "941", "w-3"], "allowed_types": ["PDF", "XLSX"], "rule": {"requires_period": True}},
                {"name": "Vendor Invoices", "aliases": ["invoice", "invoices", "bill"], "allowed_types": ["PDF", "JPG", "PNG"], "rule": {}},
                {"name": "Sales Tax Return", "aliases": ["sales tax", "vat return"], "allowed_types": ["PDF", "XLSX"], "rule": {"requires_period": True}},
            ],
        },
        {
            "name": "Corporation Year-End",
            "client_type": "Corporation",
            "items": [
                {"name": "Trial Balance", "aliases": ["trial balance", "tb"], "allowed_types": ["XLSX", "CSV", "PDF"], "rule": {"requires_period": True}},
                {"name": "General Ledger", "aliases": ["general ledger", "gl"], "allowed_types": ["XLSX", "CSV", "PDF"], "rule": {"requires_period": True}},
                {"name": "Balance Sheet", "aliases": ["balance sheet"], "allowed_types": ["PDF", "XLSX"], "rule": {"requires_period": True}},
                {"name": "Profit and Loss", "aliases": ["p&l", "income statement"], "allowed_types": ["PDF", "XLSX"], "rule": {"requires_period": True}},
                {"name": "Depreciation Schedule", "aliases": ["depreciation", "fixed asset"], "allowed_types": ["XLSX", "PDF"], "rule": {}},
                {"name": "Board Minutes", "aliases": ["minutes", "board resolution"], "allowed_types": ["PDF", "DOCX"], "rule": {}},
            ],
        },
    ]
