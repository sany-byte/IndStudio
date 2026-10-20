"""Session-based MCP Streamable HTTP adapter, sharing the studio's queue and DB."""
import base64
import hashlib
import importlib
import json
import math
import re
import threading
import time
import uuid

VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26')
FIELDS = ('subject','preparation','geometry','material','portion','composition','view','container')


def tool(name, description, properties, required, read_only=True, output=None):
    value = {'name':name,'description':description,
            'inputSchema':{'type':'object','properties':properties,'required':required,'additionalProperties':False},
            'annotations':{'readOnlyHint':read_only,'destructiveHint':False,
                           'idempotentHint':read_only or name not in ('generate','generate_image'),
                           'openWorldHint':name in ('generate','generate_image')}}
    if output:value['outputSchema']=output
    return value


ID = {'type':'string','minLength':1,'maxLength':128}
FORM_ID = {**ID,'pattern':'^[a-f0-9]{24}$','description':'Opaque form ID returned as form_id by get_ingredient/get_form. Copy exactly. NOT the ingredient source, matrix row, seed or generation_id.'}
GENERATION_ID = {**ID,'pattern':'^(?:[a-f0-9]{32}|pilot_[a-zA-Z0-9_-]+)$','description':'Full generation_id returned by generate_image or history: 32 lowercase hex characters for generated images (form_id has 24). Legacy pilot IDs start with pilot_. Copy the ENTIRE value unchanged, including its final 8 characters; never shorten to form_id length or use batch_id, seed, filename or URL.'}
VERSION_ID={**ID,'pattern':'^[a-f0-9]{32}$','description':'Opaque immutable prompt version ID; copy from current_prompt_version_id or prompt_version_id. Not form_id or generation_id.'}
FORM = {'form_id':FORM_ID}
GENERATION = {'generation_id':GENERATION_ID}
STATUSES = ['queued','running','succeeded','failed','uncertain','blocked','interrupted','cancelled']


def output(properties, required):
    return {'type':'object','properties':properties,'required':required,'additionalProperties':True}


FORM_OUTPUT = output({'current_prompt_version_id':VERSION_ID,'form_id':FORM_ID,'id':FORM_ID,'source':ID,'blocks':{'type':'object'},'prompt':{'type':'string'},
                      'ready':{'type':'boolean','description':'Description is ready, NOT proof of image quality or generation completion.'}},['form_id','id','source','blocks','prompt','ready','current_prompt_version_id'])
GENERATION_OUTPUT = output({**GENERATION,**FORM,'prompt_version_id':VERSION_ID,'status':{'type':'string','enum':STATUSES},'seed':{'type':'integer'},
                           'provider':{'enum':['fal','cloudflare']},'model':{'type':'string'},
                           'cost_currency':{'enum':['USD','neurons']},'estimated_reserve':{'type':'number','description':'Conservative reservation, NOT an actual billed charge.'},
                           'width':{'const':512},'height':{'const':512},'image_ready':{'type':'boolean'},
                           'next_tool':{'enum':['get_generation_history','get_generation_image',None]}},
                          ['generation_id','form_id','status','seed','width','height','image_ready','next_tool','provider','model','cost_currency','estimated_reserve','prompt_version_id'])
IMAGE_OUTPUT = output({**GENERATION,**FORM,'seed':{'type':'integer'},'width':{'type':'integer','minimum':1},
                      'height':{'type':'integer','minimum':1},'mime_type':{'enum':['image/png','image/jpeg']},
                      'status':{'const':'succeeded'},'image_ready':{'const':True},'review':{'type':'string'}},
                     ['generation_id','form_id','seed','width','height','mime_type','status','image_ready','review'])
GENERATE_INPUT = {**FORM,'provider':{'type':'string','enum':['cloudflare','fal'],'description':'Optional service override. fal uses paid USD budget and FLUX.2 Klein 9B; cloudflare uses neurons. Omit to use Studio default from get_generation_settings. Never silently switch providers after an error.'},'seed':{'type':'integer','minimum':0,'maximum':2147483645,
                 'description':'Random seed for this single image; use 42 initially. A seed is not an identifier.'},
                 'request_id':{'type':'string','pattern':'^[a-zA-Z0-9_-]{8,128}$','minLength':8,'maxLength':128,
                 'description':'Optional client idempotency key. Reuse for a retry of the SAME request, including across sessions; use a new key for an intentionally new attempt.'}}
