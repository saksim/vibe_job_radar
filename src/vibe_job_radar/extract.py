from __future__ import annotations

import re
from dataclasses import dataclass
from .models import JobRecord, Requirement
from .utils import digest


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    text: str
    line: int
    section: str
    vibe_section: bool

    @property
    def matching_text(self) -> str:
        # Only spans admitted by _soft_wrap_scan can contain line breaks.
        # Rejoin CJK words, retain Latin word spacing; never rewrite the quote.
        text = re.sub(r'(?<=[\u3400-\u9fff])[ \t]*\r?\n[ \t]*(?=[\u3400-\u9fff])', '', self.text)
        return re.sub(r'[ \t]*\r?\n[ \t]*', ' ', text)


_HEADINGS = (
    ('responsibilities', r'岗位职责|工作职责|工作职能|职位描述|responsibilities|(?:job|role)\s+description|what\s+you(?:\s+will|[\'’]ll)\s+do|about\s+(?:the|this)\s+role'),
    ('company', r'公司介绍|关于我们|about\s+(?:us|the\s+company|[a-z][a-z0-9 &.-]{0,40})|who\s+we\s+are|company(?:\s+(?:overview|description))?'),
    ('benefits', r'福利待遇|薪酬福利|benefits(?:\s+and\s+perks)?|perks(?:\s+and\s+benefits)?|compensation(?:\s+and\s+benefits)?|what\s+we\s+offer'),
    ('preferred', r'加分项|优先条件|nice\s+to\s+have|(?:preferred|desired|bonus)\s+(?:qualifications|skills)|bonus\s+points'),
    ('requirements', r'任职要求|任职资格|岗位要求|基本要求|招聘要求|(?:minimum\s+|basic\s+|required\s+)?qualifications|(?:job\s+)?requirements|required\s+skills|what\s+you\s+bring|you\s+(?:may\s+be\s+)?(?:a\s+)?(?:good\s+)?fit\s+if'),
    ('ai', r'AI\s*编程要求|Vibe\s*Coding要求|(?:AI[ -]coding|vibe\s+coding)\s+(?:requirements|skills)'),
)


def _heading_kind(text: str) -> str:
    heading = re.sub(r'^#{1,4}\s+', '', text).rstrip('：:。!?？ ')
    if heading.startswith('【') and heading.endswith('】'):
        heading = heading[1:-1].strip().rstrip('：: ')
    if len(heading) > 80:
        return ''
    if re.fullmatch(r'about\s+you', heading, re.I):
        return 'requirements'
    return next((kind for kind, pattern in _HEADINGS if re.fullmatch(pattern, heading, re.I)), '')


_NUMBERED_PREFIX = re.compile(r'(?:[（(]\d{1,2}[）)]|\d{1,2}[、.)）])(?!\d)[ \t]*')
_NEW_OBLIGATION = re.compile(
    r'(?:(?:但是|但|然而|不过|同时|并且|并|且)[ \t]*){0,2}'
    r'(?:必须|需要|应|须|需|禁止|不得|严禁|不能|不允许|不要求|不必|不强制|无需|不需要|'
    r'不接受|不依赖|要求|熟练|精通|掌握|熟悉|具备|能够|负责|使用|推动|参与|'
    r'建立|构建|维护|设计|完成|开发|公司|关于|福利|薪酬)')
_NEGATION_BREAKS = tuple((word[:i], word[i:])
    for word in ('不要求', '不需要', '不必', '不强制', '不允许', '不能', '不得', '禁止', '严禁', '无需')
    for i in range(1, len(word)))


