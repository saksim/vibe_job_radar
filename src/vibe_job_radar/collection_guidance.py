"""Side-effect-free form guidance. Does not contact providers or create collection tasks."""
from __future__ import annotations

import os
import ipaddress
import re
from urllib.parse import parse_qsl, urlsplit

from .config import platform_for_url
from .discovery import build_plan
from .workspace import InputError

LABELS = {
    "mode": "采集路线", "roles": "目标岗位", "platforms": "来源平台",
    "urls": "职位 URL 列表", "api_key": "Brave Search API Key",
    "endpoint": "授权 JSON 数据源地址", "contract_ref": "接口契约链接",
    "rights_note": "实际授权依据与使用范围", "consent": "执行确认",
    "search_storage_rights": "搜索结果保存权限", "permit_platforms": "正文访问许可",
    "search_budget": "搜索请求预算", "pages": "每个检索任务最多页数",
    "detail_budget": "正文获取尝试预算", "feed_budget": "数据源最大页数",
    "fresh_hours": "复用正文时间窗口",
}
PLACEHOLDER = re.compile(r"REPLACE_WITH|YOUR_(?:API|KEY|TOKEN)|【|<[^>]+>", re.I)


def url_hint(value: str, *, detail: bool = False) -> str:
    """Raise an actionable, non-echoing error for obvious non-operational inputs."""
    from .collection import safe_url
    if PLACEHOLDER.search(value):
        raise InputError("这是格式示例，需替换为你实际打开的地址；不要直接提交示例占位符。")
    try:
        normalized = safe_url(value.strip())
        parsed = urlsplit(normalized)
    except (ValueError, TypeError):
        raise InputError("请使用完整 HTTPS 地址，且不要包含密码、Token 或签名参数。") from None
    host = parsed.hostname or ""
    if host in {"127.0.0.1", "localhost", "::1"} or host.endswith(".localhost"):
        raise InputError("这是本机地址，不是招聘网站的职位详情地址。")
    # Literal IP validation is local: never resolve DNS during form preview.
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise InputError("这是非公网 IP 地址，当前采集器不会访问；请向提供方索取可用的公网 HTTPS 地址。")
    if host.endswith((".invalid", ".example", ".test")) or host in {"example.com", "example.org", "example.net"}:
        raise InputError("这是示例域名，不是真实数据源；请换成自己的实际地址。")
    if detail and (parsed.path in {"", "/", "/job_detail/", "/web/geek/jobs"} or host == "search.51job.com"):
        raise InputError("这看起来是首页或职位列表。请点进一个具体职位，再复制该详情页的地址。")
    return normalized


