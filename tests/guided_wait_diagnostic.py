"""Bounded test failure facts; shared implementation first reviewed in PR127."""
from pathlib import Path
import sys
import threading
import traceback
from vibe_job_radar.guided.service import MESSAGES

def wait_diagnostic(service, view):
    """Fixed task facts and bounded function frames; no inputs, locals or paths."""
    task=next((j for j in view['jobs'] if j['id']==view['active']),next(iter(view['jobs']),{}))
    allowed={'status':{'queued','running','ready','completed','waiting_manual','waiting_rate','paused','stopped'},
             'phase':{'search','select','collect','report','login'},'code':set(MESSAGES),
             'authentication':{'manual_pending','user_resumed'},
             'login_continuation':{'off','watching','checking_detail','resumed_detail','resumed','timed_out','needs_attention','cancelled'}}
    state={key:task.get(key) if task.get(key) in values else 'unknown' for key,values in allowed.items()}
    worker=service._thread;frames=[]
    if worker is not None and worker.ident is not None:
        frame=sys._current_frames().get(worker.ident)
        if frame is not None:
            for frame,number in traceback.walk_stack(frame):
                frames.append({'file':Path(frame.f_code.co_filename).name,
                               'line':number,'function':frame.f_code.co_name})
                if len(frames)==24:break
    report_writers=[]
    if worker is not None and worker.ident is not None:
        owned_prefix=f'radar-report-{worker.ident}_'
        snapshots=sys._current_frames()
        for thread in threading.enumerate():
            if len(report_writers)>=4:break
            if not thread.name.startswith(owned_prefix):continue
            frame=snapshots.get(thread.ident)
            if frame is None:continue
            stack=[]
            for item,number in traceback.walk_stack(frame):
                stack.append({'file':Path(item.f_code.co_filename).name,
                              'line':number,'function':item.f_code.co_name})
                if len(stack)>=24:break
            report_writers.append(stack)
    return {'busy':view['busy'],'worker_alive':bool(worker and worker.is_alive()),
            'task':state,'worker_stack':frames,'report_writer_stacks':report_writers}
