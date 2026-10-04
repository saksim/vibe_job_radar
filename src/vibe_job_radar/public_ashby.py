"""Strict listed-job adapter for the reviewed anonymous Ashby public board."""
from .public_contract import ContractError, text


def parse_ashby_board(payload, collected_at, board):
    if (not isinstance(payload, dict) or payload.get('apiVersion') != '1'
            or not isinstance(payload.get('jobs'), list) or len(payload['jobs']) > 10000):
        raise ContractError('local_public_board_incomplete')
    jobs, identities = [], set()
    for raw in payload['jobs']:
        if not isinstance(raw, dict) or type(raw.get('isListed')) is not bool:
            raise ContractError('local_public_job_invalid')
        # These are not public board listings. Do not retain their IDs, URLs,
        # descriptions or application metadata in caches, changes or reports.
        if not raw['isListed']:
            continue
        ident = raw.get('id')
        if not board.valid_id(ident):
            raise ContractError('local_public_job_invalid')
        if ident in identities:
            raise ContractError('public_duplicate_result')
        identities.add(ident)
        url = raw.get('jobUrl')
        board.accepts_job(url, ident)
        title = text(raw.get('title'), 2000)
        body = text(raw.get('descriptionPlain'), 150000)
        if len(body.strip()) < 100:
            raise ContractError('public_content_incomplete')
        primary = text(raw.get('location'), 2000, empty=True)
        additional = raw.get('secondaryLocations', [])
        if not isinstance(additional, list) or len(additional) > 50:
            raise ContractError('local_public_job_invalid')
        locations = [primary] if primary else []
        for location in additional:
            if not isinstance(location, dict):
                raise ContractError('local_public_job_invalid')
            name = text(location.get('location'), 2000)
            if name not in locations:
                locations.append(name)
        location = text('; '.join(locations), 2000, empty=True)
        remote = raw.get('isRemote')
        if remote is not None and type(remote) is not bool:
            raise ContractError('local_public_job_invalid')
        jobs.append({'id': ident, 'source': board.source.key, 'title': title,
            'company': board.company, 'location': location, 'remote': remote,
            'text': body, 'url': url, 'final_url': url, 'collected_at': collected_at,
            'completeness': 'full_text', 'adapter_version': board.adapter_version})
    jobs.sort(key=lambda row: row['id'])
    return jobs
