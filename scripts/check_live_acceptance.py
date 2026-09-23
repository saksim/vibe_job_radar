"""Prepare/capture/verify local acceptance metadata; every command is offline."""
import argparse
import json
import platform
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from vibe_job_radar.live_acceptance import AcceptanceLedger
from vibe_job_radar.qualification import fingerprint
from vibe_job_radar.workspace import InputError


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',type=Path,required=True)
    commands=parser.add_subparsers(dest='command',required=True)
    plan=commands.add_parser('plan',help='Save scope before creating collection tasks; no source request')
    plan.add_argument('--keyword',required=True)
    plan.add_argument('--role',action='append',required=True)
    plan.add_argument('--search-url',default='')
    plan.add_argument('--backend',choices=['bridge','native'],default='native')
    plan.add_argument('--selection-rule',required=True)
    plan.add_argument('--timezone',required=True,help='Actual local IANA zone, e.g. America/Denver or Asia/Shanghai')
    plan.add_argument('--browser',choices=['msedge','bundled'],required=True)
    plan.add_argument('--browser-version',default='unknown',help='Observed browser version; unknown remains an evidence gap')
    plan.add_argument('--network-mode',choices=['direct','system','static_http','static_socks5','unknown'],required=True)
    plan.add_argument('--yes',action='store_true',help='Confirm recording this local scope; does not authorize new network requests')
    for name in ('capture','verify'):
        command=commands.add_parser(name)
        command.add_argument('--plan',required=True)
    args=parser.parse_args()
    try:
        ledger=AcceptanceLedger(args.workspace)
        if args.command=='plan':
            if not args.yes: raise InputError('核对范围后加 --yes 保存计划；此操作不联网。')
            revision=subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip()
            dirty=subprocess.check_output(['git','-C',str(ROOT),'status','--porcelain','--untracked-files=normal'],text=True).strip()
            if dirty: raise InputError('验收需使用已提交的源码；请先完成开发提交，不能把修改前的 SHA 当作实际源码。')
            data=ledger.create({'keyword':args.keyword,'roles':args.role,'search_url':args.search_url,
                'backend':args.backend,'selection_rule':args.selection_rule,'timezone':args.timezone,
                'source_revision':revision,'source_sha256':fingerprint(ROOT)['sha256'],
                'browser':args.browser,'browser_version':args.browser_version,'planner_os':platform.platform(),
                'network_mode':args.network_mode,'consent':True})
            print(json.dumps({'plan_id':data['id'],'created_at':data['created_at'],
                'message':'计划已在本机保存；尚未访问平台，环境为声明值，正式验收仍须实证。'},ensure_ascii=False))
            return 0
        result=ledger.capture(args.plan) if args.command=='capture' else ledger.verify(args.plan)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 2 if result['gaps'] else 0
    except InputError as exc:
        print(json.dumps({'status':'invalid_evidence','message':str(exc)},ensure_ascii=False))
        return 1
    except (ValueError,KeyError,TypeError,OSError,subprocess.SubprocessError):
        # Never echo task contents, exception arguments, query URLs or local
        # account-related values. Detailed evidence remains local for review.
        print(json.dumps({'status':'invalid_evidence','message':'验收输入或源码状态不完整；请检查本机计划、原任务与原报告。未修改业务数据。'},ensure_ascii=False))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
