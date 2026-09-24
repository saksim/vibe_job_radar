"""Entry point bundled with Python, browser driver and application resources."""
from multiprocessing import freeze_support
import sys
from vibe_job_radar.runtime import prepare_portable
from vibe_job_radar.workbench import main


if __name__=='__main__':
    freeze_support()
    prepare_portable()
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8',errors='replace')
    raise SystemExit(main())
