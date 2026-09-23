"""Reviewed fixed public GET sources, never user-supplied board tokens or URLs."""
from dataclasses import dataclass
from urllib.parse import urlsplit

from .public_contract import ContractError, PublicSource


@dataclass(frozen=True)
class PublicBoard:
    token: str
    company: str
    source: PublicSource
    adapter_version: str
    require_company: bool = False
    job_query: str = ''
    prioritize_title: bool = False

    @property
    def api_url(self):
        return f'https://boards-api.greenhouse.io/v1/boards/{self.token}/jobs?content=true'

    def accepts_job(self, url, ident):
        self.source.accepts(url)
        parsed = urlsplit(url)
        if (parsed.path.rstrip('/') != f'/{self.token}/jobs/{ident}'
                or parsed.query != self.job_query.format(id=ident)):
            raise ContractError('public_source_mismatch')


# Keep Anthropic's existing contract/adapter bytes: old confirmed plans and
# caches must not change merely because a second independent source is added.
ANTHROPIC = PublicBoard('anthropic', 'Anthropic',
    PublicSource('greenhouse_anthropic', 'Anthropic 公开招聘（本机获取）',
        ('job-boards.greenhouse.io', 'boards.greenhouse.io'),
        '用户主动请求：Greenhouse公开只读职位接口；仅本机岗位研究，不提交申请、不推断转载授权。',
        local_access_approved=True), 'greenhouse_local_board_v1')

CLOUDFLARE = PublicBoard('cloudflare', 'Cloudflare',
    PublicSource('greenhouse_cloudflare', 'Cloudflare 公开招聘（本机获取）',
        ('boards.greenhouse.io',),
        '用户主动选择Cloudflare：Greenhouse公开Job Board GET及content=true正文；仅本机岗位研究，固定来源内ID和gh_jid，不提交申请、不推断转载或分发授权。',
        local_access_approved=True), 'greenhouse_cloudflare_board_v1',
    require_company=True, job_query='gh_jid={id}', prioritize_title=True)

BOARDS = {board.source.key: board for board in (ANTHROPIC, CLOUDFLARE)}
