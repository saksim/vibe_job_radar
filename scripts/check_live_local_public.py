"""Explicit one-GET smoke of local direct acquisition, no deployed product service.

Only metadata/counts are saved. Full public source text and cursor keys are kept
in a temporary workspace and are not uploaded as CI artifacts.
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--source',choices=('anthropic','cloudflare'),default='anthropic')
    args=parser.parse_args()
    if not args.live:
        parser.error('Pass --live to permit one fixed public job-board GET.')
    from vibe_job_radar.local_public import LocalPublicDataClient
    from vibe_job_radar.public_boards import ANTHROPIC, CLOUDFLARE
    from vibe_job_radar.public_contract import PublicQuery
    from vibe_job_radar.workspace import Workspace
    output=ROOT/'live-evidence';output.mkdir(exist_ok=True)
    board={'anthropic':ANTHROPIC,'cloudflare':CLOUDFLARE}[args.source]
    result={'success':False,'scope':'one fixed Greenhouse board; not BOSS/Liepin/51job certification',
            'api':board.api_url,'source':board.source.key,'execution_mode':'local_direct','own_service_required':False}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            client=LocalPublicDataClient(Workspace(tmp))
            batch=client.search(PublicQuery(board.company,(board.source.key,),limit=1),consent=True)
            if not batch['response']['jobs']:
                raise ValueError('empty_public_board')
            result.update(success=True,board_jobs=batch['available_jobs'],matching_jobs=batch['matching_jobs'],
                          returned_jobs=batch['returned_jobs'],network_requests=batch['network_requests'],
                          first_id=batch['response']['jobs'][0]['id'],
                          first_text_characters=len(batch['response']['jobs'][0]['text']),
                          collected_at=batch['response']['jobs'][0]['collected_at'])
    except Exception as exc:
        # Provider messages can contain query data. Emit only fixed exception codes.
        result.update(error_type=type(exc).__name__,code=getattr(exc,'code','live_local_public_failed'))
    filename='local-public.json' if board==ANTHROPIC else 'local-public-cloudflare.json'
    (output/filename).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True,indent=2))
    return 0 if result['success'] else 1


if __name__=='__main__':raise SystemExit(main())
