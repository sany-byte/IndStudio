"""fal.ai integration. Server-only keys; no submission retries."""
import base64
import datetime as dt
import gzip
import http.client
import logging
import json
import re
import time
import urllib.parse
import urllib.request
import urllib.error

MODEL = 'fal-ai/flux-2/klein/9b'
# Conservative reservation: $0.006, one full MP. USD millionths.
COST = 6000

def key(root, admin=False):
    values = {}
    for line in (root / '.env').read_text().splitlines() if (root / '.env').exists() else []:
        match = re.match(r'(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$', line)
        if match: values[match[1]] = match[2].strip('"\'')
    return (values.get('FAL_ADMIN_KEY') if admin else None) or values.get('FAL_KEY') or values.get('FAI_AI', '')

def read_json(response):
    raw=response.read()
    if response.headers.get('Content-Encoding','').lower()=='gzip':raw=gzip.decompress(raw)
    return json.loads(raw)


def result_for_request(root, request_id, AppError=None):
    """Read an existing result; retries are GETs, never new image submissions."""
    if AppError is None:
        from app import AppError
    if not isinstance(request_id,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}',request_id):
        raise AppError('Некорректный request_id fal.ai.',400)
    request=urllib.request.Request('https://queue.fal.run/fal-ai/flux-2/requests/'+request_id,
        headers={'Authorization':'Key '+key(root),'Accept-Encoding':'gzip'})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request,timeout=55) as response:return read_json(response)
        except urllib.error.HTTPError as exc:
            if attempt<2 and exc.code in (429,500,502,503,504):time.sleep(1);continue
            raise AppError(f'fal.ai: HTTP {exc.code} при получении существующего результата. Резерв удержан.',502,queue_fatal=exc.code in (401,402,403,429)) from None
        except (http.client.HTTPException,TimeoutError,urllib.error.URLError,ConnectionError,EOFError,json.JSONDecodeError,UnicodeDecodeError) as exc:
            logging.getLogger('ingredient_studio').warning('fal result read request_id=%s attempt=%s exception=%s',request_id,attempt+1,type(exc).__name__)
            if attempt==2:raise
            time.sleep(1)


def image_result(result, request_id, AppError):
    if not isinstance(result,dict):raise AppError('fal.ai вернул некорректный JSON результата. Резерв удержан.',502)
    images=result.get('images', [])
    if not isinstance(images,list) or len(images)!=1:raise AppError('fal.ai не вернул одно изображение. Резерв удержан.',502)
    url=images[0].get('url','')
    if not isinstance(url,str) or not url.startswith('data:image/png;base64,'):
        raise AppError('fal.ai не вернул ожидаемый PNG data URI. Резерв удержан.',502)
    raw=base64.b64decode(url.split(',',1)[1],validate=True)
    return raw,{'provider_request_id':request_id,'provider_seed':result.get('seed')}


def generate(root, snapshot, AppError=None, on_submitted=None):
    if AppError is None:
        from app import AppError
    token=key(root)
    if not token:raise AppError('Добавьте FAL_KEY или FAI_AI в .env.',409,queue_fatal=True)
    request=urllib.request.Request('https://fal.run/'+MODEL,
        data=json.dumps({'prompt':snapshot['prompt'],'seed':snapshot['seed'],
            'image_size':{'width':512,'height':512},'num_images':1,
            'output_format':'png','sync_mode':True}).encode(),
        headers={'Authorization':'Key '+token,'Content-Type':'application/json','X-Fal-No-Retry':'1','Accept-Encoding':'gzip'})
    request_id=None
    try:
        with urllib.request.urlopen(request,timeout=55) as response:
            request_id=response.headers.get('x-fal-request-id','')
            if request_id and on_submitted:on_submitted(request_id)
            result=read_json(response)
    except urllib.error.HTTPError as exc:
        raise AppError(f'fal.ai: HTTP {exc.code}. Резерв USD удержан; повтор только вручную.',502,queue_fatal=exc.code in (401,402,403,429)) from None
    except (http.client.HTTPException,TimeoutError,urllib.error.URLError,ConnectionError,EOFError,json.JSONDecodeError,UnicodeDecodeError):
        if not request_id:raise
        result=result_for_request(root,request_id,AppError)
    return image_result(result,request_id,AppError)

def usage(root, AppError=None):
    if AppError is None:
        from app import AppError
    now=dt.datetime.now(dt.timezone.utc)
    start=now.replace(hour=0,minute=0,second=0,microsecond=0)
    params={'start':start.isoformat(), 'end':now.isoformat(), 'bound_to_timeframe':'false',
            'endpoint_id':MODEL, 'limit':100}
    cost=0; quantity=0
    for _ in range(100):
        req=urllib.request.Request('https://api.fal.ai/v1/models/usage?'+urllib.parse.urlencode(params),
            headers={'Authorization':'Key '+key(root, True)})
        try:
            with urllib.request.urlopen(req,timeout=25) as response: result=json.load(response)
        except urllib.error.HTTPError as e:
            raise AppError(f'Сверка fal.ai: HTTP {e.code}. Для Usage API нужен Admin key (FAL_ADMIN_KEY); локальный учёт работает отдельно.',502) from None
        for bucket in result.get('time_series',[]):
            for item in bucket.get('results',[]):
                if item.get('currency')!='USD':raise AppError('Неизвестная валюта расхода fal.ai.',502)
                cost+=float(item['cost_total']);quantity+=float(item['quantity'])
        if not result.get('has_more'):
            return {'day':start.date().isoformat(),'actual_cost_usd':round(cost,8),
                    'billed_quantity':quantity,'scope':'fal workspace, this model, UTC today',
                    'checked_at':time.time(), 'source':'fal Usage API'}
        cursor=result.get('next_cursor')
        if not cursor:break
        params['cursor']=cursor
    raise AppError('Неполная сверка fal.ai: слишком много страниц. Итог не сохранён.',502)
