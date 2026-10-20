"""Contract tests: no Cloudflare calls, credentials or production data modified."""
import io
import base64
import json
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
import app


def png(width=512,height=512):
    def chunk(tag,data):
        return struct.pack('>I',len(data))+tag+data+struct.pack('>I',zlib.crc32(tag+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\0'+b'\xff\xff\xff'*width)*height))+chunk(b'IEND',b'')


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        for directory in ('inputs','work','outputs/prompt_pilot'):
            shutil.copytree(app.ROOT/directory,self.root/directory)
        (self.root/'.env').write_text('Account_ID=dummy\nYour_API_Token=test-secret-never-export\n')
        self.calls=[]
        def provider(snapshot):
            self.calls.append(snapshot);return png()
        self.provider=provider
        self.s=app.Studio(self.root,provider,False)
        self.fid=app.stable_id('tomato','Whole tomato')
        self.s.set_budget(10000)
    def tearDown(self):
        self.temp.cleanup()
    def blank_form(self):
        return self.s.list_forms('missing_image',1)['forms'][0]['form_id']
    def nav_ids(self,status):
        ids=[];cursor=None
        while True:
            page=self.s.list_forms(status,100,cursor);ids.extend(f['form_id'] for f in page['forms'])
            cursor=page['next_cursor']
            if cursor is None:return ids
    def test_navigation_success_acceptance_and_failed_attempt(self):
        fid=self.blank_form()
        first=self.s.list_forms('missing_image',1)['forms'][0]
        self.assertEqual((first['successful_generation_count'],first['accepted_generation_count']),(0,0))
        batch=self.s.enqueue(fid,42,3,'navigation-three')
        for _ in range(3):self.assertTrue(self.s.dispatch_once())
        self.assertNotIn(fid,self.nav_ids('missing_image'))
        self.assertIn(fid,self.nav_ids('has_image'));self.assertIn(fid,self.nav_ids('pending_acceptance'))
        row=next(f for f in self.s.list_forms('pending_acceptance',100)['forms'] if f['form_id']==fid)
        self.assertEqual((row['successful_generation_count'],row['accepted_generation_count']),(3,0))
        result=self.mcp_call(self.mcp_session(),'accept_generation',{'generation_id':batch['jobs'][0]})
        self.assertFalse(result['isError'])
        self.assertNotIn(fid,self.nav_ids('pending_acceptance'));self.assertIn(fid,self.nav_ids('accepted'))
        row=next(f for f in self.s.list_forms('accepted',100)['forms'] if f['form_id']==fid)
        self.assertEqual((row['successful_generation_count'],row['accepted_generation_count']),(3,1))
        self.assertEqual(row['latest_accepted_generation_id'],batch['jobs'][0]);self.assertIsNone(row['selected_generation_id'])
        self.s.enqueue(fid,49,1,'navigation-failed')
        def fail(_):raise app.AppError('Synthetic failure')
        self.s.provider=fail;self.s.dispatch_once()
        self.assertIn(fid,self.nav_ids('accepted'))
        self.s.review(batch['jobs'][0],'reject','Bad shape')
        self.assertIn(fid,self.nav_ids('pending_acceptance'))
    def test_ai_review_queue_idempotency_filters_and_shared_state(self):
        eligible=self.s.list_forms('missing_image',3)['forms']
        ids=[f['form_id'] for f in eligible]
        for i,fid in enumerate(ids):
            self.s.enqueue(fid,42,1,'ai-review-form-'+str(i));self.s.dispatch_once()
        args={'status':'all','successful_generation_count':1,'selected':False,'ai_reviewed':False}
        page=self.s.list_forms(limit=100,**args)
        self.assertTrue(set(ids).issubset({f['form_id'] for f in page['forms']}))
        h=self.mcp_session();first=self.mcp_call(h,'mark_reviewed',{'form_id':ids[0]})['structuredContent']
        self.assertTrue(first['ai_reviewed_at'].endswith('Z'))
        self.assertEqual(first,self.mcp_call(h,'mark_reviewed',{'form_id':ids[0]})['structuredContent'])
        self.assertIsNone(self.s.detail(ids[0])['selected_job'])
        self.assertEqual(self.s.detail(ids[0])['ai_reviewed_at'],first['ai_reviewed_at'])
        remaining=self.mcp_call(h,'list_forms',{**args,'limit':100})['structuredContent']
        self.assertNotIn(ids[0],[f['form_id'] for f in remaining['forms']])
        self.assertIn(ids[0],[f['form_id'] for f in self.s.list_forms('has_image',100,ai_reviewed=True)['forms']])
        self.assertIn(ids[0],[f['form_id'] for f in self.s.list_forms('has_image',100)['forms']])
        self.assertEqual(self.s.list_forms('missing_image',successful_generation_count=1)['returned'],0)
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(self.s.mark_reviewed,[ids[1],ids[1]]))
        self.assertEqual(results[0],results[1])
        with self.s.connect() as con:jid=con.execute('SELECT id FROM jobs WHERE form_id=?',(ids[2],)).fetchone()[0]
        self.s.review(jid,'select')
        self.assertIsNone(self.s.detail(ids[2])['ai_reviewed_at'])
        self.assertNotIn(ids[2],[f['form_id'] for f in self.s.list_forms(**args)['forms']])
        self.assertTrue(self.mcp_call(h,'mark_reviewed',{'form_id':'0'*24})['isError'])
        for invalid in ({'ai_reviewed':0},{'selected':'false'},{'successful_generation_count':True}):
            self.assertTrue(self.mcp_call(h,'list_forms',invalid)['isError'])
        before=self.s.detail(ids[0]);self.s.import_sources()
        self.assertEqual(self.s.detail(ids[0])['ai_reviewed_at'],before['ai_reviewed_at'])

    def test_reject_clears_ai_review_flag_and_keeps_comment(self):
        fid=self.blank_form()
        batch=self.s.enqueue(fid,42,1,'reject-reopen');self.s.dispatch_once()
        jid=batch['jobs'][0]
        self.s.review(jid,'select');self.s.mark_reviewed(fid)
        self.assertIsNotNone(self.s.detail(fid)['ai_reviewed_at'])
        result=self.mcp_call(self.mcp_session(),'reject_generation',{'generation_id':jid,'reason':'Remove the extra bowl'})
        self.assertFalse(result['isError'])
        detail=self.s.detail(fid)
        self.assertIsNone(detail['ai_reviewed_at']);self.assertIsNone(detail['selected_job'])
        with self.s.connect() as con:
            job=con.execute('SELECT review,reason FROM jobs WHERE id=?',(jid,)).fetchone()
        self.assertEqual(tuple(job),('rejected','Remove the extra bowl'))
        page=self.s.list_forms('all',100,ai_reviewed=False,selected=False,successful_generation_count=1)
        self.assertIn(fid,[f['form_id'] for f in page['forms']])

    def test_ai_review_cursor_filters_and_old_database_migration(self):
        page=self.s.list_forms(limit=1,ai_reviewed=False)
        cursor=page['next_cursor'];self.assertIsNotNone(cursor)
        with self.assertRaises(app.AppError):self.s.list_forms(cursor=cursor,ai_reviewed=True)
        with self.assertRaises(app.AppError):self.s.list_forms(cursor=cursor)
        self.assertNotEqual(page['forms'][0]['form_id'],self.s.list_forms(limit=1,cursor=cursor,ai_reviewed=False)['forms'][0]['form_id'])
        with self.s.connect() as con:con.execute('ALTER TABLE forms DROP COLUMN ai_reviewed_at')
        migrated=app.Studio(self.root,self.provider,False)
        with migrated.connect() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM forms WHERE ai_reviewed_at IS NULL').fetchone()[0],1799)
        marked=migrated.mark_reviewed(self.fid)
        restarted=app.Studio(self.root,self.provider,False)
        self.assertEqual(restarted.mark_reviewed(self.fid),marked)
        self.assertEqual(self.calls,[])

    def test_navigation_failed_and_missing_asset_are_missing(self):
        fid=self.blank_form();batch=self.s.enqueue(fid,42,1,'missing-asset');self.s.dispatch_once()
        with self.s.connect() as con:asset=con.execute('SELECT asset FROM jobs WHERE id=?',(batch['jobs'][0],)).fetchone()[0]
        (self.s.images/asset).unlink()
        self.assertIn(fid,self.nav_ids('missing_image'))
        with self.s.connect() as con:con.execute("UPDATE jobs SET status='failed' WHERE id=?",(batch['jobs'][0],))
        self.assertIn(fid,self.nav_ids('missing_image'))
    def test_navigation_pagination_limits_and_read_only(self):
        with self.s.connect() as con:before=list(con.iterdump())
        all_ids=self.nav_ids('all');self.assertEqual(len(all_ids),1799);self.assertEqual(len(set(all_ids)),1799)
        self.assertEqual(all_ids,self.nav_ids('all'))
        for status in ('all','missing_image','has_image','pending_acceptance','accepted'):
            page=self.s.list_forms(status);self.assertLessEqual(page['returned'],10)
        with self.s.connect() as con:self.assertEqual(before,list(con.iterdump()))
        self.assertEqual(self.calls,[])
        cursor=self.s.list_forms('all',1)['next_cursor']
        for args in [('all',0,None),('all',101,None),('all',True,None),('unknown',10,None),('all',10,'oops'),('accepted',10,cursor)]:
            with self.assertRaises(app.AppError):self.s.list_forms(*args)
        headers=self.mcp_session()
        for args in ({'limit':True},{'limit':101},{'status':'oops'}):self.assertTrue(self.mcp_call(headers,'list_forms',args)['isError'])
        self.assertFalse(self.mcp_call(headers,'list_forms',{'cursor':None})['isError'])
    def test_navigation_keyset_survives_batch_disappearance(self):
        first=self.s.list_forms('missing_image',2);fid=first['forms'][0]['form_id']
        self.s.enqueue(fid,42,1,'page-disappearance');self.s.dispatch_once()
        second=self.s.list_forms('missing_image',2,first['next_cursor'])
        self.assertTrue(set(f['form_id'] for f in first['forms']).isdisjoint(f['form_id'] for f in second['forms']))
        self.assertNotIn(fid,[f['form_id'] for f in self.s.list_forms('missing_image',2)['forms']])
    def test_fal_budget_setting_preserves_provider_reserves_and_restarts(self):
        before=self.s.settings()['provider'];self.s.set_fal_budget('2.25')
        self.assertEqual(self.s.settings()['provider'],before)
        self.assertEqual(self.s.settings()['fal_budget']['daily_limit_usd'],2.25)
        restarted=app.Studio(self.root,self.provider,False)
        self.assertEqual(restarted.settings()['fal_budget']['daily_limit_usd'],2.25)
        for invalid in (None,True,-1,10001,'NaN','inf'):
            with self.assertRaises(app.AppError):self.s.set_fal_budget(invalid)
        result=self.mcp_call(self.mcp_session(),'set_fal_budget',{'daily_limit_usd':0.03})
        self.assertFalse(result['isError']);self.assertEqual(result['structuredContent']['fal_budget']['daily_limit_usd'],0.03)
        self.assertEqual(result['structuredContent']['provider'],before)
    def test_public_subpath_and_origin(self):
        shutil.copytree(app.ROOT/'web',self.root/'web')
        def request(path, method='GET', origin=None, payload=None):
            h=app.Handler.__new__(app.Handler);h.studio=self.s;h.path=path;h.command=method
            raw=json.dumps(payload or {}).encode()
            h.headers={'Host':'studio.example.com','Content-Type':'application/json','Content-Length':str(len(raw))}
            if origin:h.headers['Origin']=origin
            h.rfile=io.BytesIO(raw);result=[]
            h.respond=lambda value,*args,**kwargs:result.append(value)
            h.route();return result[0]
        with patch.object(app,'BASE_PATH','/generator'),patch.object(app,'PUBLIC_URL','https://studio.example.com/generator'):
            html=request('/generator/')
            self.assertIn(b'<base href="/generator/">',html)
            self.assertIn(b'href="style.css"',html)
            catalog=request('/generator/api/catalog');self.assertEqual(catalog['total_forms'],1799)
            job=self.s.detail(self.fid)['jobs'][0]
            self.assertTrue(job['image_url'].startswith('/generator/media/'))
            preview=request('/generator/api/preview','POST','https://studio.example.com',{'form_id':self.fid,'blocks':self.s.detail(self.fid)['blocks']})
            self.assertIn('prompt',preview)
            with self.assertRaises(app.AppError):request('/generator/api/preview','POST','https://evil.example')
            with self.assertRaises(app.AppError):request('/api/settings')
    def test_mcp_bearer_authentication_all_methods(self):
        import hashlib
        token='test-mcp-token-with-enough-entropy'
        self.s.mcp_token_hash=hashlib.sha256(token.encode()).hexdigest()
        def request(method,authorization='',body=None,sid=None):
            h=app.Handler.__new__(app.Handler);h.studio=self.s;h.path='/generator/mcp';h.command=method
            raw=json.dumps(body or {'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25'}}).encode()
            h.headers={'Host':'studio.example.com','Content-Type':'application/json','Content-Length':str(len(raw)),'Authorization':authorization}
            if sid:h.headers['Mcp-Session-Id']=sid
            h.rfile=io.BytesIO(raw);response=[]
            h.respond=lambda value,status=200,**kwargs:response.append((status,value,kwargs.get('headers',{})))
            h.route();return response[0]
        with patch.object(app,'BASE_PATH','/generator'),patch.object(app,'PUBLIC_URL','https://studio.example.com/generator'):
            for method in ('GET','POST','DELETE'):
                for auth in ('','Bearer incorrect','Basic admin:password','Token '+token):
                    status,value,headers=request(method,auth)
                    self.assertEqual(status,401);self.assertTrue(headers['WWW-Authenticate'].startswith('Bearer'))
                    self.assertNotIn(token,json.dumps(value))
            self.assertEqual(len(self.s.mcp.sessions),0)
            status,result,headers=request('POST','Bearer '+token);self.assertEqual(status,200)
            sid=headers['Mcp-Session-Id']
            self.assertEqual(request('DELETE','Bearer wrong',sid=sid)[0],401)
            self.assertIn(sid,self.s.mcp.sessions)
            self.assertEqual(request('GET','Bearer '+token,sid=sid)[0],405)
            self.assertEqual(request('DELETE','Bearer '+token,sid=sid)[0],204)
            self.s.mcp_token_hash=''
            self.assertEqual(request('POST','Bearer '+token)[0],401)
            self.assertNotIn(token,json.dumps(self.s.settings()))
    def enqueue(self,count=1,key='request-test',seed=42):
        return self.s.enqueue(self.fid,seed,count,key)
    def mcp_session(self):
        status,reply,headers=self.s.mcp.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25'}},{},'POST')
        self.assertEqual(status,200);self.assertEqual(reply['result']['protocolVersion'],'2025-11-25')
        return {'Mcp-Session-Id':headers['Mcp-Session-Id'],'MCP-Protocol-Version':'2025-11-25'}
    def mcp_call(self,headers,name,args,rid=2):
        status,reply,_=self.s.mcp.handle({'jsonrpc':'2.0','id':rid,'method':'tools/call','params':{'name':name,'arguments':args}},headers,'POST')
        self.assertEqual(status,200);return reply['result']
    def test_mcp_nine_tools_and_pixel_roundtrip(self):
        headers=self.mcp_session()
        _,reply,_=self.s.mcp.handle({'jsonrpc':'2.0','id':2,'method':'tools/list'},headers,'POST')
        self.assertEqual(len(reply['result']['tools']),20)
        ingredient=self.mcp_call(headers,'get_ingredient',{'id':'tomato'})['structuredContent']
        self.assertEqual(ingredient['id'],'tomato');self.assertIn(self.fid,[f['id'] for f in ingredient['forms']])
        original=self.mcp_call(headers,'get_form',{'form_id':self.fid})['structuredContent']
        update=self.mcp_call(headers,'update_prompt_fields',{'form_id':self.fid,'changes':{'material':'Glossy red skin with small natural highlights'}})
        self.assertFalse(update['isError'])
        changed=self.s.detail(self.fid)
        self.assertEqual({k:v for k,v in original['blocks'].items() if k!='material'},{k:v for k,v in changed['blocks'].items() if k!='material'})
        prompt=self.mcp_call(headers,'get_prompt',{'form_id':self.fid})['structuredContent'];self.assertEqual(prompt['prompt'],changed['prompt'])
        generated=self.mcp_call(headers,'generate',{'form_id':self.fid,'seed':42},rid=10)['structuredContent']
        duplicate=self.mcp_call(headers,'generate',{'form_id':self.fid,'seed':42},rid=10)['structuredContent']
        self.assertEqual(generated,duplicate)
        self.assertTrue(self.mcp_call(headers,'generate',{'form_id':self.fid,'seed':43},rid=10)['isError'])
        self.s.dispatch_once();self.assertEqual(len(self.calls),1)
        jid=generated['generation_id'];image=self.mcp_call(headers,'get_generation_image',{'generation_id':jid})
        self.assertEqual(image['content'][0]['type'],'image');self.assertEqual(image['content'][0]['mimeType'],'image/png')
        self.assertEqual(base64.b64decode(image['content'][0]['data']),png())
        history=self.mcp_call(headers,'get_generation_history',{'form_id':self.fid})['structuredContent']
        self.assertEqual(history['generations'][0]['id'],jid)
        accepted=self.mcp_call(headers,'accept_generation',{'generation_id':jid})['structuredContent']
        self.assertEqual(accepted['review'],'accepted');self.assertFalse(accepted['prompt_verified']);self.assertIsNone(accepted['selected_job'])
        rejected=self.mcp_call(headers,'reject_generation',{'generation_id':jid,'reason':'Cut surface is missing'})['structuredContent']
        self.assertEqual(rejected['review'],'rejected');self.assertEqual(rejected['reason'],'Cut surface is missing')
    def test_mcp_sessions_errors_and_budget(self):
        headers=self.mcp_session()
        for tool,args in [('get_ingredient',{'id':'does-not-exist'}),('get_generation_image',{'generation_id':'missing'}),('update_prompt_fields',{'form_id':self.fid,'changes':{'unknown':'bad'}}),('reject_generation',{'generation_id':'pilot_tomato_whole','reason':' '})]:
            self.assertTrue(self.mcp_call(headers,tool,args)['isError'])
        self.s.set_budget(0)
        result=self.mcp_call(headers,'generate',{'form_id':self.fid,'seed':42})
        self.assertTrue(result['isError']);self.assertEqual(len(self.calls),0)
        self.assertIn('бюджет',result['content'][0]['text'].lower())
        self.assertEqual(self.s.mcp.handle({},headers,'GET')[0],405)
        self.assertEqual(self.s.mcp.handle({'jsonrpc':'2.0','method':'notifications/initialized'},headers,'POST')[:2],(202,None))
        self.assertEqual(self.s.mcp.handle({'jsonrpc':'2.0','id':1,'method':'ping'},{**headers,'MCP-Protocol-Version':'bad'},'POST')[0],400)
        self.assertEqual(self.s.mcp.handle({},headers,'DELETE')[0],204)
        self.assertEqual(self.s.mcp.handle({'jsonrpc':'2.0','id':1,'method':'ping'},headers,'POST')[0],404)
    def test_mcp_jpeg_pilot_pixels(self):
        result=self.mcp_call(self.mcp_session(),'get_generation_image',{'generation_id':'pilot_tomato_whole'})
        self.assertEqual(result['content'][0]['mimeType'],'image/jpeg')
        raw=base64.b64decode(result['content'][0]['data']);self.assertEqual(app.image_size(raw)[0],(1024,1024))
    def test_mcp_agent_workflow_exact_identifiers_and_pending_image(self):
        headers=self.mcp_session()
        ingredient=self.mcp_call(headers,'get_ingredient',{'id':'tomato'})['structuredContent']
        form=next(f for f in ingredient['forms'] if f['form_id']==self.fid)
        inspected=self.mcp_call(headers,'get_form',{'form_id':form['form_id']})['structuredContent']
        self.assertEqual(inspected['form_id'],inspected['id'])
        before_prompt=inspected['prompt']
        generated=self.mcp_call(headers,'generate_image',{'form_id':inspected['form_id'],'seed':42,'request_id':'workflow-exact-id'},rid=10)['structuredContent']
        gid=generated['generation_id']
        self.assertIsInstance(gid,str);self.assertNotEqual(gid,self.fid)
        self.assertEqual(generated['form_id'],self.fid)
        self.assertEqual(generated['next_tool'],'get_generation_history');self.assertFalse(generated['image_ready'])
        alias=self.mcp_call(headers,'generate',{'form_id':inspected['form_id'],'seed':42,'request_id':'workflow-exact-id'},rid=11)['structuredContent']
        self.assertEqual(alias['generation_id'],gid)
        pending=self.mcp_call(headers,'get_generation_image',{'generation_id':gid})
        self.assertTrue(pending['isError']);self.assertIn('queued',pending['content'][0]['text']);self.assertEqual(len(self.calls),0)
        self.assertTrue(self.mcp_call(headers,'get_generation_image',{'generation_id':self.fid})['isError'])
        self.assertTrue(self.mcp_call(headers,'generate_image',{'form_id':'tomato','seed':42})['isError'])
        self.s.dispatch_once();self.assertEqual(len(self.calls),1)
        history=self.mcp_call(headers,'get_generation_history',{'form_id':inspected['form_id']})['structuredContent']
        match=next(g for g in history['generations'] if g['generation_id']==gid)
        self.assertEqual(match['generation_id'],match['id']);self.assertEqual(match['status'],'succeeded');self.assertTrue(match['image_ready'])
        self.assertEqual(match['prompt'],before_prompt)
        image=self.mcp_call(headers,'get_generation_image',{'generation_id':gid})
        self.assertFalse(image['isError']);self.assertEqual(image['structuredContent']['generation_id'],gid)
        self.assertEqual(image['content'][0]['type'],'image');self.assertEqual(base64.b64decode(image['content'][0]['data']),png())
        self.assertEqual(image['structuredContent']['mime_type'],image['content'][0]['mimeType'])
        from mcp_server import TOOLS
        for name,value in [('get_ingredient',ingredient),('get_form',inspected),('generate_image',generated),('get_generation_history',history),('get_generation_image',image['structuredContent'])]:
            schema=next(t for t in TOOLS if t['name']==name)['outputSchema']
            self.assertFalse(set(schema['required'])-set(value))
    def test_import_is_idempotent_and_preserves_edits(self):
        original=self.s.detail(self.fid);blocks=original['blocks'];blocks['material']='Taut skin with subtle highlights'
        self.s.save_form(self.fid,blocks,'A split fruit')
        self.s.import_sources()
        self.assertEqual(self.s.detail(self.fid)['blocks'],blocks)
        with self.s.connect() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM forms').fetchone()[0],1799)
            self.assertEqual(con.execute('SELECT COUNT(DISTINCT source) FROM forms').fetchone()[0],751)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM jobs WHERE origin='pilot'").fetchone()[0],11)
        self.assertEqual(self.s.detail(self.fid)['requirements_ru'],original['requirements_ru'])
    def test_snapshot_and_png_and_metadata(self):
        original=self.s.detail(self.fid);batch=self.enqueue();blocks=original['blocks'].copy();blocks['portion']='Two tomatoes'
        self.s.save_form(self.fid,blocks,'');self.s.save_style('A different flat illustration style.')
        self.s.dispatch_once();j=self.s.detail(self.fid)['jobs'][0]
        self.assertEqual(j['id'],batch['jobs'][0]);self.assertEqual(j['prompt'],original['prompt']);self.assertEqual(j['blocks'],original['blocks']);self.assertEqual(j['style_id'],1)
        self.assertEqual(app.image_size((self.s.images/j['asset']).read_bytes())[0],(512,512))
        archive=zipfile.ZipFile(io.BytesIO(self.s.download(j['id'])));meta=json.loads(archive.read('metadata.json'))
        self.assertEqual(meta['width'],512);self.assertNotIn('test-secret',archive.read('metadata.json').decode())
    def test_duplicate_and_budget(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.enqueue(),range(2)))
        self.assertEqual(results[0],results[1]);self.s.dispatch_once();self.assertFalse(self.s.dispatch_once());self.assertEqual(len(self.calls),1)
        self.assertEqual(self.s.settings()['budget']['remaining_neurons'],8636.36)
        self.s.set_budget(1363.63)
        with self.assertRaises(app.AppError):self.enqueue(key='insufficient')
        self.assertEqual(len(self.calls),1)
    def test_dns_failure_releases_reserve_and_continues_without_retry(self):
        import socket
        for service in ('cloudflare','fal'):
            with self.subTest(service=service):
                if service=='fal':self.enable_fal(0.018)
                batch=self.s.enqueue(self.fid,42,3,'dns-no-retry-'+service,service)
                calls=[]
                def provider(job):
                    calls.append(job['id'])
                    if len(calls)==1:raise app.urllib.error.URLError(socket.gaierror(socket.EAI_AGAIN,'Temporary failure'))
                    return png()
                self.s.provider=provider
                self.assertTrue(self.s.dispatch_once())
                with self.s.connect() as con:
                    jobs=con.execute('SELECT * FROM jobs WHERE batch_id=? ORDER BY created_at',(batch['batch_id'],)).fetchall()
                    self.assertEqual([j['status'] for j in jobs],['failed','queued','queued'])
                    self.assertEqual(jobs[0]['reserved'],0);self.assertEqual(jobs[0]['reserved_usd_micros'],0)
                if service=='cloudflare':self.assertEqual(self.s.settings()['budget']['remaining_neurons'],10000)
                else:self.assertEqual(self.s.settings()['fal_budget']['reserved_today_usd'],0)
                self.assertTrue(self.s.dispatch_once());self.assertTrue(self.s.dispatch_once());self.assertFalse(self.s.dispatch_once())
                self.assertEqual(calls,batch['jobs'])
                with self.s.connect() as con:self.assertEqual([r[0] for r in con.execute('SELECT status FROM jobs WHERE batch_id=? ORDER BY created_at',(batch['batch_id'],))],['failed','succeeded','succeeded'])
    def test_direct_dns_failure_is_not_unknown_spending(self):
        import socket
        self.enqueue()
        def provider(_):raise socket.gaierror(socket.EAI_NONAME,'Unknown host')
        self.s.provider=provider;self.s.dispatch_once()
        with self.s.connect() as con:job=con.execute('SELECT * FROM jobs WHERE origin != \'pilot\' ORDER BY created_at DESC LIMIT 1').fetchone()
        self.assertEqual(job['status'],'failed');self.assertEqual(job['reserved'],0)
        self.assertEqual(self.s.settings()['budget']['remaining_neurons'],10000)
    def test_timeout_holds_reserve_and_stops_queue(self):
        self.enqueue(3)
        def timeout(_):raise TimeoutError()
        self.s.provider=timeout;self.s.dispatch_once();self.assertFalse(self.s.dispatch_once())
        jobs=[j for j in self.s.detail(self.fid)['jobs'] if j['origin']=='cloudflare']
        self.assertEqual(sorted(j['status'] for j in jobs),['blocked','blocked','uncertain'])
        self.assertEqual(self.s.settings()['budget']['remaining_neurons'],8636.36)
    def test_restart_never_redispatches(self):
        batch=self.enqueue(3)
        with self.s.connect() as con:
            con.execute("UPDATE jobs SET status='running',reserved=? WHERE id=?",(app.COST,batch['jobs'][0]))
            con.execute('UPDATE budget SET remaining=remaining-? WHERE id=1',(app.COST,))
        restarted=app.Studio(self.root,self.provider,False)
        self.assertFalse(restarted.dispatch_once());self.assertEqual(len(self.calls),0)
        jobs=[j for j in restarted.detail(self.fid)['jobs'] if j['origin']=='cloudflare']
        self.assertEqual(sorted(j['status'] for j in jobs),['interrupted','interrupted','uncertain'])
        self.assertEqual(restarted.settings()['budget']['remaining_neurons'],8636.36)
    def test_day_reset_and_cancel(self):
        self.enqueue(3)
        with patch('app.today',return_value='2099-01-01'):
            self.assertFalse(self.s.dispatch_once());self.assertFalse(self.s.settings()['budget']['valid'])
        self.assertEqual(len(self.calls),0)
        self.enqueue(3,key='new-request');self.s.cancel_queue();self.assertFalse(self.s.dispatch_once());self.assertEqual(self.s.settings()['budget']['remaining_neurons'],10000)
    def test_error_and_wrong_size_stop_without_retry(self):
        self.enqueue(3);self.s.provider=lambda _:png(1024,1024);self.s.dispatch_once();self.assertFalse(self.s.dispatch_once())
        jobs=[j for j in self.s.detail(self.fid)['jobs'] if j['origin']=='cloudflare'];failed=next(j for j in jobs if j['status']=='failed')
        self.assertTrue((self.s.images/(failed['id']+'.response')).exists())
        self.assertEqual(self.s.settings()['budget']['remaining_neurons'],8636.36)
    def test_verification_and_selection_are_independent(self):
        batch=self.enqueue(3)
        for _ in range(3):self.s.dispatch_once()
        self.s.review(batch['jobs'][0],'select');self.assertFalse(self.s.detail(self.fid)['verified'])
        for jid in batch['jobs']:self.s.review(jid,'accept')
        self.assertTrue(self.s.detail(self.fid)['verified'])
        self.s.review(batch['jobs'][0],'reject','Wrong outline');self.assertFalse(self.s.detail(self.fid)['verified']);self.assertIsNone(self.s.detail(self.fid)['selected_job'])
        self.s.save_style('A newly revised shared style.');self.assertFalse(self.s.detail(self.fid)['verified'])
    def test_container_and_morphology(self):
        rice=self.s.detail(app.stable_id('rice','Dry grains'));oil=self.s.detail(app.stable_id('olive oil','Liquid oil in a bowl'));half=self.s.detail(app.stable_id('tomato','Cross-section — one half'))
        self.assertEqual(rice['blocks']['container'],'');self.assertIn('bowl',oil['blocks']['container']);self.assertIn('3.5 cm',half['blocks']['geometry'])
        rice['blocks']['container']='A bowl'
        with self.assertRaises(app.AppError):self.s.save_form(rice['id'],rice['blocks'],'')
        oil['blocks']['container']=''
        with self.assertRaises(app.AppError):self.s.validate_blocks(oil,oil['blocks'],True)
        self.assertFalse(half['verified'])
        self.assertTrue(self.s.catalog('половин')['ingredients'])
    def test_usage_check_does_not_credit_held_reservations(self):
        self.enqueue();self.s.dispatch_once()
        usage={'day':app.today(),'used_neurons':1363.64,'requests':1,'free_daily_neurons':10000,'remaining_neurons':8636.36,'checked_at':0}
        with patch('app.cloudflare_usage',return_value=usage):
            result=self.s.check_budget()
        self.assertEqual(result['settings']['budget']['remaining_neurons'],7272.72)
        self.assertEqual(result['usage']['local_reservations_neurons'],1363.64)
        usage.update(used_neurons=15000,remaining_neurons=0,requests=11)
        with patch('app.cloudflare_usage',return_value=usage):self.s.check_budget()
        self.assertEqual(self.s.settings()['budget']['remaining_neurons'],0)
    def test_real_provider_request_contract_without_network(self):
        captured=[]
        def response(request,timeout):
            captured.append(request)
            return io.BytesIO(json.dumps({'success':True,'result':{'image':base64.b64encode(png()).decode()}}).encode())
        with patch('app.urllib.request.urlopen',side_effect=response):
            raw=app.cloudflare_generate(self.root,{'prompt':'A round tomato.','seed':42})
        self.assertEqual(app.image_size(raw)[0],(512,512))
        request=captured[0];body=request.data.decode()
        self.assertTrue(request.full_url.endswith('/ai/run/'+app.MODEL))
        self.assertIn('name="width"\r\n\r\n512',body);self.assertIn('name="height"\r\n\r\n512',body)
        self.assertIn('name="seed"\r\n\r\n42',body);self.assertNotIn('guidance',body)
    def test_prompt_versions_abc_restore_generate_frozen(self):
        h=self.mcp_session()
        def call(name,args,rid=100):
            r=self.mcp_call(h,name,args,rid)
            self.assertFalse(r['isError'],r)
            return r['structuredContent']
        A=call('get_prompt',{'form_id':self.fid})['current_prompt_version_id']
        B=call('update_prompt_fields',{'form_id':self.fid,'changes':{'view':'Elevated three-quarter view B','preparation':''},'expected_current_version_id':A})['prompt_version_id']
        bgen=call('generate_image',{'form_id':self.fid,'seed':42,'request_id':'version-b-image'})
        self.s.dispatch_once()
        Bsnapshot=call('get_generation_prompt',{'generation_id':bgen['generation_id']})
        C=call('update_prompt_fields',{'form_id':self.fid,'changes':{'view':'Side view C'},'expected_current_version_id':B})['prompt_version_id']
        with self.s.connect() as con:
            before=[dict(r) for r in con.execute('SELECT * FROM prompt_versions WHERE version_id IN (?,?,?)',(A,B,C))]
        restored=call('restore_prompt_from_generation',{'generation_id':bgen['generation_id'],'expected_current_version_id':C})
        self.assertEqual(restored['current_prompt_version_id'],B)
        D=call('generate_image',{'form_id':self.fid,'seed':43,'request_id':'version-d-image'})
        self.assertEqual(D['prompt_version_id'],B)
        # Later edits do not change the queued generation.
        E=call('update_prompt_fields',{'form_id':self.fid,'changes':{'material':'Material E'},'expected_current_version_id':B})['prompt_version_id']
        frozen=call('get_generation_prompt',{'generation_id':D['generation_id']})
        self.assertEqual(frozen['prompt_version_id'],B)
        self.assertEqual(frozen['prompt'],Bsnapshot['prompt'])
        self.assertEqual(frozen['blocks'],Bsnapshot['blocks'])
        self.assertEqual(frozen['blocks']['preparation'],'')
        self.s.dispatch_once()
        self.assertEqual(self.calls[-1]['prompt'],Bsnapshot['prompt'])
        self.assertEqual(json.loads(self.calls[-1]['blocks']),Bsnapshot['blocks'])
        with self.s.connect() as con:
            after=[dict(r) for r in con.execute('SELECT * FROM prompt_versions WHERE version_id IN (?,?,?)',(A,B,C))]
            self.assertEqual(before,after)
            with self.assertRaises(app.sqlite3.IntegrityError):con.execute('UPDATE prompt_versions SET prompt=? WHERE version_id=?',('tamper',B))
            with self.assertRaises(app.sqlite3.IntegrityError):con.execute('DELETE FROM prompt_versions WHERE version_id=?',(B,))
        print('Verified prompt workflow:',json.dumps({'A':A,'B':B,'C':C,'restored_current':B,'generation_D':D['generation_id'],'D_prompt_version_id':D['prompt_version_id'],'later_current_E':E,'exact_prompt_and_blocks':True,'provider':'synthetic PNG'},ensure_ascii=False))
    def test_prompt_version_noop_conflict_and_wrong_form(self):
        original=self.s.detail(self.fid);A=original['current_prompt_version_id']
        with self.s.connect() as con:count=con.execute('SELECT COUNT(*) FROM prompt_versions').fetchone()[0]
        noop=self.s.update_prompt_fields(self.fid,{'view':original['blocks']['view']},A)
        self.assertEqual(noop['prompt_version_id'],A);self.assertEqual(noop['changed_fields'],[])
        with self.s.connect() as con:self.assertEqual(con.execute('SELECT COUNT(*) FROM prompt_versions').fetchone()[0],count)
        B=self.s.update_prompt_fields(self.fid,{'view':'View B'},A)['prompt_version_id']
        h=self.mcp_session()
        r=self.mcp_call(h,'restore_prompt_version',{'form_id':self.fid,'version_id':A,'expected_current_version_id':A})
        self.assertTrue(r['isError']);self.assertEqual(r['structuredContent']['error_code'],'conflict')
        self.assertEqual(self.s.detail(self.fid)['current_prompt_version_id'],B)
        with self.assertRaises(app.AppError):self.s.update_prompt_fields(self.fid,{'view':'Stale edit'},A)
        rice=app.stable_id('rice','Dry grains')
        with self.assertRaises(app.AppError):self.s.restore_prompt_version(self.fid,self.s.detail(rice)['current_prompt_version_id'],B)
        self.assertEqual(self.s.restore_prompt_version(self.fid,A,B)['current_prompt_version_id'],A)
    def test_prompt_version_restore_old_style_exactly(self):
        original=self.s.detail(self.fid);A=original['current_prompt_version_id']
        self.s.save_style('New global style without gradients.')
        changed=self.s.detail(self.fid)
        self.assertNotEqual(changed['current_prompt_version_id'],A)
        self.s.restore_prompt_version(self.fid,A,changed['current_prompt_version_id'])
        restored=self.s.detail(self.fid)
        self.assertEqual(restored['prompt'],original['prompt']);self.assertEqual(restored['style_id'],original['style_id'])
        next_job=self.s.enqueue(self.fid,44,1,'restored-style-generation')['jobs'][0]
        snapshot=self.s.get_generation_prompt(next_job)
        self.assertEqual(snapshot['prompt_version_id'],A);self.assertEqual(snapshot['prompt'],original['prompt'])
        self.assertEqual(snapshot['style_text'],original['style_text'])
    def test_prompt_version_migration_is_idempotent_and_preserves_legacy(self):
        self.enqueue();self.s.dispatch_once()
        self.s.review(self.s.detail(self.fid)['jobs'][0]['id'],'select')
        with self.s.connect() as con:
            jobs=[dict(r) for r in con.execute('SELECT * FROM jobs')]
            selected=con.execute('SELECT selected_job FROM forms WHERE id=?',(self.fid,)).fetchone()[0]
            versions=con.execute('SELECT COUNT(*) FROM prompt_versions').fetchone()[0]
        self.s.migrate_prompt_versions();self.s.migrate_prompt_versions()
        with self.s.connect() as con:
            self.assertEqual(jobs,[dict(r) for r in con.execute('SELECT * FROM jobs')])
            self.assertEqual(versions,con.execute('SELECT COUNT(*) FROM prompt_versions').fetchone()[0])
            self.assertEqual(selected,con.execute('SELECT selected_job FROM forms WHERE id=?',(self.fid,)).fetchone()[0])
        legacy=self.s.get_generation_prompt('pilot_tomato_whole')
        self.assertEqual(legacy['blocks'],{});self.assertFalse(legacy['snapshot_complete']);self.assertIsNone(legacy['prompt_version_id'])
        with self.assertRaises(app.AppError):self.s.restore_prompt_from_generation('pilot_tomato_whole',self.s.detail(self.fid)['current_prompt_version_id'])
        self.assertEqual(len(self.s.detail(self.fid)['jobs']),len([j for j in jobs if j['form_id']==self.fid]))
    def test_versioned_fal_generation_preserves_provider_request_id(self):
        self.enable_fal()
        self.s.provider=lambda job:(png(),{'provider_request_id':'fal-provider-reference','provider_seed':job['seed']})
        version=self.s.detail(self.fid)['current_prompt_version_id']
        job_id=self.s.enqueue(self.fid,42,1,'fal-versioned-request','fal')['jobs'][0]
        self.s.dispatch_once()
        job=next(j for j in self.s.detail(self.fid)['jobs'] if j['id']==job_id)
        self.assertEqual(job['status'],'succeeded')
        self.assertEqual(job['provider_request_id'],'fal-provider-reference')
        self.assertEqual(job['prompt_version_id'],version)

    def test_prompt_version_upgrade_from_old_database(self):
        self.enqueue();self.s.dispatch_once()
        with self.s.connect() as con:
            snapshots=[{k:r[k] for k in ('id','form_id','blocks','prompt','style_id','style_text','asset','review','reason')} for r in con.execute('SELECT * FROM jobs')]
            con.execute('DROP TRIGGER prompt_versions_no_update');con.execute('DROP TRIGGER prompt_versions_no_delete')
            con.execute('DROP TABLE prompt_versions')
            con.execute('ALTER TABLE forms DROP COLUMN current_prompt_version_id')
            con.execute('ALTER TABLE jobs DROP COLUMN prompt_version_id')
        upgraded=app.Studio(self.root,self.provider,False)
        with upgraded.connect() as con:
            self.assertEqual(snapshots,[{k:r[k] for k in ('id','form_id','blocks','prompt','style_id','style_text','asset','review','reason')} for r in con.execute('SELECT * FROM jobs')])
            self.assertEqual(con.execute('SELECT COUNT(*) FROM forms WHERE current_prompt_version_id IS NULL').fetchone()[0],0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM jobs WHERE origin='pilot' AND prompt_version_id IS NOT NULL").fetchone()[0],0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM jobs WHERE origin='cloudflare' AND prompt_version_id IS NULL").fetchone()[0],0)
        self.assertTrue(all((upgraded.images/r['asset']).exists() for r in snapshots if r['asset']))

    def test_prompt_version_concurrent_restore_only_one_wins(self):
        A=self.s.detail(self.fid)['current_prompt_version_id']
        B=self.s.update_prompt_fields(self.fid,{'view':'Concurrent B'})['prompt_version_id']
        C=self.s.update_prompt_fields(self.fid,{'view':'Concurrent C'})['prompt_version_id']
        def restore(target):
            try:return self.s.restore_prompt_version(self.fid,target,C)['current_prompt_version_id']
            except app.AppError as e:return e.status
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(restore,[A,B]))
        self.assertEqual(results.count(409),1)
        self.assertIn(self.s.detail(self.fid)['current_prompt_version_id'],(A,B))

    def enable_fal(self,limit=0.018):
        with (self.root/'.env').open('a') as f:f.write('FAI_AI=fal-test-secret\n')
        self.s.set_generation_config('fal',limit)
    def test_visual_review_navigation_comments_and_conflicts(self):
        with self.s.connect() as con:con.execute("UPDATE jobs SET review='accepted' WHERE origin='pilot'")
        batch=self.enqueue(3)
        for _ in range(3):self.s.dispatch_once()
        first=self.s.next_review()['item']['generation']['id']
        self.s.review(first,'comment','Need a clearer cut',True)
        self.assertEqual(self.s.next_review()['item']['generation']['reason'],'Need a clearer cut')
        second=self.s.next_review(first)['item']['generation']['id']
        self.assertNotEqual(first,second)
        self.s.review(second,'accept','Looks good',True)
        with self.assertRaises(app.AppError) as conflict:self.s.review(second,'reject','Different reviewer',True)
        self.assertEqual(conflict.exception.status,409)
        third=self.s.next_review(first)['item']['generation']['id']
        self.s.review(third,'reject','Wrong shape',True)
        self.assertIsNone(self.s.next_review(first)['item'])
        self.assertEqual(self.s.next_review()['item']['generation']['id'],first)
        self.s.review(first,'accept','Now accepted',True)
        self.assertIsNone(self.s.next_review()['item'])
        self.assertIsNone(self.s.detail(self.fid)['selected_job'])
    def test_form_review_groups_all_variants_and_selects_exactly_one(self):
        with self.s.connect() as con:con.execute('UPDATE forms SET selected_job=(SELECT id FROM jobs WHERE form_id=forms.id LIMIT 1) WHERE id IN (SELECT form_id FROM jobs)')
        first=self.s.enqueue(self.fid,42,3,'form-review-three')
        other=app.stable_id('rice','Dry grains')
        with self.s.connect() as con:
            con.execute('UPDATE forms SET selected_job=NULL WHERE id IN (?,?)',(self.fid,other))
            con.execute("UPDATE jobs SET status='cancelled' WHERE origin='pilot' AND form_id IN (?,?)",(self.fid,other))
        second=self.s.enqueue(other,42,1,'form-review-other')
        for _ in range(4):self.s.dispatch_once()
        self.s.review(first['jobs'][0],'accept')
        self.s.review(first['jobs'][1],'reject','Bad shape')
        pending=self.s.next_form_review()
        self.assertEqual(pending['remaining'],2)
        stats=pending['stats']
        self.assertEqual(stats['total_forms'],1799)
        self.assertEqual(stats['ready_forms'],2)
        self.assertGreater(stats['without_images_forms'],0)
        self.assertEqual(stats['unselected_forms'],stats['ready_forms']+stats['without_images_forms'])
        skipped=self.s.next_form_review(self.fid)
        self.assertEqual(skipped['stats']['unselected_forms'],stats['unselected_forms'])
        self.assertEqual(skipped['stats']['skipped_forms'],1)
        self.assertEqual(pending['item']['form']['id'],self.fid)
        self.assertEqual([j['id'] for j in pending['item']['generations']],first['jobs'])
        self.assertEqual(self.s.next_form_review(self.fid)['item']['form']['id'],other)
        chosen=self.s.review(first['jobs'][2],'select','Best candidate',only_unselected=True)
        self.assertEqual(chosen['selected_job'],first['jobs'][2])
        self.assertEqual(self.s.next_form_review()['stats']['unselected_forms'],stats['unselected_forms']-1)
        self.assertEqual(next(j for j in chosen['jobs'] if j['id']==first['jobs'][2])['review'],'accepted')
        with self.assertRaises(app.AppError) as conflict:self.s.review(first['jobs'][0],'select',only_unselected=True)
        self.assertEqual(conflict.exception.status,409)
        self.assertEqual(self.s.next_form_review()['item']['form']['id'],other)
        self.s.review(second['jobs'][0],'select',only_unselected=True)
        self.assertIsNone(self.s.next_form_review()['item'])
    def test_mcp_rejects_truncated_generation_id_with_actionable_error(self):
        h=self.mcp_session();batch=self.s.enqueue(self.fid,42,1,'exact-full-id-test');self.s.dispatch_once()
        gid=batch['jobs'][0]
        for name in ('get_generation_image','accept_generation','get_generation_prompt'):
            result=self.mcp_call(h,name,{'generation_id':gid[:24]})
            self.assertTrue(result['isError']);self.assertIn('FULL 32',result['structuredContent']['error'])
            self.assertIn(gid[:24],result['structuredContent']['error'])
        image=self.mcp_call(h,'get_generation_image',{'generation_id':gid})
        self.assertEqual(image['content'][0]['type'],'image')
        self.assertFalse(self.mcp_call(h,'get_generation_image',{'generation_id':'pilot_tomato_whole'})['isError'])

    def test_mcp_select_generation_validates_ownership_and_preserves_history(self):
        h=self.mcp_session();batch=self.s.enqueue(self.fid,42,3,'mcp-select-candidates')
        other=app.stable_id('rice','Dry grains')
        args={'form_id':self.fid,'generation_id':batch['jobs'][0]}
        self.assertTrue(self.mcp_call(h,'select_generation',args)['isError'])
        for _ in range(3):self.s.dispatch_once()
        self.s.review(batch['jobs'][0],'comment','Keep this comment')
        before=self.s.detail(self.fid);calls=len(self.calls)
        self.assertTrue(self.mcp_call(h,'select_generation',{**args,'form_id':other})['isError'])
        self.assertEqual(self.s.detail(self.fid)['selected_job'],before['selected_job'])
        result=self.mcp_call(h,'select_generation',args)['structuredContent']
        self.assertEqual(result['selected_generation_id'],args['generation_id'])
        self.assertEqual(result['review'],'accepted')
        self.assertEqual(self.mcp_call(h,'select_generation',args)['structuredContent'],result)
        self.assertEqual(self.mcp_call(h,'get_form',{'form_id':self.fid})['structuredContent']['selected_job'],args['generation_id'])
        second={**args,'generation_id':batch['jobs'][1]}
        self.mcp_call(h,'select_generation',second)
        after=self.s.detail(self.fid)
        self.assertEqual(after['selected_job'],second['generation_id'])
        first=next(j for j in after['jobs'] if j['id']==args['generation_id'])
        self.assertEqual(first['reason'],'Keep this comment');self.assertEqual(first['review'],'accepted')
        self.assertEqual(before['current_prompt_version_id'],after['current_prompt_version_id'])
        self.assertEqual(len(self.calls),calls)
        with self.s.connect() as con:con.execute('UPDATE jobs SET asset=NULL WHERE id=?',(batch['jobs'][2],))
        self.assertTrue(self.mcp_call(h,'select_generation',{**args,'generation_id':batch['jobs'][2]})['isError'])
        self.assertEqual(self.s.detail(self.fid)['selected_job'],second['generation_id'])
    def test_fal_timeout_isolates_attempt_and_continues_queue(self):
        self.enable_fal()
        batch=self.s.enqueue(self.fid,42,3,'fal-isolated-timeout','fal')
        calls=[]
        def provider(job):
            calls.append(job['id'])
            if len(calls)==1:raise TimeoutError()
            return png()
        self.s.provider=provider
        for _ in range(3):self.assertTrue(self.s.dispatch_once())
        self.assertFalse(self.s.dispatch_once())
        with self.s.connect() as con:
            rows=list(con.execute('SELECT status,reserved_usd_micros FROM jobs WHERE batch_id=? ORDER BY created_at',(batch['batch_id'],)))
        self.assertEqual([r['status'] for r in rows],['uncertain','succeeded','succeeded'])
        self.assertEqual([r['reserved_usd_micros'] for r in rows],[6000]*3)
        self.assertEqual(calls,batch['jobs'])
        self.assertEqual(self.s.spending()['fal']['reserved_today_usd'],0.018)
    def test_fal_invalid_result_does_not_block_other_forms(self):
        self.enable_fal()
        self.s.enqueue(self.fid,42,3,'fal-invalid-result','fal')
        def invalid(_):raise app.AppError('Expected one image',502)
        self.s.provider=invalid;self.s.dispatch_once()
        self.assertEqual(len(self.s.queue()['jobs']),2)
        self.s.provider=self.provider;self.s.dispatch_once();self.s.dispatch_once()
        self.assertEqual(self.s.queue()['jobs'],[])
    def test_fal_account_errors_stop_queue_without_resubmission(self):
        self.enable_fal(1)
        for code in (401,402,403,429):
            batch=self.s.enqueue(self.fid,42,3,'fal-account-error-'+str(code),'fal')
            self.s.provider=self.s.default_provider
            error=app.urllib.error.HTTPError('https://fal.run/',code,'account error',{},io.BytesIO())
            with patch('app.fal_provider.urllib.request.urlopen',side_effect=error) as send:
                self.s.dispatch_once();self.assertFalse(self.s.dispatch_once())
            self.assertEqual(send.call_count,1)
            with self.s.connect() as con:
                statuses=[r[0] for r in con.execute('SELECT status FROM jobs WHERE batch_id=? ORDER BY created_at',(batch['batch_id'],))]
            self.assertEqual(statuses,['failed','blocked','blocked'])
    def test_fal_storage_failure_still_stops_queue(self):
        self.enable_fal();self.s.enqueue(self.fid,42,3,'fal-disk-full','fal')
        with patch.object(Path,'write_bytes',side_effect=OSError(28,'No space left')):
            self.s.dispatch_once()
        self.assertFalse(self.s.dispatch_once())
        self.assertEqual(self.s.spending()['fal']['reserved_today_usd'],0.006)
    def test_fal_budget_isolation_snapshot_and_mcp_pixels(self):
        self.enable_fal();self.s.set_budget(0)
        h=self.mcp_session()
        settings=self.mcp_call(h,'get_generation_settings',{})['structuredContent']
        self.assertEqual(settings['provider'],'fal')
        args={'form_id':self.fid,'seed':43,'provider':'fal','request_id':'fal-workflow-1'}
        generated=self.mcp_call(h,'generate_image',args,50)['structuredContent']
        self.assertEqual(generated['provider'],'fal')
        self.assertEqual(generated['generation_id'],self.mcp_call(h,'generate_image',args,51)['structuredContent']['generation_id'])
        self.assertTrue(self.mcp_call(h,'generate_image',{**args,'provider':'cloudflare'},52)['isError'])
        self.s.set_generation_config('cloudflare')
        self.s.dispatch_once()
        self.assertEqual(self.calls[0]['origin'],'fal')
        image=self.mcp_call(h,'get_generation_image',{'generation_id':generated['generation_id']})
        self.assertEqual(image['content'][0]['type'],'image')
        self.assertEqual(base64.b64decode(image['content'][0]['data']),png())
        spending=self.mcp_call(h,'get_spending',{})['structuredContent']
        self.assertEqual(spending['fal']['reserved_today_usd'],0.006)
        self.assertEqual(spending['cloudflare']['remaining_neurons'],0)
        self.assertEqual(spending['fal']['estimated_success_usd'],0.006)
        archive=zipfile.ZipFile(io.BytesIO(self.s.download(generated['generation_id'])))
        metadata=json.loads(archive.read('metadata.json'))
        self.assertEqual(metadata['model'],app.fal_provider.MODEL)
        self.assertNotIn('fal-test-secret',json.dumps(spending))
    def test_fal_limit_failure_restart_and_bulk(self):
        self.enable_fal(0.006)
        with self.assertRaises(app.AppError):self.s.enqueue(self.fid,42,3,'fal-too-many','fal')
        batch=self.s.enqueue(self.fid,42,1,'fal-one-image','fal')
        def failed(_):raise app.AppError('fal.ai HTTP 429',502)
        self.s.provider=failed;self.s.dispatch_once()
        self.assertEqual(self.s.spending()['fal']['uncertain_or_failed_reserve_usd'],0.006)
        with self.assertRaises(app.AppError):self.s.enqueue(self.fid,43,1,'fal-next-image','fal')
        restarted=app.Studio(self.root,self.provider,False)
        self.assertFalse(restarted.dispatch_once())
        self.assertEqual(restarted.spending()['fal']['reserved_today_usd'],0.006)
        self.s.set_generation_config('fal',0.018)
        other=app.stable_id('rice','Dry grains')
        bulk=self.s.enqueue_bulk([self.fid,other],42,'fal-bulk-images','fal')
        self.assertEqual(len(bulk['jobs']),2)
        with self.assertRaises(app.AppError):self.s.enqueue(app.stable_id('olive oil','Liquid oil in a bowl'),42,1,'fal-over-limit','fal')
    def test_fal_budget_editor_does_not_reset_spent_reserve(self):
        self.enable_fal(0.02);self.s.enqueue(self.fid,42,1,'budget-setting-job','fal');self.s.dispatch_once()
        before=self.s.spending()['fal']['reserved_today_usd']
        self.s.set_generation_config('cloudflare');self.s.set_fal_budget(0)
        self.assertEqual(self.s.settings()['provider'],'cloudflare')
        self.assertEqual(self.s.spending()['fal']['reserved_today_usd'],before)
        with self.assertRaises(app.AppError):self.s.enqueue(self.fid,43,1,'cap-lowered','fal')
        self.s.set_fal_budget(0.03)
        self.assertEqual(self.s.spending()['fal']['reserved_today_usd'],before)
        self.s.enqueue(self.fid,43,1,'cap-raised','fal')
    def test_fal_provider_request_and_usage_pagination(self):
        self.enable_fal();calls=[];usage_calls=[]
        class Response(io.BytesIO):
            headers={'x-fal-request-id':'fal-request-1'}
        def response(req,timeout):
            calls.append(req)
            if req.full_url.startswith('https://fal.run/'):
                return Response(json.dumps({'images':[{'url':'data:image/png;base64,'+base64.b64encode(png()).decode()}],'seed':42}).encode())
            usage_calls.append(req)
            return Response(json.dumps({'time_series':[{'results':[{'currency':'USD','cost_total':0.0015,'quantity':0.25}]}],'has_more':len(usage_calls)==1,'next_cursor':'next-page'}).encode())
        with patch('app.fal_provider.urllib.request.urlopen',side_effect=response):
            raw,meta=app.fal_provider.generate(self.root,{'prompt':'Tomato','seed':42})
            usage=app.fal_provider.usage(self.root)
        self.assertEqual(app.image_size(raw)[0],(512,512))
        body=json.loads(calls[0].data)
        self.assertEqual(body['image_size'],{'width':512,'height':512})
        self.assertEqual(body['num_images'],1);self.assertTrue(body['sync_mode'])
        self.assertEqual(calls[0].get_header('X-fal-no-retry'),'1')
        self.assertNotIn('guidance',body)
        self.assertEqual(meta['provider_request_id'],'fal-request-1')
        self.assertEqual(usage['actual_cost_usd'],0.003)
        self.assertIn('cursor=next-page',calls[-1].full_url)

    def test_fal_incomplete_body_recovers_same_request_without_new_charge(self):
        import gzip,http.client
        self.enable_fal();calls=[];submitted=[]
        data={'images':[{'url':'data:image/png;base64,'+base64.b64encode(png()).decode()}],'seed':42}
        class Broken(io.BytesIO):
            headers={'x-fal-request-id':'existing-123'}
            def read(self,*args):raise http.client.IncompleteRead(b'partial',20)
        class Recovered(io.BytesIO):headers={'Content-Encoding':'gzip'}
        def response(req,timeout):
            calls.append(req)
            if req.data is not None:return Broken()
            self.assertEqual(req.full_url,'https://queue.fal.run/fal-ai/flux-2/requests/existing-123')
            return Recovered(gzip.compress(json.dumps(data).encode()))
        with patch('app.fal_provider.urllib.request.urlopen',side_effect=response):
            raw,meta=app.fal_provider.generate(self.root,{'prompt':'Tomato','seed':42},on_submitted=submitted.append)
        self.assertEqual(raw,png());self.assertEqual(meta['provider_request_id'],'existing-123')
        self.assertEqual(submitted,['existing-123']);self.assertEqual(sum(req.data is not None for req in calls),1)
    def test_fal_existing_result_retries_only_safe_reads(self):
        import http.client
        self.enable_fal()
        data={'images':[{'url':'data:image/png;base64,'+base64.b64encode(png()).decode()}],'seed':42}
        class Response(io.BytesIO):headers={}
        with patch('app.fal_provider.urllib.request.urlopen',side_effect=[http.client.IncompleteRead(b'x',10),Response(json.dumps(data).encode())]) as send,patch('app.fal_provider.time.sleep'):
            result=app.fal_provider.result_for_request(self.root,'existing-123')
        self.assertEqual(result,data);self.assertEqual(send.call_count,2)
        self.assertTrue(all(call.args[0].data is None for call in send.call_args_list))
    def test_incomplete_response_without_request_id_holds_reserve(self):
        import http.client
        self.enqueue(3)
        def provider(_):raise http.client.IncompleteRead(b'x',10)
        self.s.provider=provider;self.s.dispatch_once()
        jobs=self.s.detail(self.fid)['jobs'][:3]
        self.assertEqual(sorted(j['status'] for j in jobs),['blocked','blocked','uncertain'])
        self.assertEqual(self.s.settings()['budget']['remaining_neurons'],8636.36)

    def test_provider_error_codes_are_safe_and_do_not_retry(self):
        cases = [({'errors':[{'code':3040,'message':'test-secret-never-export'}]}, 'Мощности модели'),
                 ({'errors':[{'code':3036}]}, 'исчерпании дневной'),
                 ({'errors':[{'code':5035}]}, 'Workers Paid'),
                 ({'errors':[{'code':'test-secret-never-export'}]}, 'Причина ограничения'),
                 ([], 'Причина ограничения')]
        for payload, expected in cases:
            error=app.urllib.error.HTTPError('https://provider.invalid',429,'limited',{},io.BytesIO(json.dumps(payload).encode()))
            with patch('app.urllib.request.urlopen',side_effect=error) as send:
                with self.assertRaises(app.AppError) as caught:
                    app.cloudflare_generate(self.root,{'prompt':'Private prompt.','seed':43})
            message=str(caught.exception)
            self.assertIn(expected,message)
            self.assertNotIn('test-secret-never-export',message)
            self.assertNotIn('Private prompt',message)
            self.assertEqual(send.call_count,1)
    def test_provider_failure_retains_budget_and_requires_manual_retry(self):
        self.enqueue(3)
        def failure(_):raise app.AppError('Cloudflare: HTTP 500.',502)
        self.s.provider=failure;self.s.dispatch_once();self.assertFalse(self.s.dispatch_once())
        self.assertEqual(self.s.settings()['budget']['remaining_neurons'],8636.36)
        jobs=[j for j in self.s.detail(self.fid)['jobs'] if j['origin']=='cloudflare']
        self.assertEqual(sorted(j['status'] for j in jobs),['blocked','blocked','failed'])
    def test_automatic_preparation_all_rows_and_preservation(self):
        initial=self.s.detail(self.fid)['blocks']
        result=self.s.prepare_forms()
        self.assertEqual(result['total'],1799)
        self.assertEqual(self.s.detail(self.fid)['blocks'],initial)
        rows=self.s.bulk_forms();self.assertEqual(len(rows),1799)
        self.assertTrue(all(r['ready'] for r in rows))
        self.assertEqual(self.s.prepare_forms()['prepared'],0)
        exported=self.s.export_prompts()
        self.assertEqual(len(exported['forms']),1799)
        self.assertTrue(all(r['ready'] for r in exported['forms']))
        self.assertNotIn('test-secret',json.dumps(exported))
        carrot_id=app.stable_id('carrot','Fresh: cubes')
        carrot=self.s.detail(carrot_id)
        self.assertIn('15–25 cubes',carrot['blocks']['portion'])
        self.assertIn('orange',carrot['blocks']['material'])
        self.assertNotIn('15–20 cm',carrot['blocks']['material'])
        blocks=carrot['blocks'];blocks['material']='Firm bright orange flesh with a pale core'
        self.s.save_form(carrot_id,blocks,'My rejection criterion')
        self.s.prepare_forms([carrot_id])
        self.assertEqual(self.s.detail(carrot_id)['blocks']['material'],blocks['material'])
        self.assertEqual(self.s.detail(carrot_id)['rejection_rules'],'My rejection criterion')
        self.assertEqual(len(self.calls),0)
    def test_auto_form_semantics(self):
        import prompt_builder
        for source in ('olive oil','butter','carrot','tomato','onion','potato','cheese','garlic','egg','chicken breast','beef steak','cod','rice','plain flour','sugar','salt','basil','soy sauce'):
            for form in self.s.forms(source):
                draft=prompt_builder.prepare(form)
                self.s.validate_blocks(form,draft['blocks'],True)
                self.assertTrue(draft['blocks']['geometry'])
                self.assertTrue(draft['blocks']['material'])
                self.assertEqual(bool(draft['blocks']['container']),form['container_policy']=='required')
        half=self.s.detail(app.stable_id('tomato','Cross-section — one half'))
        geometry=prompt_builder.prepare(half)['blocks']['geometry']
        self.assertIn('three-dimensional',geometry);self.assertIn('visible depth',geometry)
        rice=self.s.detail(app.stable_id('rice','Dry grains'))
        self.assertEqual(prompt_builder.prepare(rice)['blocks']['container'],'')
    def test_bulk_budget_deduplication_and_remaining(self):
        ids=[r['id'] for r in self.s.forms('carrot')]
        self.s.set_budget(4090.92)
        result=self.s.enqueue_bulk(ids,42,'bulk-test-request')
        self.assertEqual(len(result['jobs']),3);self.assertEqual(len(result['remaining_forms']),4)
        duplicate=self.s.enqueue_bulk(ids,42,'bulk-test-request')
        self.assertEqual(result['jobs'],duplicate['jobs'])
        with self.assertRaises(app.AppError):self.s.enqueue_bulk(ids,42,'bulk-extra-request')
        for _ in range(3):self.s.dispatch_once()
        self.assertEqual(len(self.calls),3);self.assertEqual(self.s.settings()['budget']['remaining_neurons'],0)
        self.s.set_budget(10000)
        rest=self.s.enqueue_bulk(ids,42,'bulk-next-day')
        self.assertEqual(len(rest['jobs']),4);self.assertEqual(len(rest['skipped_forms']),3)
        self.assertEqual(len(self.calls),3)
    def test_bulk_insufficient_budget_sends_nothing(self):
        self.s.set_budget(0)
        with self.assertRaises(app.AppError):self.s.enqueue_bulk([self.fid],42,'bulk-zero-budget')
        self.assertEqual(self.s.prepare_forms()['total'],1799)
        self.assertFalse(self.s.dispatch_once());self.assertEqual(len(self.calls),0)

if __name__=='__main__':unittest.main()
