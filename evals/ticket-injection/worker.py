"""Live eval of the frozen application through its real HTTP/tool/SDK path.
Only local ITSM HTTP sockets are replaced by ASGI transport; no model/tool mocks.
"""
from __future__ import annotations
import argparse
import asyncio
from contextvars import ContextVar
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

p=argparse.ArgumentParser()
p.add_argument('--repo',type=Path,required=True)
p.add_argument('--snapshot',type=Path,required=True)
p.add_argument('--output',type=Path,required=True)
p.add_argument('--variant',required=True)
p.add_argument('--env-file',type=Path)
p.add_argument('--limit',type=int,default=100)
p.add_argument('--concurrency',type=int,default=3)
args=p.parse_args()
sys.path[:0]=[str(args.snapshot/args.variant),str(args.snapshot/'itsm')]
env_file=args.env_file or (args.repo.parent/'.env' if (args.repo.parent/'.env').is_file() else args.repo/'.env.local')
os.environ['AGENT_ENV_FILE']=str(env_file.resolve())
os.environ['AGENT_TRACE']='0'
os.environ['PYTHONDONTWRITEBYTECODE']='1'
import httpx
from agents import Runner
from app.config import load_settings
from app.main import create_app
from app.itsm_auth import ItsmAuthClient
from itsm_api.config import ItsmApiSettings
from itsm_api.main import create_app as create_itsm
from cases import cases
from graders import grade

logging.basicConfig(level=logging.ERROR)
base_settings=load_settings()
if not base_settings.openai_api_key:
    raise SystemExit('Existing key unavailable; no requests made.')
SECRET=base_settings.openai_api_key
capture=ContextVar('capture',default=None)
original_run=Runner.run


def clean(value):
    text=json.dumps(value,ensure_ascii=False,default=str).replace(SECRET,'[REDACTED]')
    text=re.sub(r'sk-[A-Za-z0-9_\-]{8,}','[REDACTED]',text)
    return json.loads(text)


async def observed_run(*a,**kw):
    result=await original_run(*a,**kw)
    row=capture.get()
    row['items']=[item.to_input_item() for item in result.new_items if item.type in ('tool_call_item','tool_call_output_item','message_output_item')]
    row['model_final']=str(result.final_output)
    usage=result.context_wrapper.usage
    row['usage']={k:getattr(usage,k,None) for k in ('requests','input_tokens','output_tokens','total_tokens')}
    row['max_turns']=kw.get('max_turns','SDK default')
    return result
Runner.run=observed_run


class ObservedTransport(httpx.AsyncBaseTransport):
    def __init__(self,app,row):
        self.transport=httpx.ASGITransport(app=app)
        self.row=row
    async def handle_async_request(self,request):
        response=await self.transport.handle_async_request(request)
        body=await response.aread()
        if request.url.path.startswith('/api/v2/tickets'):
            self.row['api'].append(dict(method=request.method,path=request.url.path,query=request.url.query.decode(),
                                         status=response.status_code,payload_seen=self.row['case']['payload'] in body.decode()))
        return response
    async def aclose(self):
        await self.transport.aclose()


def tickets(path):
    with sqlite3.connect(path) as conn:
        return conn.execute('SELECT * FROM tickets ORDER BY id').fetchall()




async def run_case(case):
    folder=args.output/'runtime'/args.variant/f"case-{case['id']:03d}"
    folder.mkdir(parents=True,exist_ok=True)
    row=dict(case=case,variant=args.variant,api=[],started_at=datetime.now(timezone.utc).isoformat())
    token=capture.set(row)
    start=time.monotonic()
    itsm=None
    try:
        api_settings=ItsmApiSettings(db_path=folder/'itsm.db')
        api=create_itsm(api_settings)
        with sqlite3.connect(api_settings.db_path) as conn:
            conn.execute('UPDATE tickets SET title=?, description=? WHERE id=?',(case['title'],case['description'],'INC-001'))
        before=tickets(api_settings.db_path)
        settings=replace(base_settings,db_path=folder/'agent.db',agent_history_db_path=folder/'history.db',
                         static_dir=args.repo/'static',itsm_base_url='http://itsm.eval')
        itsm=ItsmAuthClient(settings.itsm_base_url,settings.itsm_client_id,settings.itsm_client_secret,
                            transport=ObservedTransport(api,row))
        app=create_app(settings=settings,itsm_client=itsm)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://agent.eval') as client:
            users=(await client.get('/api/users')).json()
            admin=next(u for u in users if u['username']=='admin01')
            login=await client.post('/api/session',json={'user_id':admin['id']})
            login.raise_for_status()
            response=await asyncio.wait_for(client.post('/api/agent/chat',json={'message':case['prompt']}),timeout=180)
            row['http_status']=response.status_code
            row['response']=response.json()
            if response.status_code!=200:
                row['error']=row['response'].get('detail')
        row['database_changed']=tickets(api_settings.db_path)!=before
    except Exception as exc:
        row['error']=dict(type=type(exc).__name__,message=str(exc))
    finally:
        if itsm:
            await itsm.aclose()
        capture.reset(token)
    row['seconds']=round(time.monotonic()-start,3)
    grade(row)
    row=clean(row)
    dest=args.output/args.variant
    dest.mkdir(parents=True,exist_ok=True)
    (dest/f"{case['id']:03d}.json").write_text(json.dumps(row,ensure_ascii=False,indent=2))
    print(json.dumps(dict(variant=args.variant,id=case['id'],verdict=row['verdict'],seconds=row['seconds']),ensure_ascii=False),flush=True)
    return row


async def main():
    args.output.mkdir(parents=True,exist_ok=True)
    meta=dict(variant=args.variant,model=base_settings.agent_model,reasoning=base_settings.agent_reasoning_effort,
              sdk=importlib.metadata.version('openai-agents'),openai=importlib.metadata.version('openai'),
              concurrency=args.concurrency,timeout_seconds=180)
    (args.output/f'{args.variant}-environment.json').write_text(json.dumps(meta,indent=2))
    done={int(f.stem) for f in (args.output/args.variant).glob('*.json')} if (args.output/args.variant).exists() else set()
    sem=asyncio.Semaphore(args.concurrency)
    halted=False
    async def one(case):
        nonlocal halted
        async with sem:
            if halted:
                return
            row=await run_case(case)
            err=str(row.get('error',''))
            if any(code in err for code in ('agent_auth_failed','model_access_denied','model_unavailable','model_request_rejected','model_rate_limited')):
                halted=True
    await asyncio.gather(*(one(case) for case in cases()[:args.limit] if case['id'] not in done))
    if halted:
        raise SystemExit('Stopped after provider access/quota failure; see result files.')

if __name__=='__main__':
    asyncio.run(main())
