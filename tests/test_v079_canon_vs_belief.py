"""v0.7.9: the world does not rewrite itself to validate an NPC's opinion."""
from memory import keep_world_claims_at_source_level, validated_extractions, EXTRACT_INSTRUCTIONS
from prompting import PromptComposer
from storage import Store


def fact(evidence, *, category='world', certainty='confirmed'):
    return {'category':category, 'text':'Порядок проверки путешественников установлен законом.',
            'importance':4, 'certainty':certainty, 'evidence':evidence, 'known_by':''}


def test_npc_said_it_so_it_is_not_objective_world_law():
    user = 'Кирай объяснила, что никакие печати не нужны.'
    bot = 'Авинель уверяла, что писарь требует по три копии каждой бумаги.'
    guarded = keep_world_claims_at_source_level([fact('писарь требует по три копии')], user, bot)
    assert guarded[0]['certainty'] == 'uncertain'
    assert guarded[0]['importance'] == 4
    assert guarded[0]['evidence'] == 'писарь требует по три копии'


def test_player_spoken_claim_is_belief_not_automatic_canon():
    user = '- В Севтосе всегда берут пошлину с ведьм, — сказала Кирай.'
    bot = 'Авинель промолчала.'
    guarded = keep_world_claims_at_source_level([fact('В Севтосе всегда берут пошлину с ведьм')], user, bot)
    assert guarded[0]['certainty'] == 'belief'


def test_author_narration_can_establish_local_reality():
    user = 'В порту Псеумы на рога суккубов никто не обращал внимания.'
    bot = 'Авинель всё равно опасалась досмотра.'
    guarded = keep_world_claims_at_source_level([fact('на рога суккубов никто не обращал внимания')], user, bot)
    assert guarded[0]['certainty'] == 'confirmed'


def test_events_and_private_character_beliefs_not_mangled():
    claims = [fact('Светоносцы погнались следом', category='event'),
              fact('Она боялась дозорных', category='goal', certainty='belief')]
    claims_snapshot = [dict(f) for f in claims]
    guarded = keep_world_claims_at_source_level(claims, '',
                 'Светоносцы погнались следом. Она боялась дозорных.')
    assert guarded == claims_snapshot
    assert claims == claims_snapshot  # do not mutate caller's parsed JSON


def test_prompt_keeps_explicit_canon_over_npc_confidence(tmp_path):
    db=Store(str(tmp_path/'canon.sqlite3'))
    wid=db.add_world('Сквозь миры')
    db.set_world_description(wid,'В Псеуме демоны являются обычными путешественниками.')
    db.add_world_lore(wid,'Порт Псеумы','Пассажирские корабли принимают путешественников разных рас.')
    sid=db.new_session(1,'Кирай и Авинель')
    db.assign_story_world(sid,wid)
    db.add_character(sid,'Авинель','bot')
    db.add_character(sid,'Кирай','user')
    db.add_turn(sid,
       'Кирай показала рога. - Я годами спокойно езжу по Псеуме.',
       'Авинель объявила, что в любом порту за это вешают.', 'gemini', 20,40)
    compiled=PromptComposer(db, lambda sid: 'Авинель верит Капитулу.').assemble(sid,
      'Кирай напомнила, что никого не пугают её рога.')
    assert 'В Псеуме демоны являются обычными' in compiled.user
    assert 'Пассажирские корабли принимают' in compiled.user
    assert 'ПРАВИЛА ИСТОЧНИКОВ' in compiled.user
    assert 'Не придумывай с уверенностью новые универсальные законы' in compiled.system
    assert 'не вводи новые придуманные аргументы' in compiled.system
    assert 'его ПРАВОТУ' in compiled.system
    assert 'реплики КАЖДОГО героя' in compiled.user


def test_extraction_prompts_world_law_as_belief():
    assert 'Реплики героев ОБЕИХ сторон могут быть неверными' in EXTRACT_INSTRUCTIONS
