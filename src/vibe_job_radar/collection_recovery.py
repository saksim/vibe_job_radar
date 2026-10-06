"""Human-readable collection outcomes for the existing HTTP path."""
from __future__ import annotations

REASONS = {
    'ok': ('已取得完整正文', '可以下载本批报告并核对原文。'),
    'fresh_reused': ('复用了已保存的新鲜正文', '没有为这条记录重复访问网站。'),
    'budget_skipped': ('未执行：本批正文尝试预算已用完', '不是抓取失败；先确认已尝试条目的问题，再为剩余链接新建小批次。'),
    'local_proxy_configuration_conflict': ('本程序的HTTP与SOCKS设置同时存在', '请只保留一种专用覆盖；系统自动模式不会猜测冲突配置。'),
    'local_socks_configuration_invalid': ('SOCKS5入口配置无效', '仅接受无userinfo的本机socks5入口，凭据需单独设置；socks5h、SOCKS4及远程入口不会静默降级。'),
    'local_socks_auth_unsupported': ('SOCKS代理要求未支持的认证', '未发送网站请求，也不会绕过所选代理；不要把网站密码填写到代理配置。'),
    'local_socks_protocol_error': ('SOCKS代理响应格式不正确', '任务已停止且保留进度；检查所选端口是否提供SOCKS5协议。'),
    'local_socks_truncated_reply': ('SOCKS代理握手中断', '未改走直连，未重放网站请求。'),
    'local_socks_timeout': ('SOCKS代理握手超时', '等待网络恢复后继续；增加岗位预算不能解决握手超时。'),
    'local_socks_connection_failed': ('无法连接所选SOCKS代理', '保留任务，代理恢复后再继续；不会偷偷直连。'),
    'local_socks_request_rejected': ('SOCKS代理拒绝目标连接', '已停止，不通过切换身份、出口或直连绕过拒绝。'),
    'local_proxy_configuration_invalid': ('本机HTTP代理配置不符合要求', '只能填写明确的本机HTTP代理地址，凭据需单独设置；不支持远程代理。不要在职位URL中填写代理地址。'),
    'local_proxy_credentials_invalid': ('代理凭据配置无效', '用户名和密码需同时提供并符合字符/长度限制；未发送凭据或目标请求。'),
    'local_proxy_credentials_require_explicit': ('代理凭据没有绑定明确入口', '请配置本程序专用HTTP或SOCKS5入口；不会将凭据交给系统发现的其他代理。'),
    'local_proxy_auth_failed': ('本机代理认证未通过', '核对所选代理凭据；不会降为匿名、重复认证或改走直连。'),
    'local_proxy_connection_failed': ('所选本机代理无法建立连接或隧道', '核对代理实际HTTP监听端口。程序不会因代理失败自动直连；增加职位预算不能修复代理连接。'),
    'tls_verification_failed': ('TLS证书验证失败', '请检查系统时间和证书，不要关闭TLS验证。'),
    'tls_handshake_failed': ('TLS握手失败', '检查网络及服务端TLS支持；不自动重放已发送请求。'),
    'redirect_not_followed': ('旧版遇到跳转即停止，目标未被记录', '历史日志不能判断是否需要登录。更新后新建任务可看到跳转阶段，或转到浏览器向导。'),
    'redirect_login_required': ('网站跳转到了登录入口', 'HTTP路线没有浏览器登录会话。转到浏览器向导，在平台原生页人工登录后再采集。'),
    'redirect_verification_required': ('网站跳转到了验证入口', '自动请求已停止；在浏览器确认访问条件，不重复增加预算。'),
    'login_or_challenge': ('返回了登录或验证页面，不是职位正文', '使用浏览器向导人工处理；本次没有保存该页面为JD。'),
    'redirect_domain_not_permitted': ('跳转到了未获准的域名', '没有访问目标；核对原链接和来源权限，不自动扩大域名范围。'),
    'redirect_credentials_blocked': ('跳转地址含疑似凭据参数', '没有转发或保存凭据；应使用平台原生浏览器流程。'),
    'redirect_unsafe_target': ('跳转目标不满足HTTPS/标准端口/无凭据要求', '未跟随；核对目标地址，不能关闭安全检查。'),
    'redirect_missing_location': ('服务器返回跳转状态但没有目标地址', '保存本次诊断，人工核对来源；不能猜测目标。'),
    'redirect_loop': ('检测到跳转循环', '已停止，不进行无限重试。'),
    'redirect_limit': ('达到每条职位最多3次跳转限制', '请人工确认最终详情链接或使用浏览器向导。'),
    'robots_redirect_cross_origin': ('robots规则跳转到另一来源，未自动采用', '需核验发布方规则，不把另一个站点的规则当成本源许可。'),
    'robots_denied': ('来源robots规则拒绝此自动访问路径', '停止自动获取；使用允许的接口或有权处理的手工正文。'),
    'robots_unavailable': ('无法确认来源的robots规则', '未继续自动请求正文；不是缺少搜索Key。'),
    'permission_required': ('没有本次正文访问许可', '确认实际许可后再创建任务，不由软件自动勾选。'),
    'host_stopped': ('同一站点已因前项拒绝或限额而停止', '先处理前面条目的原因，不换任务反复请求。'),
    'rate_wait': ('共享访问间隔尚未结束', '本批保留结果；间隔结束后核对剩余范围再新建小批任务，不自动重放。'),
    'publisher_wait': ('发布方要求的共享等待尚未结束', '本批保留结果；等待发布方时间窗口允许后再核对剩余链接，新任务仍使用同一账本。'),
    'hourly_limit': ('工作区该平台的小时限额已用完', '等待窗口恢复后再新建小批任务；更换任务、入口或重启不会增加额度。'),
    'daily_limit': ('工作区该平台的每日限额已用完', '等待窗口恢复后再新建小批任务；不删除账本或退还已预留的尝试。'),
    'cooldown': ('该平台仍处于工作区共享冷却', '先核对触发原因；冷却结束后可新建已确认的小批任务。未自动重试。'),
    'clock_rollback': ('账本检测到时钟回退', '保留任务并核对系统时间；不清空账本重置配额。'),
    'rate_storage_error': ('无法可靠读取或写入共享限额账本', '本批已保留，停止访问；先检查原工作区文件，不能改用无计量请求。'),
    'publisher_policy_invalid': ('发布方节奏无法可靠保存', '本批已停止并保留原因；不按更宽松的规则继续。'),
    'unsafe_workspace': ('共享限额工作区路径无法确认', '保留原文件，核对工作区路径后再执行。'),
    'non_public_address': ('DNS返回非公网地址', '先做网络检查；这不是账号或浏览器安装错误。'),
    'parse_error': ('取得页面但不能确认独立职位正文', '可能是动态页面或结构变化，可转浏览器或手工录入。'),
    'job_identity_mismatch': ('返回的岗位与所选分享链接不一致', '未保存这份正文；请核对具体职位链接。'),
    'jd_incomplete': ('页面没有提供完整职位正文', '未将摘要或折叠内容作为完整JD；请在原平台核对可见正文。'),
    'job_unavailable': ('该职位已暂停或结束招聘', '本次未保存为可用正文；请选择仍可阅读的具体职位。'),
    'manual_required': ('页面要求人工完成登录或验证', '本次已停止；请在原平台完成正常操作。'),
    'interrupted_uncertain': ('上次请求中断，结果不确定', '预算已计入，不自动重试；确认后新建任务。'),
    'category_identity_mismatch': ('公开分类页面身份不匹配', '未使用该页发现岗位；需核对发布方页面。'),
    'category_structure_changed': ('公开分类结构无法确认', '未把页面改版当成没有岗位，也不从推荐区补充。'),
    'category_no_confirmed_jobs': ('分类页没有可确认的主列表职位', '这不能证明零岗位；保留页面诊断等待核验。'),
    'category_pagination_invalid': ('分类页的实际页号或分页链接无法确认', '未读取该页正文；请保留页标题、当前页标志和来源记录核验。'),
    'category_repeated_page': ('这一页的有效职位均已在前页出现', '已停止，不把重复页算作新岗位或继续翻页；旧结果与本次观察保留。'),
    'category_invalid_card': ('已选卡片无法确认具体职位', '本项保留在已选结果中，不以另一岗位补齐。'),
    'category_unsupported_detail': ('已选职位属于尚未核验的公开详情类型', '保留该项且不发正文请求，不以另一岗位补齐。'),
    'category_job_title_changed': ('详情标题与分类卡片不一致', '本次未保存为匹配岗位；请核对发布方信息。'),
}


