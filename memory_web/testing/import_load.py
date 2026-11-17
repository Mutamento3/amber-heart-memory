"""Explicit synthetic load server/client; never used by the normal app factory."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import http.client
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import resource
import time
from zipfile import ZipFile, ZIP_DEFLATED

ROOT=Path('/var/lib/memory-admin')
ORIGIN='http://localhost:8080'


def metrics():
    group=Path('/sys/fs/cgroup')
    stat=dict(line.split() for line in (group/'memory.stat').read_text().splitlines())
    current=int((group/'memory.current').read_text())
    return dict(rss_peak_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
        cgroup_peak_bytes=int((group/'memory.peak').read_text()),
        working_set_bytes=current-int(stat.get('inactive_file',0)),
        anonymous_bytes=int(stat.get('anon',0)))


def server():
    from memory_core.config import Settings
    from memory_core.install import install
    from memory_core.space import Participant, SpaceConfiguration
    from memory_core.tests.support import DatabaseTestCase
    from memory_web.app import create_app
    from memory_web.config import WebSettings
    from memory_web.extraction_model import ExtractionModel
    from memory_web.install import install_web
    from memory_web.password import write_password
    from memory_web.testing.import_model import SyntheticImportFixed
    import uvicorn

    case=DatabaseTestCase()
    case.setUp()
    install(case.owner,Settings(3))
    install_web(case.owner)
    SpaceConfiguration(case.owner).configure('load',Participant('合成读者'),Participant('合成助手'))
    password=ROOT/'password.hash'
    write_password(password,'synthetic load password')
    settings=WebSettings(case.app,case.owner,ORIGIN,password,local_http=True)
    model=ExtractionModel(replace(settings,extraction_model='synthetic-load-model'),
        fixed_models=SyntheticImportFixed(record_calls=False))
    app=create_app(settings,extraction_models=model)

    @app.get('/__load/metrics')
    async def read_metrics():
        return metrics()

    try:
        uvicorn.run(app,host='0.0.0.0',port=8080,access_log=False)
    finally:
        case.tearDown()


def generate(source):
    start=datetime(2020,1,1,12,tzinfo=timezone.utc)
    path=ROOT/(source+'.zip')
    raw_size=0
    count=256 if source=='chatgpt' else 8192
    # A large conversation is intentionally much larger than a Takeout entry.
    text='合成原话：我们约好一起学习陶艺，记下每一天值得收藏的片刻。'*(6400 if source=='chatgpt' else 230)
    filename='conversations.json' if source=='chatgpt' else 'MyActivity.json'
    with ZipFile(path,'w',ZIP_DEFLATED) as archive:
        with archive.open(filename,'w') as output:
            output.write(b'[')
            raw_size+=1
            for index in range(count):
                day=index if source=='chatgpt' else (index*37)%256
                instant=start+timedelta(days=day,seconds=index%32)
                if source=='chatgpt':
                    record=dict(id='synthetic-load-'+str(index),title='合成压测对话',current_node='a',mapping={
                        'u':dict(parent=None,message=dict(author=dict(role='user'),create_time=instant.timestamp(),
                            content=dict(content_type='text',parts=[text]))),
                        'a':dict(parent='u',message=dict(author=dict(role='assistant'),create_time=instant.timestamp()+1,
                            content=dict(content_type='text',parts=[text])))})
                else:
                    record=dict(header='Gemini Apps',title='Prompted '+text,time=instant.isoformat(),
                        safeHtmlItem=[dict(html='<p>'+text+'</p>')],
                        subtitles=[dict(name='Synthetic Gem was used in this chat. Manage your Gems.',value='METADATA_ONLY')])
                encoded=json.dumps(record,ensure_ascii=False).encode()
                if index:
                    output.write(b',')
                    raw_size+=1
                output.write(encoded)
                raw_size+=len(encoded)
            output.write(b']')
            raw_size+=1
    return dict(source=source,path=str(path),json_bytes=raw_size,upload_bytes=path.stat().st_size,records=count)


def client():
    cookie=csrf=''
    def request(path,*,body=None,file=None):
        connection=http.client.HTTPConnection('load-app',8080,timeout=600)
        headers={'Host':'localhost:8080','Origin':ORIGIN,'Cookie':cookie,'X-CSRF-Token':csrf}
        if file:
            headers.update({'Content-Type':'application/octet-stream','Content-Length':str(file.stat().st_size)})
            with file.open('rb') as content:
                connection.request('POST',path,body=content,headers=headers)
        elif body is not None:
            headers['Content-Type']='application/json'
            connection.request('POST',path,body=json.dumps(body),headers=headers)
        else:
            connection.request('GET',path,headers=headers)
        response=connection.getresponse()
        value=json.loads(response.read())
        saved_cookie=response.getheader('Set-Cookie')
        status=response.status
        connection.close()
        if status!=200:
            raise RuntimeError('synthetic load request failed: '+str(status)+' '+str(value))
        return value,saved_cookie

    for _ in range(180):
        try:
            login,header=request('/api/login',body=dict(password='synthetic load password'))
            jar=SimpleCookie(header)
            cookie='; '.join(key+'='+item.value for key,item in jar.items())
            csrf=login['csrf']
            break
        except (OSError,http.client.HTTPException):
            time.sleep(1)
    else:
        raise RuntimeError('synthetic load server did not become ready')
    report=dict(baseline=request('/__load/metrics')[0],cases=[])
    with ThreadPoolExecutor(max_workers=1) as pool:
        for source in ('chatgpt','gemini'):
            sample=json.loads((ROOT/(source+'.json')).read_text())
            filename=source+'.zip'
            started=time.monotonic()
            future=pool.submit(request,'/api/spaces/load/imports/upload?source='+source+'&filename='+filename,
                file=Path(sample['path']))
            peaks={}
            def observe():
                value=request('/__load/metrics')[0]
                for key,number in value.items():
                    peaks[key]=max(peaks.get(key,0),number)
            while not future.done():
                observe()
                time.sleep(.2)
            job=future.result()[0]
            observe()
            endpoint='/api/spaces/load/imports/'+job['id']
            plan=request(endpoint+'/preview',body={})[0]
            request(endpoint+'/confirm',body={})
            while True:
                progress=request(endpoint)[0]
                observe()
                if progress['status']=='failed':
                    raise RuntimeError('synthetic load job failed: '+str(progress['error_code']))
                if progress['status']=='completed':
                    break
                if time.monotonic()-started>1800:
                    raise RuntimeError('synthetic load timed out')
                time.sleep(.2)
            assert progress['progress']['processed']==plan['material_count']
            result=dict(sample,preview=dict(days=plan['day_count'],messages=plan['message_count']),
                processed=progress['progress']['processed'],model_calls=progress['model_calls'],
                duration_seconds=round(time.monotonic()-started,2),peaks=peaks)
            report['cases'].append(result)
            (ROOT/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
            print(json.dumps(result,ensure_ascii=False),flush=True)
    print('Synthetic load completed',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',choices=('server','generate','client'))
    args=parser.parse_args()
    if args.mode=='server':
        server()
    elif args.mode=='generate':
        ROOT.mkdir(parents=True,exist_ok=True)
        for source in ('chatgpt','gemini'):
            result=generate(source)
            (ROOT/(source+'.json')).write_text(json.dumps(result))
            print(json.dumps(result),flush=True)
    else:
        client()