def _soft_wrap_scan(text: str) -> str:
    """Same-length scan view for bounded Chinese numbered-item continuations.

    An explicit new obligation is not evidence of a layout wrap. Unnumbered,
    English-only, oversized or uncertain boundaries retain their original LF.
    """
    lines = list(re.finditer(r'[^\n]*\n|[^\n]+$', text))
    scan = list(text)

    def continuation(previous: str, following: str) -> bool:
        previous, following = previous.strip(), following.strip()
        if not previous or not re.match(r'[\u3400-\u9fff]', following):
            return False
        if re.search(r'[。！？；;.!?：:][”’"）)\]】]*$', previous):
            return False
        if re.match(r'[一二三四五六七八九十百]+[、.）)]', following):
            return False
        if _heading_kind(following) or re.match(r'[^：:]{1,80}[：:]', following):
            return False
        if _NEW_OBLIGATION.match(following):
            # A split negation (不 / 要求) is a continuation, not a new positive
            # obligation. Only the existing, finite negation vocabulary qualifies.
            return any(previous.endswith(left) and following.startswith(right) for left, right in _NEGATION_BREAKS)
        return True

    i = 0
    while i < len(lines):
        first = lines[i].group().strip()
        if not _NUMBERED_PREFIX.match(first) or not re.search(r'[\u3400-\u9fff]', first):
            i += 1
            continue
        end = i + 1
        while end < len(lines) and continuation(lines[end-1].group(), lines[end].group()):
            end += 1
        if end - i <= 8 and lines[end-1].end() - lines[i].start() <= 2000:
            for line in lines[i:end-1]:
                for position in range(line.end()-2, line.end()):
                    if position >= line.start() and text[position] in '\r\n':
                        scan[position] = ' '
        i = end
    return ''.join(scan)


def spans(text: str):
    section = ""
    vibe_section = False
    pattern = r"[^\n。！？；;]+(?:[。！？；;]|(?=\n|$))"
    # Split a comma only where a new obligation/negation begins, not arbitrary noun lists.
    contrast = re.compile(r"[，,](?=\s*(?:(?:但(?:是)?|然而|不过|同时|且|并)?\s*(?:必须|需要|应当|禁止|不得|严禁|不能|不允许|不要求|不必|不强制|无需)|(?:(?:but|however|and|also)\s+)?(?:you\s+)?(?:must\b|need\s+to\b|should\b|do\s+not\b|don['’]t\b)))", re.I)
    # Only clear new English clauses. Decimal/version dots, initials and
    # abbreviations such as e.g. / U.S. are not arbitrary sentence boundaries.
    english = re.compile(r'[.!?](?=[ \t]+(?:You|We|The|Our|This|Must|Do|Use|Build|Write|Review|Maintain|Experience|Knowledge|Proficiency|No|Please|Candidates?|Applicants?)\b)')
    for match in re.finditer(pattern, _soft_wrap_scan(text)):
        raw_match = match.group()
        endings = [m.end() for m in english.finditer(raw_match)
                   if not re.search(r'(?:\b(?:e\.g|i\.e|vs|Dr|Mr|Ms|Prof|Inc|Ltd|No)|(?:\b[A-Z]\.)+[A-Z])\.$', raw_match[:m.end()], re.I)]
        bounds = sorted({0, *(m.end() for m in contrast.finditer(raw_match)), *endings, len(raw_match)})
        for lo, hi in zip(bounds, bounds[1:]):
            raw = match.group()[lo:hi]
            left = len(raw) - len(raw.lstrip())
            prefix = re.match(r"[-*•·][ \t]*", raw[left:]) or _NUMBERED_PREFIX.match(raw[left:])
            if prefix:
                left += len(prefix.group())
            right = len(raw.rstrip())
            start, end = match.start() + lo + left, match.start() + lo + right
            quote = text[start:end]
            if not quote:
                continue
            kind = _heading_kind(quote)
            if kind:
                section = quote.rstrip('：:。!?？ ')
                vibe_section = kind == 'ai'
                continue
            inline = re.match(r'([^：:\n]{1,80})[：:]\s*', quote)
            if inline and (kind := _heading_kind(inline.group(1))):
                section = inline.group(1)
                vibe_section = kind == 'ai'
                start += inline.end()
                quote = text[start:end]
                if not quote:
                    continue
            yield Span(start, end, quote, text.count("\n", 0, start), section, vibe_section)


