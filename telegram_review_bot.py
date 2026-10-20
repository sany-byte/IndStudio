"""Private Telegram form reviewer. Shares Studio DB; never starts its worker."""
import json
import logging
import os
from pathlib import Path
import time
import urllib.request
import uuid
import app

class TelegramError(Exception):
    pass

class Telegram:
    def __init__(self, token):self.token=token
    def call(self, method, payload=None, files=None, timeout=65):
        payload=payload or {}
        headers={}
        if files:
            boundary='studio'+uuid.uuid4().hex;parts=[]
            for name,value in payload.items():
                value=json.dumps(value,ensure_ascii=False) if isinstance(value,(dict,list)) else str(value)
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()+value.encode()+b'\r\n')
            for name,path in files.items():
                path=Path(path);mime='image/png' if path.suffix=='.png' else 'image/jpeg'
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{path.name}"\r\nContent-Type: {mime}\r\n\r\n'.encode()+path.read_bytes()+b'\r\n')
            data=b''.join(parts)+f'--{boundary}--\r\n'.encode();headers['Content-Type']='multipart/form-data; boundary='+boundary
        else:
            data=json.dumps(payload,ensure_ascii=False).encode();headers['Content-Type']='application/json'
        try:
            with urllib.request.urlopen(urllib.request.Request('https://api.telegram.org/bot'+self.token+'/'+method,data=data,headers=headers),timeout=timeout) as response:result=json.load(response)
        except Exception as exc:
            # Exception strings can contain the token-bearing URL. Never log them.
            raise TelegramError('Telegram request failed: '+type(exc).__name__) from None
        if not result.get('ok'):raise TelegramError('Telegram API error '+str(result.get('error_code','unknown')))
        return result['result']

