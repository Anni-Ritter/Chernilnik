from pathlib import Path
from types import SimpleNamespace
import pytest

from setup_dialogue import (DIALOGUE_SYSTEM, generate_setup_reply, is_discussion_only,
                            setup_world_context, setup_dialogue_messages, save_assistant_reply)
from quickstart_core import QuickDrafts, install_plan, validate_plan, setup_prompt
from storage import Store


@pytest.fixture
def world(tmp_path):
    db = Store(str(tmp_path/'roleplay.sqlite3'))
    wid = db.add_world('Сквозь миры')
    db.set_world_description(wid, 'Мир с магией и разными странами.')
    db.add_world_lore(wid, 'Эльдора', 'Эльдора связана с Солемиром и Светоносцами.')
    db.add_world('Другой мир')
    sid=db.new_session(100, 'Другая история')
    db.assign_story_world(sid,wid)
    cid=db.add_character(sid,'Кирай','user')
    db.set_character_text(cid, 'description', 'Ведьма, которая не обязана доверять инквизиции.')
    return db, wid


def test_discussion_not_written_as_canon(tmp_path):
    assert is_discussion_only('Что ты поняла?')
    assert is_discussion_only('Что ты записала?')
    assert is_discussion_only('Как тебе Авинель?')
    assert not is_discussion_only('Авинель верна инквизиции, Кирай — ведьма.')
    assert not is_discussion_only('Кирай думает, что она сможет войти в город?')
    drafts=QuickDrafts(tmp_path)
    drafts.start(55, 0, seed='Авинель твоя, Кирай моя.')
    save_assistant_reply(drafts,55,'Авинель действительно может стать сложным соперником.')
    saved=drafts.get(55)
    assert saved['notes']==['Авинель твоя, Кирай моя.']
    assert 'соперником' in saved['assistant_replies'][-1]


def test_setup_prompt_grounded_in_world_and_photos(world, tmp_path):
    db,wid=world
    drafts=QuickDrafts(tmp_path/'drafts')
    drafts.start(1,wid)
    drafts.add_note(1,'Авинель — твоя Светоносец, Кирай — моя ведьма. Начало в Эльдоре.')
    drafts.add_photo(1,'tg-ref','/tmp/reference.png','Это Авинель')
    drafts.set_photo_analysis(1,1,'Фиолетовые глаза и длинные серебряные волосы.')
    d=drafts.get(1)
    prompts=setup_dialogue_messages(db,d,'Авинель и Кирай встречаются на границе.')
    assert len(prompts)==2
    assert 'Эльдора связана' in prompts[1]['content']
    assert 'Ведьма, которая не обязана' in prompts[1]['content']
    assert 'Фиолетовые глаза' in prompts[1]['content']
    assert 'Кирай встречаются' in prompts[1]['content']
    assert 'фигуру' not in setup_prompt(d) # avoid false extra detail
    assert 'устойчивые визуальные признаки' in setup_prompt(d)
    assert 'отдельный механизм' in DIALOGUE_SYSTEM


@pytest.mark.asyncio
async def test_plain_polza_reply_no_json(world):
    db,wid=world
    class Completion:
        def __init__(self): self.requests=[]
        async def create(self, **kwargs):
            self.requests.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content='Авинель, похоже, будет воспринимать Кирай как угрозу. А встреча в Эльдоре даст ей официальный повод вмешаться.'))])
    calls=Completion()
    engine=SimpleNamespace(generation_choice=lambda:SimpleNamespace(provider='polza',model='google/gemini-3.7-flash'),
                           polza_client=SimpleNamespace(chat=SimpleNamespace(completions=calls)), client=None)
    d={'notes':['Авинель твоя, она из Эльдоры, Кирай моя, ведьма.'], 'world_id':wid,'photos':[]}
    answer=await generate_setup_reply(engine,db,d,'Кирай путешествует из Севтоса.')
    assert 'Кирай' in answer
    assert 'json' not in calls.requests[0]
    assert calls.requests[0]['model']=='google/gemini-3.7-flash'
    assert calls.requests[0]['messages'][0]['role']=='system'


def test_visual_analysis_reused_no_second_photo_call(world,tmp_path):
    db,wid=world
    drafts=QuickDrafts(tmp_path/'drafts')
    drafts.start(11,wid,seed='Авинель твоя, Кирай моя')
    drafts.add_photo(11,'photo',str(tmp_path/'img.jpg'),'Авинель')
    drafts.set_photo_analysis(11,1,'Устойчивые черты: белые волосы, золотые глаза.')
    d=drafts.get(11)
    plan=validate_plan({'title':'Встреча','characters':[{'name':'Авинель','role':'bot'},
              {'name':'Кирай','role':'user'}], 'photos':[{'index':1,'character':'Авинель'}]},d['photos'])
    sid=install_plan(db,drafts,11,d,plan)
    pic=db.photos(db.list_characters(sid)[0]['id'])[0]
    assert pic['analysis_status']=='ok'
    assert 'золотые глаза' in pic['visual_description']

@pytest.mark.asyncio
async def test_handler_replies_to_actual_idea_and_does_not_store_meta_question(world, monkeypatch):
    from unittest.mock import AsyncMock
    pytest.importorskip('aiogram')
    from quickstart_ui import register_quickstart
    db, wid = world
    class Completion:
        def __init__(self): self.calls=[]
        async def create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content='Авинель — инквизитор, и встреча с Кирай на границе будет для неё служебной обязанностью, а не случайным знакомством.'))])
    completion=Completion()
    engine=SimpleNamespace(generation_choice=lambda:SimpleNamespace(provider='polza', model='google/gemini-3.7-flash'),
        polza_client=SimpleNamespace(chat=SimpleNamespace(completions=completion)), client=None)
    router,drafts,_,_=await register_quickstart(db, engine, 9001, AsyncMock())
    handler=next(item.callback for item in router.message.handlers if item.callback.__name__=='natural_message')
    bot=SimpleNamespace(send_chat_action=AsyncMock())
    def msg(text):
        return SimpleNamespace(text=text, from_user=SimpleNamespace(id=9001),
            chat=SimpleNamespace(type='private',id=9001), answer=AsyncMock(),bot=bot)
    first=msg('Хочу новую историю в Сквозь миры. Авинель — инквизитор, твоя героиня.')
    await handler(first)
    assert first.answer.await_count==1
    assert 'Авинель' in first.answer.await_args.args[0]
    assert drafts.get(9001)['world_id']==wid
    second=msg('Кирай — моя, она отправилась через границу Эльдоры.')
    await handler(second)
    assert 'Авинель' in second.answer.await_args.args[0]
    status=msg('Что ты запомнила?')
    await handler(status)
    assert len(drafts.get(9001)['notes'])==2
    assert 'Что ты запомнила' not in '\n'.join(drafts.get(9001)['notes'])
    assert len(completion.calls)==3
    assert 'Кирай' in completion.calls[2]['messages'][1]['content']
