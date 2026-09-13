import sys, os
sys.path.insert(0, '..')
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
from character_chat.memory.mood import MoodSystem
mood = MoodSystem(state_path='../data/test_real.json')

# 真实 reply
ut = '这个函数写得很干净'
reply = '……你又来 (//▽//) 就、就照着文档写的，没什么特别的……'
inner = mood.extract_inner_from_reply(reply, ut)
print('inner:', repr(inner))

# 调试
import re
lines = reply.replace('。','\n').replace('！','\n').replace('？','\n').split('\n')
print('lines:', lines)
for line in lines:
    line = line.strip()
    if not line: continue
    has_ell = line.startswith('……') or line.startswith('...')
    print(f'  line={repr(line)} ell={has_ell}')
    if not has_ell: continue
    frag = line.lstrip('.…').strip()
    print(f'    frag={repr(frag)}')
    frag = re.sub(r'\([^)]{1,15}\)', '', frag).strip()
    print(f'    frag_clean={repr(frag)} len={len(frag)}')
    if any(w in frag for w in ['step','loss','epoch','batch','optimizer','import','def ','class ']):
        print('    -> skip technical')
        continue
    if len(frag) <= 20:
        print('    -> candidate')
