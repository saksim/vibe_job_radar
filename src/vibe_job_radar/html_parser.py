from __future__ import annotations

import json
import re
from dataclasses import InitVar, dataclass, field
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urljoin, urlsplit
from .utils import canonical_url, compact
from .url_safety import credential_query_key


class ParseError(ValueError):
    pass


def _style_declarations(style: str):
    """Split declarations without treating strings/functions/comments as CSS."""
    field, quote, depth, index = [], "", 0, 0
    while index < len(style):
        char = style[index]
        if char == "\\" and index + 1 < len(style):
            field.extend(style[index:index + 2])
            index += 2
            continue
        if quote:
            field.append(char)
            if char == quote:
                quote = ""
        elif style.startswith("/*", index):
            end = style.find("*/", index + 2)
            field.append(" ")
            index = len(style) if end < 0 else end + 2
            continue
        elif char in "\"'":
            quote = char
            field.append(char)
        elif char in "([{":
            depth += 1
            field.append(char)
        elif char in ")]}":
            depth = max(0, depth - 1)
            field.append(char)
        elif char == ";" and not depth:
            yield "".join(field)
            field.clear()
        else:
            field.append(char)
        index += 1
    yield "".join(field)


_CSS_WIDE = {"initial", "inherit", "unset", "revert", "revert-layer"}
_DISPLAY_OUTSIDE = {"block", "inline", "run-in"}
_DISPLAY_INSIDE = {"flow", "flow-root", "table", "flex", "grid", "ruby"}
_DISPLAY_SINGLE = {
    "none", "contents", "inline-block", "inline-table", "inline-flex", "inline-grid",
    "table-row-group", "table-header-group", "table-footer-group", "table-row",
    "table-cell", "table-column-group", "table-column", "table-caption",
    "ruby-base", "ruby-text", "ruby-base-container", "ruby-text-container",
}


def _valid_display(value: str) -> bool:
    # CSS Display 3 literal grammar: invalid declarations must not replace a
    # previous valid one. Multi-keyword display values are order-independent.
    if value in _CSS_WIDE or value in _DISPLAY_SINGLE:
        return True
    words = value.split()
    tokens = set(words)
    if len(words) != len(tokens):
        return False
    inside = {"flow", "flow-root"} if "list-item" in tokens else _DISPLAY_INSIDE
    allowed = _DISPLAY_OUTSIDE | inside | {"list-item"}
    return (bool(tokens) and tokens <= allowed
            and len(tokens & _DISPLAY_OUTSIDE) <= 1 and len(tokens & inside) <= 1)


def _inline_values(style: str) -> dict[str, str]:
    # Bounded literal inline cascade only; external stylesheets and custom
    # property substitutions require a rendered observation.
    values = {}
    for declaration in _style_declarations(style):
        name, separator, value = declaration.partition(":")
        name, value = name.strip().lower(), value.strip().lower()
        if not separator or name not in {"display", "visibility", "all"} or not value:
            continue
        annotation = re.search(r"!\s*important\s*$", value)
        important = annotation is not None
        if annotation:
            value = value[:annotation.start()].strip()
        valid = (_valid_display(value) if name == "display" else
                 value in _CSS_WIDE | ({"visible", "hidden", "collapse"}
                                      if name == "visibility" else set()))
        if not valid:
            continue
        for key in (("display", "visibility") if name == "all" else (name,)):
            if key not in values or important >= values[key][1]:
                values[key] = value, important
    return {key: value for key, (value, _) in values.items()}


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list = field(default_factory=list)
    parent: InitVar[Node | None] = None
    _subtree_hidden: bool = field(init=False, repr=False, compare=False)
    _visibility_hidden: bool = field(init=False, repr=False, compare=False)

    def __post_init__(self, parent):
        # Document builds immutable snapshots, parent before child. Compute
        # inheritance once without retaining parent cycles or reparsing styles
        # on every traversal. A child's declaration overrides inherited values
        # even when the parent's declaration was !important.
        values = _inline_values(self.attrs.get("style") or "")
        self._subtree_hidden = bool(
            (parent is not None and parent.blocks_subtree())
            or self.tag in {"script", "style", "template", "noscript", "head"}
            or "hidden" in self.attrs or values.get("display") == "none")
        visibility = values.get("visibility")
        if visibility in {"visible", "initial"}:
            self._visibility_hidden = False
        elif visibility in {"hidden", "collapse"}:
            self._visibility_hidden = True
        else:
            self._visibility_hidden = bool(parent is not None and parent._visibility_hidden)

    def blocks_subtree(self) -> bool:
        return self._subtree_hidden

    def hidden(self) -> bool:
        """Own visibility, including ancestors; aria-hidden is not visual hiding."""
        return self._subtree_hidden or self._visibility_hidden

    def walk(self, *, visible_only: bool = False, exclude_tags: tuple[str, ...] = ()):
        if self.tag in exclude_tags or (visible_only and self.blocks_subtree()):
            return
        # A semantic container can have hidden own text and visible children.
        if not visible_only or not self.hidden() or self.text(visible_only=True).strip():
            yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk(visible_only=visible_only, exclude_tags=exclude_tags)

    def text(self, include_script: bool = False, *, visible_only: bool = False) -> str:
        if visible_only and self.blocks_subtree():
            return ""
        if not include_script and self.tag in {"script", "style", "nav", "footer", "noscript"}:
            return ""
        own_visible = not visible_only or not self.hidden()
        s = "".join(c.text(include_script, visible_only=visible_only)
                    if isinstance(c, Node) else (c if own_visible else "") for c in self.children)
        return s + ("\n" if (s or own_visible) and self.tag in {"p", "div", "li", "br", "h1", "h2", "h3", "section"} else "")


