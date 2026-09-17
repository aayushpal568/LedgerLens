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


def detect_period(name: str, text: str):
    """Return {'year': int|None, 'month': int|None} from filename + text.

    Prefer a year in the filename because accounting documents commonly include
    comparative prior-year figures in their body text.
    """
    filename = (name or "").lower()
    body = (text or "")[:2000].lower()
    haystack = f"{filename} {body}"
    year = None
    ym = _YEAR_RE.search(filename) or _YEAR_RE.search(body)
    if ym:
        year = int(ym.group(0))
    month = None
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
