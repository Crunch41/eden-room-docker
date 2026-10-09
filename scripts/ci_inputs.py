#!/usr/bin/env python3
"""Cheap input probe; only publication may advance the durable build record."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request

STATE = '.maintenance/last-build.json'

def command(args):
    return subprocess.check_output(args, text=True, timeout=90).strip()

def source_id(markers):
    rows=command(['git','ls-tree','-r','HEAD']).splitlines()
    ignored={STATE,*markers}
    rows=[line for line in rows if line.split('\t',1)[-1] not in ignored]
    return hashlib.sha256('\n'.join(rows).encode()).hexdigest()

def base_images(source):
    stages=set(); result=[]
    for line in source.splitlines():
        match=re.match(r'^FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?',line,re.I)
        if not match:continue
        ref,stage=match.groups()
        if '$' in ref:raise ValueError('Unresolved Docker FROM variable requires explicit input support')
        if ref not in stages and ref != 'scratch':result.append(ref)
        if stage:stages.add(stage)
    return sorted(set(result))

def resolve_image(ref):
    if '@sha256:' in ref:return ref.split('@',1)[1]
    # Read the registry's manifest digest without downloading image layers.
    output=command(['docker','buildx','imagetools','inspect',ref])
    match=re.search(r'^Digest:\s+(sha256:[a-f0-9]{64})\s*$',output,re.M)
    if not match:raise ValueError('Registry returned no immutable manifest digest')
    return match.group(1)

def decision(current, previous, event, now):
    reasons=[]
    if event != 'schedule':reasons.append('explicit push or manual run')
    if not previous:reasons.append('no successful publication record')
    for key in ['upstreams','bases','source']:
        if current.get(key) != previous.get(key):reasons.append(key+' changed')
    try:
        age=now-dt.datetime.fromisoformat(previous['published_at'])
        refresh=age >= dt.timedelta(days=6)  # weekly runs start hours apart
    except (KeyError,ValueError,TypeError):refresh=True
    if refresh:reasons.append('weekly OS/package refresh due')
    return bool(reasons),refresh,reasons

def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['probe','record','pin']);p.add_argument('--snapshot',required=True);p.add_argument('--upstream',action='append',default=[]);p.add_argument('--dockerfile');a=p.parse_args()
    snap=Path(a.snapshot)
    if a.action=='record':
        data=json.loads(snap.read_text());data['published_at']=dt.datetime.now(dt.timezone.utc).isoformat()
        target=Path(STATE);target.parent.mkdir(exist_ok=True);target.write_text(json.dumps(data,indent=2)+'\n');return
    if a.action=='pin':
        data=json.loads(snap.read_text());path=Path(a.dockerfile);source=path.read_text()
        for ref in base_images(source):
            digest=data['bases'][ref]
            if '@' not in ref:source=re.sub(r'(?m)^(FROM\s+(?:--platform=\S+\s+)?)'+re.escape(ref)+r'(?=\s|$)',lambda m:m[1]+ref+'@'+digest,source,flags=re.I)
        path.write_text(source);return
    config=json.loads(Path('.maintenance/inputs.json').read_text())
    selected=dict(x.split('=',1) for x in a.upstream)
    for name,item in config['upstreams'].items():
        if name not in selected:
            selected[name]=command(['git','ls-remote',item['url'],item['ref']]).split()[0]
        if not re.fullmatch('[a-f0-9]{40}',selected[name]):raise ValueError('Invalid upstream revision')
    refs=set()
    for item in config.get('dockerfiles',[]):
        if 'path' in item:source=Path(item['path']).read_text()
        else:
            url=f'https://raw.githubusercontent.com/{item["repo"]}/{selected[item["upstream"]]}/{item["file"]}'
            with urllib.request.urlopen(url,timeout=20) as response:source=response.read(1024*1024).decode()
        refs.update(base_images(source))
    current={'upstreams':selected,'bases':{ref:resolve_image(ref) for ref in sorted(refs)},'source':source_id(config.get('markers',[]))}
    previous=json.loads(Path(STATE).read_text()) if Path(STATE).exists() else {}
    build,refresh,reasons=decision(current,previous,os.environ.get('GITHUB_EVENT_NAME','workflow_dispatch'),dt.datetime.now(dt.timezone.utc))
    snap.write_text(json.dumps(current,indent=2)+'\n')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'],'a') as output:output.write(f'should_build={str(build).lower()}\nrefresh={str(refresh).lower()}\n')
    print(json.dumps({'should_build':build,'refresh':refresh,'reasons':reasons,'upstreams':selected,'bases':current['bases']}))

if __name__=='__main__':main()
