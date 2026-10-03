"""Loopback raw HTTP provider; no response bytes for connection_error."""
import asyncio
import hashlib
import json
from collections import Counter, deque
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from c3_inputs import SCENARIOS
import provider_stub as stub

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def fixed_request(data):
    if not isinstance(data,dict) or data.get('method') != 'tools/call':
        return None
    params = data.get('params',{})
    args = params.get('arguments',{})
    for name,fixed in SCENARIOS.items():
        if params.get('name')==fixed['tool'] and args.get('operation_id')==fixed['operation_id'] and isinstance(args.get('content'),str) and digest(args['content'])==fixed['sha256']:
            return name
    return None

class Controller:
    def __init__(self, ob):
        self.ob = ob
        self.armed = None
        self.generation = 0
        self.counts = Counter()
        self.events = deque(maxlen=1024)
        self.gate = asyncio.Event()
        self.deadline = None
        self.expired = False
        self.completed = set()
        self.tasks = set()
    def event(self, event, scenario=None, generation=None):
        self.events.append(dict(event=event,scenario=scenario,generation=generation,
                                monotonic=round(asyncio.get_running_loop().time(),3)))
    def projection(self):
        receipts = {}
        for name,fixed in SCENARIOS.items():
            row = self.ob.bucket_mgr.inspect_trace_request(fixed['operation_id'])
            if row:
                receipts[name] = dict(status=row['status'],result_sha256=digest(row.get('result_text') or ''))
                if row['status']=='completed' and name not in self.completed:
                    self.completed.add(name)
                    self.event('completed',name,self.generation if name==self.armed else None)
        return dict(armed=self.armed,generation=self.generation,counts=dict(self.counts),
                    events=list(self.events),receipts=receipts,gate_expired=self.expired,
                    waiting_seconds_remaining=max(0,round(self.deadline-asyncio.get_running_loop().time(),2)) if self.deadline else None)
    async def control(self, request):
        if request.client.host not in ('127.0.0.1','::1') or request.headers.get('origin'):
            return JSONResponse({'error':'loopback_required'},status_code=403)
        action = request.path_params['action']
        if action=='status' and request.method=='GET':
            return JSONResponse(self.projection())
        if request.method!='POST' or request.headers.get('content-type','').split(';')[0]!='application/json':
            return JSONResponse({'error':'method_or_type_refused'},status_code=405)
        raw = await request.body()
        if len(raw)>256:
            return JSONResponse({'error':'control_size_refused'},status_code=413)
        try:
            data = json.loads(raw)
        except ValueError:
            return JSONResponse({'error':'invalid_control'},status_code=400)
        if not isinstance(data,dict):
            return JSONResponse({'error':'invalid_control'},status_code=400)
        if action=='arm':
            name = data.get('scenario')
            if name not in SCENARIOS or self.armed:
                return JSONResponse({'error':'scenario_or_busy'},status_code=409)
            self.armed = name
            self.generation += 1
            self.gate.clear()
            self.deadline = None
            self.expired = False
            self.event('armed',name,self.generation)
        elif action=='release':
            now = asyncio.get_running_loop().time()
            observed = {e['event'] for e in self.events if e['scenario']=='disconnect' and e['generation']==self.generation}
            if self.armed!='disconnect' or self.deadline is None or now>=self.deadline or self.expired or not {'waiting','http.disconnect'} <= observed:
                return JSONResponse({'error':'gate_not_releasable'},status_code=409)
            self.gate.set()
            self.event('released',self.armed,self.generation)
        elif action=='disarm':
            row = self.ob.bucket_mgr.inspect_trace_request(SCENARIOS[self.armed]['operation_id']) if self.armed else None
            if self.tasks or (row and row['status']!='completed'):
                return JSONResponse({'error':'scenario_busy'},status_code=409)
            self.event('disarmed',self.armed,self.generation)
            self.armed = None
        else:
            return JSONResponse({'error':'action_refused'},status_code=404)
        return JSONResponse(self.projection())
    def app(self):
        return Starlette(routes=[Route('/{action}',self.control,methods=['GET','POST'])])
    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        name,generation = self.armed,self.generation
        try:
            async with asyncio.timeout(50):
                header = await reader.readuntil(b'\r\n\r\n')
                lines = header.decode('ascii').split('\r\n')
                method,path,_ = lines[0].split(' ')
                headers = {k.lower():v.strip() for k,v in (line.split(':',1) for line in lines[1:] if ':' in line)}
                length = int(headers.get('content-length','0'))
                if not 0 <= length <= 1024*1024:
                    return
                raw = await reader.readexactly(length)
                data = json.loads(raw)
                messages = data.get('messages',[])
                system = messages[0].get('content','') if messages else ''
                body = messages[-1].get('content','') if messages else ''
                kind = 'digest' if '日记整理专家' in system else 'analyze' if '内容分析器' in system else 'other'
                fixed = SCENARIOS.get(name)
                match = fixed and method=='POST' and path==fixed['path'] and kind==fixed['prompt'] and digest(body)==fixed['sha256']
                if match:
                    self.counts[name] += 1
                    self.event('provider_attempt',name,generation)
                    if name=='connection':
                        self.event('tcp_closed_without_http',name,generation)
                        return
                    if name=='disconnect':
                        # One deadline across all attempts; timeout never starts another window.
                        if self.deadline is None:
                            self.deadline = asyncio.get_running_loop().time()+45
                            self.event('waiting',name,generation)
                        remaining = self.deadline-asyncio.get_running_loop().time()
                        if not self.expired and remaining>0:
                            try:
                                await asyncio.wait_for(self.gate.wait(),remaining)
                            except TimeoutError:
                                self.expired = True
                                self.event('gate_timeout_unaccepted',name,generation)
                        elif not self.gate.is_set():
                            self.expired = True
                        # Finish normal business work after deadline; never count as acceptance.
                    elif name=='rate':
                        await self.respond(writer,429,json.dumps({'error':{'type':'rate_limit_error','message':'synthetic'}}).encode(),[(b'retry-after',b'0')])
                        return
                    else:
                        payload = dict(id='c3-invalid',object='chat.completion',created=0,model=data['model'],choices=[dict(index=0,message=dict(role='assistant',content='invalid-json'),finish_reason='stop')])
                        await self.respond(writer,200,json.dumps(payload).encode())
                        return
                result = []
                async def receive():
                    return dict(type='http.request',body=raw,more_body=False)
                async def send(message):
                    result.append(message)
                scope = dict(type='http',asgi={'version':'3.0'},http_version='1.1',method=method,scheme='http',path=path,raw_path=path.encode(),query_string=b'',root_path='',headers=[],server=('127.0.0.1',18995),client=('127.0.0.1',0))
                await stub.app(scope,receive,send)
                status = next(m['status'] for m in result if m['type']=='http.response.start')
                await self.respond(writer,status,b''.join(m.get('body',b'') for m in result if m['type']=='http.response.body'))
        except (ConnectionError,asyncio.IncompleteReadError,TimeoutError,ValueError):
            self.event('provider_transport_closed',name,generation)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.tasks.discard(task)
    async def respond(self, writer, status, body, headers=()):
        extra = b''.join(k+b': '+v+b'\r\n' for k,v in headers)
        writer.write(f'HTTP/1.1 {status} Response\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n'.encode()+extra+b'\r\n'+body)
        await writer.drain()
    async def close(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)

class TransportEvents:
    def __init__(self, app, controller):
        self.app,self.controller = app,controller
    async def __call__(self, scope, receive, send):
        if scope['type']!='http':
            return await self.app(scope,receive,send)
        queue = asyncio.Queue()
        name = None
        generation = self.controller.generation
        response_done = False
        async def pump():
            nonlocal name
            body = bytearray()
            while True:
                message = await receive()
                if message['type']=='http.request':
                    body.extend(message.get('body',b''))
                    if len(body)>1024*1024:
                        body.clear()
                    elif not message.get('more_body',False):
                        try:
                            name = fixed_request(json.loads(body))
                        except (ValueError,TypeError,AttributeError):
                            pass
                elif message['type']=='http.disconnect':
                    # Only the exact keyed call, before its response completes, owns this event.
                    if name and not response_done:
                        self.controller.event('http.disconnect',name,generation)
                    await queue.put(message)
                    return
                await queue.put(message)
        async def observed_send(message):
            nonlocal response_done
            if message['type']=='http.response.body' and not message.get('more_body',False):
                response_done = True
            await send(message)
        task = asyncio.create_task(pump())
        try:
            await self.app(scope,queue.get,observed_send)
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
