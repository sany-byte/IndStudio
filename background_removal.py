"""Non-destructive edge-connected white background removal with ImageMagick."""
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import uuid

METHOD='imagemagick-edge-white-6pct-v1'
SCHEMA='''CREATE TABLE IF NOT EXISTS image_variants(
 id TEXT PRIMARY KEY, generation_id TEXT NOT NULL, kind TEXT NOT NULL,
 source_sha256 TEXT NOT NULL, method TEXT NOT NULL, asset TEXT,
 status TEXT NOT NULL, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(generation_id,kind,source_sha256,method));
 CREATE INDEX IF NOT EXISTS image_variants_generation ON image_variants(generation_id,kind,status);'''

def initialize(con):con.executescript(SCHEMA)

def command():
    binary=shutil.which('magick') or shutil.which('convert')
    if not binary:raise RuntimeError('ImageMagick is not installed')
    return binary

def cutout(source, target):
    binary=command()
    subprocess.run([binary,'-limit','memory','128MiB','-limit','map','256MiB',str(source),
        '-alpha','set','-bordercolor','white','-border','1','-fuzz','6%',
        '-fill','none','-draw',('alpha 0,0 floodfill' if Path(binary).name=='magick' else 'matte 0,0 floodfill'),'-shave','1x1','PNG32:'+str(target)],
        check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=45)
    # Reject empty/opaque results rather than publishing a misleading cutout.
    check=subprocess.run([binary,str(target),'-alpha','extract','-format','%[fx:mean]','info:'],
        check=True,capture_output=True,text=True,timeout=15)
    coverage=float(check.stdout)
    if not .001<coverage<.98:raise RuntimeError('Background mask is empty or background was not removed')
    return coverage

def process_one(root):
    root=Path(root);images=root/'data/images'
    con=sqlite3.connect(root/'data/studio.sqlite3',timeout=15);con.row_factory=sqlite3.Row
    try:
        # Check only selected sources; never change forms.selected_job or jobs.asset.
        rows=con.execute("SELECT DISTINCT j.id,j.asset FROM forms f JOIN jobs j ON j.id=f.selected_job WHERE j.status='succeeded' AND j.asset IS NOT NULL AND NOT EXISTS (SELECT 1 FROM image_variants v WHERE v.generation_id=j.id AND v.method=? AND v.status IN ('ready','error')) ORDER BY f.source,f.id",(METHOD,)).fetchall()
        for row in rows:
            source=images/row['asset']
            if not source.is_file():continue
            digest=hashlib.sha256(source.read_bytes()).hexdigest()
            previous=con.execute('SELECT * FROM image_variants WHERE generation_id=? AND source_sha256=? AND method=?',(row['id'],digest,METHOD)).fetchone()
            if previous and (previous['status']=='error' or (previous['status']=='ready' and (images/previous['asset']).is_file())):continue
            identity=previous['id'] if previous else uuid.uuid4().hex
            now=time.time()
            with con:
                con.execute('INSERT OR IGNORE INTO image_variants(id,generation_id,kind,source_sha256,method,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                    (identity,row['id'],'transparent',digest,METHOD,'processing',now,now))
                con.execute("UPDATE image_variants SET status='processing',error=NULL,updated_at=? WHERE id=?",(now,identity))
            asset=row['id']+'-transparent-'+identity+'.png';temp=images/('.'+identity+'.tmp.png')
            try:
                coverage=cutout(source,temp)
                if hashlib.sha256(source.read_bytes()).hexdigest()!=digest:raise RuntimeError('Source changed during processing')
                temp.replace(images/asset)
                with con:con.execute("UPDATE image_variants SET status='ready',asset=?,updated_at=? WHERE id=?",(asset,time.time(),identity))
                print('ready',row['id'],'coverage',round(coverage,3),flush=True)
            except Exception as exc:
                temp.unlink(missing_ok=True)
                with con:con.execute("UPDATE image_variants SET status='error',error=?,updated_at=? WHERE id=?",('Не удалось удалить фон: '+type(exc).__name__,time.time(),identity))
                print('error',row['id'],type(exc).__name__,flush=True)
            return True
        return False
    finally:con.close()

if __name__=='__main__':
    import argparse,fcntl
    parser=argparse.ArgumentParser();parser.add_argument('--root',default=str(Path(__file__).resolve().parent));parser.add_argument('--once',action='store_true');args=parser.parse_args()
    root=Path(args.root);lock=(root/'data/background-removal.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    with sqlite3.connect(root/'data/studio.sqlite3') as con:initialize(con)
    while True:
        try:worked=process_one(root)
        except Exception as exc:print('worker error',type(exc).__name__,flush=True);worked=False
        if args.once:break
        if not worked:time.sleep(4)
