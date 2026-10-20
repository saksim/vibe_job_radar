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
_HTML_ROW_DISPLAY = {"tr": "table-row", "tbody": "table-row-group",
                     "thead": "table-header-group", "tfoot": "table-footer-group"}
_HTML_TABLE_DISPLAY = {**_HTML_ROW_DISPLAY, "table": "table", "col": "table-column",
                       "colgroup": "table-column-group", "td": "table-cell",
                       "th": "table-cell", "caption": "table-caption"}
_INLINE_KEYWORDS = {
    "visibility": {"visible", "hidden", "collapse"},
    "position": {"static", "relative", "absolute", "fixed", "sticky"},
    "float": {"none", "left", "right", "inline-start", "inline-end"},
}
_DISPLAY_OUTSIDE = {"block", "inline", "run-in"}
_DISPLAY_INSIDE = {"flow", "flow-root", "table", "flex", "grid", "ruby"}
_DISPLAY_SINGLE = {
    "none", "contents", "inline-block", "inline-table", "inline-flex", "inline-grid",
    "table-row-group", "table-header-group", "table-footer-group", "table-row",
    "table-cell", "table-column-group", "table-column", "table-caption",
    "ruby-base", "ruby-text", "ruby-base-container", "ruby-text-container",
    "-webkit-box", "-webkit-inline-box", "-webkit-flex", "-webkit-inline-flex",
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


def _css_keywords(text: str) -> list[str]:
    """Tokenize the supported literal keywords, without reinterpreting escapes."""
    words, word, index = [], [], 0
    whitespace = " \t\r\n\f"
    letters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-"
    while index < len(text):
        char = text[index]
        if char in whitespace or char == "!":
            if word:
                words.append("".join(word).lower())
                word.clear()
            if char == "!":
                words.append("!")
            index += 1
            continue
        if char == "\\":
            index += 1
            # Outside strings a backslash followed by a newline is invalid.
            if index == len(text) or text[index] in "\r\n\f":
                return []
            start = index
            while index < len(text) and index - start < 6 and text[index] in "0123456789abcdefABCDEF":
                index += 1
            if index > start:
                codepoint = int(text[start:index], 16)
                char = chr(codepoint) if codepoint <= 0x10ffff else ""
                if index < len(text) and text[index] in whitespace:
                    if text[index:index + 2] == "\r\n":
                        index += 1
                    index += 1
            else:
                char = text[index]
                index += 1
        else:
            index += 1
        # All supported keywords use ASCII letters/hyphens. An escaped space,
        # bang or punctuation is part of an identifier, never a token boundary.
        if char not in letters or not char:
            return []
        word.append(char)
    if word:
        words.append("".join(word).lower())
    return words


def _inline_values(style: str) -> dict[str, str]:
    # Bounded literal inline cascade only; external stylesheets and custom
    # property substitutions require a rendered observation.
    values = {}
    for declaration in _style_declarations(style):
        name, separator, value = declaration.partition(":")
        names, tokens = _css_keywords(name), _css_keywords(value)
        if not separator or len(names) != 1 or not tokens:
            continue
        name = names[0]
        if name not in {"display", "visibility", "position", "float", "all"}:
            continue
        important = tokens[-2:] == ["!", "important"]
        value = " ".join(tokens[:-2] if important else tokens)
        valid = (_valid_display(value) if name == "display" else
                 value in _CSS_WIDE | _INLINE_KEYWORDS.get(name, set()))
        if not valid:
            continue
        for key in (("display", "visibility", "position", "float") if name == "all" else (name,)):
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
    _visibility: str = field(init=False, repr=False, compare=False)
    _display: str = field(init=False, repr=False, compare=False)
    _position: str = field(init=False, repr=False, compare=False)
    _float: str = field(init=False, repr=False, compare=False)
    _box_parent_display: str = field(init=False, repr=False, compare=False)

    def __post_init__(self, parent):
        # Document builds immutable snapshots, parent before child. Compute
        # inheritance once without retaining parent cycles or reparsing styles
        # on every traversal. A child's declaration overrides inherited values
        # even when the parent's declaration was !important.
        values = _inline_values(self.attrs.get("style") or "")
        def noninherited(name, initial, default):
            value = values.get(name)
            if value == "inherit":
                return getattr(parent, "_" + name, initial)
            if value in {"initial", "unset"}:
                return initial
            return default if value in {None, "revert", "revert-layer"} else value

        self._display = noninherited("display", "inline", _HTML_TABLE_DISPLAY.get(self.tag, "inline"))
        # Modern prefixed flex aliases share item semantics. Legacy box is
        # a distinct Chromium layout mode; do not pretend it is modern flex.
        self._display = {"-webkit-flex": "flex", "-webkit-inline-flex": "inline-flex"}.get(
            self._display, self._display)
        self._position = noninherited("position", "static", "static")
        self._float = noninherited("float", "none", "none")
        visibility = values.get("visibility")
        self._visibility = ("visible" if visibility == "initial" else visibility
                            if visibility in {"visible", "hidden", "collapse"} else
                            getattr(parent, "_visibility", "visible"))
        # display:contents has no item box, so children use its box parent.
        self._box_parent_display = (
            parent._box_parent_display if parent is not None and parent._display == "contents"
            else getattr(parent, "_display", ""))
        in_flow = self._position not in {"absolute", "fixed"}
        flex_item = (in_flow and self._display != "contents"
                     and bool({"flex", "inline-flex"} & set(self._box_parent_display.split())))
        # Known row roles are blockified out of flow or as flex items. Store
        # that result so an explicit display:inherit child gets the same role.
        if self._display in set(_HTML_TABLE_DISPLAY.values()) - {"table"} and (
                not in_flow or self._float != "none" or flex_item):
            self._display = "block"
        row = self._display in _HTML_ROW_DISPLAY.values()
        # Collapsed rows/groups and known inline flex items remove their boxes:
        # descendant visibility cannot undo that. Ordinary hidden/collapse
        # content remains inherited and overrideable. External layout is unknown.
        collapsed_box = self._visibility == "collapse" and (row or flex_item)
        # Ordinary hidden is a UA display rule and can be overridden by an
        # explicit author display. until-found uses content-visibility instead.
        hidden_attribute = ("hidden" in self.attrs and (
            values.get("display") in {None, "revert", "revert-layer"}
            or (self.attrs.get("hidden") or "").lower() == "until-found"))
        self._subtree_hidden = bool(
            (parent is not None and parent.blocks_subtree())
            or self.tag in {"script", "style", "template", "noscript", "head"}
            or hidden_attribute or self._display == "none" or collapsed_box)

    def blocks_subtree(self) -> bool:
        return self._subtree_hidden

    def hidden(self) -> bool:
        """Own visibility, including ancestors; aria-hidden is not visual hiding."""
        return self._subtree_hidden or self._visibility in {"hidden", "collapse"}

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


_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

_P_END_BLOCKS = {
    "address", "article", "aside", "blockquote", "details", "dialog", "div", "dl",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hgroup", "hr", "main", "menu", "nav", "ol", "p",
    "pre", "search", "section", "table", "ul",
}
_TABLE_PARTS = {"caption", "colgroup", "thead", "tbody", "tfoot", "tr"}
_VOID_TAGS = {"br", "hr", "meta", "link", "img", "input", "area", "base", "col",
              "embed", "source", "track", "wbr", "param"}



# HTML's generic end-tag algorithm stops on special elements before finding a
# matching ordinary element. Explicit block/table/formatting algorithms keep
# their own scope rules instead of inheriting this generic barrier set.
_SPECIAL_TAGS = set("""
address applet area article aside base basefont bgsound blockquote body br button
caption center col colgroup dd details dir div dl dt embed fieldset figcaption
figure footer form frame frameset h1 h2 h3 h4 h5 h6 head header hgroup hr html
iframe img input keygen li link listing main marquee menu meta nav noembed noframes
noscript object ol p param plaintext pre script search section select source style
summary table tbody td template textarea tfoot th thead title tr track ul wbr xmp
""".split())
_SPECIFIC_END_TAGS = ((_P_END_BLOCKS - {"dialog", "hr"}) | _TABLE_PARTS | {"summary"} | {
    "applet", "body", "button", "center", "dd", "dir", "dt", "form", "html", "li",
    "listing", "marquee", "object", "select", "option", "optgroup", "td", "th",
    "template", "noscript", "a", "b", "big", "code", "em", "font", "i", "nobr",
    "s", "small", "strike", "strong", "tt", "u", "br"})


def _first_attributes(attrs):
    """HTML tokenization discards later attributes with the same name."""
    result = {}
    for name, value in attrs:
        result.setdefault(name, value)
    return result


class Document(HTMLParser):
    def __init__(self, markup: str):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]
        self._table_locations = {}
        self.feed(markup)
        self.close()
        open_nodes = {id(node) for node in self.stack}
        self._table_locations = {key: value for key, value in self._table_locations.items()
                                 if key in open_nodes}
        self._collapse_columns()

    @staticmethod
    def _span(node, name, maximum=1000):
        match = re.match(r"[ \t\r\n\f]*\+?([0-9]+)", node.attrs.get(name) or "")
        # Saturate before int conversion, including arbitrarily long attributes.
        digits = match[1].lstrip("0") if match else ""
        value = maximum if len(digits) > 5 else int(digits or "0")
        return min(value, maximum) if name == "rowspan" and match else max(1, min(value, maximum))

    def _collapse_columns(self):
        # Column tracks are siblings of rows, not ancestors of their cells.
        # Keep spans as intervals so large attributes do not expand into arrays.
        for table in self.root.walk():
            if table._display not in {"table", "inline-table", "inline table", "block table"}:
                continue
            columns, cursor = [], 0
            for group in table.children:
                if not isinstance(group, Node):
                    continue
                if group._display == "table-column":
                    parts = [group]
                    collapsed_group = False
                elif group._display == "table-column-group" and not group.blocks_subtree():
                    parts = [c for c in group.children if isinstance(c, Node)
                             and c._display == "table-column"]
                    parts = parts or [group]
                    collapsed_group = group._visibility == "collapse"
                else:
                    continue
                for column in parts:
                    width = self._span(column, "span")
                    if collapsed_group or column._visibility == "collapse":
                        if columns and columns[-1][1] == cursor:
                            columns[-1] = (columns[-1][0], cursor + width)
                        else:
                            columns.append((cursor, cursor + width))
                    cursor += width
            if not columns:
                continue
            groups = []
            for group in table.children:
                if not isinstance(group, Node):
                    continue
                if group._display == "table-row":
                    if groups and groups[-1][0] is table:
                        groups[-1][1].append(group)
                    else:
                        groups.append((table, [group]))
                elif group._display in set(_HTML_ROW_DISPLAY.values()) - {"table-row"}:
                    groups.append((group, [c for c in group.children if isinstance(c, Node)
                                           and c._display == "table-row"]))
            for _, rows in groups:
                occupied = []
                for index, row in enumerate(rows):
                    occupied = [slot for slot in occupied if slot[2] > index]
                    cursor = 0
                    for cell in row.children:
                        if not isinstance(cell, Node) or cell._display != "table-cell":
                            continue
                        while True:
                            stops = [end for start, end, until in occupied if start <= cursor < end]
                            if not stops:
                                break
                            cursor = max(stops)
                        end = cursor + self._span(cell, "colspan")
                        if any(start <= cursor and end <= stop for start, stop in columns):
                            for descendant in cell.walk():
                                descendant._subtree_hidden = True
                        height = self._span(cell, "rowspan", 65534)
                        until = len(rows) if height == 0 else min(len(rows), index + height)
                        if until > index + 1:
                            occupied.append((cursor, end, until))
                        cursor = end

    def _insert(self, parent, item, position):
        if position is None:
            position = len(parent.children)
            parent.children.append(item)
        else:
            parent.children.insert(position, item)
            # Only open tables can be used again; stack depth is bounded.
            # Closed table entries need no index maintenance.
            for node in self.stack:
                location = self._table_locations.get(id(node))
                if location and location[0] is parent and location[1] >= position:
                    location[1] += 1
        if isinstance(item, Node) and item.tag == "table":
            self._table_locations[id(item)] = [parent, position]

    def _insertion_location(self, tag="", *, whitespace=False):
        current = self.stack[-1]
        if current.tag == "colgroup" and not whitespace and tag not in {"col", "template", "script"}:
            self.stack.pop()
            current = self.stack[-1]
        structural = _TABLE_PARTS | {"table", "td", "th", "col", "script", "style", "template", "form"}
        if (current.tag not in {"table", "tbody", "thead", "tfoot", "tr"}
                or whitespace or tag in structural):
            return current, None
        # In-table non-table tokens are inserted before the last table. They
        # stay on the open-element stack, so their own children nest normally.
        for index in range(len(self.stack) - 1, 0, -1):
            table = self.stack[index]
            if table.tag in {"template", "noscript"}:
                return current, None
            if table.tag == "table":
                location = self._table_locations.get(id(table))
                if location is not None:
                    return location[0], location[1]
                return self.stack[index - 1], None
        return current, None

    def _close_optional_head(self, tag):
        # HTMLParser tokenizes markup but does not apply the browser's implied
        # head end tag. Body content must not remain under inert metadata.
        head_content = {"base", "basefont", "bgsound", "link", "meta", "noframes",
                        "script", "style", "template", "title", "noscript"}
        if self.stack[-1].tag == "head" and tag not in head_content:
            self.stack.pop()

    def _close_open(self, tags, boundaries):
        # Never close an outer element across a template or a nested structural
        # container. Its descendants must keep the correct visibility ancestry.
        for index in range(len(self.stack) - 1, 0, -1):
            current = self.stack[index].tag
            if current in tags:
                del self.stack[index:]
                return
            if current in boundaries or current in {"template", "noscript", "html"}:
                return

    def _close_optional_elements(self, tag):
        self._close_optional_head(tag)
        if tag == "button":
            self._close_open({"button"}, {"table", "td", "th", "caption",
                                         "applet", "marquee", "object", "select"})
        if tag in _P_END_BLOCKS:
            self._close_open({"p"}, {"button", "table", "td", "th", "caption",
                                    "applet", "marquee", "object"})
        if tag in _HEADINGS and self.stack[-1].tag in _HEADINGS:
            self.stack.pop()
        if tag == "li":
            self._close_open({"li"}, {"ul", "ol", "menu", "table", "td", "th"})
        elif tag in {"dt", "dd"}:
            self._close_open({"dt", "dd"}, {"dl", "table", "td", "th"})
        elif tag in {"rt", "rp"}:
            self._close_open({"rt", "rp"}, {"ruby"})
        if tag in {"option", "optgroup", "hr"}:
            self._close_open({"option"}, {"select", "datalist", "optgroup"})
            if tag in {"optgroup", "hr"}:
                self._close_open({"optgroup"}, {"select", "datalist"})
        if tag in _TABLE_PARTS | {"td", "th"}:
            self._close_open({"colgroup", "caption"}, {"table"})
        if tag in {"thead", "tbody", "tfoot"}:
            self._close_open({"thead", "tbody", "tfoot"}, {"table"})
        elif tag == "tr":
            self._close_open({"tr"}, {"table", "thead", "tbody", "tfoot"})
        elif tag in {"td", "th"}:
            self._close_open({"td", "th"}, {"tr", "table"})
        # These omitted start tags also participate in inherited visibility.
        if self.stack[-1].tag == "table" and tag in {"tr", "col", "td", "th"}:
            parent = self.stack[-1]
            group = Node("colgroup" if tag == "col" else "tbody", parent=parent)
            parent.children.append(group)
            self.stack.append(group)
        if tag in {"td", "th"} and self.stack[-1].tag in {"tbody", "thead", "tfoot"}:
            parent = self.stack[-1]
            row = Node("tr", parent=parent)
            parent.children.append(row)
            self.stack.append(row)

    def handle_starttag(self, tag, attrs):
        namespace = self._foreign_namespace(tag)
        self._close_optional_elements(tag)
        table_form = tag == "form" and self.stack[-1].tag in {
            "table", "tbody", "thead", "tfoot", "tr"}
        parent, position = self._insertion_location(tag)
        node = Node(tag, _first_attributes(attrs), parent=parent)
        # SVG descriptive metadata is never rendered, even when its children
        # are parsed in the HTML namespace or an author supplies display.
        if namespace == "svg" and tag in {"desc", "title"}:
            node._display = "none"
            node._subtree_hidden = True
        self._insert(parent, node, position)
        if tag not in _VOID_TAGS and not table_form:
            if len(self.stack) > 200:
                raise ParseError("HTML nesting too deep")
            self.stack.append(node)

    def _foreign_namespace(self, tag):
        # Identify the bounded SVG/MathML scope used by empty-node handling.
        # HTML integration points resume HTML start-tag semantics.
        if tag in {"svg", "math"}:
            return tag
        for index in range(len(self.stack) - 1, 0, -1):
            namespace = self.stack[index].tag
            if namespace not in {"svg", "math"}:
                continue
            for ancestor in self.stack[index + 1:]:
                if namespace == "svg" and ancestor.tag in {"foreignobject", "desc", "title"}:
                    return ""
                if namespace == "math":
                    if (ancestor.tag == "annotation-xml"
                            and (ancestor.attrs.get("encoding") or "").lower()
                            in {"text/html", "application/xhtml+xml"}):
                        return ""
                    if ancestor.tag in {"mi", "mo", "mn", "ms", "mtext"}:
                        return namespace if (ancestor is self.stack[-1]
                                             and tag in {"mglyph", "malignmark"}) else ""
            return namespace
        return ""

    def handle_startendtag(self, tag, attrs):
        if not self._foreign_namespace(tag):
            # A trailing slash never closes a non-void HTML element. Reuse
            # normal insertion, inherited visibility and the nesting bound.
            self.handle_starttag(tag, attrs)
            # HTMLParser normally enters raw-text mode only on its ordinary
            # start-tag path, so an ignored slash needs the same transition.
            if tag in self.CDATA_CONTENT_ELEMENTS:
                self.set_cdata_mode(tag)
            return
        self._close_optional_elements(tag)
        parent, position = self._insertion_location(tag)
        node = Node(tag, _first_attributes(attrs), parent=parent)
        self._insert(parent, node, position)

    def handle_endtag(self, tag):
        # A tokenizer's matching end tag is not enough: an outer element cannot
        # be closed through a template, table cell, or another scope boundary.
        if tag in {"body", "html"}:
            # Subsequent body tokens still belong to the existing body/html.
            return
        barriers = {"template", "noscript", "html", "table", "td", "th", "caption",
                    "applet", "marquee", "object", "select"}
        if tag in {"template", "noscript"}:
            barriers = {"template", "noscript", "html"}
        elif tag in _TABLE_PARTS | {"td", "th", "table"}:
            barriers = {"template", "noscript", "html", "table"}
        elif tag == "p":
            barriers |= {"button"}
        elif tag == "li":
            barriers |= {"ol", "ul"}
        elif tag not in _SPECIFIC_END_TAGS:
            barriers |= _SPECIAL_TAGS
        if tag in _HEADINGS:
            self._close_open(_HEADINGS, barriers)
            return
        for i in range(len(self.stack) - 1, 0, -1):
            current = self.stack[i].tag
            if current == tag:
                del self.stack[i:]
                break
            if current in barriers:
                break

    def handle_data(self, data):
        if self.stack[-1].tag == "head" and data.strip():
            self.stack.pop()
        parent, position = self._insertion_location(whitespace=not data.strip())
        self._insert(parent, data, position)


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
