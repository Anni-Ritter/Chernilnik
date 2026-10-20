from types import SimpleNamespace
import pytest
from setup_dialogue import (generate_setup_reply, split_telegram_text,
                            is_discussion_only, SETUP_MAX_TOKENS, SETUP_RETRY_MAX_TOKENS)


@pytest.fixture
def empty_store(tmp_path):
    from storage import Store
    return Store(str(tmp_path / 'r.sqlite3'))


def fake_polza(content, finish_reason='stop'):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content), finish_reason=finish_reason)])


def make_polza_engine(client):
    return SimpleNamespace(
        generation_choice=lambda: SimpleNamespace(provider='polza', model='google/gemini-3.7-flash'),
        polza_client=SimpleNamespace(chat=SimpleNamespace(completions=client)), client=None)


@pytest.mark.asyncio
async def test_continues_instead_of_sending_truncated_polza_reply(empty_store):
    class Replies:
        def __init__(self): self.calls=[]
        async def create(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return fake_polza('A dangerously unfin', 'length')
            return fake_polza('ished sentence.', 'stop')
    replies=Replies()
    text = await generate_setup_reply(make_polza_engine(replies), empty_store,
                                      {'notes':['test'], 'world_id':0, 'photos':[]}, 'test')
    assert text == 'A dangerously unfinished sentence.'
    assert replies.calls[1]['messages'][-2]['role'] == 'assistant'
    assert [r['max_tokens'] for r in replies.calls] == [SETUP_MAX_TOKENS, SETUP_RETRY_MAX_TOKENS]


@pytest.mark.asyncio
async def test_two_token_limit_responses_raise_instead_of_showing_fragment(empty_store):
    class Replies:
        async def create(self, **kwargs):
            return fake_polza('A dangerously unfin', 'length')
    with pytest.raises(RuntimeError, match='token limit'):
        await generate_setup_reply(make_polza_engine(Replies()), empty_store,
                                    {'notes':[], 'world_id':0, 'photos':[]}, 'test')


@pytest.mark.asyncio
async def test_openai_incomplete_is_retried(empty_store):
    class Replies:
        def __init__(self): self.calls=[]
        async def create(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return SimpleNamespace(status='incomplete',
                   incomplete_details=SimpleNamespace(reason='max_output_tokens'),
                   output_text='Some unfinished')
            return SimpleNamespace(status='completed', incomplete_details=None,
                                   output_text=' response.')
    replies=Replies()
    engine=SimpleNamespace(
        generation_choice=lambda: SimpleNamespace(provider='openai', model='test'),
        client=SimpleNamespace(responses=replies), polza_client=None)
    result=await generate_setup_reply(engine, empty_store,
                   {'notes':[], 'world_id':0, 'photos':[]}, 'test')
    assert result == 'Some unfinished response.'
    assert replies.calls[1]['input'][-2]['role'] == 'assistant'
    assert replies.calls[1]['max_output_tokens'] == SETUP_RETRY_MAX_TOKENS


def test_full_text_not_lost_by_old_3400_character_cutoff():
    prose = ('One complete and fairly long sentence. ' * 150)
    chunks = split_telegram_text(prose)
    assert len(chunks) >= 2
    assert all(len(x) <= 3800 for x in chunks)
    assert ' '.join(chunks) == prose.strip()
    assert chunks[0].endswith('.')


def test_continue_in_setup_is_not_saved_as_a_new_story_fact():
    assert is_discussion_only('\u043f\u0440\u043e\u0434\u043e\u043b\u0436\u0438')
    assert is_discussion_only('\u0434\u043e\u043f\u0438\u0448\u0438')
