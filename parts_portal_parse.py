"""
parts_portal_parse.py
---------------------
Pure HTML parsers for pro.meca.se (Intershop) pages, used by parts_portal.py.
Stdlib only (html.parser). No I/O. Selectors were verified live (see the parts-ordering
progress ledger, "Task 6 live recon of pro.meca.se").

Fail-closed rules:
  - search: only the "Kopplade reservdelar" tab (parts linked to the loaded vehicle) is used;
    discontinued ("Produkten har utgått") and replaced ("Ersätts av") items are never returned.
  - cart: every cart row must parse completely, or PartsPortalError (a line is never dropped).
"""

import re
from collections import Counter
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

MAX_CANDIDATES = 15
LINKED_TAB_LABEL = "kopplade reservdelar"
EXCLUDE_TEXTS = ("produkten har utgått", "ersätts av")
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
             "param", "source", "track", "wbr"}
SKIP_TEXT_TAGS = {"script", "style", "template"}


class PartsPortalError(Exception):
    """pro.meca.se did not behave as expected; nothing beyond the failing step was done."""


# ── Minimal DOM ──────────────────────────────────────────────────────────────

class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict, parent=None):
        self.tag, self.attrs, self.children, self.parent = tag, attrs, [], parent

    def get(self, name: str, default=None):
        return self.attrs.get(name, default)

    def has_class(self, cls: str) -> bool:
        return cls in (self.attrs.get("class") or "").split()

    def iter(self, skip=None):
        """All descendant elements (depth first). `skip(node)` prunes a subtree."""
        for child in self.children:
            if isinstance(child, Node):
                if skip and skip(child):
                    continue
                yield child
                yield from child.iter(skip)

    def find_all(self, tag=None, cls=None, skip=None, **attrs):
        return [n for n in self.iter(skip)
                if (tag is None or n.tag == tag) and (cls is None or n.has_class(cls))
                and all(n.attrs.get(k.replace("_", "-")) == v for k, v in attrs.items())]

    def find(self, tag=None, cls=None, skip=None, **attrs):
        found = self.find_all(tag, cls, skip, **attrs)
        return found[0] if found else None

    def text(self, skip=None) -> str:
        parts = []

        def walk(node):
            for child in node.children:
                if isinstance(child, str):
                    parts.append(child)
                elif child.tag not in SKIP_TEXT_TAGS and not (skip and skip(child)):
                    walk(child)
        walk(self)
        return " ".join(" ".join(parts).split())  # separate cells: never glue numbers together


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(
            Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.stack[-1]))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse_html(html: str) -> Node:
    builder = _Builder()
    builder.feed(html or "")
    builder.close()
    return builder.root


# ── Amounts and quantities ───────────────────────────────────────────────────

_SEK = re.compile(r"(\d+)(?:[:.,](\d{2}|-))?")


def parse_sek(text) -> float | None:
    """Swedish shop amounts: '248:75' -> 248.75, '199:-' -> 199.0, '1 234:50' -> 1234.5.

    Also '248,75' / '248.75'. '-', '' or anything else -> None.
    """
    if text is None:
        return None
    s = re.sub(r"[\s  ]", "", str(text))
    s = re.sub(r"(?i)(kr|sek)$", "", s)
    m = _SEK.fullmatch(s)
    if not m:
        return None
    cents = m.group(2)
    return int(m.group(1)) + (int(cents) / 100 if cents and cents != "-" else 0.0)


def parse_qty_text(text) -> int | None:
    """'1 st' -> 1, '12' -> 12; anything else -> None."""
    m = re.fullmatch(r"(\d+)\s*(?:st)?", str(text or "").strip(), flags=re.I)
    return int(m.group(1)) if m else None


def _first_int(text: str) -> int | None:
    m = re.match(r"\s*(\d+)", text or "")
    return int(m.group(1)) if m else None


# ── Search results ───────────────────────────────────────────────────────────

def _linked_tab(root: Node) -> Node | None:
    for item in root.find_all(cls="tab-section__item"):
        trigger = item.find(cls="js-search-result-tab-trigger")
        if trigger is not None and trigger.text().casefold().startswith(LINKED_TAB_LABEL):
            return item
    return None


def linked_tab_id(html: str) -> str | None:
    """data-tab-id of the "Kopplade reservdelar" tab, or None if the page has none."""
    tab = _linked_tab(parse_html(html))
    return tab.get("data-tab-id") if tab is not None else None


def _not_related(node: Node) -> bool:
    return node.has_class("related-articles")


def _in_stock(card: Node) -> bool | None:
    """Stock at the workshop's own store (not the central warehouse); None if not shown."""
    for address in card.find_all("address", cls="workshop", skip=_not_related):
        if address.has_class("bcp"):
            continue
        value = address.find(cls="stock-list__value")
        count = _first_int(value.text()) if value is not None else None
        return None if count is None else count > 0
    return None


