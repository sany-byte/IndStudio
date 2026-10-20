import json
import unittest
from unittest.mock import patch
import test_app
from telegram_review_bot import Reviewer

class FakeTelegram:
    def __init__(self):self.calls=[];self.mid=0
    def call(self,method,payload=None,files=None):
        self.calls.append((method,payload,files));self.mid+=1
        return {'message_id':self.mid}

class BotTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_app.StudioTests();self.fixture.setUp();self.s=self.fixture.s
        with self.s.connect() as con:
            con.execute('UPDATE forms SET selected_job=(SELECT id FROM jobs WHERE form_id=forms.id LIMIT 1) WHERE id IN (SELECT form_id FROM jobs)')
            con.execute('UPDATE forms SET selected_job=NULL WHERE id=?',(self.fixture.fid,))
            con.execute("UPDATE jobs SET status='cancelled' WHERE origin='pilot' AND form_id=?",(self.fixture.fid,))
        self.batch=self.s.enqueue(self.fixture.fid,42,3,'tg-test-candidates')
        for _ in range(3):self.s.dispatch_once()
        self.telegram=FakeTelegram();self.path=self.fixture.root/'bot.json';self.bot=Reviewer(self.s,self.telegram,self.path)
        self.user={'id':123,'username':'axelder'};self.chat={'id':123,'type':'private'}
    def tearDown(self):self.fixture.tearDown()
    def start(self):
        with patch('telegram_review_bot.time.sleep'):self.bot.handle({'message':{'from':self.user,'chat':self.chat,'text':'/start'}})
    def callback(self,data,mid):return {'callback_query':{'id':'cb1','from':self.user,'data':data,'message':{'message_id':mid,'chat':self.chat}}}
    def test_numbered_photos_selection_counts_and_stale_buttons(self):
        self.start();current=self.bot.session(123)['current']
        photos=[c for c in self.telegram.calls if c[0]=='sendPhoto']
        self.assertEqual(len(photos),3)
        for index,photo in enumerate(photos):
            self.assertIn('ВАРИАНТ '+str(index+1)+' ИЗ 3',photo[1]['caption'])
            self.assertEqual(len(photo[2]),1)
            row=photo[1]['reply_markup']['inline_keyboard'][0]
            self.assertEqual(row[0]['callback_data'],f"s:{current['nonce']}:{index}")
            self.assertEqual(row[1]['callback_data'],f"c:{current['nonce']}:{index}")
            self.assertTrue(all(len(b['callback_data'].encode())<=64 for b in row))
        cb=self.callback('s:'+current['nonce']+':1',current['photo_message_ids'][1])
        self.bot.handle(cb)
        self.assertEqual(self.s.detail(self.fixture.fid)['selected_job'],self.batch['jobs'][1])
        self.assertIsNone(self.bot.session(123)['current'])
        self.bot.handle(cb)
        self.assertEqual(self.s.detail(self.fixture.fid)['selected_job'],self.batch['jobs'][1])
        self.assertIn('Не выбрано:',self.bot.stats(self.s.next_form_review()))
    def test_comment_matches_exact_photo_persists_and_preserves_selection(self):
        self.start();current=self.bot.session(123)['current']
        self.bot.handle(self.callback('c:'+current['nonce']+':1',current['photo_message_ids'][1]))
        pending=self.bot.session(123)['pending_comment']
        restarted=Reviewer(self.s,self.telegram,self.path)
        restarted.handle({'message':{'from':self.user,'chat':self.chat,'text':'Wrong reply','reply_to_message':{'message_id':999}}})
        self.assertEqual(next(j for j in self.s.detail(self.fixture.fid)['jobs'] if j['id']==self.batch['jobs'][1])['reason'],'')
        restarted.handle({'message':{'from':self.user,'chat':self.chat,'text':'Нужна другая текстура','reply_to_message':{'message_id':pending['prompt_id']}}})
        detail=self.s.detail(self.fixture.fid);job=next(j for j in detail['jobs'] if j['id']==self.batch['jobs'][1])
        self.assertEqual(job['reason'],'Нужна другая текстура');self.assertEqual(job['review'],'unreviewed')
        self.assertIsNone(detail['selected_job'])
        self.assertNotIn('pending_comment',restarted.session(123))
        restarted.handle(self.callback('s:'+current['nonce']+':1',current['photo_message_ids'][1]))
        detail=self.s.detail(self.fixture.fid)
        self.assertEqual(detail['selected_job'],self.batch['jobs'][1])
        self.assertEqual(next(j for j in detail['jobs'] if j['id']==self.batch['jobs'][1])['reason'],'Нужна другая текстура')

    def test_access_bound_ids_private_only_and_restart(self):
        self.assertFalse(self.bot.authorized({'id':5,'username':'outsider'},{'id':5,'type':'private'}))
        self.assertFalse(self.bot.authorized(self.user,{'id':-1,'type':'group'}))
        self.assertTrue(self.bot.authorized(self.user,self.chat))
        self.assertFalse(self.bot.authorized({'id':7,'username':'axelder'},{'id':7,'type':'private'}))
        self.assertTrue(self.bot.authorized({'id':123,'username':'renamed'},self.chat))
        restarted=Reviewer(self.s,self.telegram,self.path)
        self.assertFalse(restarted.authorized({'id':7,'username':'axelder'},{'id':7,'type':'private'}))
        self.assertTrue(restarted.authorized({'id':456,'username':'egolfeld'},{'id':456,'type':'private'}))
    def test_skip_is_persistent_and_does_not_select(self):
        self.start();current=self.bot.session(123)['current'];before=self.s.next_form_review()['stats']['unselected_forms']
        self.bot.handle(self.callback('k:'+current['nonce'],current['message_id']))
        self.assertIsNone(self.s.detail(self.fixture.fid)['selected_job'])
        self.assertEqual(before,self.s.next_form_review()['stats']['unselected_forms'])
        restarted=Reviewer(self.s,self.telegram,self.path)
        self.assertIn(self.fixture.fid,restarted.session(123)['skipped'])
        with patch('telegram_review_bot.time.sleep'):restarted.handle({'message':{'from':self.user,'chat':self.chat,'text':'/skipped'}})
        self.assertEqual(restarted.session(123)['current']['form_id'],self.fixture.fid)

if __name__=='__main__':unittest.main()
