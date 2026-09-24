"""Source entry for the independent local public-plan worker."""
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from vibe_job_radar.public_worker import main

if __name__=='__main__':raise SystemExit(main())
