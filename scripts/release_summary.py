"""Validate maintainer-written release outcomes without inventing product claims."""
from __future__ import annotations

import re

SECTIONS = ("解决了什么", "提高了什么", "验证结果", "剩余问题")


def validate_summary(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > 65536:
        raise ValueError("Release summary must be UTF-8 text of at most 64 KiB")
    text = text.replace("\r\n", "\n").strip()
    headings = list(re.finditer(r"^##[ \t]+([^\r\n]+)[ \t]*$", text, re.MULTILINE))
    sections = {}
    for index, match in enumerate(headings):
        title = match.group(1).strip()
        if title in SECTIONS:
            if title in sections:
                raise ValueError("Duplicate release summary section: " + title)
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            content = text[match.end():end].strip()
            # Reject empty headings and unfilled template markers.
            meaningful = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL).strip()
            if not meaningful or meaningful in {"-", "*", "TODO", "TBD", "待填写"}:
                raise ValueError("Fill release summary section: " + title)
            sections[title] = meaningful
    missing = set(SECTIONS) - sections.keys()
    if missing:
        raise ValueError("Missing release summary sections: " + ", ".join(sorted(missing)))
    if not re.search(r"(?:^|\s|\()#\d+\b|/issues/\d+\b|/pull/\d+\b", text):
        raise ValueError("Release summary must link a related Issue or PR")
    return text + "\n"
