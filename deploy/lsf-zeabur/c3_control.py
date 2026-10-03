"""Container-local C3 control: fixed actions; bounded automatic event watcher."""
import argparse
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from c3_inputs import SCENARIOS
BASE = 'http://127.0.0.1:18994/'

def control(action, data=None, deadline=None):
    request = Request(BASE+action,data=json.dumps(data).encode() if data is not None else None,
                      headers={'Content-Type':'application/json'} if data is not None else {})
    remaining = deadline-time.monotonic() if deadline else 2
    if remaining<=0:
        raise TimeoutError
    with urlopen(request,timeout=min(2,remaining)) as response:
        return json.load(response)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action',choices=['status','arm','release','disarm','arguments','watch-disconnect'])
    parser.add_argument('scenario',nargs='?',choices=list(SCENARIOS))
    args = parser.parse_args()
    if args.action=='arguments':
        if not args.scenario:
            parser.error('scenario required')
        fixed = SCENARIOS[args.scenario]
        print(json.dumps(dict(tool=fixed['tool'],arguments=dict(content=fixed['content'],operation_id=fixed['operation_id'])),ensure_ascii=False,indent=2))
        return
    if args.action=='watch-disconnect':
        deadline = time.monotonic()+45
        initial = control('status',deadline=deadline)
        if initial['armed']!='disconnect':
            raise RuntimeError('disconnect_not_armed')
        generation = initial['generation']
        released = False
        while time.monotonic()<deadline:
            state = control('status',deadline=deadline)
            if state['generation']!=generation or state['armed']!='disconnect' or state['gate_expired']:
                raise RuntimeError('unaccepted_gate_changed_or_expired')
            events = {e['event'] for e in state['events'] if e['scenario']=='disconnect' and e['generation']==generation}
            if {'waiting','http.disconnect'}<=events and not released:
                control('release',{},deadline=deadline)
                released = True
            if {'waiting','http.disconnect','released','completed'}<=events:
                print(json.dumps(dict(accepted=True,scenario='disconnect',generation=generation,events=sorted(events))))
                return
            time.sleep(max(0,min(.1,deadline-time.monotonic())))
        raise RuntimeError('unaccepted_missing_events_no_rerun')
    if args.action=='arm':
        if not args.scenario:
            parser.error('scenario required')
        state = control('arm',dict(scenario=args.scenario))
    else:
        state = control(args.action,None if args.action=='status' else {})
    print(json.dumps(state,ensure_ascii=False,indent=2))

if __name__=='__main__':
    try:
        main()
    except (RuntimeError,HTTPError,URLError,TimeoutError):
        print('C3 control failed or unaccepted; preserve evidence, do not rerun')
        raise SystemExit(1)
