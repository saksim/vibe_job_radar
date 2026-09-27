"""Generate/check the documentation table from the same offline UI catalogue."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from vibe_job_radar.acquisition_status import markdown_table

START = '<!-- acquisition-status:start -->'
END = '<!-- acquisition-status:end -->'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args()
    path = ROOT/'docs/ACQUISITION_CAPABILITIES.md'
    source = path.read_text(encoding='utf-8')
    before, middle = source.split(START)
    _, after = middle.split(END)
    expected = before+START+'\n'+markdown_table()+'\n'+END+after
    if args.write:
        path.write_text(expected, encoding='utf-8')
    elif source != expected:
        raise SystemExit('Capability documentation differs from registry; run with --write and review the evidence.')
    print('Acquisition status catalogue and documentation agree; no network calls.')


if __name__ == '__main__':
    main()