def preview(workspace, data: dict) -> dict:
    """Inspect user inputs locally; readiness is NOT permission or live-service verification."""
    errors: list[dict] = []
    warnings: list[str] = []
    def fail(field: str, message: str) -> None:
        errors.append({"field": field, "label": LABELS.get(field, field), "message": message})

    mode = data.get("mode", "search")
    if not isinstance(mode, str) or mode not in {"search", "urls", "feed"}:
        mode = "invalid"
        fail("mode", "请选择职位 URL、搜索 API 或授权数据源中的一种。")
    selected = {}
    for field, defaults in (("roles", list(workspace.config["roles"])),
                            ("platforms", list(workspace.config["platforms"]))):
        values = data.get(field, defaults)
        if not isinstance(values, list) or not values or not all(isinstance(v, str) and v in workspace.config[field] for v in values):
            fail(field, "请至少勾选一个已配置选项。")
            values = []
        selected[field] = list(dict.fromkeys(values))

    limits = {"search_budget": (24, 1, 300), "pages": (1, 1, 10),
              "detail_budget": (20, 0, 300), "feed_budget": (5, 1, 20),
              "fresh_hours": (24, 1, 720)}
    relevant = {"urls": {"detail_budget", "fresh_hours"},
                "search": {"search_budget", "pages", "detail_budget", "fresh_hours"},
                "feed": {"feed_budget"}}.get(mode, set())
    budgets = {}
    for field in relevant:
        default, low, high = limits[field]
        value = data.get(field, default)
        if type(value) is not int or not low <= value <= high:
            fail(field, f"填写 {low}～{high} 的整数；不确定时使用页面上的入门参数。")
            value = default
        budgets[field] = value

    rights = data.get("rights_note", "")
    if not isinstance(rights, str) or not rights.strip() or len(rights) > 2000:
        fail("rights_note", "写清实际来源、允许使用的依据和本次用途。没有自动访问许可时返回基础页粘贴获准处理的 JD。")
    elif PLACEHOLDER.search(rights):
        fail("rights_note", "请用真实依据替换示例中的括号内容，不能把模板当作授权。")
    if data.get("consent") is not True:
        fail("consent", "检查预算和使用范围后，由你本人勾选执行确认。预检不会替你勾选。")
    permits = data.get("permit_platforms", [])
    if not isinstance(permits, list) or not all(isinstance(p, str) and p in selected["platforms"] for p in permits):
        fail("permit_platforms", "正文许可只能勾选当前已选择的平台；切换来源后请重新核对。")
        permits = []

    rows, detected, queries = [], [], []
    query_count, valid_urls, normalized_urls = 0, 0, 0
    credential_configured = False
    if mode == "urls":
        from .public_job_links import prepare_public_job_link
        raw = data.get("urls", "")
        if not isinstance(raw, str) or not raw.strip() or len(raw) > 100000:
            fail("urls", "先在招聘网站打开一个具体职位，复制地址栏中的完整 HTTPS 地址粘贴到这里；每行一个。")
        elif len(raw.splitlines()) > 300:
            fail("urls", "每次最多 300 行，请拆分；第一次只粘贴 1 个真实职位链接。")
        else:
            seen = set()
            for line, value in enumerate(raw.splitlines(), 1):
                if not value.strip():
                    continue
                try:
                    url, normalization = prepare_public_job_link(url_hint(value, detail=True))
                except InputError as exc:
                    fail("urls", f"第 {line} 行：{exc}")
                    continue
                platform = platform_for_url(url, workspace.config)
                if platform == "unknown":
                    fail("urls", f"第 {line} 行：域名未配置。请核对链接，或在“新增招聘平台”中添加真实来源域名。")
                    continue
                if platform not in detected:
                    detected.append(platform)
                duplicate = url in seen
                rows.append({"line": line, "platform": platform,
                             "label": workspace.config["platforms"][platform]["label"], "duplicate": duplicate})
                if normalization:
                    normalized_urls += 1
                    rows[-1]['link_normalization'] = normalization
                seen.add(url)
                if not duplicate:
                    valid_urls += 1
                if platform not in selected["platforms"]:
                    fail("platforms", f"第 {line} 行来自 {workspace.config['platforms'][platform]['label']}，请勾选该平台或点击“从链接识别来源平台”。")
            missing = [p for p in detected if p in selected["platforms"] and p not in permits]
            if missing:
                fail("permit_platforms", "这些链接尚未确认正文访问许可。只有确有依据时才勾选；不确定时使用基础页的粘贴 JD 路线。")
        if budgets.get("detail_budget") == 0:
            fail("detail_budget", "URL 路线需要至少 1 次正文预算；0 表示完全不取正文。")
        if valid_urls > budgets.get("detail_budget", 0):
            warnings.append("链接数多于正文尝试预算；未复用的超额链接会跳过，不会保证全部获取。")
        if normalized_urls:
            warnings.append(f"已识别 {normalized_urls} 条猎聘分享链接；将去除分享跟踪参数，按同一职位编号的公开地址采集。")
        warnings.append("识别出平台不等于已取得授权或已验证链接有效；登录页、动态页面、robots 拒绝仍可能无法取得正文。")
    elif mode == "search":
        key = data.get("api_key", "")
        if not isinstance(key, str) or len(key) > 1000:
            fail("api_key", "请粘贴自己在 Brave Search API 控制台创建的 Key，不是账号密码或招聘网站链接。")
            key = ""
        key = key.strip() or os.environ.get("BRAVE_SEARCH_API_KEY", "")
        credential_configured = bool(key)
        if not key:
            fail("api_key", "尚无 Brave Search API Key。可先看下方检索计划；执行前需在官方控制台注册、启用 Search 套餐并创建 Key。")
        elif PLACEHOLDER.search(key) or key.lower() in {"your_api_key", "your_key", "your_token"}:
            fail("api_key", "示例 Key 不能执行搜索；请替换为 Brave 控制台实际生成的值。")
        if data.get("search_storage_rights") is not True:
            fail("search_storage_rights", "确认自己的 Brave 套餐允许保存搜索结果后，再勾选此项；付费/启用套餐本身不替代用途核对。")
        if selected["roles"] and selected["platforms"]:
            tasks = build_plan(workspace.config, selected["platforms"], selected["roles"])
            query_count = len(tasks)
            queries = [task.query for task in tasks[:5]]
        if budgets.get("detail_budget") == 0:
            warnings.append("本次只搜索标题、链接和摘要，不请求职位正文；摘要不能生成正式正文要求统计，任务可能显示 empty/needs_attention。")
        elif not permits:
            warnings.append("没有确认任何正文许可的平台：只保留搜索线索，不会获取详情。首次测试可直接把正文预算设为 0。")
        warnings.append("搜索预算是 API 调用上限，不是职位数量，也不是全平台覆盖保证。")
    elif mode == "feed":
        for field in ("endpoint", "contract_ref"):
            value = data.get(field, "")
            if not isinstance(value, str) or not value.strip():
                fail(field, "请向实际的数据提供方索取；没有现成接口就不要选这条路线，不能用招聘首页代替。")
            else:
                try:
                    normalized = url_hint(value)
                    if field == "endpoint" and any(k == "cursor" for k, _ in parse_qsl(urlsplit(normalized).query, keep_blank_values=True)):
                        raise InputError("请移除数据源地址中的 cursor 参数；分页游标由程序管理，不要复制已翻页的接口地址。")
                except InputError as exc:
                    fail(field, str(exc))
        credential_configured = bool(data.get("api_key"))
        warnings.append("数据源须符合项目 GET jobs/next_cursor 契约；预检不联网，不确认接口存在、Token 有效或返回结构正确。")

    return {"ready": not errors, "mode": mode, "errors": errors, "warnings": warnings,
            "budgets": budgets, "detected_platforms": detected, "url_rows": rows,
            "unique_url_count": valid_urls, "query_count": query_count, "query_preview": queries,
            "normalized_url_count": normalized_urls,
            "credential_configured": credential_configured, "credential_verified": False,
            "external_network_requests": 0, "task_created": False,
            "message": "填写检查通过，可核对后点击创建执行；未验证实站、凭据或授权。" if not errors else "尚有字段需要处理；下方按字段说明怎么补，不会发起采集。"}


class CollectionGuidance:
    """Read-only HTTP service sharing the existing workspace configuration."""
    def __init__(self, workspace):
        self.workspace = workspace

    def preview(self, data: dict) -> dict:
        return preview(self.workspace, data)
