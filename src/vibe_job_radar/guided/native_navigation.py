"""Main-document identity from owned browser events, never page HTML or values."""
from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class NativeDocument:
    target: str
    frame: str
    sequence: int
    document_url: str = field(repr=False)
    url: str = field(repr=False)


def observe_document(previous, target, method, event):
    if method == 'Page.frameNavigated':
        frame = event['frame']
        if frame.get('parentId'):
            return previous
        ident, url = frame['id'], frame['url']
        if not isinstance(ident, str) or not ident or not isinstance(url, str):
            raise ValueError('invalid browser frame observation')
        sequence = previous.sequence + 1 if previous is not None else 1
        return NativeDocument(target, ident, sequence, url, url)
    if method == 'Page.navigatedWithinDocument':
        if previous is None or event.get('frameId') != previous.frame:
            return previous
        url = event['url']
        if not isinstance(url, str):
            raise ValueError('invalid browser history observation')
        return replace(previous, sequence=previous.sequence+1, url=url)
    return previous
