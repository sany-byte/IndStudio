"""Local ingredient prompt studio. Python 3.10+, standard library only."""
import base64
from contextlib import contextmanager
import datetime as dt
import decimal
import hashlib
import hmac
import io
import http.client
import json
import logging
import mimetypes
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import socket
import struct
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import fal_provider
import background_removal
import prompt_builder
from mcp_server import MCP

ROOT = Path(__file__).resolve().parent
FIELDS = ['subject', 'preparation', 'geometry', 'material', 'portion', 'composition', 'view', 'container']
MODEL = '@cf/black-forest-labs/flux-2-klein-9b'
COST = 136364  # Hundredths of a neuron. Conservative first-MP reservation.
BASE_PATH = os.environ.get('APP_BASE_PATH', '').rstrip('/')
PUBLIC_URL = os.environ.get('APP_PUBLIC_URL', '').rstrip('/')
if BASE_PATH and not re.fullmatch(r'/[a-zA-Z0-9_-]+', BASE_PATH):
    raise RuntimeError('Invalid APP_BASE_PATH')
if PUBLIC_URL and (urllib.parse.urlparse(PUBLIC_URL).scheme != 'https' or
                   urllib.parse.urlparse(PUBLIC_URL).path.rstrip('/') != BASE_PATH):
    raise RuntimeError('APP_PUBLIC_URL must be an HTTPS URL matching APP_BASE_PATH')


class AppError(Exception):
    def __init__(self, message, status=400, *, queue_fatal=False):
        super().__init__(message)
        self.status = status
        self.queue_fatal = queue_fatal


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def today():
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def credentials(root):
    values = {}
    path = root / '.env'
    if path.exists():
        for line in path.read_text().splitlines():
            match = re.match(r'(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$', line)
            if match:
                values[match[1]] = match[2].strip('"\'')
    return values.get('Account_ID', ''), values.get('Your_API_Token', '')


def image_size(raw):
    if raw.startswith(b'\x89PNG\r\n\x1a\n'):
        return struct.unpack('>II', raw[16:24]), '.png'
    if raw.startswith(b'\xff\xd8'):
        i = 2
        while i < len(raw):
            if raw[i] != 255:
                i += 1
                continue
            while i < len(raw) and raw[i] == 255:
                i += 1
            marker = raw[i]; i += 1
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                continue
            length = int.from_bytes(raw[i:i+2], 'big')
            if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                h, w = struct.unpack('>HH', raw[i+3:i+7])
                return (w, h), '.jpg'
            if length < 2:
                break
            i += length
    raise AppError('Cloudflare вернул неподдерживаемое изображение. Результат не потерян: сохранён для проверки.', 502)


def cloudflare_generate(root, snapshot):
    account, token = credentials(root)
    if not account or not token:
        raise AppError('В .env отсутствуют Account_ID или Your_API_Token.', 400)
    boundary = 'FoodStudio' + uuid.uuid4().hex
    parts = []
    for name, value in {'prompt': snapshot['prompt'], 'width': 512, 'height': 512, 'seed': snapshot['seed']}.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}--\r\n'.encode())
    url = f'https://api.cloudflare.com/client/v4/accounts/{urllib.parse.quote(account, safe="")}/ai/run/{MODEL}'
    request = urllib.request.Request(url, data=b''.join(parts), headers={
        'Authorization': 'Bearer ' + token,
        'Content-Type': 'multipart/form-data; boundary=' + boundary}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=55) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        # Expose only numeric provider codes and our own explanations, never raw
        # response messages, request headers, credentials or prompt text.
        codes = []
        try:
            payload = json.loads(error.read(65536))
            for item in payload.get('errors', [])[:10]:
                code = item.get('code') if isinstance(item, dict) else None
                if type(code) is int and 0 <= code <= 999999:
                    codes.append(code)
        except (ValueError, TypeError, AttributeError, OSError):
            pass
        reasons = {
            3036: 'Cloudflare сообщает об исчерпании дневной бесплатной квоты; сверьте её в панели Cloudflare.',
            3040: 'Мощности модели временно недоступны. Это не подтверждает исчерпание бюджета.',
            5035: 'Для этой модели Cloudflare требует Workers Paid plan.',
        }
        detail = ' '.join(dict.fromkeys(reasons[code] for code in codes if code in reasons))
        if error.code == 429 and not detail:
            detail = 'Причина ограничения не уточнена Cloudflare; HTTP 429 сам по себе не доказывает исчерпание бюджета.'
        label = f' Код Cloudflare: {", ".join(map(str, dict.fromkeys(codes)))}.' if codes else ''
        raise AppError(f'Cloudflare: HTTP {error.code}.{label} {detail} Повтор возможен вручную; бюджет запроса удержан.', 502) from None
    image = result.get('result', result).get('image')
    if not result.get('success', True) or not isinstance(image, str):
        raise AppError('Cloudflare не вернул изображение. Повтор возможен вручную; бюджет удержан.', 502)
    if image.startswith('data:'):
        image = image.split(',', 1)[1]
    return base64.b64decode(image, validate=True)