class Document(HTMLParser):
    def __init__(self, markup: str):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]
        self.feed(markup)
        self.close()

    def _close_optional_head(self, tag):
        # HTMLParser tokenizes markup but does not apply the browser's implied
        # head end tag. Body content must not remain under inert metadata.
        head_content = {"base", "basefont", "bgsound", "link", "meta", "noframes",
                        "script", "style", "template", "title", "noscript"}
        if self.stack[-1].tag == "head" and tag not in head_content:
            self.stack.pop()

    def handle_starttag(self, tag, attrs):
        self._close_optional_head(tag)
        node = Node(tag, dict(attrs), parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in {"br", "hr", "meta", "link", "img", "input", "area", "base", "embed", "source", "wbr", "param"}:
            if len(self.stack) > 200:
                raise ParseError("HTML nesting too deep")
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self._close_optional_head(tag)
        self.stack[-1].children.append(Node(tag, dict(attrs), parent=self.stack[-1]))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self.stack[-1].tag == "head" and data.strip():
            self.stack.pop()
        self.stack[-1].children.append(data)


def _normalize_text(text: str) -> str:
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def plain_text(markup: str) -> str:
    return _normalize_text(Document(markup).root.text())


def jobpostings(value, depth=0):
    if depth > 60:
        raise ParseError("JSON-LD nesting too deep")
    if isinstance(value, dict):
        typ = value.get("@type", "")
        if "JobPosting" in (typ if isinstance(typ, list) else [typ]):
            yield value
        else:
            for child in value.values():
                yield from jobpostings(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from jobpostings(child, depth + 1)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ParseError("duplicate structured-data field")
        value[key] = item
    return value


def _posting_identity(posting: dict, source_url: str) -> list[str]:
    """Read identity only. Never follow metadata URLs or equate different paths."""
    refs = []
    for key in ("url", "mainEntityOfPage"):
        value = posting.get(key)
        if value is None:
            continue
        if isinstance(value, dict):
            # Conflicting declarations remain visible to the matching rule.
            values = [value[k] for k in ("@id", "url") if k in value]
        else:
            values = [value]
        if not values:
            raise ParseError("unusable structured identity")
        refs.extend(values)
    if not refs and isinstance(posting.get("@id"), str):
        value = posting["@id"]
        if value.startswith(("https://", "http://", "/", "#")):
            refs.append(value)
    normalized = []
    for value in refs:
        if (not isinstance(value, str) or not value.strip() or len(value) > 2048
                or "\\" in value or any(ord(c) < 32 for c in value)):
            raise ParseError("invalid structured identity")
        try:
            target = urljoin(source_url, value)
            if any(credential_query_key(k) for k, _ in parse_qsl(urlsplit(target).query)):
                raise ValueError("credential identity")
            normalized.append(canonical_url(target))
        except ValueError as exc:
            raise ParseError("invalid structured identity") from exc
    return normalized


def _select_posting(postings: list[dict], source_url: str) -> dict:
    unique = list({json.dumps(x, sort_keys=True, ensure_ascii=False): x for x in postings}.values())
    if not source_url:
        if len(unique) != 1:
            raise ParseError("multiple JobPosting objects: no source identity")
        return unique[0]
    target = canonical_url(source_url)
    identities = [(p, _posting_identity(p, source_url)) for p in unique]
    matches = [p for p, refs in identities if refs and all(ref == target for ref in refs)]
    if len(matches) == 1:
        return matches[0]
    if len(unique) == 1 and not identities[0][1]:
        return unique[0]  # Existing single-posting documents without identity.
    raise ParseError("ambiguous or mismatching JobPosting identity")


def _posting_location(posting: dict) -> str:
    """Read bounded addresses from the already selected JobPosting only.

    Unknown or partly unsupported multi-place metadata stays unknown. Employer
    headquarters and applicant eligibility are not a job's physical location.
    """
    places = posting.get("jobLocation")
    places = places if isinstance(places, list) else [places]
    if not 1 <= len(places) <= 16:
        return ""

    def text(value):
        if value is None:
            return ""
        if not isinstance(value, str) or len(value) > 512:
            return None
        value = unescape(value)
        if any((ord(c) < 32 and c not in "\r\n\t") or ord(c) == 127
               or 0xD800 <= ord(c) <= 0xDFFF for c in value):
            return None
        return " ".join(value.split())

    locations = {}
    for place in places:
        if not isinstance(place, dict):
            return ""
        address = place.get("address")
        if isinstance(address, str):
            location = text(address)
        elif isinstance(address, dict):
            parts = []
            for key in ("addressCountry", "addressRegion", "addressLocality",
                        "streetAddress", "postalCode"):
                value = address.get(key)
                if key in {"addressCountry", "addressRegion"} and isinstance(value, dict):
                    value = value.get("name")
                value = text(value)
                if value is None:
                    return ""
                if value and value not in parts:
                    parts.append(value)
            location = " / ".join(parts)
        else:
            return ""
        if not location:
            return ""
        # Match the fingerprint's normalization before ordering the place set.
        # Keep one deterministic original spelling for the displayed address.
        key = compact(location)
        locations[key] = min(location, locations.get(key, location))
    result = "; ".join(locations[key] for key in sorted(locations))
    return result if len(result) <= 2048 else ""


_CHALLENGE = re.compile(
    r"请完成.{0,12}验证|滑动.{0,8}验证|安全验证|captcha|登录后.{0,8}(?:查看|浏览)", re.I)


def parse_job_html(markup: str, *, source_url: str = "", visible_text: str | None = None) -> dict:
    """Return only an isolated JD, never the entire body or search recommendations."""
    if source_url and urlsplit(source_url).path in {"", "/"}:
        raise ParseError("homepage is not a job-detail URL")
    if visible_text is not None and (not isinstance(visible_text, str) or len(visible_text) > 5_000_000):
        raise ParseError("invalid rendered observation")
    doc = Document(markup)
    observed = visible_text if visible_text is not None else doc.root.text(visible_only=True)
    if _CHALLENGE.search(observed):
        raise ParseError("login/challenge page")
    nodes = list(doc.root.walk(exclude_tags=("template", "noscript")))
    structured = []
    for node in nodes:
        if node.tag == "script" and (node.attrs.get("type") or "").lower() == "application/ld+json":
            try:
                structured.extend(jobpostings(json.loads(node.text(include_script=True), object_pairs_hook=_unique_object)))
            except (json.JSONDecodeError, TypeError):
                continue
    if structured:
        posting = _select_posting(structured, source_url)
        description, title = posting.get("description"), posting.get("title")
        if not isinstance(description, str) or not isinstance(title, str):
            raise ParseError("JobPosting title and description must be text")
        text, title = plain_text(description), unescape(title).strip()
        if len(text) < 20 or not title:
            raise ParseError("incomplete JobPosting description/title")
        org = posting.get("hiringOrganization") or {}
        company = org.get("name", "") if isinstance(org, dict) else ""
        return {"title": title, "text": text, "company": str(company), "parser": "json_ld_jobposting",
                "published_at": str(posting.get("datePosted") or ""), "location": _posting_location(posting)}
    # Structured metadata above may live in <head>; DOM candidates must not.
    nodes = list(doc.root.walk(visible_only=True))
    # Dedicated containers only. No full-body fallback, and no brittle undocumented API.
    for selector in ("job-sec-text", "job-description", "job-detail-body", "job-detail-content", "job_msg", "bmsg", "job-detail"):
        matches = [n for n in nodes if selector in (n.attrs.get("class") or "").split()]
        if len(matches) > 1:
            raise ParseError(f"ambiguous container: {selector}")
        if len(matches) == 1:
            title_nodes = [n for n in nodes if n.tag == "h1"]
            if len(title_nodes) != 1:
                raise ParseError("exactly one job title h1 required for DOM extraction")
            # Node.text is already HTML-decoded. Parsing it again destroys
            # literal <T>/<script> examples and decodes escaped entities twice.
            text = _normalize_text(matches[0].text(visible_only=True))
            title = _normalize_text(title_nodes[0].text(visible_only=True))
            if len(text) < 20 or not title:
                raise ParseError("incomplete job-description container/title")
            if _CHALLENGE.search(text):
                raise ParseError("login/challenge job-description container")
            return {"title": title, "text": text,
                    "parser": "dom:" + selector}
    raise ParseError("no isolated JobPosting/container found; supply authorized JD text")