def explain(state: dict) -> dict:
    details = []
    for original in state['details']:
        code = original['status']
        message, action = REASONS.get(code, ('本条状态：'+code, '展开采集诊断查看原因，不要只增加预算。'))
        delay = original.get('retry_after_seconds')
        if type(delay) in (int, float) and delay > 0:
            action += f' 本次受阻时需至少等待{delay:.0f}秒；这是当次记录，不是实时倒计时，之后仍需重新检查共享额度。'
        details.append({**original, 'status_message': message, 'next_action': action})
    saved = sum(d['status'] in {'ok', 'fresh_reused'} for d in details)
    skipped = sum(d['status'] == 'budget_skipped' for d in details)
    text = f'本批 {len(details)} 条链接，已尝试 {state["detail_attempts"]} 条，取得或复用 {saved} 条正文，预算未执行 {skipped} 条。'
    if state['mode'] == 'urls':
        text += ' 搜索和数据源预算不参与URL路线；报告阶段不代表取得了正文。'
    category_outcomes = []
    for original in state.get('category_outcomes', []):
        message = (('使用已保存的公开分类名单' if original.get('snapshot_reused') else '已读取公开分类主列表') if original['status'] == 'ok' else
                   REASONS.get(original['status'], ('分类状态：' + original['status'], ''))[0])
        action = REASONS.get(original['status'], ('', ''))[1]
        category_outcomes.append({**original, 'status_message': message, 'next_action': action})
    if state['mode'] == 'liepin_category':
        from .public_category import get_category, page_for_state
        category = get_category(state.get('category_id', 'architect'))
        page = page_for_state(state) + 1
        text += f' 分类读取尝试 {state.get("category_attempts", 0)} 次；范围为{category.name}分类第 {page} 页，不含关键词或地区筛选。'
        if category_outcomes:
            text += ' ' + category_outcomes[0]['status_message'] + '。'
            text += ' ' + category_outcomes[0]['next_action']
    return {'details': details, 'user_summary': text,
            'category_outcomes': category_outcomes,
            'route_label': {'urls': '公开HTTP（无浏览器登录会话）', 'search': '搜索API＋公开HTTP',
                            'liepin_category': f'{category.label}（第 {page} 页，最多5个职位）' if state['mode'] == 'liepin_category' else '',
                            'feed': '用户配置的授权JSON源'}.get(state['mode'], state['mode']),
            'saved_detail_count': saved, 'budget_skipped_count': skipped}

# Additional fixed messages; never display raw resolver/proxy payloads.
from .network_settings import DNS_MESSAGES

REASONS.update({code: ("网络解析未完成", message) for code, message in DNS_MESSAGES.items()})
from .vm_proxy import ERROR_MESSAGES as VM_MESSAGES
REASONS.update({code: ("宿主机代理未连接", message) for code, message in VM_MESSAGES.items()})