def strength(text: str, section: str = "") -> str:
    if re.search(r"禁止|严禁|不得|不允许|不能(?:使用|上传|发送)|must not|do not (?:use|upload|send)", text, re.I):
        return "prohibited"
    if re.search(r"无需|不要求|不需要|不必|不强制|not (?:required|necessary|mandatory)|no .{0,40}experience.{0,20}required|\b(?:do not|don['’]t) (?:need|have to)\b|\bneed not\b", text, re.I):
        return "not_required"
    if _heading_kind(section) == 'preferred' or re.search(r"优先|加分|有更好|更佳|nice.to.have|preferred|\ba plus\b", text + section, re.I):
        return "preferred"
    if re.search(r"必须|必备|要求|至少|精通|熟练|具备|能够|独立|熟悉|掌握|\bmust\b|\brequired\b", text, re.I):
        return "required"
    if re.search(r"负责|使用|应用|推动|参与|建立|构建|维护|设计|完成|开发", text):
        return "expected"
    if _heading_kind(section) == 'requirements' or re.match(
            r'(?:hands-on\s+)?(?:experience|proficiency|expertise|familiarity)\s+(?:with|in|using)\b|(?:proficient|comfortable)\s+(?:with|in)\b|ability\s+to\b', text, re.I):
        return "required"
    if re.match(r"(?:(?:you(?:\s+will|['’]ll)?|candidates?\s+will)\s+)?(?:use|apply|employ|adopt|integrate)\b", text, re.I):
        return "expected"
    return "unspecified"


class RuleExtractor:
    version = "rules-0.2.1"

    def __init__(self, config: dict):
        self.config = config
        self.direct_patterns = [re.compile(p, re.I) for p in config["direct_patterns"]]
        self.tool_patterns = {name: re.compile(p, re.I) for name, p in config["tools"].items()}
        self.cap_patterns = {name: [re.compile(p, re.I) for p in cap["patterns"]]
                             for name, cap in config["capabilities"].items()}

    def tools_in(self, text: str) -> list[str]:
        out = []
        for name, pattern in self.tool_patterns.items():
            matches = list(pattern.finditer(text))
            if not matches:
                continue
            if name == "Cursor":
                matches = [m for m in matches if not re.match(r"\s*\.(?:execute|fetch|close|rowcount)", text[m.end():], re.I)]
                if re.search(r"数据库游标|SQL\s*(?:数据库)?游标|database\s+cursor", text, re.I):
                    matches = []
            if name == "GitHub Copilot" and not re.search(r"github|代码|编程|编码|开发", text, re.I) and re.search(r"office|ppt|excel|word|办公", text, re.I):
                matches = []
            if matches:
                out.append(name)
        return out

    def direct(self, text: str) -> bool:
        return bool(self.tools_in(text) or any(p.search(text) for p in self.direct_patterns))

    def extract(self, job: JobRecord, roles: list[str], *, group_id: str | None = None) -> list[Requirement]:
        units = list(spans(job.text))
        anchors = [s for s in units if self.direct(s.matching_text) and strength(s.matching_text, s.section) in {"required", "expected", "preferred"}
                   and _heading_kind(s.section) not in {'benefits', 'company'}]
        group_id = group_id or "g_" + job.fingerprint[:24]
        rows = []
        for s in units:
            if _heading_kind(s.section) in {'benefits', 'company'}:
                continue
            clause = s.matching_text
            tools = self.tools_in(clause)
            is_direct = self.direct(clause)
            nearby = next((a for a in anchors if a.line == s.line and a != s and abs(a.start - s.start) <= 250), None)
            if is_direct:
                relation, score = "direct", 0.95
            elif s.vibe_section or nearby:
                relation, score = "contextual", 0.80
            elif anchors:
                relation, score = "role_related", 0.55
            else:
                continue
            capabilities = {k for k, ps in self.cap_patterns.items() if any(p.search(clause) for p in ps)}
            if is_direct:
                capabilities.add("ai_coding")
            if tools:
                capabilities.add("tool_fluency")
            if not capabilities:
                continue
            priority = strength(clause, s.section)
            mixed = bool(re.search(r"(?:无需|不要求|禁止|不得).{0,100}(?:但|同时|不过).{0,100}(?:必须|要求|熟练|需要)", clause))
            ambiguous = bool(re.search(r"不接受只会|不能只|不依赖|不能依赖|\bnot (?:just|only)\b|\b(?:cannot|can't|must not) rely\b", clause, re.I))
            # Building/selling the named product is not proof of using it as
            # a coding tool. Keep the original clause for explicit review.
            product_mention = any(re.search(
                r'\b(?:build|develop|design|sell|market|maintain|support)(?:ing|s)?\s+(?:the\s+)?$',
                clause[:match.start()], re.I)
                for tool in tools for match in self.tool_patterns[tool].finditer(clause))
            for cap in sorted(capabilities):
                rid = "r_" + digest(group_id + f"|{s.start}|{s.end}|{cap}")[:24]
                review = "needs_review" if job.evidence_level == "snippet" or mixed or ambiguous or product_mention or relation != "direct" or priority == "unspecified" else "rule_accepted"
                notes = []
                if relation == "role_related":
                    notes.append("same-JD supporting capability; NOT evidence that this is a Vibe Coding requirement")
                if mixed or ambiguous:
                    notes.append("mixed/ambiguous polarity; human review required")
                if priority == "unspecified":
                    notes.append("tool/topic mention without a clear candidate obligation; human review required")
                if product_mention:
                    notes.append("named product work is not proof of coding-tool use; human review required")
                if job.evidence_level == "snippet":
                    notes.append("search snippet; excluded from full-text statistics even if approved")
                rows.append(Requirement(
                    requirement_id=rid, record_id=job.record_id, job_group_id=group_id, capability=cap,
                    quote=s.text, start=s.start, end=s.end, relation=relation, strength=priority,
                    tools=tools, rule_score=score, extraction_method=self.version, review_status=review,
                    roles=roles, evidence_level=job.evidence_level, is_synthetic=job.is_synthetic,
                    platform=job.platform, title=job.title, company=job.company, url=job.url,
                    source_record_ids=[job.record_id], source_urls=[job.url] if job.url else [],
                    context_quote=nearby.text if nearby else (s.section if s.vibe_section else ""), notes="; ".join(notes)))
        return rows