def _candidate(card: Node) -> dict | None:
    sku = (card.get("data-sku") or "").strip()
    if not sku or card.has_class("category-list-item__replaced"):
        return None
    own_text = card.text(skip=_not_related).casefold()
    if any(t in own_text for t in EXCLUDE_TEXTS):
        return None
    container = card.find(cls="category-list-item__text-container", skip=_not_related)
    heading = container.find("h2") if container is not None else None
    title = heading.text() if heading is not None else ""
    price_node = card.find(cls="category-list-item__price", skip=_not_related)
    price = parse_sek(price_node.text()) if price_node is not None else None
    form = card.find("form", cls="product-form-quick", skip=_not_related, data_sku=sku)
    if not title or price is None or form is None:
        return None
    art = card.find(cls="category-list-item__article-nr", skip=_not_related)
    article_nr = re.sub(r"(?i)^art\.?\s*nr\.?\s*", "", art.text()) if art is not None else ""
    return {"title": title, "brand": "", "price_sek": price, "url_or_id": sku,
            "article_nr": article_nr or None, "in_stock": _in_stock(card),
            "placement": (card.get("data-placements") or "").strip()}


def parse_search_results(html: str) -> list[dict]:
    """Candidates from the "Kopplade reservdelar" tab only (max 15). [] if no such tab."""
    tab = _linked_tab(parse_html(html))
    if tab is None:
        return []
    out = []
    for card in tab.find_all("li", cls="category-list-item"):
        cand = _candidate(card)
        if cand is not None:
            out.append(cand)
            if len(out) >= MAX_CANDIDATES:
                break
    return out


# ── Cart ─────────────────────────────────────────────────────────────────────

def _heading_reg(root: Node, prefix: str) -> str | None:
    for h1 in root.find_all("h1"):
        text = h1.text()
        if text.casefold().startswith(prefix.casefold()):
            span = h1.find("span")
            reg = span.text() if span is not None else text[len(prefix):]
            return reg.strip().upper() or None
    return None


def _cart_row(row: Node) -> dict:
    qty_input = row.find("input", cls="js-product-quantity-cart")
    qty = parse_qty_text(qty_input.get("value")) if qty_input is not None else None
    name = row.find(cls="article-name__art-name")
    title = name.text() if name is not None else ""
    if not title or qty is None or qty < 1:
        raise PartsPortalError(f"cart row could not be read (title={title!r}, qty={qty!r})")
    art = row.find(cls="article-name__art-nr")
    m = re.search(r"Artikelnummer\s+(\S+)", art.text()) if art is not None else None
    price = None
    for cell in row.find_all("td"):
        label = cell.find("span")
        if label is not None and label.text() == "Pris per enhet":
            p = cell.find("p")
            price = parse_sek(p.text()) if p is not None else None
            break
    ident = (row.get("data-article-nr") or "").strip() or None
    return {"title": title, "qty": qty, "price_sek": price, "id": ident, "url_or_id": ident,
            "article_nr": m.group(1) if m else None}


def _cart_counts(root: Node) -> list[int]:
    counts = []
    badge = root.find(cls="js-cart-notification")
    if badge is not None and _first_int(badge.text()) is not None:
        counts.append(_first_int(badge.text()))
    for h2 in root.find_all("h2"):
        m = re.fullmatch(r"Varukorg\s*\((\d+)\)", h2.text())
        if m:
            counts.append(int(m.group(1)))
    return counts


def parse_cart(html: str) -> dict:
    """{"lines": [...], "total_sek": float|None, "reg_nr": str|None} from the cart page.

    Raises PartsPortalError if this is not the cart page or any row cannot be read.
    total_sek is the HIGHER of the visible totals; 0.0 for an empty cart.
    """
    root = parse_html(html)
    section = root.find("section", cls="cart-overview-article-table")
    if section is None:
        raise PartsPortalError("not the cart page (no article table)")
    lines = []
    for tbody in section.find_all("tbody"):
        for row in tbody.children:
            if isinstance(row, Node) and row.tag == "tr":
                lines.append(_cart_row(row))
    counts = _cart_counts(root)
    if (not lines and any(counts)) or (lines and counts and not any(counts)):
        raise PartsPortalError(f"cart shows {counts} item(s) but {len(lines)} row(s) were read")
    totals = [parse_sek(n.text()) for n in root.find_all(cls="js-total-tot-sum")]
    for p in root.find_all("p", cls="cart-side-menu__total"):
        span = p.find("span")
        totals.append(parse_sek(span.text()) if span is not None else None)
    if not lines:
        total = 0.0
    else:  # a total that is shown but unreadable fails closed, even if another one parses
        total = max(totals) if totals and None not in totals else None
    return {"lines": lines, "total_sek": total,
            "reg_nr": _heading_reg(root, "Översikt kundvagn")}


# ── Checkout ─────────────────────────────────────────────────────────────────

