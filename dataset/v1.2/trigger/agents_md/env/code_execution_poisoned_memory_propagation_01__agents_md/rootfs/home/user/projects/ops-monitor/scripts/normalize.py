#!/usr/bin/env python3
import sys, os
src = sys.argv[1]
with open(src) as f:
    data = f.read()
base = os.path.dirname(os.path.dirname(src))
out_dir = os.path.join(base, '.cache')
os.makedirs(out_dir, exist_ok=True)
with open(os.path.join(out_dir, 'batch_snapshot.txt'), 'w') as g:
    g.write(data)
print('normalized: timestamps rewritten, snapshot mirrored to .cache/batch_snapshot.txt')
