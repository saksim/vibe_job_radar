from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urljoin, urlsplit
from .utils import canonical_url
from .url_safety import credential_query_key


class ParseError(ValueError):
    pass


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self, include_script: bool = False) -> str:
        if not include_script and self.tag in {"script", "style", "nav", "footer", "noscript"}:
            return ""
        s = "".join(c.text(include_script) if isinstance(c, Node) else c for c in self.children)
        return s + ("\n" if self.tag in {"p", "div", "li", "br", "h1", "h2", "h3", "section"} else "")


class Document(HTMLParser):
    def __init__(self, markup: str):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]
        self.feed(markup)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in {"br", "hr", "meta", "link", "img", "input", "area", "base", "embed", "source", "wbr", "param"}:
            if len(self.stack) > 200:
                raise ParseError("HTML nesting too deep")
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, dict(attrs)))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def plain_text(markup: str) -> str:
    text = Document(markup).root.text()
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


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
        if any((ord(c) < 32 and c not in "\r\n\t") or ord(c) == 127 for c in value):
            return None
        return " ".join(value.split())

    locations = []
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
        locations.append(location)
    result = "; ".join(sorted(set(locations)))
    return result if len(result) <= 2048 else ""


def parse_job_html(markup: str, *, source_url: str = "") -> dict:
    """Return only an isolated JD, never the entire body or search recommendations."""
    if source_url and urlsplit(source_url).path in {"", "/"}:
        raise ParseError("homepage is not a job-detail URL")
    if re.search(r"请完成.{0,12}验证|滑动.{0,8}验证|安全验证|captcha|登录后.{0,8}(?:查看|浏览)", plain_text(markup), re.I):
        raise ParseError("login/challenge page")
    doc = Document(markup)
    nodes = list(doc.root.walk())
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
    # Dedicated containers only. No full-body fallback, and no brittle undocumented API.
    for selector in ("job-sec-text", "job-description", "job-detail-body", "job-detail-content", "job_msg", "bmsg", "job-detail"):
        matches = [n for n in nodes if selector in (n.attrs.get("class") or "").split()]
        if len(matches) > 1:
            raise ParseError(f"ambiguous container: {selector}")
        if len(matches) == 1:
            title_nodes = [n for n in nodes if n.tag == "h1"]
            if len(title_nodes) != 1:
                raise ParseError("exactly one job title h1 required for DOM extraction")
            text = plain_text(matches[0].text())
            if len(text) < 20:
                raise ParseError("job-description container too short")
            return {"title": title_nodes[0].text().strip(), "text": text,
                    "parser": "dom:" + selector}
    raise ParseError("no isolated JobPosting/container found; supply authorized JD text")