GENERATE_DESCRIPTION = ('STEP 2 after get_form(form_id). Enqueue exactly ONE 512x512 image using Studio\'s existing prompt snapshot and provider-specific budget. Call get_generation_settings/get_spending to inspect service and costs. '
    'Return structuredContent.generation_id: an opaque string accepted VERBATIM by get_generation_image(generation_id). '
    'This call is asynchronous, not an image result. For queued/running, poll get_generation_history with the SAME form_id and find the record with the SAME generation_id; '
    'wait between polls. Only when status=succeeded call get_generation_image with that generation_id to SEE the pixels. '
    'Do not call generate_image again just because the image is pending. On failed/uncertain/blocked/interrupted/cancelled stop and inspect the error; no automatic provider retry. Studio continues other fal queue items after an isolated attempt failure; account, rate, budget and local storage failures may block the queue. '
    'An isError result means no success ID was returned; never invent an ID. Optional request_id protects client retries.')
TOOLS = [
    tool('list_forms','Read-only global batch navigation. missing_image: no succeeded generation with a saved image; has_image: at least one. pending_acceptance: has an image but zero jobs.review=accepted; accepted: at least one accepted review. Acceptance and final selection are separate. Pilots count at their original size. latest_generation_id is the latest successful saved image, not the latest failed attempt. Order: ingredient_id, form_id. For optimizer batches use status=all, successful_generation_count=1, selected=false, ai_reviewed=false. missing_image always means zero saved images and cannot match count=1. After a final form-level outcome call mark_reviewed, including unsuccessful/HUMAN_REVIEW outcomes. Never mark at start or after an interrupted attempt. Copy next_cursor unchanged with the same status and all filters; total_matching counts the whole current selection. For primary batches call missing_image without cursor: successful forms disappear automatically. For moderation use pending_acceptance → get_form/history → get_generation_image → accept/reject. Never regenerate merely because acceptance is pending. This tool does not prepare prompts, generate, review or change data.',
         {'status':{'type':'string','enum':['all','missing_image','has_image','pending_acceptance','accepted'],'default':'all'},'limit':{'type':'integer','minimum':1,'maximum':100,'default':10},'cursor':{'type':['string','null'],'minLength':1,'maxLength':4096},'ai_reviewed':{'type':'boolean','description':'False: ai_reviewed_at is null; true: already completed AI review. Omit for legacy behavior.'},'successful_generation_count':{'type':'integer','minimum':0,'description':'Exact number of successful saved images, including pilots.'},'selected':{'type':'boolean','description':'False: no final selected_generation_id. True: final image selected.'}},[],
         output=output({'forms':{'type':'array','items':output({'form_id':FORM_ID,'ingredient_id':ID,'ingredient_name':{'type':'string'},'visual_form':{'type':'string'},'current_prompt_version_id':VERSION_ID,'successful_generation_count':{'type':'integer','minimum':0},'latest_generation_id':{'type':['string','null']},'accepted_generation_count':{'type':'integer','minimum':0},'latest_accepted_generation_id':{'type':['string','null']},'selected_generation_id':{'type':['string','null']},'ai_reviewed_at':{'type':['string','null'],'description':'UTC ISO 8601 first completed AI review timestamp.'}},['form_id','ingredient_id','ingredient_name','visual_form','current_prompt_version_id','successful_generation_count','latest_generation_id','accepted_generation_count','latest_accepted_generation_id','selected_generation_id','ai_reviewed_at'])},'total_matching':{'type':'integer','minimum':0},'returned':{'type':'integer','minimum':0,'maximum':100},'next_cursor':{'type':['string','null']}},['forms','total_matching','returned','next_cursor'])),
    tool('mark_reviewed','After completing a final form-level AI outcome (production ready, best available, not production ready, spec conflict, provider/vision unavailable, or human review), persist that this form was processed. Do not call when merely fetching/starting a form, while generation is pending, or after a crash mid-form. Idempotent: preserves the FIRST UTC timestamp on repeat. Does not select or accept images, modify prompts, generate, spend budget or imply visual quality. Explicit re-review can use get_form regardless of this timestamp.',
         FORM,['form_id'],False,output=output({**FORM,'ai_reviewed_at':{'type':'string','description':'First completed AI review, UTC ISO 8601 ending Z.'}},['form_id','ai_reviewed_at'])),
    tool('set_fal_budget','Set the Studio daily fal.ai USD spending cap (0–10000), shared with Settings. Does not add money to fal, change provider, erase existing reservations, generate images or retry jobs. Lowering the cap below current reserves blocks new requests. Day boundary is UTC midnight.',
         {'daily_limit_usd':{'type':'number','minimum':0,'maximum':10000}},['daily_limit_usd'],False),
    tool('get_generation_prompt','Read the exact frozen prompt, all blocks and style used by a generation. snapshot_complete=false / exact_rollback_possible=false means incomplete legacy history; never reconstruct missing fields or restore it by guessing.',GENERATION,['generation_id'],
         output=output({**GENERATION,**FORM,'prompt_version_id':{'type':['string','null']},'blocks':{'type':'object'},'prompt':{'type':'string'},'style_id':{'type':['integer','null']},'style_text':{'type':['string','null']},'snapshot_complete':{'type':'boolean'},'exact_rollback_possible':{'type':'boolean'}},['generation_id','form_id','prompt_version_id','blocks','prompt','style_id','style_text','snapshot_complete','exact_rollback_possible'])),
    tool('restore_prompt_version','Atomically activate an existing immutable version of this form, including its historical style. Read get_prompt first and pass its current_prompt_version_id as expected_current_version_id. Conflict means someone changed current; stop and reread, never overwrite blindly. Does not generate or alter queued snapshots. Next NEW generate_image uses this exact version; use a new request_id.',
         {**FORM,'version_id':VERSION_ID,'expected_current_version_id':VERSION_ID},['form_id','version_id','expected_current_version_id'],False,
         output=output({**FORM,'previous_prompt_version_id':VERSION_ID,'current_prompt_version_id':VERSION_ID,'restored_from_version_id':VERSION_ID},['form_id','previous_prompt_version_id','current_prompt_version_id','restored_from_version_id'])),
    tool('restore_prompt_from_generation','Restore the exact complete version used by a best candidate generation. Read get_prompt for expected_current_version_id first. Atomic compare-and-swap; conflicts do not overwrite another edit. Rejects incomplete legacy pilots. Restores fields AND style without compiling or autofilling. No generation or provider call.',
         {**GENERATION,'expected_current_version_id':VERSION_ID},['generation_id','expected_current_version_id'],False,
         output=output({**FORM,'source_generation_id':GENERATION_ID,'previous_prompt_version_id':VERSION_ID,'current_prompt_version_id':VERSION_ID,'restored_prompt_version_id':VERSION_ID},['form_id','source_generation_id','previous_prompt_version_id','current_prompt_version_id','restored_prompt_version_id'])),
    tool('get_generation_settings','Inspect the default service, configured services, 512x512 parameters and separate Cloudflare neuron/fal USD budgets before generating.',{},[]),
    tool('get_spending','Read local conservative reservations and last fal Usage API reconciliation. USD estimates are NOT actual invoices. Failed/uncertain attempts retain reserves. External workspace usage is shown separately if reconciled.',{},[]),
    tool('refresh_fal_spending','Read actual billed usage for this model today from fal Usage API (requires Admin key). No image generation, no budget credit, no retries. Saves timestamped reconciliation.',{},[],False),
    tool('get_ingredient','START HERE if you do not have a form_id. id is the exact ingredient source string, e.g. tomato. Returns forms[].form_id; choose a form and pass that exact ID to get_form.',
         {'id':{**ID,'description':'Exact ingredient source string from the CSV, NOT a form_id or generation_id.'}},['id'],
         output=output({'id':ID,'forms':{'type':'array','items':FORM_OUTPUT}},['id','forms'])),
    tool('get_form','STEP 1: inspect this form\'s RU/EN matrix requirements, blocks, prompt, container policy and rejection criteria BEFORE generating. Returns form_id (id is the same legacy alias). Pass form_id unchanged to generate_image. ready describes prompt readiness only; it does not mean an image is good or complete.',
         FORM,['form_id'],output=FORM_OUTPUT),
    tool('get_prompt','Optional before generation: inspect the current English prompt, blocks, style and rejection criteria. form_id identifies the form; old generations retain their own immutable prompt snapshots in history.',
         FORM,['form_id'],output=output({**FORM,'current_prompt_version_id':VERSION_ID,'id':FORM_ID,'prompt':{'type':'string'},'blocks':{'type':'object'},'style':{'type':'object'}},['form_id','id','prompt','blocks','style','current_prompt_version_id'])),
    tool('get_generation_history','Between STEP 2 and STEP 3: poll with the SAME form_id used to generate. Find generations[].generation_id equal to the exact ID returned by generate_image; id is its legacy alias. queued/running means wait; succeeded means call get_generation_image(generation_id). Other statuses mean stop and inspect error, without automatic regeneration. Review flags (accepted/rejected) are distinct from completion status.',
         FORM,['form_id'],output=output({**FORM,'generations':{'type':'array','items':output({**GENERATION,**FORM,'id':GENERATION_ID,'status':{'enum':STATUSES},'image_ready':{'type':'boolean'},'prompt':{'type':'string'}},['generation_id','form_id','id','status','image_ready','prompt'])}},['form_id','generations'])),
    tool('generate_image',GENERATE_DESCRIPTION,GENERATE_INPUT,['form_id','seed'],False,GENERATION_OUTPUT),
    tool('generate','LEGACY ALIAS of generate_image, with identical input, output and idempotency. Prefer generate_image; never call BOTH for one desired attempt. '+GENERATE_DESCRIPTION,
         GENERATE_INPUT,['form_id','seed'],False,GENERATION_OUTPUT),
    tool('get_generation_image','STEP 3: pass EXACTLY the generation_id from generate_image, without conversion. Only status=succeeded is retrievable. The tool result content includes REAL MCP ImageContent: type=image, data=base64 ORIGINAL PNG/JPEG bytes, mimeType=image/png or image/jpeg, directly available to a vision-capable Hermes model. This is not a link, placeholder or textual description. structuredContent contains metadata only; inspect the IMAGE content to compare it with get_form requirements and rejection criteria. Pending means poll history; a missing ID must not be guessed. Pilot images may be 1024x1024, new generations are 512x512.',
         GENERATION,['generation_id'],output=IMAGE_OUTPUT),
    tool('update_prompt_fields','Create a new immutable full version on each actual field change, preserving other blocks and the current version style. No-op retains version. Returns previous_prompt_version_id, prompt_version_id and changed_fields. Optional expected_current_version_id prevents concurrent overwrite. Does not generate.',
         {**FORM,'expected_current_version_id':VERSION_ID,'changes':{'type':'object','properties':{f:{'type':'string','maxLength':2500} for f in FIELDS},'additionalProperties':False,'minProperties':1}},['form_id','changes'],False,output=output({**FORM,'previous_prompt_version_id':VERSION_ID,'prompt_version_id':VERSION_ID,'changed_fields':{'type':'array','items':{'type':'string','enum':list(FIELDS)}}},['form_id','previous_prompt_version_id','prompt_version_id','changed_fields'])),
    tool('accept_generation','Mark a completed image accepted; does not select it as the final image or automatically verify a prompt. Verification still requires 3 accepted distinct seeds with the same prompt/style/size.',GENERATION,['generation_id'],False),
    tool('select_generation','After inspecting candidate images with get_generation_image, choose ONE final image for this form, like Studio review page Select. Pass form_id from get_form and the exact generation_id from history/generate_image. Atomically verifies the image belongs to this form, succeeded and has a saved file; marks it accepted and sets the form\'s single final selection. Replaces any previous final selection, preserving other candidates, existing comments and prompt versions. Does not generate, restore a prompt or automatically verify it. Repeating the same choice is safe. Confirm selected_generation_id in this result or list_forms, or selected_job in get_form.',
         {**FORM,**GENERATION},['form_id','generation_id'],False,output=output({**FORM,**GENERATION,'selected_generation_id':GENERATION_ID,'review':{'type':'string','enum':['accepted']},'prompt_verified':{'type':'boolean'}},['form_id','generation_id','selected_generation_id','review','prompt_verified'])),
    tool('reject_generation','Reject a completed image with a non-empty reason. Clears final selection if this image was selected. Does not automatically regenerate.',
         {**GENERATION,'reason':{'type':'string','minLength':1,'maxLength':5000}},['generation_id','reason'],False),
]


