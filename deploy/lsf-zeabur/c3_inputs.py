"""Fixed synthetic inputs: never match arbitrary business content."""
import hashlib
SCENARIOS = {}
for name in ('rate', 'parse', 'connection', 'fallback', 'disconnect'):
    content = f'C3-v1 {name}：第一条隔离事项是准备蓝色纸卡。第二条独立事项是核对绿色纸卡。只用于合成验收。'
    SCENARIOS[name] = dict(content=content, operation_id=f'obweb-ls-c3-v1-{name}',
        sha256=hashlib.sha256(content.encode()).hexdigest(),
        prompt='digest' if name in ('rate', 'parse', 'connection') else 'analyze',
        path='/v1/chat/completions', tool='grow' if name in ('rate', 'parse', 'connection') else 'hold')
