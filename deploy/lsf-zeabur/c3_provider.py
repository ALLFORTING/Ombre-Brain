"""Loopback raw HTTP provider; no response bytes for connection_error."""
import asyncio
import hashlib
import json
import uuid
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
        self.target_request = None
        self.waiting = False
        self.unaccepted = None
    def event(self, event, scenario=None, generation=None, request_id=None):
        self.events.append(dict(event=event,scenario=scenario,generation=generation,
                                request_id=request_id, monotonic=asyncio.get_running_loop().time(),
                                time_basis="observed_at"))
    def projection(self):
        receipts = {}
        for name,fixed in SCENARIOS.items():
            row = self.ob.bucket_mgr.inspect_trace_request(fixed['operation_id'])
            if row:
                receipts[name] = dict(status=row['status'],result_sha256=digest(row.get('result_text') or ''))
                if row['status']=='completed' and name not in self.completed:
                    self.completed.add(name)
                    self.event('completed',name,self.generation if name==self.armed else None,
                               self.target_request if name==self.armed else None)
        return dict(armed=self.armed,generation=self.generation,counts=dict(self.counts),
                    target_request=self.target_request,waiting=self.waiting,unaccepted=self.unaccepted,
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
            self.target_request = None
            self.waiting = False
            self.unaccepted = None
            self.event('armed',name,self.generation)
        elif action in ('mark-operation','release'):
            state = self.projection()
            request_id = data.get('request_id')
            row = self.ob.bucket_mgr.inspect_trace_request(SCENARIOS['disconnect']['operation_id'])
            own = [e['event'] for e in self.events if e['scenario']=='disconnect'
                   and e['generation']==self.generation and e['request_id']==self.target_request]
            expected = ['waiting'] if action=='mark-operation' else ['waiting','operation_marked','http.disconnect']
            ordered = [e for e in own if e in ('waiting','operation_marked','http.disconnect','released','completed')]
            if (self.armed!='disconnect' or not request_id or request_id!=self.target_request
                    or not self.waiting or self.gate.is_set() or self.deadline is None
                    or asyncio.get_running_loop().time()>=self.deadline or self.expired
                    or self.unaccepted or not row or row['status']=='completed' or ordered!=expected):
                self.unaccepted = self.unaccepted or 'gate_evidence_or_request_refused'
                self.event('control_refused_unaccepted','disconnect',self.generation,request_id)
                return JSONResponse({'error':'gate_not_releasable','accepted':False},status_code=409)
            if action=='mark-operation':
                self.event('operation_marked',self.armed,self.generation,request_id)
            else:
                self.gate.set()
                self.waiting = False
                self.event('released',self.armed,self.generation,request_id)
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
                            self.waiting = True
                            self.event('waiting',name,generation,self.target_request)
                        remaining = self.deadline-asyncio.get_running_loop().time()
                        if not self.expired and remaining>0:
                            try:
                                await asyncio.wait_for(self.gate.wait(),remaining)
                            except TimeoutError:
                                self.expired = True
                                self.waiting = False
                                self.unaccepted = self.unaccepted or 'gate_timeout'
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
        request_id = None
        async def pump():
            nonlocal name, request_id
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
                            if name:
                                request_id = uuid.uuid4().hex
                                c = self.controller
                                c.event('request_matched',name,generation,request_id)
                                if name=='disconnect' and c.armed==name and c.generation==generation:
                                    if c.target_request is None:
                                        c.target_request = request_id
                                    elif not c.gate.is_set() and not c.expired:
                                        c.unaccepted = 'matching_request_overlap'
                                        c.event('request_mismatch_unaccepted',name,generation,request_id)
                        except (ValueError,TypeError,AttributeError):
                            pass
                elif message['type']=='http.disconnect':
                    # Only the exact keyed call, before its response completes, owns this event.
                    if name and not response_done:
                        c = self.controller
                        c.event('http.disconnect',name,generation,request_id)
                        if name=='disconnect' and c.armed==name and c.generation==generation:
                            own = [e['event'] for e in c.events if e['scenario']==name
                                   and e['generation']==generation and e['request_id']==request_id]
                            ordered = [e for e in own if e in ('waiting','operation_marked','http.disconnect')]
                            if request_id!=c.target_request or ordered!=['waiting','operation_marked','http.disconnect']:
                                c.unaccepted = 'early_or_mismatched_disconnect'
                                c.event('disconnect_refused_unaccepted',name,generation,request_id)
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
