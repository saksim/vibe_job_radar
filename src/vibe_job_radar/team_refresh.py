"""One bounded refresh of a fixed source after an operator approves distribution."""
from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager

from .collection import writer_lock
from .local_public import LocalPublicDataClient
from .public_boards import BOARDS
from .public_contract import PublicQuery
from .team_catalog import TeamCatalog,read_snapshot
from .team_store import TeamError,safe_path
from .workspace import Workspace


@contextmanager
def refresh_operation(root):
    # Separate from the HTTP server's lifetime lock and the client's internal
    # workspace lock: serialize a complete fetch+publish with a complete backup.
    directory=safe_path(Path(root)/'.refresh-operation')
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    with writer_lock(directory):yield


def refresh_approved(root,approval,source,*,client_factory=LocalPublicDataClient):
    from .team_store import ensure_recovery_complete
    ensure_recovery_complete(root)
    registry,seed,_observations=read_snapshot(approval)
    if seed or source not in registry or source not in BOARDS:
        raise TeamError('team_refresh_approval_invalid')
    board=BOARDS[source];approved=registry[source]
    if set(approved.domains)!=set(board.source.domains):raise TeamError('team_refresh_approval_invalid')
    # The operator's explicit source contract is distinct from local access.
    # This path never promotes built-in local-only registrations automatically.
    with refresh_operation(root):
        workspace=Workspace(Path(root)/'refresh-workspace')
        client=client_factory(workspace)
        try:
            outcome=client.search(PublicQuery('*',(source,),limit=1),consent=True)
            if outcome.get('stale') or outcome.get('refresh_error'):raise TeamError('team_refresh_not_confirmed',503)
            with writer_lock(client.root):
                snapshot=client._cached(client.clock(),board)
                if snapshot is None:raise TeamError('team_refresh_not_confirmed',503)
                jobs=snapshot['jobs']
            result=TeamCatalog(root,clock=client.clock).publish({source:approved},jobs,observed_at={source:outcome['checked_at']})
            return {**result,'source':source,'source_requests':outcome['network_requests'],
                    'source_observed_at':snapshot['observed_at'],'jobs':len(jobs)}
        finally:workspace.close_network()
