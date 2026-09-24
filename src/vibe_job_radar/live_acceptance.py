"""Offline, append-only observations for a preregistered Liepin acceptance plan.

Counts and consistency checks are not site certification. In particular, old
checkpoints cannot prove omitted retries, runtime environment or manual review.
No browser, transport, Store writer or personal session is opened here.
"""
from __future__ import annotations
from collections import Counter
from contextlib import closing
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .collection import writer_lock
from .config import load_config
from .guided.adapters import builtins
from .guided.contracts import CrawlError
from .guided import attempt_history
from .html_parser import _unique_object
from .models import JobRecord
from .utils import atomic_json, parse_time, utc_now
from .workspace import InputError

ROOT_NAME = '.radar-acceptance'
HEX32 = re.compile(r'[0-9a-f]{32}')
HEX64 = re.compile(r'[0-9a-f]{64}')
MAX_TASKS, MAX_CAPTURES = 200, 200


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode('utf-8')


def _text(value, maximum=500):
    return isinstance(value, str) and 0 < len(value) <= maximum and not any(ord(c) < 32 for c in value)


class AcceptanceLedger:
    def __init__(self, workspace, *, clock=utc_now):
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise InputError('请指定已经存在的工作区；验收不会创建或代替业务数据。')
        self.clock = clock
        self.root = self._path(ROOT_NAME)

    def _path(self, *parts):
        path = self.workspace.joinpath(*parts)
        if path.is_symlink() or not path.resolve().is_relative_to(self.workspace):
            raise InputError('验收路径不能使用工作区外的链接。')
        return path

    def _read(self, path, limit=2_000_000):
        path = self._path(path.relative_to(self.workspace))
        with path.open('rb') as stream:
            raw = stream.read(limit+1)
        if len(raw) > limit:
            raise InputError('验收输入过大，未截断或忽略剩余数据。')
        return raw

    def _json(self, path, limit=2_000_000):
        try:
            value = json.loads(self._read(path, limit).decode('utf-8'), object_pairs_hook=_unique_object)
            if not isinstance(value, dict): raise ValueError()
            return value
        except (OSError, ValueError, TypeError, RecursionError):
            raise InputError('验收输入不存在、损坏或包含重复字段；未修改原文件。') from None

    def create(self, data):
        fields = {'keyword','roles','search_url','backend','selection_rule','timezone',
                  'source_revision','source_sha256','browser','browser_version','planner_os','network_mode','consent'}
        if set(data) != fields or data['consent'] is not True:
            raise InputError('创建验收计划需明确确认全部范围字段；不接受密码、Cookie或附加数据。')
        adapter = builtins().get('liepin')
        roles = data['roles']
        if (not _text(data['keyword'],100) or not _text(data['selection_rule'],1000)
                or not isinstance(roles,list) or not roles
                or any(not isinstance(r,str) or r not in load_config()['roles'] for r in roles)
                or len(set(roles)) != len(roles)
                or data['backend'] not in {'bridge','native'}
                or data['browser'] not in {'msedge','bundled'}
                or not _text(data['browser_version'],80) or not _text(data['planner_os'],200)
                or data['network_mode'] not in {'direct','system','static_http','static_socks5','unknown'}
                or not isinstance(data['source_revision'],str) or not re.fullmatch('[0-9a-f]{40}',data['source_revision'])
                or not isinstance(data['source_sha256'],str) or not HEX64.fullmatch(data['source_sha256'])):
            raise InputError('验收计划中的目标、源码或环境字段无效。')
        try:
            ZoneInfo(data['timezone'])
            url = adapter.accept_url(data['search_url'] or adapter.search_url(data['keyword']))
            from urllib.parse import parse_qsl, urlsplit
            parsed = urlsplit(url)
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            if (parsed.path.rstrip('/') != '/zhaopin' or len({k for k,v in pairs}) != len(pairs)
                    or dict(pairs).get('key') != data['keyword']):
                raise ValueError()
        except (ValueError, TypeError, ZoneInfoNotFoundError, CrawlError):
            raise InputError('请选择有效 IANA 时区与同一检索词的猎聘搜索条件；Windows 请安装验收可选依赖 tzdata。') from None
        plan = {k:v for k,v in data.items() if k != 'consent'}
        plan.update(schema_version=1, id=uuid.uuid4().hex, created_at=self.clock(), platform='liepin',
                    adapter_version=adapter.version, search_url=url,
                    proposed_thresholds={'unique_full_jobs':30,'local_dates':3,'completion_ratio':0.9},
                    threshold_approved=False, environment_scope='operator_declared_not_runtime_attested')
        plan['sha256'] = _hash(_encoded(plan))
        folder = self._path(ROOT_NAME, plan['id']); folder.mkdir(parents=True, mode=0o700)
        atomic_json(folder/'plan.json',plan)
        return plan

    def _plan(self, ident):
        if not isinstance(ident,str) or not HEX32.fullmatch(ident):
            raise InputError('验收计划编号无效。')
        folder = self._path(ROOT_NAME,ident)
        plan = self._json(folder/'plan.json')
        copied = dict(plan); expected = copied.pop('sha256',None)
        if (plan.get('schema_version') != 1 or plan.get('id') != ident
                or plan.get('platform') != 'liepin' or expected != _hash(_encoded(copied))):
            raise InputError('计划内容已变化或版本不兼容；请保留原计划并新建明确范围。')
        try:
            parse_time(plan['created_at']); ZoneInfo(plan['timezone'])
        except (ValueError, KeyError, TypeError, ZoneInfoNotFoundError):
            raise InputError('计划时间或时区无效。') from None
        return folder,plan

    def _history(self, folder, plan):
        paths = sorted(folder.glob('capture-*.json'))
        if len(paths) > MAX_CAPTURES: raise InputError('验收观测数量超过上限。')
        previous, history = plan['sha256'], []
        previous_time = parse_time(plan['created_at'])
        for index,path in enumerate(paths,1):
            if path.name != f'capture-{index:04}.json':
                raise InputError('验收观测序列缺失，不能忽略或重排历史。')
            item = self._json(path, 5_000_000)
            copied = dict(item); expected = copied.pop('sha256',None)
            if (item.get('plan_sha256') != plan['sha256'] or item.get('previous_sha256') != previous
                    or expected != _hash(_encoded(copied))):
                raise InputError('验收观测链不一致，未合并可疑结果。')
            observed = parse_time(item['captured_at'])
            if not previous_time <= observed <= parse_time(self.clock()):
                raise InputError('验收观测日期逆序或来自未来，未模拟或改写日期。')
            previous_time = observed
            history.append(item); previous = expected
        head_path = folder/'head.json'
        if paths or head_path.exists():
            head = self._json(head_path)
            if head != {'captures':len(history),'sha256':previous}:
                raise InputError('验收观测尾部缺失或写入未完成，不能忽略历史。')
        return history,previous

    def _report(self, ident):
        if not isinstance(ident,str) or not HEX32.fullmatch(ident):
            raise InputError('成功条目缺少有效的原报告。')
        folder = self._path('reports',ident)
        manifest = self._json(folder/'run_manifest.json')
        files = {}
        for name in ('guided_acquisition.json','jobs.jsonl','input_audit.csv'):
            raw = self._read(folder/name, 20_000_000)
            if _hash(raw) != manifest.get('output_files_sha256',{}).get(name):
                raise InputError('原报告文件哈希不一致，未计为完整正文证据。')
            files[name] = raw
        audit = json.loads(files['guided_acquisition.json'], object_pairs_hook=_unique_object)
        jobs = [JobRecord.from_dict(json.loads(line,object_pairs_hook=_unique_object))
                for line in files['jobs.jsonl'].decode('utf-8').splitlines() if line.strip()]
        if len({j.record_id for j in jobs}) != len(jobs): raise InputError('原报告包含重复记录。')
        selected = {r['record_id'] for r in csv.DictReader(io.StringIO(files['input_audit.csv'].decode('utf-8-sig')))
                    if r['status'] == 'selected'}
        return manifest,audit,{j.record_id:j for j in jobs},selected

    def _verified_item(self, state, row, report, plan):
        manifest,audit,jobs,selected = report
        record = jobs.get(row.get('record_id'))
        if (record is None or audit.get('task_id') != state['id'] or record.is_synthetic
                or record.source_mode != 'browser_fetch' or record.evidence_level != 'full_text'
                or record.platform != 'liepin' or not record.parser or not HEX64.fullmatch(record.raw_sha256)):
            raise InputError('成功条目缺少本站浏览器完整正文证据。')
        if (record.source_ref != f"guided:{state['id']}:{row['id']}"
                or row.get('body_sha256') != _hash(record.text.encode('utf-8'))
                or row.get('title') != record.title or row.get('adapter_version') != plan['adapter_version']):
            raise InputError('正文与当前条目的身份、标题、解析版本或哈希不一致。')
        if (record.record_id not in selected or manifest.get('status') != 'completed'
                or manifest.get('filters',{}).get('roles') != plan['roles']
                or manifest.get('filters',{}).get('platforms') != ['liepin']
                or len(audit['items']) != len(state['selection'])
                or {r['id'] for r in audit['items']} != set(state['selection'])):
            raise InputError('正文未被原报告纳入目标岗位，不能计为完整目标样本。')
        adapter = builtins().get('liepin')
        identity = adapter.job_identity(row['url'])
        if identity != adapter.job_identity(row['resolved_url']) or identity != adapter.job_identity(record.url):
            raise InputError('原链接、最终链接和正文岗位身份不一致。')
        matching = [r for r in audit['items'] if r.get('id') == row['id']]
        if (len(matching) != 1 or matching[0].get('status') != 'ok'
                or matching[0].get('record_id') != record.record_id
                or matching[0].get('body_sha256') != row['body_sha256']):
            raise InputError('原报告逐条取数审计与任务不一致。')
        collected = parse_time(record.collected_at)
        if not max(parse_time(plan['created_at']),parse_time(state['created_at'])) <= collected <= parse_time(self.clock()):
            raise InputError('样本日期在计划之前或当前时间之后。')
        with closing(sqlite3.connect(self._path('jobs.sqlite').as_uri()+'?mode=ro',uri=True)) as db:
            observation = db.execute('SELECT 1 FROM observations WHERE record_id=? AND collected_at=? AND source_ref=? AND source_mode=?',
                (record.record_id,record.collected_at,record.source_ref,'browser_fetch')).fetchone()
        if not observation: raise InputError('缺少该次实际入库观测，不能补造跨日期记录。')
        return {'entity':identity,'record_id':record.record_id,'body_sha256':row['body_sha256'],
                'title_sha256':_hash(record.title.encode('utf-8')),
                'raw_sha256':record.raw_sha256,'parser':record.parser,'collected_at':record.collected_at,
                'local_date':collected.astimezone(ZoneInfo(plan['timezone'])).date().isoformat(), 'full_target_verified':True}

    def capture(self, ident):
        folder,plan = self._plan(ident)
        with writer_lock(folder):
            history,previous = self._history(folder,plan)
            prior_attempts = {}
            for observed in history:
                for task in observed['tasks']:
                    prior_attempts[task['id']] = task.get(attempt_history.KEY)
            if len(history) >= MAX_CAPTURES: raise InputError('验收观测已达到上限。')
            paths = sorted(self._path('guided').glob('*.json'))
            paths = [p for p in paths if HEX32.fullmatch(p.stem)]
            if len(paths) > MAX_TASKS: raise InputError('工作区任务过多，请使用独立验收工作区。')
            tasks,excluded = [],[]
            for path in paths:
                state = self._json(path)
                if state.get('id') != path.stem: raise InputError('任务 ID 与文件不一致。')
                if parse_time(state['created_at']) > parse_time(self.clock()):
                    raise InputError('任务日期来自未来，未纳入真实日期验收。')
                if state.get('platform') != 'liepin' or parse_time(state['created_at']) < parse_time(plan['created_at']):
                    excluded.append({'id':path.stem,'reason':'before_plan_or_other_platform'}); continue
                if any(state.get(k) != plan[k] for k in ('keyword','roles','search_url','backend')):
                    excluded.append({'id':path.stem,'reason':'outside_registered_query'}); continue
                if state.get('status') in {'queued','running'}:
                    raise InputError('请等待或暂停当前批次，再保存一致的验收观测；不会替你停止任务。')
                cards,selection = state.get('cards'),state.get('selection')
                if (not isinstance(cards,list) or len(cards)>100 or not isinstance(selection,list)
                        or len(selection)!=len(set(selection)) or len(selection)>20
                        or len({r['id'] for r in cards})!=len(cards)
                        or not set(selection)<={r['id'] for r in cards}):
                    raise InputError('任务选择与卡片不一致，不能忽略缺失条目。')
                try:
                    attempts = attempt_history.snapshot(state)
                    attempt_history.extends(prior_attempts.get(state['id']), attempts)
                except CrawlError:
                    raise InputError('逐次采集历史损坏、缺失或已覆盖，未忽略早期失败。') from None
                historical_selected = {i for selection in (attempts or {}).get('selections', []) for i in selection['items']}
                report,report_error,report_hash = None,'',''
                if any(r['id'] in selection and r.get('status')=='ok' for r in cards):
                    try:
                        report = self._report(state.get('report_id'))
                        report_hash = _hash(self._read(self._path('reports',state['report_id'],'run_manifest.json')))
                    except (InputError,OSError,ValueError,KeyError,TypeError): report_error='report_evidence_invalid'
                items = []
                for row in cards:
                    if row['id'] not in set(selection) | historical_selected: continue
                    item = {'id':row['id'],'status':row['status'],'full_target_verified':False,
                            'permission':'not_granted' if row['status']=='robots_denied' else 'not_independently_verified'}
                    if row['id'] not in selection:
                        item['historical_selection_only'] = True
                    elif row['status']=='ok':
                        try:
                            if report_error: raise InputError(report_error)
                            item.update(self._verified_item(state,row,report,plan))
                        except (InputError,OSError,ValueError,KeyError,TypeError,sqlite3.Error,CrawlError):
                            item['evidence_error']=report_error or 'item_evidence_invalid'
                    items.append(item)
                # Capture source byte identity; an active writer cannot silently
                # produce a mixed-state observation of this task.
                if self._json(path) != state: raise InputError('采集任务在观测期间改变，请稳定后重试。')
                tasks.append({'id':state['id'],'created_at':state['created_at'],'status':state['status'],'code':state.get('code',''),
                    'source_sha256':_hash(_encoded(state)),'report_id':state.get('report_id',''),
                    'report_sha256':report_hash,'selected':len(selection),'items':items,
                    attempt_history.KEY:attempts})
            unknown = ['runtime_environment','per_request_cost', 'search_login_and_http_attempts_not_recorded',
                       'human_identity_title_full_text_review']
            if not tasks or any(not task[attempt_history.KEY] or task[attempt_history.KEY]['origin'] != 'task_creation' for task in tasks):
                unknown += ['per_attempt_latency', 'complete_retry_history_before_capture']
            if any(a['outcome'] == 'unfinished' for task in tasks for a in (task[attempt_history.KEY] or {}).get('attempts', [])):
                unknown.append('unfinished_detail_attempts_have_unknown_outcome_and_duration')
            if any(item['status'] == 'ok' and not any(a['item_id'] == item['id'] and a['outcome'] == 'ok'
                    for a in (task[attempt_history.KEY] or {}).get('attempts', []))
                    for task in tasks for item in task['items']):
                unknown.append('saved_items_without_recorded_successful_detail_attempt')
            capture = {'schema_version':1,'plan_sha256':plan['sha256'],'previous_sha256':previous,
                'captured_at':self.clock(),'tasks':tasks,'excluded_tasks':excluded,
                'unknown_evidence':unknown}
            capture['sha256']=_hash(_encoded(capture))
            if len(_encoded(capture)) > 5_000_000: raise InputError('验收观测过大。')
            atomic_json(folder/f'capture-{len(history)+1:04}.json',capture)
            atomic_json(folder/'head.json',{'captures':len(history)+1,'sha256':capture['sha256']})
        return self.verify(ident)

    def verify(self, ident):
        folder,plan = self._plan(ident)
        history,_ = self._history(folder,plan)
        latest,origins,previous_outcomes,unknown = {},{},Counter(),set()
        attempts_by_task = {}
        for capture in history:
            unknown.update(k for k in capture['unknown_evidence']
                           if k != 'unfinished_detail_attempts_have_unknown_outcome_and_duration')
            for task in capture['tasks']:
                attempts = task.get(attempt_history.KEY)
                try:
                    # Historical captures do not have task cards. Their complete
                    # selected union supplies the identities needed for validation.
                    if attempts is not None:
                        attempt_history.validate({'cards': task['items'], attempt_history.KEY: attempts})
                    attempt_history.extends(attempts_by_task.get(task['id']), attempts)
                except CrawlError:
                    raise InputError('验收观测中的逐次历史缺失或改变，不能删除早期失败。') from None
                attempts_by_task[task['id']] = attempts
                for item in task['items']:
                    key = (task['id'],item['id'])
                    if item.get('historical_selection_only') and key in latest:
                        continue
                    if key in latest and latest[key] != item:
                        previous_outcomes[latest[key]['status']] += 1
                    latest[key] = item
                    origins[key] = task
        items = list(latest.values())
        complete, reports = [],{}
        for key,item in latest.items():
            if not item['full_target_verified']: continue
            task = origins[key]
            try:
                report_id = task['report_id']
                if _hash(self._read(self._path('reports',report_id,'run_manifest.json'))) != task['report_sha256']:
                    raise InputError('原报告已改变。')
                if report_id not in reports: reports[report_id] = self._report(report_id)
                report = reports[report_id]
                row = next(r for r in report[1]['items'] if r['id']==item['id'])
                row = {**row,'title':report[2][item['record_id']].title}
                checked = self._verified_item({'id':task['id'],'created_at':task['created_at'],
                    'selection':[r['id'] for r in task['items'] if not r.get('historical_selection_only')]},row,report,plan)
                if any(item.get(k) != v for k,v in checked.items()): raise InputError('原始条目已改变。')
                complete.append(item)
            except (InputError,OSError,ValueError,KeyError,TypeError,sqlite3.Error,CrawlError,StopIteration):
                unknown.add('previously_observed_evidence_no_longer_valid')
        entities = {i['entity'] for i in complete}; dates = sorted({i['local_date'] for i in complete})
        denied = sum(i['permission']=='not_granted' for i in items)
        denominator = len(items)-denied
        ratio = len(complete)/denominator if denominator else None
        if any(a['outcome'] == 'unfinished' for h in attempts_by_task.values() if h for a in h['attempts']):
            unknown.add('unfinished_detail_attempts_have_unknown_outcome_and_duration')
        gaps = sorted(unknown)
        if not history: gaps.append('no_observations')
        if len(entities)<30: gaps.append('fewer_than_30_unique_full_target_jobs')
        if len(dates)<3: gaps.append('fewer_than_3_observed_local_dates')
        if ratio is None or ratio<0.9: gaps.append('below_proposed_completion_ratio')
        if any(i.get('evidence_error') for i in items): gaps.append('invalid_item_evidence')
        gaps.extend(['proposed_threshold_requires_user_review','live_access_and_environment_require_review'])
        return {'schema_version':1,'plan_id':ident,'status':'evidence_incomplete','certification':'not_live_verified',
            'default_backend_changed':False,'captures':len(history),'selected_items':len(items),
            'known_not_permitted_items':denied,'selected_items_except_known_denials':denominator,
            'verified_full_target_items':len(complete),'unique_full_target_jobs':len(entities),'local_dates':dates,
            'observed_completion_ratio':ratio,'statuses':dict(Counter(i['status'] for i in items)),
            'previous_observed_outcomes':dict(previous_outcomes),'gaps':gaps,
            'detail_attempts':attempt_history.summarize(attempts_by_task),
            'scope':'本机观测的选中条目及历史变化；非全部 HTTP 尝试分母，不能据此自动认证或发布'}