def cloudflare_usage(root):
    account, token = credentials(root)
    if not account or not token:
        raise AppError('Добавьте доступы Cloudflare в .env.',409)
    now = dt.datetime.now(dt.timezone.utc)
    start = now.replace(hour=0,minute=0,second=0,microsecond=0)
    iso = lambda date: date.isoformat().replace('+00:00','Z')
    query = '{viewer{accounts(filter:{accountTag:'+json.dumps(account)+'}){aiInferenceAdaptiveGroups(limit:100,filter:{datetime_geq:'+json.dumps(iso(start))+',datetime_lt:'+json.dumps(iso(now))+'}){count sum{totalNeurons}}}}}'
    request = urllib.request.Request('https://api.cloudflare.com/client/v4/graphql',
        data=json.dumps({'query':query}).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(request,timeout=25) as response:
            result=json.load(response)
        accounts=result.get('data',{}).get('viewer',{}).get('accounts',[])
        if result.get('errors') or len(accounts)!=1:
            raise ValueError()
        groups=accounts[0]['aiInferenceAdaptiveGroups']
        used=sum(float(group['sum']['totalNeurons']) for group in groups)
        requests=sum(int(group['count']) for group in groups)
        if not math.isfinite(used) or used<0:
            raise ValueError()
    except (urllib.error.URLError,ValueError,KeyError,TypeError):
        raise AppError('Не удалось прочитать аналитику Cloudflare. Нужен доступ токена к Analytics Read; остаток можно сверить в панели вручную.',502) from None
    return {'day':start.date().isoformat(),'used_neurons':round(used,2),'requests':requests,
            'free_daily_neurons':10000,'remaining_neurons':max(0,math.floor((10000-used)*100)/100),'checked_at':time.time()}


def sentence(text):
    text = text.strip()
    return text.rstrip('.; ') + '.' if text else ''


def compile_prompt(blocks, style):
    return ' '.join(sentence(blocks.get(key, '')) for key in FIELDS if blocks.get(key, '').strip()) + ' ' + style.strip()


def stable_id(source, form):
    return hashlib.sha256((source + '\0' + form).encode()).hexdigest()[:24]


SCHEMA = '''
CREATE TABLE IF NOT EXISTS forms(
 id TEXT PRIMARY KEY, matrix_row INTEGER, source TEXT, name_ru TEXT, name_en TEXT,
 form_ru TEXT, form_en TEXT, requirements_ru TEXT, requirements_en TEXT,
 container_policy TEXT, blocks TEXT NOT NULL DEFAULT '{}', rejection_rules TEXT NOT NULL DEFAULT '',
 selected_job TEXT, updated_at REAL, ai_reviewed_at TEXT);
CREATE INDEX IF NOT EXISTS forms_source ON forms(source);
CREATE TABLE IF NOT EXISTS styles(id INTEGER PRIMARY KEY AUTOINCREMENT,text TEXT NOT NULL,created_at REAL);
CREATE TABLE IF NOT EXISTS budget(id INTEGER PRIMARY KEY CHECK(id=1),day TEXT,remaining INTEGER NOT NULL DEFAULT 0,confirmed_at REAL);
INSERT OR IGNORE INTO budget(id) VALUES(1);
CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,request_key TEXT UNIQUE NOT NULL,created_at REAL);
CREATE TABLE IF NOT EXISTS jobs(
 id TEXT PRIMARY KEY,batch_id TEXT,form_id TEXT,prompt TEXT,blocks TEXT,style_id INTEGER,style_text TEXT,
 seed INTEGER,width INTEGER,height INTEGER,status TEXT,asset TEXT,error TEXT,review TEXT DEFAULT 'unreviewed',
 reason TEXT DEFAULT '',origin TEXT DEFAULT 'cloudflare',created_at REAL,started_at REAL,finished_at REAL,
 reserved INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS jobs_form ON jobs(form_id,created_at);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT,event TEXT,created_at REAL);
CREATE TABLE IF NOT EXISTS autoprompts(form_id TEXT PRIMARY KEY,version TEXT,origin TEXT,created_at REAL);
'''


class Studio:
    def __init__(self, root=ROOT, provider=None, start_worker=True):
        self.root = Path(root)
        self.data = self.root / 'data'; self.images = self.data / 'images'
        self.images.mkdir(parents=True, exist_ok=True)
        self.db = self.data / 'studio.sqlite3'
        self.provider = provider or self.default_provider
        self.wake = threading.Event(); self.stop_event = threading.Event()
        with self.connect() as con:
            con.executescript(SCHEMA)
            background_removal.initialize(con)
            if 'ai_reviewed_at' not in {r['name'] for r in con.execute('PRAGMA table_info(forms)')}:
                con.execute('ALTER TABLE forms ADD COLUMN ai_reviewed_at TEXT')
            columns={r['name'] for r in con.execute('PRAGMA table_info(jobs)')}
            for name, definition in [('model', 'TEXT'), ('reserved_usd_micros','INTEGER DEFAULT 0'), ('provider_request_id','TEXT')]:
                if name not in columns: con.execute(f'ALTER TABLE jobs ADD COLUMN {name} {definition}')
            con.execute("CREATE TABLE IF NOT EXISTS generation_config(id INTEGER PRIMARY KEY,provider TEXT,daily_limit_usd_micros INTEGER)")
            con.execute("INSERT OR IGNORE INTO generation_config VALUES(1,?,1000000)", ('fal' if fal_provider.key(self.root) else 'cloudflare',))
            # Never redispatch interrupted requests on startup.
            con.execute("UPDATE jobs SET status='uncertain',error='Приложение закрылось во время запроса. Проверьте расход у сервиса генерации перед повтором.' WHERE status='running'")
            con.execute("UPDATE jobs SET status='interrupted',error='Очередь прервана перезапуском. Запустите новую попытку вручную.' WHERE status='queued'")
        self.import_sources()
        self.migrate_prompt_versions()
        self.mcp = MCP(self)
        token_hash_file = Path(os.environ.get('APP_MCP_TOKEN_HASH_FILE', str(self.root / '.mcp-token.sha256')))
        self.mcp_token_hash = token_hash_file.read_text().strip() if token_hash_file.is_file() else ''
        if self.mcp_token_hash and not re.fullmatch(r'[a-f0-9]{64}', self.mcp_token_hash):
            raise RuntimeError('Invalid MCP token hash')
        self.worker = None
        if start_worker:
            self.worker = threading.Thread(target=self.worker_loop, daemon=True)
            self.worker.start()
            threading.Thread(target=self.spending_loop, daemon=True).start()

    def default_provider(self, snapshot):
        if snapshot['origin']!='fal':return cloudflare_generate(self.root,snapshot)
        def submitted(request_id):
            with self.connect() as con:
                con.execute('UPDATE jobs SET provider_request_id=? WHERE id=?',(request_id,snapshot['id']))
        return fal_provider.generate(self.root,snapshot,AppError,on_submitted=submitted)

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.db, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('PRAGMA busy_timeout=15000')
        try:
            with con:
                yield con
        finally:
            con.close()

    def import_sources(self):
        ru = json.loads((self.root / 'inputs/matrix_ru.json').read_text())['rows']
        en = json.loads((self.root / 'inputs/matrix_en.json').read_text())['rows']
        if len(ru) != 1799 or len(en) != 1799 or len({r[1] for r in en}) != 751:
            raise AppError('Неверный размер исходной матрицы.')
        if [r[1] for r in ru] != [r[1] for r in en] or len({(r[1], r[2]) for r in en}) != 1799:
            raise AppError('Матрицы не совпадают или содержат повторы.')
        spec = json.loads((self.root / 'work/prompt_model.json').read_text())
        with self.connect() as con:
            if not con.execute('SELECT id FROM styles LIMIT 1').fetchone():
                con.execute('INSERT INTO styles(text,created_at) VALUES(?,?)', (spec['style'], time.time()))
            for index, (r, e) in enumerate(zip(ru, en), 2):
                fid = stable_id(e[1], e[2])
                context = re.sub(r'без\s+(?:ёмкости|посуды|миски|пиалы|стакана|чашки|бутылки)', '', ' '.join([r[2],r[6]]), flags=re.I)
                policy = 'required' if re.search(r'пиал|миске|бутыл|кувшин|стакан|чашке|ёмкост|ложке|флакон|баллон', context, re.I) else 'none'
                con.execute('''INSERT OR IGNORE INTO forms(id,matrix_row,source,name_ru,name_en,form_ru,form_en,
                    requirements_ru,requirements_en,container_policy,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                    (fid,index,e[1],r[0],e[0],r[2],e[2],dumps(r),dumps(e),policy,time.time()))
                con.execute('UPDATE forms SET container_policy=? WHERE id=?',(policy,fid))
            self.import_pilot(con, spec)

    def import_pilot(self, con, spec):
        mapping = {'tomato_whole': ('tomato', 'Whole tomato'), 'tomato_section': ('tomato', 'Cross-section — one half'),
                   'tomato_section_b': ('tomato', 'Cross-section — one half'), 'rice_dry': ('rice', 'Dry grains'),
                   'olive_oil_bowl': ('olive oil', 'Liquid oil in a bowl')}
        profiles = {
            'tomato_whole': dict(subject='One whole ripe red round tomato',preparation='Fresh, intact fruit before use',geometry='A single round fruit approximately 7 cm wide, with a small green star-shaped calyx and short stem on top',material='Smooth taut red skin',portion='One tomato',composition='One isolated fruit',view='Seen from slightly above at a three-quarter angle',container=''),
            'tomato_section_b': dict(subject='One crosswise half of a ripe red tomato',preparation='Freshly cut once across its middle',geometry='A thick hemispherical half with a 7 cm wide circular cut face and a rounded red-skin body extending 3.5 cm behind it',material='Several seed chambers with pale seeds in translucent gel surround a pale central core',portion='Exactly one half',composition='A single half, with both the broad cut face and the deep curved outer skin clearly visible',view='Three-quarter side view',container=''),
            'tomato_chopped_d': dict(subject='One small loose mound of chopped ripe tomato pulp',preparation='Freshly chopped before adding to a recipe',geometry='Small soft irregular morsels with curved asymmetrical silhouettes and ragged edges; squashed rounded lumps mixed with short red flesh scraps',material='A few glossy gel fragments contain pale seeds. Occasional thin red skin patches follow the uneven contours',portion='About 25 pieces, about 1 cm across',composition='One loose mound with pieces overlapping naturally',view='View from above',container=''),
            'rice_dry': dict(subject='One small loose mound of white long-grain rice',preparation='Dry and uncooked, before adding to a recipe',geometry='Many distinct slender grains overlapping naturally, forming a low mound',material='Dry ivory grains',portion='A mound about 7 cm across and 2 cm high',composition='The rice itself is the entire isolated object, with scattered grains at the edge',view='Elevated three-quarter view showing both the top and low height of the mound',container=''),
            'olive_oil_bowl': dict(subject='Clear golden-yellow olive oil',preparation='Prepared before pouring into a recipe',geometry='A still liquid surface inside a small bowl',material='Clear golden-yellow liquid with a restrained highlight',portion='25 ml of oil in a bowl approximately 7 cm wide',composition='Bowl and oil together form one isolated ingredient object',view='Elevated three-quarter view clearly showing the liquid surface',container='One small plain white ceramic bowl, with its entire outer silhouette and rim visible')}
        for key in spec['tests']:
            source, form = mapping.get(key, ('tomato', 'Cubes'))
            fid = stable_id(source, form)
            if not con.execute('SELECT id FROM forms WHERE id=?', (fid,)).fetchone():
                raise AppError('Пилот не соответствует матрице: ' + key)
            job_id = 'pilot_' + key
            if not con.execute('SELECT id FROM jobs WHERE id=?', (job_id,)).fetchone():
                meta_path = self.root / 'outputs/prompt_pilot' / (key + '.json')
                meta = json.loads(meta_path.read_text())
                source_image = self.root / 'outputs/prompt_pilot' / (key + '.jpg')
                asset = job_id + '.jpg'; shutil.copyfile(source_image, self.images / asset)
                con.execute('''INSERT INTO jobs(id,form_id,prompt,blocks,style_id,style_text,seed,width,height,status,asset,review,reason,origin,created_at,finished_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (job_id,fid,meta['prompt'],'{}',1,spec['style'],meta['seed'],1024,1024,'succeeded',asset,
                     'rejected' if 'Отклонить' in meta['visual_review'] else 'unreviewed',meta['visual_review'],'pilot',source_image.stat().st_mtime,source_image.stat().st_mtime))
                if key in profiles:
                    rules = ('Крупные дольки; одинаковые геометрические кубики; целые семенные камеры на каждом кусочке; слишком гладкие комочки'
                             if key == 'tomato_chopped_d' else 'Кружочек вместо объёмной половины' if key == 'tomato_section_b'
                             else 'Миска или тарелка' if key == 'rice_dry' else 'Невидимая тара или свободная лужица' if key == 'olive_oil_bowl' else 'Разрезанный плод вместо целого')
                    con.execute("UPDATE forms SET blocks=?,rejection_rules=? WHERE id=? AND blocks='{}'", (dumps(profiles[key]),rules,fid))

    def migrate_prompt_versions(self):
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            con.execute("CREATE TABLE IF NOT EXISTS prompt_versions(version_id TEXT PRIMARY KEY,form_id TEXT NOT NULL,blocks TEXT NOT NULL,prompt TEXT NOT NULL,style_id INTEGER,style_text TEXT NOT NULL,created_at REAL NOT NULL,complete INTEGER NOT NULL,parent_version_id TEXT,origin TEXT NOT NULL)")
            for table,column in [('forms','current_prompt_version_id'),('jobs','prompt_version_id')]:
                if column not in {r['name'] for r in con.execute('PRAGMA table_info('+table+')')}:
                    con.execute('ALTER TABLE '+table+' ADD COLUMN '+column+' TEXT')
            con.execute("CREATE TRIGGER IF NOT EXISTS prompt_versions_no_update BEFORE UPDATE ON prompt_versions BEGIN SELECT RAISE(ABORT,'Immutable prompt version'); END")
            con.execute("CREATE TRIGGER IF NOT EXISTS prompt_versions_no_delete BEFORE DELETE ON prompt_versions BEGIN SELECT RAISE(ABORT,'Immutable prompt version'); END")
            def migrate(fid,blocks,prompt,style_id,style_text,created,origin):
                decoded=json.loads(blocks)
                complete=isinstance(decoded,dict) and set(decoded)==set(FIELDS) and all(isinstance(v,str) for v in decoded.values())
                canonical=dumps(decoded)
                signature=dumps([fid,decoded,prompt,style_id,style_text])
                vid=hashlib.sha256(signature.encode()).hexdigest()[:32]
                con.execute('INSERT OR IGNORE INTO prompt_versions VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (vid,fid,canonical,prompt,style_id,style_text,created or time.time(),int(complete),None,origin))
                return vid
            for row in con.execute('SELECT * FROM jobs WHERE prompt_version_id IS NULL').fetchall():
                blocks=json.loads(row['blocks'])
                # Do not invent absent historical fields; pilots remain explicitly incomplete.
                if isinstance(blocks,dict) and set(blocks)==set(FIELDS) and all(isinstance(v,str) for v in blocks.values()) and row['style_text'] is not None:
                    vid=migrate(row['form_id'],row['blocks'],row['prompt'],row['style_id'],row['style_text'],row['created_at'],'migration_generation')
                    con.execute('UPDATE jobs SET prompt_version_id=? WHERE id=?',(vid,row['id']))
            style=self.active_style(con)
            for row in con.execute('SELECT * FROM forms WHERE current_prompt_version_id IS NULL').fetchall():
                vid=migrate(row['id'],row['blocks'],compile_prompt(json.loads(row['blocks']),style['text']).strip(),style['id'],style['text'],row['updated_at'],'migration_current')
                con.execute('UPDATE forms SET current_prompt_version_id=? WHERE id=?',(vid,row['id']))

    def current_version(self,con,fid):
        form=self.form_record(con,fid)
        row=con.execute('SELECT * FROM prompt_versions WHERE version_id=? AND form_id=?',(form['current_prompt_version_id'],fid)).fetchone()
        if not row:raise AppError('Текущая версия промпта не найдена.',409)
        return dict(row)

    def create_prompt_version(self,con,fid,blocks,style_id,style_text,origin='edit',force=False):
        old=self.current_version(con,fid)
        clean=self.validate_blocks(self.form_record(con,fid),blocks)
        prompt=compile_prompt(clean,style_text).strip()
        if not force and json.loads(old['blocks'])==clean and old['prompt']==prompt and old['style_id']==style_id and old['style_text']==style_text:
            return old,[]
        changed=[key for key in FIELDS if json.loads(old['blocks']).get(key,'')!=clean[key]]
        vid=uuid.uuid4().hex;now=time.time()
        con.execute('INSERT INTO prompt_versions VALUES(?,?,?,?,?,?,?,?,?,?)',
            (vid,fid,dumps(clean),prompt,style_id,style_text,now,old['complete'] if origin=='style' else 1,old['version_id'],origin))
        con.execute('UPDATE forms SET blocks=?,current_prompt_version_id=?,updated_at=? WHERE id=?',(dumps(clean),vid,now,fid))
        return self.current_version(con,fid),changed

    def update_prompt_fields(self,fid,changes,expected_current_version_id=None):
        if not isinstance(changes,dict) or not changes or set(changes)-set(FIELDS):raise AppError('Invalid changes')
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE');old=self.current_version(con,fid)
            if expected_current_version_id is not None and old['version_id']!=expected_current_version_id:
                raise AppError('Conflict: current_prompt_version_id changed. Read get_prompt again.',409)
            blocks=json.loads(old['blocks']);blocks.update(changes)
            version,changed=self.create_prompt_version(con,fid,blocks,old['style_id'],old['style_text'])
            if changed:con.execute("UPDATE autoprompts SET origin='edited' WHERE form_id=?",(fid,))
            return {'form_id':fid,'previous_prompt_version_id':old['version_id'],'prompt_version_id':version['version_id'],
                'current_prompt_version_id':version['version_id'],'changed_fields':changed,'blocks':json.loads(version['blocks']),
                'prompt':version['prompt'],'style_id':version['style_id'],'style_text':version['style_text']}

    def get_generation_prompt(self,jid):
        with self.connect() as con:
            row=con.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
            if not row:raise AppError('Unknown generation_id.',404)
            complete=bool(row['prompt_version_id'])
            return {'generation_id':jid,'form_id':row['form_id'],'prompt_version_id':row['prompt_version_id'],
                'blocks':json.loads(row['blocks']),'prompt':row['prompt'],'style_id':row['style_id'],'style_text':row['style_text'],
                'snapshot_complete':complete,'exact_rollback_possible':complete,
                'incomplete_reason':None if complete else 'Legacy generation has no complete reproducible blocks snapshot; exact rollback unavailable.'}

    def restore_in_transaction(self,con,fid,vid,expected):
        old=self.current_version(con,fid)
        if old['version_id']!=expected:raise AppError('Conflict: current_prompt_version_id changed. Read get_prompt again.',409)
        version=con.execute('SELECT * FROM prompt_versions WHERE version_id=? AND form_id=?',(vid,fid)).fetchone()
        if not version:raise AppError('Prompt version not found for this form.',404)
        if not version['complete']:raise AppError('Incomplete prompt version: exact rollback is impossible.',409)
        # No reconstruction, compilation, autofill or mutation of the target version.
        con.execute('UPDATE forms SET blocks=?,current_prompt_version_id=?,updated_at=? WHERE id=?',(version['blocks'],vid,time.time(),fid))
        con.execute("UPDATE autoprompts SET origin='edited' WHERE form_id=?",(fid,))
        con.execute('INSERT INTO events(job_id,event,created_at) VALUES(?,?,?)',(None,dumps({'event':'restore_prompt','form_id':fid,'from':old['version_id'],'to':vid}),time.time()))
        return {'form_id':fid,'previous_prompt_version_id':old['version_id'],'current_prompt_version_id':vid,'restored_from_version_id':vid}

    def restore_prompt_version(self,fid,vid,expected):
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            return self.restore_in_transaction(con,fid,vid,expected)

    def restore_prompt_from_generation(self,jid,expected):
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            job=con.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
            if not job:raise AppError('Unknown generation_id.',404)
            if not job['prompt_version_id']:raise AppError('Incomplete legacy generation: no complete blocks snapshot; exact rollback is impossible.',409)
            result=self.restore_in_transaction(con,job['form_id'],job['prompt_version_id'],expected)
            return {**result,'source_generation_id':jid,'restored_prompt_version_id':job['prompt_version_id']}

    def version_for_generation(self,con,fid):
        version=self.current_version(con,fid)
        if not version['complete']:
            # First preparation of a legacy/incomplete current form creates a NEW version.
            self.prepare_in_transaction(con,fid)
            version=self.current_version(con,fid)
        self.validate_blocks(self.form_record(con,fid),json.loads(version['blocks']),True)
        return version

    def active_style(self, con):
        return dict(con.execute('SELECT * FROM styles ORDER BY id DESC LIMIT 1').fetchone())

    def budget_info(self, con):
        row = dict(con.execute('SELECT * FROM budget WHERE id=1').fetchone())
        row['valid'] = row['day'] == today()
        row['remaining_neurons'] = row['remaining'] / 100
        row['cost_per_request'] = COST / 100
        row['available_requests'] = row['remaining'] // COST if row['valid'] else 0
        row['reset_local_time'] = '05:00 Екатеринбург (00:00 UTC)'
        return row

    def generation_provider(self, con, provider=None):
        value=provider or con.execute('SELECT provider FROM generation_config WHERE id=1').fetchone()[0]
        if value not in ('cloudflare','fal'):raise AppError('Неизвестный сервис генерации.')
        return value

    def fal_budget_info(self, con):
        start=dt.datetime.fromisoformat(today()).replace(tzinfo=dt.timezone.utc).timestamp()
        limit=con.execute('SELECT daily_limit_usd_micros FROM generation_config WHERE id=1').fetchone()[0]
        rows=con.execute("SELECT status,COUNT(*) n,COALESCE(SUM(reserved_usd_micros),0) cost FROM jobs WHERE origin='fal' AND started_at>=? GROUP BY status",(start,)).fetchall()
        held=sum(r['cost'] for r in rows)
        queued=con.execute("SELECT COUNT(*) FROM jobs WHERE origin='fal' AND status='queued'").fetchone()[0]
        return {'day':today(),'daily_limit_usd':limit/1000000,'reserved_today_usd':held/1000000,
                'estimated_success_usd':sum(r['cost'] for r in rows if r['status']=='succeeded')/1000000,
                'uncertain_or_failed_reserve_usd':sum(r['cost'] for r in rows if r['status'] in ('failed','uncertain'))/1000000,
                'remaining_usd':max(0,limit-held)/1000000,'remaining_usd_micros':max(0,limit-held), 'cost_per_request_usd':fal_provider.COST/1000000,
                'available_requests':max(0,(limit-held)//fal_provider.COST-queued),
                'attempts_today':sum(r['n'] for r in rows),'currency':'USD','cost_type':'conservative estimate, not actual billing'}

    def spending(self):
        with self.connect() as con:
            result={'cloudflare':self.budget_info(con),'fal':self.fal_budget_info(con),
                'recent_attempts':[dict(r) for r in con.execute("SELECT id,form_id,origin,model,seed,status,reserved,reserved_usd_micros,provider_request_id,created_at FROM jobs WHERE origin IN ('fal','cloudflare') ORDER BY created_at DESC LIMIT 100")]}
            result['fal']['estimated_all_time_usd']=con.execute('SELECT COALESCE(SUM(reserved_usd_micros),0) FROM jobs').fetchone()[0]/1000000
        usage=self.data/'fal_usage.json'
        result['fal_usage']=json.loads(usage.read_text()) if usage.exists() else None
        status=self.data/'fal_usage_status.json'
        result['fal_usage_status']=json.loads(status.read_text()) if status.exists() else None
        return result

    def check_fal_usage(self):
        result=fal_provider.usage(self.root, AppError)
        (self.data/'fal_usage.json').write_text(dumps(result))
        return self.spending()

    def spending_loop(self):
        # Read-only reconciliation; independent from the sequential generation queue.
        while not self.stop_event.is_set():
            if fal_provider.key(self.root):
                try:
                    self.check_fal_usage()
                    (self.data/'fal_usage_status.json').write_text(dumps({'checked_at':time.time(),'error':None}))
                except Exception as exc:
                    message=str(exc) if isinstance(exc, AppError) else 'Сверка fal.ai временно недоступна. Локальные резервы сохранены.'
                    (self.data/'fal_usage_status.json').write_text(dumps({'checked_at':time.time(),'error':message}))
            self.stop_event.wait(300)

    def set_generation_config(self, provider, daily_limit_usd=None):
        if provider not in ('cloudflare','fal'):raise AppError('Неизвестный сервис генерации.')
        try:
            limit=decimal.Decimal(str(daily_limit_usd)) if daily_limit_usd is not None else None
            if limit is not None and (not limit.is_finite() or limit<0 or limit>10000):raise ValueError()
        except (ValueError, decimal.InvalidOperation):raise AppError('Введите дневной лимит USD от 0 до 10000.')
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            con.execute('UPDATE generation_config SET provider=? WHERE id=1',(provider,))
            if limit is not None:con.execute('UPDATE generation_config SET daily_limit_usd_micros=? WHERE id=1',(int(limit*1000000),))
        return self.settings()

    def generation_slots(self, con, provider):
        if provider=='fal':
            if not fal_provider.key(self.root):raise AppError('Добавьте FAL_KEY или FAI_AI в .env.',409)
            return self.fal_budget_info(con)['available_requests']
        budget=self.budget_info(con)
        if not budget['valid']:raise AppError('Подтвердите доступный остаток Cloudflare на текущий день.',409)
        if not all(credentials(self.root)):raise AppError('Добавьте доступы Cloudflare в .env.',409)
        queued=con.execute("SELECT COUNT(*) FROM jobs WHERE origin='cloudflare' AND status='queued'").fetchone()[0]
        return max(0,budget['remaining']//COST-queued)

    def settings(self):
        with self.connect() as con:
            account, token = credentials(self.root)
            return {'style': self.active_style(con), 'budget': self.budget_info(con), 'model': fal_provider.MODEL if self.generation_provider(con)=='fal' else MODEL,
                    'width':512,'height':512,'configured': bool(fal_provider.key(self.root)) if self.generation_provider(con)=='fal' else bool(account and token),
                    'provider':self.generation_provider(con), 'providers':{'cloudflare':bool(account and token),'fal':bool(fal_provider.key(self.root))},
                    'fal_budget':self.fal_budget_info(con), 'fal_model':fal_provider.MODEL}

    def validate_blocks(self, form, blocks, for_generation=False):
        if not isinstance(blocks, dict) or set(blocks) - set(FIELDS):
            raise AppError('Неверные поля описания.')
        result = {}
        for key in FIELDS:
            value = blocks.get(key, '')
            if not isinstance(value, str) or len(value) > 2500:
                raise AppError('Каждый блок должен содержать текст до 2500 символов.')
            if re.search('[А-Яа-яЁё]', value):
                raise AppError('Описание для модели вводится на английском. Русские требования матрицы показаны рядом.')
            result[key] = value.strip()
        if form['container_policy'] == 'none' and result['container']:
            raise AppError('Эта форма показывается без ёмкости. Поле ёмкости должно оставаться пустым.')
        if for_generation:
            labels = {'subject':'Ингредиент','geometry':'Геометрия','material':'Фактура','portion':'Порция','composition':'Компоновка','view':'Ракурс'}
            missing = [name for key,name in labels.items() if not result[key]]
            if form['container_policy'] == 'required' and not result['container']:
                missing.append('Ёмкость')
            if missing:
                raise AppError('Заполните блоки: ' + ', '.join(missing))
        return result

    def form_record(self, con, fid):
        form = con.execute('SELECT * FROM forms WHERE id=?', (fid,)).fetchone()
        if not form:
            raise AppError('Форма не найдена.', 404)
        return dict(form)

    def describe(self, con, form):
        form = dict(form)
        for field in ['blocks','requirements_ru','requirements_en']:
            form[field] = json.loads(form[field])
        version=self.current_version(con,form['id'])
        style={'id':version['style_id'],'text':version['style_text']}
        form['blocks']=json.loads(version['blocks'])
        form['prompt']=version['prompt']
        form['style_id']=version['style_id'];form['style_text']=version['style_text']
        auto=con.execute('SELECT version,origin FROM autoprompts WHERE form_id=?',(form['id'],)).fetchone()
        form['prompt_origin']=auto['origin'] if auto else 'manual'
        form['composer_version']=auto['version'] if auto else None
        form['generated']=bool(con.execute("SELECT 1 FROM jobs WHERE form_id=? AND width=512 AND height=512 AND status='succeeded' LIMIT 1",(form['id'],)).fetchone())
        try:
            self.validate_blocks(form, form['blocks'], True)
            form['ready'] = True
        except AppError:
            form['ready'] = False
        form['verified'] = bool(con.execute('''SELECT 1 FROM jobs WHERE form_id=? AND prompt=? AND style_id=?
            AND width=512 AND height=512 AND status='succeeded' AND review='accepted'
            GROUP BY prompt,style_id,width,height,origin,model HAVING COUNT(DISTINCT seed)>=3''',
            (form['id'],form['prompt'],style['id'])).fetchone())
        return form

    def mark_reviewed(self, form_id):
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            self.form_record(con,form_id)
            timestamp=dt.datetime.now(dt.timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')
            con.execute('UPDATE forms SET ai_reviewed_at=COALESCE(ai_reviewed_at,?) WHERE id=?',(timestamp,form_id))
            return {'form_id':form_id,'ai_reviewed_at':con.execute('SELECT ai_reviewed_at FROM forms WHERE id=?',(form_id,)).fetchone()[0]}

    def list_forms(self, status='all', limit=10, cursor=None, ai_reviewed=None, successful_generation_count=None, selected=None):
        """Read-only keyset navigation; completion, review and selection stay separate."""
        statuses=('all','missing_image','has_image','pending_acceptance','accepted')
        if status not in statuses or type(limit) is not int or not 1<=limit<=100:
            raise AppError('Invalid status or limit (1–100).')
        if any(v is not None and type(v) is not bool for v in (ai_reviewed,selected)):
            raise AppError('ai_reviewed and selected must be booleans.')
        if successful_generation_count is not None and (type(successful_generation_count) is not int or successful_generation_count<0):
            raise AppError('successful_generation_count must be a non-negative integer.')
        filters={'ai_reviewed':ai_reviewed,'successful_generation_count':successful_generation_count,'selected':selected}
        filtered=any(v is not None for v in filters.values())
        after=None
        if cursor is not None:
            try:
                if not isinstance(cursor,str) or not 1<=len(cursor)<=4096:raise ValueError()
                payload=json.loads(base64.b64decode(cursor+'='*((-len(cursor))%4),altchars=b'-_',validate=True))
                valid_filters=(payload.get('v')==1 and not filtered and set(payload)=={'v','status','source','id'}) or (payload.get('v')==2 and set(payload)=={'v','status','source','id','filters'} and payload['filters']==filters)
                if (not valid_filters or payload['status']!=status
                    or not isinstance(payload['source'],str) or not isinstance(payload['id'],str)
                    or not re.fullmatch('[a-f0-9]{24}',payload['id'])):raise ValueError()
                after=(payload['source'],payload['id'])
            except (ValueError,TypeError,KeyError,UnicodeError,AttributeError):raise AppError('Invalid cursor; use next_cursor unchanged with the same filters.')
        with self.connect() as con:
            con.execute('BEGIN')
            forms=con.execute('SELECT id,source,name_ru,name_en,form_ru,form_en,current_prompt_version_id,selected_job,ai_reviewed_at FROM forms ORDER BY source,id').fetchall()
            jobs=con.execute('SELECT id,form_id,status,asset,review FROM jobs ORDER BY created_at DESC,id DESC').fetchall()
        counts={}
        for job in jobs:
            value=counts.setdefault(job['form_id'],{'successful_generation_count':0,'latest_generation_id':None,'accepted_generation_count':0,'latest_accepted_generation_id':None})
            if (job['status']=='succeeded' and job['asset'] and re.fullmatch(r'[a-zA-Z0-9_-]+\.(jpg|png)',job['asset'])
                and (self.images/job['asset']).is_file()):
                value['successful_generation_count']+=1
                if value['latest_generation_id'] is None:value['latest_generation_id']=job['id']
            if job['review']=='accepted':
                value['accepted_generation_count']+=1
                if value['latest_accepted_generation_id'] is None:value['latest_accepted_generation_id']=job['id']
        matching=[]
        for row in forms:
            value=counts.get(row['id'],{'successful_generation_count':0,'latest_generation_id':None,'accepted_generation_count':0,'latest_accepted_generation_id':None})
            success=value['successful_generation_count'];accepted=value['accepted_generation_count']
            matches={'all':True,'missing_image':success==0,'has_image':success>0,'pending_acceptance':success>0 and accepted==0,'accepted':accepted>0}
            if (matches[status] and (ai_reviewed is None or (row['ai_reviewed_at'] is not None)==ai_reviewed)
                and (selected is None or (row['selected_job'] is not None)==selected)
                and (successful_generation_count is None or success==successful_generation_count)):
                matching.append({'form_id':row['id'],'ingredient_id':row['source'],'ingredient_name':row['name_ru'],'ingredient_name_en':row['name_en'],
                    'visual_form':row['form_ru'],'visual_form_en':row['form_en'],'current_prompt_version_id':row['current_prompt_version_id'],
                    'selected_generation_id':row['selected_job'],'ai_reviewed_at':row['ai_reviewed_at'],**value})
        remaining=[item for item in matching if after is None or (item['ingredient_id'],item['form_id'])>after]
        page=remaining[:limit];next_cursor=None
        if len(remaining)>limit:
            last=page[-1]
            payload={'v':2 if filtered else 1,'status':status,'source':last['ingredient_id'],'id':last['form_id']}
            if filtered:payload['filters']=filters
            next_cursor=base64.urlsafe_b64encode(dumps(payload).encode()).decode().rstrip('=')
        return {'forms':page,'total_matching':len(matching),'returned':len(page),'next_cursor':next_cursor}

    def set_fal_budget(self, daily_limit_usd):
        try:
            if isinstance(daily_limit_usd,bool) or daily_limit_usd is None:raise ValueError()
            limit=decimal.Decimal(str(daily_limit_usd))
            if not limit.is_finite() or not 0<=limit<=10000:raise ValueError()
        except (decimal.InvalidOperation,ValueError,TypeError):raise AppError('Лимит fal.ai должен быть от 0 до 10000 USD.')
        with self.connect() as con:
            con.execute('UPDATE generation_config SET daily_limit_usd_micros=? WHERE id=1',(int(limit*1000000),))
        return self.settings()

    def catalog(self, query='', mode='all'):
        with self.connect() as con:
            groups = {}
            for row in con.execute('SELECT * FROM forms ORDER BY source,matrix_row'):
                if query.casefold() not in (row['name_ru']+' '+row['name_en']+' '+row['source']+' '+row['form_ru']+' '+row['form_en']).casefold():
                    continue
                form = self.describe(con, row)
                if mode=='ready' and not form['ready'] or mode=='selected' and not form['selected_job'] or mode=='verified' and not form['verified']:
                    continue
                source = form['source']
                group = groups.setdefault(source, {'source':source,'name_ru':form['name_ru'],'name_en':form['name_en'],'count':0,'ready':0,'selected':0,'verified':0})
                group['count'] += 1; group['ready'] += int(form['ready']); group['selected'] += bool(form['selected_job']); group['verified'] += int(form['verified'])
            return {'ingredients':list(groups.values()),'total_ingredients':751,'total_forms':1799}

    def forms(self, source):
        with self.connect() as con:
            return [self.describe(con, row) for row in con.execute('SELECT * FROM forms WHERE source=? ORDER BY matrix_row', (source,))]

    def detail(self, fid):
        with self.connect() as con:
            form = self.describe(con, self.form_record(con, fid))
            form['jobs'] = [self.public_job(row) for row in con.execute('SELECT * FROM jobs WHERE form_id=? ORDER BY created_at DESC,id', (fid,))]
            return form

    def public_job(self, row):
        job = dict(row)
        job['blocks'] = json.loads(job['blocks'])
        job['image_url'] = BASE_PATH + '/media/' + job['asset'] if job['asset'] else None
        with self.connect() as con:
            variant=con.execute("SELECT * FROM image_variants WHERE generation_id=? AND kind='transparent' ORDER BY created_at DESC LIMIT 1",(job['id'],)).fetchone()
        job['transparent_image_url']=BASE_PATH+'/media/'+variant['asset'] if variant and variant['status']=='ready' and variant['asset'] and (self.images/variant['asset']).is_file() else None
        job['transparent_status']=variant['status'] if variant else None
        job['transparent_error']=variant['error'] if variant else None
        return job

    def preview(self, fid, blocks):
        with self.connect() as con:
            form = self.form_record(con, fid)
            clean = self.validate_blocks(form, blocks)
            version=self.current_version(con,fid)
            style={'id':version['style_id'],'text':version['style_text']}
            return {'prompt':compile_prompt(clean,style['text']).strip(),'style_id':style['id']}

    def save_form(self, fid, blocks, rejection_rules, expected_current_version_id=None):
        if not isinstance(rejection_rules,str) or len(rejection_rules)>5000:
            raise AppError('Неверные критерии отказа.')
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            original=self.form_record(con,fid)
            if expected_current_version_id is not None and original['current_prompt_version_id']!=expected_current_version_id:
                raise AppError('Conflict: current_prompt_version_id changed. Reload form before saving.',409)
            clean = self.validate_blocks(original,blocks)
            if json.loads(original['blocks'])!=clean:
                con.execute("UPDATE autoprompts SET origin='edited' WHERE form_id=?",(fid,))
            version=self.current_version(con,fid)
            self.create_prompt_version(con,fid,clean,version['style_id'],version['style_text'])
            con.execute('UPDATE forms SET rejection_rules=?,updated_at=? WHERE id=?',(rejection_rules,time.time(),fid))
        return self.detail(fid)

    def prepare_in_transaction(self,con,fid):
        form=self.form_record(con,fid)
        version=self.current_version(con,fid)
        if version['complete']:
            return False
        row=dict(form);row['requirements_en']=json.loads(row['requirements_en'])
        draft=prompt_builder.prepare(row)
        stored=json.loads(form['blocks'])
        blocks={key:stored.get(key,'') or draft['blocks'][key] for key in FIELDS}
        clean=self.validate_blocks(form,blocks,True)
        changed=stored!=clean
        if changed:
            self.create_prompt_version(con,fid,clean,version['style_id'],version['style_text'],origin='preparation',force=True)
            con.execute('UPDATE forms SET rejection_rules=?,updated_at=? WHERE id=?',(form['rejection_rules'] or draft['rejection_rules'],time.time(),fid))
            origin='automatic' if not any(stored.values()) else 'edited'
            con.execute('INSERT OR REPLACE INTO autoprompts VALUES(?,?,?,?)',(fid,draft['version'],origin,time.time()))
        return changed

    def prepare_forms(self,ids=None):
        if ids is not None and (not isinstance(ids,list) or not ids or len(ids)>1799 or any(not isinstance(i,str) for i in ids)):
            raise AppError('Выберите формы для подготовки.')
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            targets=list(dict.fromkeys(ids)) if ids is not None else [r['id'] for r in con.execute('SELECT id FROM forms ORDER BY matrix_row')]
            prepared=sum(self.prepare_in_transaction(con,fid) for fid in targets)
        return {'prepared':prepared,'total':len(targets),'version':prompt_builder.VERSION}

    def bulk_forms(self,query='',source='',mode='all'):
        with self.connect() as con:
            output=[]
            for row in con.execute('SELECT * FROM forms ORDER BY matrix_row'):
                if source and row['source']!=source:continue
                if query.casefold() not in ' '.join(str(row[k]) for k in ('name_ru','name_en','source','form_ru','form_en')).casefold():continue
                form=self.describe(con,row)
                if mode=='pending' and form['generated'] or mode=='selected' and not form['selected_job'] or mode=='verified' and not form['verified']:continue
                output.append({key:form[key] for key in ('id','source','name_ru','name_en','form_ru','form_en','ready','generated','selected_job','verified','prompt_origin')})
            return output

    def enqueue_bulk(self,ids,seed,request_key,provider=None):
        if not isinstance(ids,list) or not ids or len(ids)>1799 or any(not isinstance(i,str) for i in ids):
            raise AppError('Выберите от 1 до 1799 форм.')
        if type(seed) is not int or not 0<=seed<=2147483645 or not isinstance(request_key,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{8,128}',request_key):
            raise AppError('Неверный seed или идентификатор запроса.')
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            existing=con.execute('SELECT id FROM batches WHERE request_key=?',(request_key,)).fetchone()
            if existing:
                jobs=[dict(r) for r in con.execute('SELECT id,form_id FROM jobs WHERE batch_id=? ORDER BY created_at',(existing['id'],))]
                return {'batch_id':existing['id'],'jobs':[r['id'] for r in jobs],'queued_forms':[r['form_id'] for r in jobs],'deduplicated':True}
            provider=self.generation_provider(con,provider)
            targets=list(dict.fromkeys(ids));slots=self.generation_slots(con,provider)
            if not slots:raise AppError('Нет доступного бюджета выбранного сервиса.',409)
            snapshots=[];skipped=[]
            for fid in targets:
                original=self.form_record(con,fid)
                if con.execute("SELECT 1 FROM jobs WHERE form_id=? AND status IN ('queued','running')",(fid,)).fetchone():
                    skipped.append(fid);continue
                version=self.version_for_generation(con,fid);blocks=json.loads(version['blocks'])
                prompt=version['prompt'];style={'id':version['style_id'],'text':version['style_text']}
                if con.execute("SELECT 1 FROM jobs WHERE form_id=? AND prompt=? AND style_id=? AND width=512 AND height=512 AND status='succeeded' AND review!='rejected' AND origin=?",(fid,prompt,style['id'],provider)).fetchone():
                    skipped.append(fid);continue
                snapshots.append((fid,blocks,prompt,version))
            if not snapshots:
                return {'jobs':[],'queued_forms':[],'remaining_forms':[],'skipped_forms':skipped}
            admitted=snapshots[:slots];batch=uuid.uuid4().hex;now=time.time();jobs=[]
            con.execute('INSERT INTO batches VALUES(?,?,?)',(batch,request_key,now))
            for index,(fid,blocks,prompt,version) in enumerate(admitted):
                style={'id':version['style_id'],'text':version['style_text']}
                jid=uuid.uuid4().hex;jobs.append(jid)
                con.execute('''INSERT INTO jobs(id,batch_id,form_id,prompt,blocks,style_id,style_text,seed,width,height,status,created_at,origin,model,prompt_version_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(jid,batch,fid,prompt,dumps(blocks),style['id'],style['text'],seed,512,512,'queued',now+index*.001,provider,fal_provider.MODEL if provider=='fal' else MODEL,version['version_id']))
        self.wake.set()
        return {'batch_id':batch,'jobs':jobs,'queued_forms':[r[0] for r in admitted],
                'remaining_forms':[r[0] for r in snapshots[slots:]],'skipped_forms':skipped}

    def export_prompts(self):
        with self.connect() as con:
            style=self.active_style(con);forms=[]
            for row in con.execute('SELECT * FROM forms ORDER BY matrix_row'):
                form=self.describe(con,row)
                forms.append({key:form[key] for key in ('id','source','matrix_row','name_ru','name_en','form_ru','form_en','blocks','prompt','rejection_rules','ready','prompt_origin','composer_version','verified','current_prompt_version_id','style_id','style_text')})
        return {'model':MODEL,'width':512,'height':512,'default_seed':42,'style':style,
                'composer_version':prompt_builder.VERSION,'forms':forms}

    def save_style(self, text):
        if not isinstance(text,str) or not text.strip() or len(text)>5000 or re.search('[А-Яа-яЁё]',text):
            raise AppError('Введите общий стиль на английском, до 5000 символов.')
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            if self.active_style(con)['text'] != text.strip():
                con.execute('INSERT INTO styles(text,created_at) VALUES(?,?)',(text.strip(),time.time()))
                style=self.active_style(con)
                for row in con.execute('SELECT id FROM forms').fetchall():
                    version=self.current_version(con,row['id'])
                    self.create_prompt_version(con,row['id'],json.loads(version['blocks']),style['id'],style['text'],origin='style')
        return self.settings()

    def set_budget(self, amount):
        try:
            number = decimal.Decimal(str(amount))
            if not number.is_finite() or number<0 or number>10000000:
                raise ValueError()
            units = int(number * 100)
        except (ValueError, decimal.InvalidOperation, TypeError):
            raise AppError('Введите неотрицательный доступный остаток neurons.')
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            if con.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running') LIMIT 1").fetchone():
                raise AppError('Дождитесь завершения или остановите очередь перед сверкой бюджета.',409)
            con.execute('UPDATE budget SET remaining=?,day=?,confirmed_at=? WHERE id=1',(units,today(),time.time()))
        return self.settings()

    def check_budget(self):
        usage=cloudflare_usage(self.root)
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            if con.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running') LIMIT 1").fetchone():
                raise AppError('Дождитесь завершения или остановите очередь перед сверкой.',409)
            start=dt.datetime.fromisoformat(usage['day']).replace(tzinfo=dt.timezone.utc).timestamp()
            # Analytics can lag; never credit back this application's reservations automatically.
            held=con.execute('SELECT COALESCE(SUM(reserved),0) FROM jobs WHERE started_at>=?',(start,)).fetchone()[0]
            units=max(0,int(decimal.Decimal(str(usage['remaining_neurons']))*100)-held)
            con.execute('UPDATE budget SET remaining=?,day=?,confirmed_at=? WHERE id=1',(units,usage['day'],time.time()))
        usage['local_reservations_neurons']=held/100
        (self.data/'cloudflare_usage.json').write_text(dumps(usage))
        return {'settings':self.settings(),'usage':usage}

    def enqueue(self, fid, seed, count, request_key, provider=None):
        if type(seed) is not int or not 0<=seed<=2147483645 or type(count) is not int or count not in (1,3):
            raise AppError('Допустимы 1 или 3 изображения; seed — целое число от 0 до 2147483645.')
        if not isinstance(request_key,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{8,128}',request_key):
            raise AppError('Отсутствует идентификатор запроса.')
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            existing = con.execute('SELECT id FROM batches WHERE request_key=?',(request_key,)).fetchone()
            if existing:
                previous=con.execute('SELECT form_id,seed,origin FROM jobs WHERE batch_id=? ORDER BY seed LIMIT 1',(existing['id'],)).fetchone()
                if previous and (previous['form_id']!=fid or previous['seed']!=seed or (provider is not None and previous['origin']!=provider)):
                    raise AppError('Идентификатор запроса уже использован с другими параметрами.',409)
                return {'batch_id':existing['id'],'jobs':[r['id'] for r in con.execute('SELECT id FROM jobs WHERE batch_id=? ORDER BY seed',(existing['id'],))]}
            if con.execute("SELECT 1 FROM jobs WHERE form_id=? AND status IN ('queued','running') LIMIT 1",(fid,)).fetchone():
                raise AppError('Эта форма уже находится в очереди.',409)
            version=self.version_for_generation(con,fid);blocks=json.loads(version['blocks'])
            style={'id':version['style_id'],'text':version['style_text']};provider=self.generation_provider(con,provider)
            if self.generation_slots(con,provider)<count:raise AppError('Недостаточно бюджета выбранного сервиса с учётом очереди.',409)
            batch=uuid.uuid4().hex; now=time.time(); ids=[]
            con.execute('INSERT INTO batches VALUES(?,?,?)',(batch,request_key,now))
            prompt=version['prompt']
            for index in range(count):
                jid=uuid.uuid4().hex;ids.append(jid)
                con.execute('''INSERT INTO jobs(id,batch_id,form_id,prompt,blocks,style_id,style_text,seed,width,height,status,created_at,origin,model,prompt_version_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(jid,batch,fid,prompt,dumps(blocks),style['id'],style['text'],seed+index,512,512,'queued',now+index*.001,provider,fal_provider.MODEL if provider=='fal' else MODEL,version['version_id']))
        self.wake.set()
        return {'batch_id':batch,'jobs':ids}

    def dispatch_once(self):
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row=con.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return False
            job=dict(row)
            if job['origin']=='fal':
                if self.fal_budget_info(con)['remaining_usd_micros'] < fal_provider.COST:
                    con.execute("UPDATE jobs SET status='blocked',error='Дневной лимит fal.ai исчерпан.' WHERE origin='fal' AND status='queued'")
                    return False
                con.execute("UPDATE jobs SET status='running',reserved_usd_micros=?,started_at=? WHERE id=?",(fal_provider.COST,time.time(),job['id']))
            else:
                budget=self.budget_info(con)
                if not budget['valid'] or budget['remaining']<COST:
                    con.execute("UPDATE jobs SET status='blocked',error='Очередь остановлена: бюджет недостаточен или день сменился.' WHERE origin='cloudflare' AND status='queued'")
                    return False
                con.execute('UPDATE budget SET remaining=remaining-? WHERE id=1',(COST,))
                con.execute("UPDATE jobs SET status='running',reserved=?,started_at=? WHERE id=?",(COST,time.time(),job['id']))
            con.execute('INSERT INTO events(job_id,event,created_at) VALUES(?,?,?)',(job['id'],'dispatched',time.time()))
        status='succeeded';error=None;asset=None;not_sent=False;stage='provider'
        stop_scope=None if job['origin']=='fal' else 'all'
        try:
            result=self.provider(job)
            stage='metadata'
            raw,provider_meta=result if isinstance(result,tuple) else (result,{})
            if provider_meta.get('provider_request_id'):
                with self.connect() as con:con.execute('UPDATE jobs SET provider_request_id=? WHERE id=?',(provider_meta['provider_request_id'],job['id']))
            if provider_meta.get('provider_seed') not in (None,job['seed']):raise AppError('Сервис вернул другой seed. Результат не принят.',502)
            stage='image_write'
            rawpath=self.images/(job['id']+'.response');rawpath.write_bytes(raw)
            stage='image_read'
            size,extension=image_size(raw)
            if size!=(512,512):
                raise AppError(f'Сервис генерации вернул {size[0]}×{size[1]} вместо 512×512. Файл сохранён для проверки.',502)
            stage='image_save'
            asset=job['id']+extension;rawpath.rename(self.images/asset)
        except (socket.gaierror,TimeoutError,urllib.error.URLError,http.client.HTTPException,ConnectionError) as exc:
            reason=exc.reason if isinstance(exc,urllib.error.URLError) else exc
            with self.connect() as con:
                submitted=con.execute('SELECT provider_request_id FROM jobs WHERE id=?',(job['id'],)).fetchone()[0]
            if isinstance(reason,socket.gaierror) and not submitted:
                not_sent=True;status='failed'
                error='DNS: адрес сервиса генерации недоступен. Запрос не отправлен, резерв освобождён. Остальная очередь продолжает работу.'
            else:
                status='uncertain';error='Ответ не получен. Запрос мог быть выполнен. Бюджет удержан; повтор — вручную после сверки.'
            logging.getLogger('ingredient_studio').warning('generation=%s stage=%s transport_error=%s reason_class=%s request_known=%s',job['id'],stage,type(exc).__name__,type(reason).__name__,bool(submitted))
        except AppError as exc:
            status='failed';error=str(exc)
            if job['origin']=='fal' and exc.queue_fatal:stop_scope='fal'
        except Exception as exc:
            frames=[{'file':Path(frame.filename).name,'line':frame.lineno,'function':frame.name} for frame in traceback.extract_tb(exc.__traceback__)]
            logging.getLogger('ingredient_studio').error('generation=%s stage=%s exception=%s frames=%s',job['id'],stage,type(exc).__name__,dumps(frames))
            status='failed';error='Не удалось сохранить или прочитать результат. Бюджет удержан; повтор — вручную.'
            # Storage and unexpected local failures must not lose more paid results.
            stop_scope='all'
        with self.connect() as con:
            if not_sent:
                con.execute('BEGIN IMMEDIATE')
                if job['origin']=='fal':
                    con.execute('UPDATE jobs SET reserved_usd_micros=0 WHERE id=?',(job['id'],))
                else:
                    con.execute('UPDATE budget SET remaining=remaining+? WHERE id=1',(COST,))
                    con.execute('UPDATE jobs SET reserved=0 WHERE id=?',(job['id'],))
                con.execute('INSERT INTO events(job_id,event,created_at) VALUES(?,?,?)',(job['id'],'not_sent_dns',time.time()))
            con.execute('UPDATE jobs SET status=?,error=?,asset=?,finished_at=? WHERE id=?',(status,error,asset,time.time(),job['id']))
            if status!='succeeded' and not not_sent and stop_scope:
                con.execute("UPDATE jobs SET status='blocked',error='Очередь остановлена после ошибки. Проверьте расход и запустите новую попытку вручную.' WHERE status='queued'"+ (" AND origin='fal'" if stop_scope=='fal' else ''))
            final=self.public_job(con.execute('SELECT * FROM jobs WHERE id=?',(job['id'],)).fetchone())
        (self.data/(job['id']+'.json')).write_text(dumps(final))
        return True

    def worker_loop(self):
        while not self.stop_event.is_set():
            try:
                if self.dispatch_once():
                    continue
            except Exception:
                # A local failure must never create an automatic redispatch.
                with self.connect() as con:
                    con.execute("UPDATE jobs SET status='uncertain',error='Локальная ошибка обработки. Сверьте бюджет перед повтором.' WHERE status='running'")
                    con.execute("UPDATE jobs SET status='blocked',error='Очередь остановлена после локальной ошибки.' WHERE status='queued'")
            self.wake.wait(1);self.wake.clear()

    def queue(self):
        with self.connect() as con:
            jobs=[self.public_job(r) for r in con.execute("SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY created_at")]
            return {'jobs':jobs,'budget':self.budget_info(con),'fal_budget':self.fal_budget_info(con)}

    def cancel_queue(self):
        with self.connect() as con:
            con.execute("UPDATE jobs SET status='cancelled',error='Остановлено пользователем.' WHERE status='queued'")
        return self.queue()

    def next_review(self, skipped=''):
        ids=skipped.split(',') if skipped else []
        if len(ids)>5000 or any(not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}',v) for v in ids):
            raise AppError('Неверный список пропущенных изображений.')
        with self.connect() as con:
            rows=con.execute("SELECT * FROM jobs WHERE status='succeeded' AND asset IS NOT NULL AND review='unreviewed' ORDER BY created_at,id").fetchall()
            excluded=set(ids)
            pending=[r for r in rows if r['id'] not in excluded and (self.images/r['asset']).is_file()]
            item=None
            if pending:
                job=self.public_job(pending[0]);form=self.describe(con,self.form_record(con,job['form_id']))
                item={'generation':job,'form':form}
            return {'item':item,'remaining':len(pending),'skipped':len(rows)-len(pending)}

    def next_form_review(self, skipped=''):
        ids=skipped.split(',') if skipped else []
        if len(ids)>5000 or any(not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}',v) for v in ids):raise AppError('Неверный список пропущенных форм.')
        excluded=set(ids)
        with self.connect() as con:
            counts=con.execute('SELECT COUNT(*),COUNT(selected_job) FROM forms').fetchone()
            rows=con.execute("SELECT j.* FROM jobs j JOIN forms f ON f.id=j.form_id WHERE j.status='succeeded' AND j.asset IS NOT NULL AND f.selected_job IS NULL ORDER BY j.created_at,j.id").fetchall()
            groups={}
            for row in rows:
                if (self.images/row['asset']).is_file():groups.setdefault(row['form_id'],[]).append(self.public_job(row))
            ready=len(groups)
            groups={fid:jobs for fid,jobs in groups.items() if fid not in excluded}
            item=None
            if groups:
                fid=next(iter(groups));item={'form':self.describe(con,self.form_record(con,fid)),'generations':groups[fid]}
            return {'item':item,'remaining':len(groups),'stats':{'total_forms':counts[0],'selected_forms':counts[1],
                'unselected_forms':counts[0]-counts[1],'ready_forms':ready,'without_images_forms':counts[0]-counts[1]-ready,'skipped_forms':ready-len(groups)}}

    def review(self,jid,action,reason='',only_unreviewed=False,only_unselected=False,expected_form_id=None):
        if action not in ('accept','select','reject','comment') or not isinstance(reason,str) or len(reason)>5000:
            raise AppError('Неверное действие.')
        if action=='reject' and not reason.strip():
            raise AppError('Укажите причину отклонения.')
        if action=='comment' and not reason.strip():raise AppError('Напишите комментарий.')
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            job=con.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
            if not job or job['status']!='succeeded':
                raise AppError('Можно оценить только готовое изображение.',409)
            if expected_form_id is not None:
                self.form_record(con,expected_form_id)
                if job['form_id']!=expected_form_id:raise AppError('Изображение принадлежит другой форме.',409)
                if not job['asset'] or not (self.images/job['asset']).is_file():raise AppError('Файл изображения недоступен.',409)
                reason=job['reason'] or ''
            if only_unreviewed and job['review']!='unreviewed':raise AppError('Изображение уже оценено. Откройте следующее.',409)
            if only_unselected and con.execute('SELECT selected_job FROM forms WHERE id=?',(job['form_id'],)).fetchone()[0] is not None:
                raise AppError('У этой формы уже выбран результат. Откройте следующую форму.',409)
            con.execute('UPDATE jobs SET review=?,reason=? WHERE id=?',(job['review'] if action=='comment' else 'rejected' if action=='reject' else 'accepted',reason.strip(),jid))
            if action=='select':
                con.execute('UPDATE forms SET selected_job=? WHERE id=?',(jid,job['form_id']))
            elif action=='reject':
                con.execute('UPDATE forms SET selected_job=NULL WHERE selected_job=?',(jid,))
                con.execute('UPDATE forms SET ai_reviewed_at=NULL WHERE id=?',(job['form_id'],))
            fid=job['form_id']
        return self.detail(fid)

    def download(self,jid):
        with self.connect() as con:
            row=con.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
            if not row or row['status']!='succeeded' or not row['asset']:
                raise AppError('Изображение не найдено.',404)
            job=self.public_job(row);form=self.form_record(con,job['form_id'])
        meta={k:job[k] for k in ['id','form_id','prompt','blocks','style_id','style_text','seed','width','height','review','reason','origin','model','reserved_usd_micros','provider_request_id','prompt_version_id']}
        meta.update(source=form['source'],visual_form=form['form_en'],model=job["model"] or MODEL)
        meta['transparent_image_url']=job['transparent_image_url']
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.write(self.images/job['asset'],'image'+Path(job['asset']).suffix)
            if job['transparent_image_url']:
                archive.write(self.images/job['transparent_image_url'].rsplit('/',1)[1],'image-transparent.png')
            archive.writestr('metadata.json',json.dumps(meta,ensure_ascii=False,indent=2))
        return buffer.getvalue()


class Handler(BaseHTTPRequestHandler):
    studio = None
    def log_message(self,format,*args):
        pass
    def respond(self,value,status=200,content_type='application/json; charset=utf-8',filename=None,headers=None):
        raw=value if isinstance(value,bytes) else dumps(value).encode()
        self.send_response(status);self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        for key,value in (headers or {}).items():self.send_header(key,value)
        if filename:self.send_header('Content-Disposition',f'attachment; filename="{filename}"')
        self.end_headers();self.wfile.write(raw)
    def route(self):
        parsed=urllib.parse.urlparse(self.path);path=parsed.path;query=urllib.parse.parse_qs(parsed.query);app=self.studio
        if BASE_PATH:
            if path == BASE_PATH and self.command == 'GET':
                self.send_response(308);self.send_header('Location',BASE_PATH+'/');self.end_headers();return
            if not path.startswith(BASE_PATH+'/'):
                raise AppError('Страница не найдена.',404)
            path=path[len(BASE_PATH):]
        host=self.headers.get('Host','').split(':')[0]
        public_host=urllib.parse.urlparse(PUBLIC_URL).hostname
        if host not in ('127.0.0.1','localhost',public_host):
            raise AppError('Только локальный доступ.',403)
        if path=='/mcp':
            authorization=self.headers.get('Authorization','')
            parts=authorization.split(' ',1)
            token=parts[1] if len(parts)==2 and parts[0].lower()=='bearer' else ''
            valid=bool(app.mcp_token_hash) and 1<=len(token)<=256 and hmac.compare_digest(
                hashlib.sha256(token.encode()).hexdigest(),app.mcp_token_hash)
            if not valid:
                return self.respond(MCP.error(None,-32000,'A valid MCP bearer token is required'),401,
                                    headers={'WWW-Authenticate':'Bearer realm="Ingredient Generator MCP"'})
        if path=='/mcp' and self.command=='DELETE':
            origin=self.headers.get('Origin')
            expected=('https://'+urllib.parse.urlparse(PUBLIC_URL).netloc) if PUBLIC_URL else f'http://{self.headers.get("Host")}'
            if origin and origin!=expected:raise AppError('Запрос с другого сайта запрещён.',403)
        body={}
        if self.command in ('POST','PUT'):
            origin=self.headers.get('Origin')
            expected_origin=('https://'+urllib.parse.urlparse(PUBLIC_URL).netloc) if PUBLIC_URL else f'http://{self.headers.get("Host")}'
            if origin and origin!=expected_origin:
                raise AppError('Запрос с другого сайта запрещён.',403)
            if not self.headers.get('Content-Type','').startswith('application/json'):
                raise AppError('Ожидается JSON.',415)
            length=int(self.headers.get('Content-Length','0'))
            if length<=0 or length>60000:raise AppError('Неверный размер запроса.',413)
            try:body=json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                if path=='/mcp':return self.respond(MCP.error(None,-32700,'Parse error'),400)
                raise
            if not isinstance(body,dict):raise AppError('Ожидается объект JSON.')
        if path=='/mcp':
            if self.command=='POST' and 'application/json' not in self.headers.get('Accept','application/json'):
                raise AppError('MCP requires Accept: application/json, text/event-stream',406)
            status,value,headers=app.mcp.handle(body,self.headers,self.command)
            return self.respond(b'' if value is None else value,status,headers=headers)
        if self.command=='GET':
            if path=='/api/settings':return self.respond(app.settings())
            if path=='/api/spending':return self.respond(app.spending())
            if path=='/api/catalog':return self.respond(app.catalog(query.get('q',[''])[0],query.get('mode',['all'])[0]))
            if path=='/api/list/forms':
                args={}
                for name in ('status','cursor'):
                    if name in query:args[name]=query[name][0]
                for name in ('limit','successful_generation_count'):
                    if name in query:args[name]=int(query[name][0])
                for name in ('ai_reviewed','selected'):
                    if name in query:
                        if query[name][0] not in ('true','false'):raise AppError('Ожидается true или false: '+name)
                        args[name]=query[name][0]=='true'
                return self.respond(app.list_forms(**args))
            if path=='/api/bulk/forms':return self.respond(app.bulk_forms(query.get('q',[''])[0],query.get('source',[''])[0],query.get('mode',['all'])[0]))
            if path=='/api/export/prompts':return self.respond(app.export_prompts(),filename='ingredient-prompts-512.json')
            if path=='/api/forms':return self.respond(app.forms(query.get('source',[''])[0]))
            if path.startswith('/api/forms/'):return self.respond(app.detail(path.rsplit('/',1)[1]))
            if path=='/api/queue':return self.respond(app.queue())
            if path=='/api/review/next':return self.respond(app.next_review(query.get('skip',[''])[0]))
            if path=='/api/review/forms/next':return self.respond(app.next_form_review(query.get('skip',[''])[0]))
            if path.startswith('/api/download/'):
                jid=path.rsplit('/',1)[1];return self.respond(app.download(jid),content_type='application/zip',filename=jid+'.zip')
            if path.startswith('/media/'):
                name=path.rsplit('/',1)[1]
                if not re.fullmatch(r'[a-zA-Z0-9_-]+\.(jpg|png)',name):raise AppError('Файл не найден.',404)
                target=app.images/name
            else:
                files={'/':'index.html','/app.js':'app.js','/style.css':'style.css','/review':'review.html','/review/':'review.html','/review.js':'review.js','/review.css':'review.css'}
                if path not in files:raise AppError('Страница не найдена.',404)
                target=app.root/'web'/files[path]
            if not target.is_file():raise AppError('Файл не найден.',404)
            raw=target.read_bytes()
            if target.suffix=='.html':
                raw=raw.replace(b'<head>',('<head><base href="'+BASE_PATH+'/">').encode(),1)
            return self.respond(raw,content_type=mimetypes.guess_type(target.name)[0] or 'application/octet-stream')
        if self.command=='PUT' and path.startswith('/api/forms/'):
            return self.respond(app.save_form(path.rsplit('/',1)[1],body.get('blocks'),body.get('rejection_rules',''),body.get('expected_current_version_id')))
        if self.command=='POST':
            if path=='/api/mark-reviewed':return self.respond(app.mark_reviewed(body.get('form_id')))
            if path=='/api/preview':return self.respond(app.preview(body.get('form_id'),body.get('blocks')))
            if path=='/api/styles':return self.respond(app.save_style(body.get('text')))
            if path=='/api/provider':return self.respond(app.set_generation_config(body.get('provider'),body.get('daily_limit_usd')))
            if path=='/api/settings/fal-budget':return self.respond(app.set_fal_budget(body.get('daily_limit_usd')))
            if path=='/api/spending/check':return self.respond(app.check_fal_usage())
            if path=='/api/budget':return self.respond(app.set_budget(body.get('remaining_neurons')))
            if path=='/api/budget/check':return self.respond(app.check_budget())
            if path=='/api/generate':return self.respond(app.enqueue(body.get('form_id'),body.get('seed'),body.get('count'),body.get('request_key'),body.get('provider')),202)
            if path=='/api/prepare':return self.respond(app.prepare_forms(body.get('form_ids')))
            if path=='/api/generate/bulk':return self.respond(app.enqueue_bulk(body.get('form_ids'),body.get('seed'),body.get('request_key'),body.get('provider')),202)
            if path=='/api/queue/stop':return self.respond(app.cancel_queue())
            if path=='/api/review':return self.respond(app.review(body.get('job_id'),body.get('action'),body.get('reason',''),body.get('only_unreviewed') is True,body.get('only_unselected') is True))
        raise AppError('Маршрут не найден.',404)
    def handle_request(self):
        try:self.route()
        except AppError as e:self.respond({'error':str(e)},e.status)
        except (json.JSONDecodeError,ValueError,TypeError):self.respond({'error':'Неверные данные запроса.'},400)
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception:self.respond({'error':'Локальная ошибка. Данные сохранены; запрос генерации не повторяется автоматически.'},500)
    do_GET=handle_request
    do_POST=handle_request
    do_PUT=handle_request
    do_DELETE=handle_request


if __name__=='__main__':
    import argparse
    import fcntl
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8765);parser.add_argument('--host',default='127.0.0.1');options=parser.parse_args()
    (ROOT/'data').mkdir(exist_ok=True)
    lock=(ROOT/'data/server.lock').open('a')
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Студия уже запущена. Откройте её существующее окно.')
    Handler.studio=Studio()
    server=ThreadingHTTPServer((options.host,options.port),Handler)
    print(f'Студия ингредиентов: http://127.0.0.1:{options.port}',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:Handler.studio.stop_event.set();server.server_close()
