import io
import json
import subprocess
import sys
import os
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen
from PIL import Image, ImageDraw, ImageFont

out = Path(__file__).parent / 'AprilTag_线上库存_20261106'
out.mkdir(exist_ok=True)
query = 'import sqlite3,json; c=sqlite3.connect("file:/home/xiao/face/unified_web_app/instance/unified.db?mode=ro",uri=True); c.row_factory=sqlite3.Row; print(json.dumps([dict(r) for r in c.execute("select c.id as cabinet_id,c.cabinet_number,c.equipment_name,cc.id as component_id,cc.component_name,cc.quantity from cabinets c left join cabinet_components cc on cc.cabinet_id=c.id order by c.id,cc.id")]))'
import shlex
cmd = [r'C:\Program Files\PuTTY\plink.exe', '-hostkey', 'ssh-ed25519 255 SHA256:ugfywNbbLkUcbEdT+1mVawLLspiLekHKpXqwTbT0PXo', 'xiao@192.168.0.100', '/home/xiao/face/unified_web_app/.venv/bin/python -c ' + shlex.quote(query)]
if os.environ.get('PARTS_SSH_PASSWORD'):
    cmd[1:1] = ['-batch', '-pw', os.environ['PARTS_SSH_PASSWORD']]
if '--snapshot' in sys.argv:
    rows = json.loads((out / 'inventory.json').read_text(encoding='utf-8'))['rows']
else:
    rows = json.loads(subprocess.check_output(cmd, text=True))
    (out / 'inventory.json').write_text(json.dumps({'source':'192.168.0.100 live SQLite','retrieved_at':datetime.now().isoformat(),'rows':rows},ensure_ascii=False,indent=2),encoding='utf-8')
groups = {}
for r in rows:
    groups.setdefault(int(r['cabinet_number']), []).append(r)
font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 22)
small = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 18)
sheet = Image.new('RGB', (1800, 1950), 'white')
d = ImageDraw.Draw(sheet)
d.text((30,15),'树莓派实时库存 · tag36h11 · 2026-11-06',fill='black',font=font)
lines=['# AprilTag 与线上库存对照','', '来源：192.168.0.100；读取日期：2026-11-06。Tag ID = 柜号整数；与数据库柜子主键不同。','', '| Tag ID | 柜号 | 数据库柜子 ID | 零件 ID | 零件名称 / 柜子名称 | 数量 |','|---|---|---|---|---|---|']
for index,(tag,items) in enumerate(sorted(groups.items())):
    url=f'https://raw.githubusercontent.com/AprilRobotics/apriltag-imgs/master/tag36h11/tag36_11_{tag:05d}.png'
    cached = out / f'tag36h11_{tag:03d}_original.png'
    raw=cached.read_bytes() if cached.exists() else urlopen(url,timeout=30).read()
    original=Image.open(io.BytesIO(raw)).convert('RGB')
    original.save(out / f'tag36h11_{tag:03d}_original.png')
    enlarged=original.resize((1000,1000),Image.Resampling.NEAREST)
    enlarged.save(out / f'tag36h11_{tag:03d}.png')
    x=(index%3)*600+25; y=(index//3)*620+65
    d.text((x,y),f'Tag {tag} · 柜号 {items[0]["cabinet_number"]} · 柜子主键 {items[0]["cabinet_id"]}',fill='black',font=font)
    sheet.paste(original.resize((330,330),Image.Resampling.NEAREST),(x+100,y+40))
    for j,r in enumerate(items):
        label=f'ID {r["component_id"]}：{r["component_name"]} × {r["quantity"]}' if r['component_id'] is not None else f'{r["equipment_name"]}（零件 ID 未分配）'
        d.text((x,y+385+j*36),label,fill='black',font=small)
        lines.append(f'| {tag} | {r["cabinet_number"]} | {r["cabinet_id"]} | {r["component_id"] if r["component_id"] is not None else "—"} | {r["component_name"] or r["equipment_name"]} | {r["quantity"] if r["quantity"] is not None else "—"} |')
lines += ['', '图案源：AprilRobotics/apriltag-imgs 官方 tag36h11。放大采用最近邻，保留白边。', '总览用于核对，不能直接按总览缩放打印来满足 5 cm 标定。打印单张时应使黑色外框外边长为 50 mm，并保留外部白边；不要把含白边的整张图片设为 50 mm。', '没有独立零件条目的柜子以柜名作为展示用零件名称；零件 ID、数量未分配，数据库保持原样。']
sheet.save(out / '全部标签与库存对照.png')
(out / '标签与零件对照.md').write_text('\n'.join(lines),encoding='utf-8')
print(out)
print('tags', len(groups), 'components',sum(r['component_id'] is not None for r in rows))
