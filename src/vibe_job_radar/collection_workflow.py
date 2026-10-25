"""Explicit create-and-run admission, independent of page refreshes."""
from .workspace import InputError, text_field


class CollectionPreflightRejected(InputError):
    def __init__(self, preview):
        super().__init__('采集填写检查未通过；未创建或执行批次。')
        self.preview = preview


class CollectionWorkflow:
    def __init__(self, application):
        self.application = application

    def create_and_start(self, data):
        # ApplicationCommands holds the one mutation gate across both original
        # operations. Never nest execute() or hold a Collector writer lease
        # while asking the runner to acquire its own ownership.
        if (set(data) != {'collection', 'execution'}
                or not isinstance(data['collection'], dict)
                or not isinstance(data['execution'], dict)):
            raise InputError('创建执行需要采集范围及明确的执行确认。')
        collection, execution = dict(data['collection']), data['execution']
        if ('api_key' in collection or execution.get('consent') is not True
                or set(execution) - {'consent', 'api_key', 'without_token'}
                or ('without_token' in execution and type(execution['without_token']) is not bool)):
            raise InputError('凭据只可用于当前执行，并须明确确认；不会保存到批次。')
        mode = collection.get('mode', 'search')
        key = text_field(execution, 'api_key', limit=1000).strip()
        if mode not in {'search', 'feed'} and 'api_key' in execution:
            raise InputError('当前采集路线不接受 API 凭据。')
        if mode == 'feed' and not key and execution.get('without_token') is not True:
            raise InputError('没有填写数据源 Token，请明确确认接口允许无 Token 访问。')
        preview_data = dict(collection)
        if mode in {'search', 'feed'}:
            preview_data['api_key'] = key
        preview = self.application.guidance.preview(preview_data)
        if not preview['ready']:
            raise CollectionPreflightRejected(preview)
        # Collector still owns all original rights, source, scope and budget
        # checks. It never receives the execution-only credential.
        task = self.application.collector.start(collection)
        request = {'id': task['id'], 'consent': True}
        if mode in {'search', 'feed'}:
            request['api_key'] = key
        try:
            runner = self.application.collection_runner.start(request)
        except Exception:
            # start() can fail while returning state AFTER the worker started.
            # Preserve identity, expose uncertainty and never replay or delete.
            return {'accepted': None, 'created': True, 'code': 'start_unconfirmed',
                'task': task, 'runner': None,
                'message': '批次已创建，但后台接纳结果未确认；请先核对该批次与后台状态，勿重复创建或自动重试。'}
        return {'accepted': True, 'created': True, 'code': 'accepted',
                'task': task, 'runner': runner}
