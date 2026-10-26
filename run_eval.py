#!/usr/bin/env python3
"""Run 100 paired live injection cases against each agent (paid API calls)."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parent
EVAL=ROOT/'evals/ticket-injection'
sys.path.insert(0,str(EVAL))
from cases import cases


def prepare(output):
    snapshot=output/'snapshot'
    if snapshot.exists():
        manifest=json.loads((snapshot/'manifest.json').read_text())
        for label,info in manifest.items():
            module='itsm_api' if label=='itsm' else 'app'
            for filename,digest in info['files_sha256'].items():
                if hashlib.sha256((snapshot/label/module/filename).read_bytes()).hexdigest()!=digest:
                    raise RuntimeError('Snapshot changed; choose a new output directory.')
        if json.loads((output/'cases.json').read_text())!=cases():
            raise RuntimeError('Case matrix changed; choose a new output directory.')
        return
    output.mkdir(parents=True,exist_ok=True)
    manifest={}
    for label,name,module in [('baseline','itsm_dlc','app'),('harness','harness-1','app'),('itsm','itsm','itsm_api')]:
        dest=snapshot/label/module
        shutil.copytree(ROOT/name/module,dest,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        revision=subprocess.run(['git','-C',str(ROOT),'rev-parse','HEAD'],capture_output=True,text=True)
        manifest[label]={'repo':name,'head':revision.stdout.strip() or 'uncommitted',
                         'files_sha256':{str(p.relative_to(dest)):hashlib.sha256(p.read_bytes()).hexdigest() for p in dest.rglob('*.py')}}
    (snapshot/'manifest.json').write_text(json.dumps(manifest,indent=2))
    (output/'cases.json').write_text(json.dumps(cases(),ensure_ascii=False,indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=EVAL/'results'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    parser.add_argument('--concurrency',type=int,default=3)
    parser.add_argument('--env-file',type=Path,default=ROOT/'.env')
    args=parser.parse_args()
    if args.concurrency<1:
        parser.error('concurrency must be positive')
    if not args.env_file.is_file():
        parser.error('Create .env with OPENAI_API_KEY first (see README).')
    output=args.output.resolve()
    prepare(output)
    processes=[]
    try:
        for variant,name in [('baseline','itsm_dlc'),('harness','harness-1')]:
            cmd=[str(ROOT/name/'.venv/bin/python'),str(EVAL/'worker.py'),'--repo',str(ROOT/name),
                 '--snapshot',str(output/'snapshot'),'--output',str(output),'--variant',variant,
                 '--limit','100','--concurrency',str(args.concurrency),'--env-file',str(args.env_file.resolve())]
            processes.append(subprocess.Popen(cmd,cwd=ROOT))
        codes=[proc.wait() for proc in processes]
        if any(codes):
            raise SystemExit(f'Evaluation incomplete; results preserved in {output}')
        subprocess.run([sys.executable,str(EVAL/'summarize.py'),str(output)],check=True)
        print(f'Report: {output / "report.md"}')
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
        for proc in processes:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


if __name__=='__main__':
    main()