class MCP:
    def __init__(self, studio):
        self.studio=studio
        self.sessions={}
        self.lock=threading.Lock()

    def handle(self, body, headers, command):
        """Return HTTP status, JSON body (None for empty), and response headers."""
        sid=headers.get('Mcp-Session-Id')
        with self.lock:
            now=time.time()
            self.sessions={k:v for k,v in self.sessions.items() if now-v['used']<86400}
            session=self.sessions.get(sid)
            if session:session['used']=now
        if command=='GET':return 405,None,{'Allow':'POST, DELETE'}
        if command=='DELETE':
            if not session:return 404,None,{}
            with self.lock:self.sessions.pop(sid,None)
            return 204,None,{}
        if not isinstance(body,dict) or body.get('jsonrpc')!='2.0' or not isinstance(body.get('method'),str):
            return 400,self.error(None,-32600,'Invalid JSON-RPC request'),{}
        rid=body.get('id');method=body['method'];params=body.get('params',{})
        if not isinstance(params,dict) or ('id' in body and (type(rid) not in (str,int))):
            return 400,self.error(None,-32600,'Invalid request ID or parameters'),{}
        if method=='initialize' and 'id' in body:
            version=params.get('protocolVersion')
            version=version if version in VERSIONS else VERSIONS[0]
            sid=uuid.uuid4().hex
            with self.lock:
                if len(self.sessions)>=1000:return 503,self.error(rid,-32000,'Too many sessions'),{}
                self.sessions[sid]={'version':version,'used':time.time()}
            result={'protocolVersion':version,'capabilities':{'tools':{'listChanged':False}},
                    'serverInfo':{'name':'ingredient-generator','version':'1.4.7'},
                    'instructions':'AI optimizer batches: list_forms(status=all, successful_generation_count=1, selected=false, ai_reviewed=false); Studio is authoritative, completed previous forms do not count toward another N. Call mark_reviewed only after a final outcome, never at start or after interruption. Batch navigation: list_forms(missing_image) for primary generation, list_forms(pending_acceptance) for review; successful unaccepted images must not be regenerated. Workflow: get_ingredient(source) to find forms[].form_id → get_form(form_id) → generate_image(form_id,seed) → retain its exact generation_id → poll get_generation_history(form_id) for that generation_id until succeeded → get_generation_image(generation_id) → inspect actual ImageContent against form requirements → accept_generation or reject_generation with reason; to choose one final asset for the form call select_generation(form_id,generation_id). Acceptance and final selection differ. Ingredient source, form_id, generation_id and seed are different concepts. Copy opaque IDs unchanged. generate is a legacy alias; never call both generators for one attempt. Queued/running means wait, not regenerate. Budget and provider error policy remain Studio\'s; never auto-retry failed/uncertain attempts. structuredContent is metadata, not the pixels. Prompt optimization: preserve prompt_version_id for candidates; get_generation_prompt → read get_prompt.current_prompt_version_id → restore_prompt_from_generation with expected_current_version_id → confirm current version. Incomplete legacy snapshots cannot be restored. New generation after restore uses that frozen version.'}
            return 200,{'jsonrpc':'2.0','id':rid,'result':result},{'Mcp-Session-Id':sid}
        if not session:return 404,self.error(rid,-32000,'Session expired or missing; initialize again'),{}
        version=headers.get('MCP-Protocol-Version',session['version'])
        if version!=session['version']:return 400,self.error(rid,-32600,'Protocol version mismatch'),{}
        if 'id' not in body:return 202,None,{}
        if method=='ping':result={}
        elif method=='tools/list':result={'tools':TOOLS}
        elif method=='tools/call':
            if params.get('name') not in {tool['name'] for tool in TOOLS}:
                return 200,self.error(rid,-32602,'Unknown tool name'),{}
            result=self.call(params,sid,rid)
        else:return 200,self.error(rid,-32601,'Method not found'),{}
        return 200,{'jsonrpc':'2.0','id':rid,'result':result},{}

    @staticmethod
    def error(rid,code,message):
        return {'jsonrpc':'2.0','id':rid,'error':{'code':code,'message':message}}

    def call(self, params, sid, rid):
        # Import lazily: app.py owns the adapter; avoid a circular import.
        api=importlib.import_module(self.studio.__class__.__module__)
        AppError,dumps,image_size,compile_prompt=api.AppError,api.dumps,api.image_size,api.compile_prompt
        try:
            name=params.get('name');args=params.get('arguments',{})
            definition=next((t for t in TOOLS if t['name']==name),None)
            if not definition:raise AppError('Unknown tool')
            schema=definition['inputSchema']
            if not isinstance(args,dict) or set(args)-set(schema['properties']) or set(schema['required'])-set(args):
                raise AppError('Missing or unknown tool arguments')
            for key,value in args.items():
                field=schema['properties'][key];kind=field.get('type');types=kind if isinstance(kind,list) else [kind]
                if value is None and 'null' in types:continue
                if 'string' in types:
                    if not isinstance(value,str) or not value.strip() or not field.get('minLength',1)<=len(value)<=field.get('maxLength',128):raise AppError('Invalid '+key)
                elif kind=='integer':
                    if type(value) is not int:raise AppError('Invalid '+key)
                elif kind=='boolean':
                    if type(value) is not bool:raise AppError('Invalid '+key)
                elif kind=='number':
                    if type(value) not in (int,float) or not math.isfinite(value):raise AppError('Invalid '+key)
                elif kind=='object' and not isinstance(value,dict):raise AppError('Invalid '+key)
                if 'enum' in field and value not in field['enum']:raise AppError('Invalid '+key)
                if 'minimum' in field and value<field['minimum'] or 'maximum' in field and value>field['maximum']:raise AppError('Invalid '+key)
                pattern=schema['properties'][key].get('pattern')
                if pattern and (not isinstance(value,str) or not re.fullmatch(pattern,value)):
                    if key=='generation_id':
                        raise AppError('Incomplete or invalid generation_id '+repr(value)+' ('+str(len(value))+' characters). Generated images require the FULL 32 lowercase hex characters; form_id uses 24. Re-read get_generation_history(form_id) and copy generations[].generation_id unchanged. Do not retry this shortened ID.',400)
                    raise AppError('Invalid '+key+'; copy the exact identifier from the preceding tool result')
            s=self.studio
            if name=='list_forms':value=s.list_forms(**args)
            elif name=='mark_reviewed':value=s.mark_reviewed(args['form_id'])
            elif name=='set_fal_budget':value=s.set_fal_budget(args['daily_limit_usd'])
            elif name=='get_generation_prompt':value=s.get_generation_prompt(args['generation_id'])
            elif name=='restore_prompt_version':value=s.restore_prompt_version(args['form_id'],args['version_id'],args['expected_current_version_id'])
            elif name=='restore_prompt_from_generation':value=s.restore_prompt_from_generation(args['generation_id'],args['expected_current_version_id'])
            elif name=='get_generation_settings':value=s.settings()
            elif name=='get_spending':value=s.spending()
            elif name=='refresh_fal_spending':value=s.check_fal_usage()
            elif name=='get_ingredient':
                forms=s.forms(args['id'])
                if not forms:raise AppError('Ingredient not found; use its exact source string',404)
                value={'id':args['id'],'name_ru':forms[0]['name_ru'],'name_en':forms[0]['name_en'],'forms':[{**form,'form_id':form['id']} for form in forms]}
            elif name in ('get_form','get_prompt','get_generation_history'):
                detail=s.detail(args['form_id'])
                if name=='get_form':value={**{k:v for k,v in detail.items() if k!='jobs'},'form_id':detail['id']}
                elif name=='get_prompt':
                    value={k:detail[k] for k in ('id','source','blocks','prompt','rejection_rules','ready','verified','current_prompt_version_id')}
                    value['style']={'id':detail['style_id'],'text':detail['style_text']}
                    value['form_id']=detail['id']
                else:value={'form_id':detail['id'],'generations':[{**job,'generation_id':job['id'],'image_ready':job['status']=='succeeded' and bool(job['asset'])} for job in detail['jobs']]}
            elif name in ('generate','generate_image'):
                if 'request_id' in args and not re.fullmatch(r'[a-zA-Z0-9_-]{8,128}',args['request_id']):raise AppError('Invalid request_id')
                key=args.get('request_id') or hashlib.sha256((sid+'\0'+json.dumps(rid)).encode()).hexdigest()
                request_key='mcp_'+hashlib.sha256(key.encode()).hexdigest()
                with s.connect() as con:
                    previous=con.execute('SELECT j.form_id,j.seed,j.origin FROM jobs j JOIN batches b ON j.batch_id=b.id WHERE b.request_key=?',(request_key,)).fetchone()
                    if previous and (previous['form_id']!=args['form_id'] or previous['seed']!=args['seed'] or ('provider' in args and previous['origin']!=args['provider'])):
                        raise AppError('Request ID already used with different arguments')
                batch=s.enqueue(args['form_id'],args['seed'],1,request_key,args.get('provider'))
                with s.connect() as con:
                    row=con.execute('SELECT * FROM jobs WHERE id=?',(batch['jobs'][0],)).fetchone()
                    if row['form_id']!=args['form_id'] or row['seed']!=args['seed']:
                        raise AppError('Request ID already used with different arguments')
                    value={'generation_id':row['id'],'prompt_version_id':row['prompt_version_id'],'form_id':row['form_id'],'status':row['status'],'seed':row['seed'],'width':512,'height':512,'provider':row['origin'],'model':row['model'],'cost_currency':'USD' if row['origin']=='fal' else 'neurons',
                           'estimated_reserve':0.006 if row['origin']=='fal' else 1363.64,
                           'image_ready':row['status']=='succeeded' and bool(row['asset']),
                           'next_tool':'get_generation_image' if row['status']=='succeeded' and row['asset'] else 'get_generation_history' if row['status'] in ('queued','running') else None}
            elif name=='get_generation_image':
                with s.connect() as con:
                    row=con.execute('SELECT * FROM jobs WHERE id=?',(args['generation_id'],)).fetchone()
                if not row:
                    raise AppError('Unknown generation_id. Copy generation_id unchanged from generate_image or get_generation_history; never use form_id, seed, filename or URL.',404)
                if row['status']!='succeeded' or not row['asset']:
                    raise AppError('Generation '+row['id']+' has status='+row['status']+'. No image is available. Inspect get_generation_history(form_id='+row['form_id']+'); queued/running means wait, other statuses mean stop. Do not automatically generate another image.',409)
                if not re.fullmatch(r'[a-zA-Z0-9_-]+\.(jpg|png)',row['asset']):raise AppError('Invalid image asset')
                raw=(s.images/row['asset']).read_bytes();size,extension=image_size(raw)
                metadata={'generation_id':row['id'],'form_id':row['form_id'],'seed':row['seed'],'width':size[0],'height':size[1],'review':row['review'],
                          'provider':row['origin'],'model':row['model'],'status':'succeeded','image_ready':True,'mime_type':'image/png' if extension=='.png' else 'image/jpeg'}
                return {'content':[{'type':'image','data':base64.b64encode(raw).decode(),'mimeType':'image/png' if extension=='.png' else 'image/jpeg'},
                                   {'type':'text','text':dumps(metadata)}],'structuredContent':metadata,'isError':False}
            elif name=='update_prompt_fields':
                value=s.update_prompt_fields(args['form_id'],args['changes'],args.get('expected_current_version_id'))
            elif name=='select_generation':
                detail=s.review(args['generation_id'],'select',expected_form_id=args['form_id'])
                value={'form_id':detail['id'],'generation_id':args['generation_id'],'selected_generation_id':detail['selected_job'],'review':'accepted','prompt_verified':detail['verified']}
            else:
                detail=s.review(args['generation_id'],'accept' if name=='accept_generation' else 'reject',args.get('reason',''))
                job=next(j for j in detail['jobs'] if j['id']==args['generation_id'])
                value={'generation_id':job['id'],'review':job['review'],'reason':job['reason'],'selected_job':detail['selected_job'],'prompt_verified':detail['verified']}
            return {'content':[{'type':'text','text':dumps(value)}],'structuredContent':value,'isError':False}
        except AppError as e:
            return {'content':[{'type':'text','text':str(e)}],'isError':True,'structuredContent':{'error':str(e),'http_status':e.status,'error_code':'conflict' if str(e).startswith('Conflict:') else 'tool_error'}}
        except Exception:
            return {'content':[{'type':'text','text':'Internal tool error; no automatic retry. Inspect history before generating again.'}],'isError':True}