class Reviewer:
    def __init__(self, studio, telegram, state_path, allowed=('axelder','egolfeld')):
        self.studio=studio;self.telegram=telegram;self.path=Path(state_path);self.allowed=set(allowed)
        self.state=json.loads(self.path.read_text()) if self.path.exists() else {'offset':0,'users':{},'bindings':{}}
    def save(self):
        tmp=self.path.with_suffix('.tmp')
        with tmp.open('w') as f:json.dump(self.state,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
        os.chmod(tmp,0o600);tmp.replace(self.path)
    def authorized(self, user, chat):
        if not user or not chat or chat.get('type')!='private' or chat['id']!=user['id'] or user.get('is_bot'):return False
        uid=str(user['id']);bindings=self.state['bindings']
        if uid in bindings.values():return True
        name=user.get('username','').lower()
        if name not in self.allowed or (name in bindings and bindings[name]!=uid):return False
        bindings[name]=uid;self.save();return True
    def session(self, uid):return self.state['users'].setdefault(str(uid),{'skipped':[],'current':None})
    @staticmethod
    def stats(value):
        s=value['stats'];return f"Выбрано: {s['selected_forms']} из {s['total_forms']}\nНе выбрано: {s['unselected_forms']}\nГотовы к ревью: {s['ready_forms']} · Без картинок: {s['without_images_forms']}"
    def message(self, chat, text, keyboard=None):
        payload={'chat_id':chat,'text':text[:4096]}
        if keyboard:payload['reply_markup']={'inline_keyboard':keyboard}
        return self.telegram.call('sendMessage',payload)
    def answer(self, payload):
        try:return self.telegram.call('answerCallbackQuery',payload)
        except TelegramError:return None  # A late acknowledgement must not undo a saved choice.
    def next(self, uid, chat):
        session=self.session(uid)
        old=session.get('current');session['current']=None;session.pop('pending_comment',None);self.save()
        if old:
            for mid in set(old.get('photo_message_ids',[])+([old['message_id']] if old.get('message_id') else [])):
                try:self.telegram.call('editMessageReplyMarkup',{'chat_id':chat,'message_id':mid,'reply_markup':{'inline_keyboard':[]}})
                except TelegramError:pass
        result=self.studio.next_form_review(','.join(session['skipped']))
        item=result['item']
        if not item:
            self.message(chat,self.stats(result)+'\n\nНет следующих готовых форм. /skipped — вернуться к пропущенным, /next — проверить снова.');return
        form=item['form'];jobs=item['generations'];nonce=uuid.uuid4().hex[:12]
        current={'form_id':form['id'],'nonce':nonce,'jobs':[j['id'] for j in jobs]}
        # Activate buttons only after ALL images have been delivered.
        description=f"{form['name_ru']} — {form['form_ru']}\n{form['source']}\n\n"+self.stats(result)+'\n\n'
        for label,value in zip(('Количество','Размер','Ориентация','Компоновка','Примечание'),form['requirements_ru'][3:]):
            if value:description+=label+': '+value+'\n'
        if form['rejection_rules']:description+='\nКритерии проверки: '+form['rejection_rules']
        self.message(chat,description)
        current['photo_message_ids']=[];current['captions']=[]
        session['current']=current;self.save();time.sleep(1.1)
        for i,j in enumerate(jobs):
            caption=f"ВАРИАНТ {i+1} ИЗ {len(jobs)}\n{form['name_ru']} — {form['form_ru']}\nseed {j['seed']} · {j['width']}×{j['height']} · стиль v{j['style_id']}"
            if j['review']=='rejected':caption+=' · ранее отклонён'
            elif j['review']=='accepted':caption+=' · ранее принят'
            current['captions'].append(caption)
            if j['reason']:caption+='\nКомментарий: '+j['reason'][:500]
            keyboard=[[{'text':f'Выбрать вариант {i+1}','callback_data':f's:{nonce}:{i}'},
                       {'text':'Комментарий','callback_data':f'c:{nonce}:{i}'}]]
            sent=self.telegram.call('sendPhoto',{'chat_id':chat,'photo':'attach://image','caption':caption[:1024],
                 'reply_markup':{'inline_keyboard':keyboard}},{'image':self.studio.images/j['asset']})
            current['photo_message_ids'].append(sent['message_id']);self.save();time.sleep(1.1)
        keyboard=[[{'text':'Пропустить форму','callback_data':'k:'+nonce},{'text':'Счётчики','callback_data':'t:'+nonce}]]
        sent=self.message(chat,'У каждого изображения своя кнопка выбора. Комментарий сохраняется к конкретному варианту.',keyboard)
        current['message_id']=sent['message_id'];self.save()
    def handle(self, update):
        callback=update.get('callback_query');message=update.get('message') or (callback or {}).get('message') or {}
        user=(callback or message).get('from');chat=message.get('chat')
        if not self.authorized(user,chat):
            if callback:self.answer({'callback_query_id':callback['id'],'text':'Доступ запрещён','show_alert':True})
            elif chat and chat.get('type')=='private':self.message(chat,'Доступ только для разрешённых пользователей.')
            return
        uid=user['id'];chat_id=chat['id'];session=self.session(uid)
        if callback:
            bits=callback.get('data','').split(':');current=session.get('current')
            if len(bits)<2 or not current or bits[1]!=current['nonce'] or message.get('message_id') not in current.get('photo_message_ids',[])+([current['message_id']] if current.get('message_id') else []):
                self.answer({'callback_query_id':callback['id'],'text':'Эта форма уже закрыта. /next — следующая.'});return
            action=bits[0]
            if action=='t':
                self.answer({'callback_query_id':callback['id'],'text':self.stats(self.studio.next_form_review()),'show_alert':True});return
            if action=='c':
                try:
                    index=int(bits[2]);assert 0<=index<len(current['jobs'])
                    assert message['message_id']==current['photo_message_ids'][index]
                except (ValueError,IndexError,AssertionError):return
                self.answer({'callback_query_id':callback['id'],'text':'Напишите комментарий ответом на сообщение'})
                prompt=self.telegram.call('sendMessage',{'chat_id':chat_id,'text':f'Комментарий к варианту {index+1}. Ответьте на это сообщение. /cancel — отмена.',
                    'reply_markup':{'force_reply':True,'selective':True}})
                session['pending_comment']={'nonce':current['nonce'],'index':index,'prompt_id':prompt['message_id']};self.save();return
            if action=='s':
                try:
                    index=int(bits[2]);assert 0<=index<len(current['jobs'])
                    self.studio.review(current['jobs'][index],'select',only_unselected=True,expected_form_id=current['form_id'])
                except (ValueError,IndexError,AssertionError):return
                except app.AppError:
                    self.answer({'callback_query_id':callback['id'],'text':'Форма уже выбрана или картинка недоступна. Открываю следующую.'})
                    self.next(uid,chat_id);return
                self.answer({'callback_query_id':callback['id'],'text':'Вариант '+str(index+1)+' выбран'})
            elif action=='k':
                if current['form_id'] not in session['skipped']:session['skipped'].append(current['form_id'])
                self.save();self.answer({'callback_query_id':callback['id'],'text':'Форма пропущена'})
            else:return
            self.next(uid,chat_id);return
        command=message.get('text','').split(' ',1)[0].split('@',1)[0]
        pending=session.get('pending_comment')
        if command=='/cancel':
            session.pop('pending_comment',None);self.save();self.message(chat_id,'Ввод комментария отменён.');return
        if pending and not command.startswith('/'):
            current=session.get('current')
            if not current or pending['nonce']!=current['nonce']:
                session.pop('pending_comment',None);self.save();self.message(chat_id,'Форма уже закрыта. /next — следующая.');return
            if message.get('reply_to_message',{}).get('message_id')!=pending['prompt_id']:
                self.message(chat_id,'Ответьте именно на сообщение с запросом комментария. /cancel — отмена.');return
            text=message.get('text','').strip()
            if not text or len(text)>5000:
                self.message(chat_id,'Комментарий должен содержать от 1 до 5000 символов.');return
            index=pending['index'];jid=current['jobs'][index]
            try:self.studio.review(jid,'comment',text,only_unselected=True)
            except app.AppError:
                session.pop('pending_comment',None);self.save();self.message(chat_id,'Форма уже выбрана или вариант недоступен. /next — следующая.');return
            session.pop('pending_comment',None);self.save()
            self.message(chat_id,f'Комментарий к варианту {index+1} сохранён в Studio и Food MCP. Можно выбирать картинку.')
            if current.get('captions'):
                try:self.telegram.call('editMessageCaption',{'chat_id':chat_id,'message_id':current['photo_message_ids'][index],
                    'caption':(current['captions'][index]+'\nКомментарий: '+text[:500])[:1024],
                    'reply_markup':{'inline_keyboard':[[{'text':f'Выбрать вариант {index+1}','callback_data':f"s:{current['nonce']}:{index}"},
                        {'text':'Комментарий','callback_data':f"c:{current['nonce']}:{index}"}]]}})
                except TelegramError:pass
            return
        if command=='/stats':self.message(chat_id,self.stats(self.studio.next_form_review()))
        elif command in ('/start','/next','/review','/skipped'):
            if command=='/skipped':session['skipped']=[];self.save()
            self.next(uid,chat_id)
        else:self.message(chat_id,'/next — следующая форма\n/stats — счётчики\n/skipped — вернуться к пропущенным')
    def run(self):
        while True:
            try:
                updates=self.telegram.call('getUpdates',{'offset':self.state['offset'],'timeout':40,'allowed_updates':['message','callback_query']})
                for update in updates:
                    try:self.handle(update)
                    except Exception as exc:logging.error('Review update failed: %s',type(exc).__name__)
                    self.state['offset']=update['update_id']+1;self.save()
            except Exception as exc:
                logging.error('Review polling failed: %s',type(exc).__name__);time.sleep(5)

if __name__=='__main__':
    import fcntl
    logging.basicConfig(level=logging.INFO)
    root=Path(__file__).resolve().parent
    values={}
    for line in (root/'.env').read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            name,value=line.split('=',1);values[name.strip()]=value.strip().strip('\"\'')
    token=values.get('TELEGRAM_REVIEW_BOT_TOKEN','')
    if not token:raise SystemExit('Missing TELEGRAM_REVIEW_BOT_TOKEN')
    studio=app.Studio.__new__(app.Studio)
    studio.root=root;studio.data=root/'data';studio.images=studio.data/'images';studio.db=studio.data/'studio.sqlite3'
    lock=(studio.data/'telegram-review.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    Reviewer(studio,Telegram(token),studio.data/'telegram-review-state.json').run()