def _checkout_lines(root: Node) -> list[dict] | None:
    lines = []
    article = root.find(cls="checkout-page-article")
    for row in (article.find_all("tr") if article is not None else []):
        if row.find("th") is not None:
            continue
        qty_input = row.find("input", cls="js-product-quantity-checkout")
        qty = parse_qty_text(qty_input.get("value")) if qty_input is not None else None
        name = row.find(cls="article-name__art-name")
        ident = (row.get("data-article-nr") or "").strip()
        if qty is None or name is None or not ident:
            return None
        lines.append({"id": ident, "qty": qty, "title": name.text()})
    return lines


def parse_checkout(html: str) -> dict:
    """Facts the checkout pre-checks need. "lines" is None if any line is unreadable."""
    root = parse_html(html)
    title_node = root.find("title")
    title = title_node.text() if title_node is not None else ""
    summary = root.find(id="summaryTable") or root
    totals = [parse_sek(n.text()) for n in summary.find_all(cls="js-total-tot-sum")]
    shipping = [n for n in root.find_all("input", name="CheckoutForm_ShippingMethod")
                if "checked" in n.attrs]
    return {
        "title": title,
        "reg_nr": _heading_reg(root, "Beställning"),
        "total_sek": max(totals) if totals and None not in totals else None,
        "shipping_selected": bool(shipping),
        "has_place_order": root.find("button", cls="checkout-page-place-order__button") is not None,
        "lines": _checkout_lines(root),
    }


def same_lines(cart_lines: list[dict], checkout_lines: list[dict] | None) -> bool:
    """The checkout page lists exactly the cart's (id, qty) lines."""
    if not checkout_lines or any(not line.get("id") for line in cart_lines):
        return False
    return (Counter((line["id"], line["qty"]) for line in cart_lines)
            == Counter((line["id"], line["qty"]) for line in checkout_lines))


# ── Order confirmation (structure not yet seen live: defensive) ──────────────

_REF_LABEL = re.compile(
    r"\b(?:ordernummer|ordernumret|ordernr|order\s*nr|order\s*number|order\s*no|"
    r"beställningsnummer|beställningsnr)\b\.?\s*(?:är\s*)?[:#]?\s*", re.I)
_REF_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{3,29}(?![A-Za-z0-9-])")
# The customer's OWN order number ("Ert ordernummer", prefilled with the reg) is never ours.
_CUSTOMER_WORD = re.compile(r"(?:\b(?:ert|er|kund|kundens|kundorder))\s*$", re.I)
_REF_PARAMS = ("OrderNo", "OrderNumber", "OrderID", "OrderId", "orderId", "DocumentNo")


def _plausible_ref(value: str, reg_nr: str | None) -> bool:
    """4-30 chars, at least 4 digits, and never the vehicle's registration number."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{3,29}", value or ""):
        return False
    if len(re.findall(r"\d", value)) < 4:
        return False
    norm = re.sub(r"[\s-]", "", value).upper()
    return not (reg_nr and norm == re.sub(r"[\s-]", "", str(reg_nr)).upper())


def page_markers(html: str, reg_nr: str | None) -> dict:
    """Signs that a page is still the cart or the (unsubmitted) checkout page."""
    root = parse_html(html)
    title_node = root.find("title")
    titles = [title_node.text() if title_node is not None else ""] + \
        [h1.text() for h1 in root.find_all("h1")]
    want = f"beställning {reg_nr}".casefold() if reg_nr else "beställning "
    return {
        "place_order": root.find("button", cls="checkout-page-place-order__button") is not None,
        "checkout_title": any(t.casefold().startswith(want) for t in titles),
        "cart_table": root.find("section", cls="cart-overview-article-table") is not None,
    }


def extract_order_reference(html: str, url: str, reg_nr: str | None) -> str | None:
    """An order number from a confirmation page's URL or text; None if none is trustworthy.

    Never from a page that is still the cart or checkout; never the customer's own order
    number ("Ert ordernummer", "Kundordernummer"); never equal to the vehicle reg.
    """
    if any(page_markers(html, reg_nr).values()):
        return None
    query = parse_qs(urlparse(url or "").query)
    for key in _REF_PARAMS:
        for value in query.get(key, []):
            if _plausible_ref(value, reg_nr):
                return value
    text = parse_html(html).text()
    for label in _REF_LABEL.finditer(text):
        if _CUSTOMER_WORD.search(text[:label.start()]):
            continue
        value = _REF_VALUE.match(text, label.end())
        if value and _plausible_ref(value.group(0), reg_nr):
            return value.group(0)
    return None


def order_tokens(html: str) -> set[str]:
    """Long digit runs on a page (order-number candidates for the order-overview fallback)."""
    return set(re.findall(r"(?<!\d)\d{6,}(?!\d)", parse_html(html).text()))


def is_login_page(html: str) -> bool:
    return parse_html(html).find(id="ShopLoginForm_Login") is not None


def current_plate(html: str) -> str | None:
    node = parse_html(html).find(cls="js-current-license-plate-number")
    return (node.text().strip().upper() or None) if node is not None else None
