"""Optional HTTP validator bound to a full v2 catalog; v2 bytes stay rollback-safe."""
import json
import math

from .network import USER_AGENT, valid_etag
from .public_contract import ContractError
from .utils import atomic_json
from .workspace import InputError

REQUEST_FORMAT='anonymous-json-identity-v1:'+USER_AGENT
FIELDS={'schema_version','api','revision','observed_at','checked_at','etag','request_format'}


class CatalogValidation:
    def __init__(self,root,board):
        self.path=root/f'{board.token}-validation-v1.json'
        self.api=board.api_url

    def _safe_path(self):
        if self.path.is_symlink():raise InputError('目录版本校验文件不能使用符号链接。')

    def read(self,snapshot,now):
        self._safe_path()
        if not self.path.exists():return None
        if self.path.stat().st_size>8192:raise ContractError('public_validation_invalid')
        try:
            value=json.loads(self.path.read_text(encoding='utf-8'))
            if (not isinstance(value,dict) or set(value)!=FIELDS or type(value['schema_version']) is not int
                    or value['schema_version']!=1 or value['api']!=self.api
                    or not isinstance(value['revision'],str)
                    or any(type(value[key]) not in (int,float) or not math.isfinite(value[key])
                           for key in ('observed_at','checked_at'))
                    or not value['observed_at']<=value['checked_at']<=now
                    or (value['etag'] is not None and not valid_etag(value['etag']))
                    or (value['etag'] is None and value['checked_at']!=value['observed_at'])):
                raise ValueError
        except (ValueError,TypeError,KeyError) as exc:raise ContractError('public_validation_invalid') from exc
        # A complete 200 may have committed before its optional metadata. Old
        # metadata never authorizes another revision or a changed request format.
        if (snapshot is None or value['revision']!=snapshot['revision']
                or value['observed_at']!=snapshot['observed_at'] or value['request_format']!=REQUEST_FORMAT):
            return None
        return value

    def save(self,snapshot,etag,checked_at):
        self._safe_path()
        atomic_json(self.path,{'schema_version':1,'api':self.api,'revision':snapshot['revision'],
            'observed_at':snapshot['observed_at'],'checked_at':checked_at,'etag':etag,'request_format':REQUEST_FORMAT})