def hard_constraints(job: JobRecord, group_id: str, roles: list[str]) -> list[dict]:
    patterns = {
        "education": r"本科|硕士|博士|学士|大专|学历|学位",
        "experience": r"\d+\s*[-~至到]?\s*\d*\s*年.{0,10}经验|经验.{0,10}\d+\s*年",
        "work_mode": r"驻场|到岗|坐班|远程|出差|工作地点",
        "credential": r"资格证|资格认证|职称|\bPMP\b",
    }
    rows = []
    for s in spans(job.text):
        for category, pattern in patterns.items():
            if re.search(pattern, s.matching_text, re.I):
                rows.append({"constraint_id": "h_" + digest(group_id + str(s.start) + category)[:20],
                             "job_group_id": group_id, "record_id": job.record_id, "roles": roles,
                             "category": category, "quote": s.text, "start": s.start, "end": s.end,
                             "strength": strength(s.matching_text, s.section), "url": job.url,
                             "evidence_level": job.evidence_level, "is_synthetic": job.is_synthetic,
                             "note": "Do not infer satisfaction from polished wording; verify separately."})
    return rows


def apply_reviews(requirements: list[Requirement], reviews: dict) -> list[str]:
    unknown = sorted(set(reviews) - {r.requirement_id for r in requirements})
    for r in requirements:
        item = reviews.get(r.requirement_id)
        if item is None or (isinstance(item, dict) and item.get("decision") == "pending"):
            continue
        if not isinstance(item, dict) or item.get("decision") not in {"approve", "reject"} or not item.get("reviewer") or not item.get("reason"):
            raise ValueError(f"review {r.requirement_id} needs decision, reviewer, reason")
        r.review_status = "approved" if item["decision"] == "approve" else "rejected"
        r.notes += " | human review: " + str(item["reason"])
    return unknown
