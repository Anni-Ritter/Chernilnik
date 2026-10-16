"""Resumable AI import of a world bible, with no SQLite writes before approval.

The source and *validated* chunk outputs are checkpointed atomically on disk.
Bad responses are retried, then split into smaller independent requests.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import tempfile
from pathlib import Path

from worlds import PROFILE_FIELDS

log = logging.getLogger(__name__)
MAX_SOURCE = 155_000
CHUNK_CHARS = 2200
MAX_ATTEMPTS = 3
MAX_SPLIT_DEPTH = 3

IMPORT_INSTRUCTIONS = '''Ты индексатор авторской библии мира. Получаешь ОДИН небольшой фрагмент.
Составь КОРОТКИЙ JSON-объект с ключами summary, articles, people. Все три ключа обязательны,
их значения — массивы (пустые массивы разрешены). Только факты, написанные в этом фрагменте.
summary: до 3 объектов {"text":"краткое правило мира", "evidence":"точная цитата"}.
articles: до 5 объектов {"title":"заголовок", "kind":"place|history|rule|organization|culture|rumour|other", "body":"содержательное, но короткое описание", "certainty":"canon|legend|unknown", "evidence":"точная цитата"}.
people: до 5 объектов {"name":"имя", "description":"подтверждённая биография/роль", "fields":[{"key":"species", "value":"точное указание вида"}], "evidence":"точная цитата"}.
В fields только явно указанные значения. Допустимые key: age,gender,species,origin,residence,occupation,family,values,goals,fears,defenses,attachments,boundaries,conflicts,habits,self_image.
Каждое evidence — ДОСЛОВНАЯ непрерывная цитата из полученного текста; иначе запись отбросится.
Пустые поля не заполняй догадками. Не создавай персонажей для голых имён в списке.
Точно различай факты, легенды и неизвестное. Разрушенные исторические территории — не современные обитаемые государства.
Мета-канон — не знание всех персонажей. Не смешивай версии людей из разных миров.
Не возвращай Markdown, комментарии или текст вне JSON. Краткость важнее полноты одной порции.'''


def _obj(properties):
    return {'type':'object', 'properties':properties, 'required':list(properties),
            'additionalProperties':False}

_STR={'type':'string'}
_RESPONSE_SCHEMA = _obj({
    'summary': {'type':'array','items':_obj({'text':_STR,'evidence':_STR})},
    'articles': {'type':'array','items':_obj({
        'title':_STR,'kind':{'type':'string','enum':['place','history','rule','organization','culture','rumour','other']},
        'body':_STR,'certainty':{'type':'string','enum':['canon','legend','unknown']},'evidence':_STR})},
    'people': {'type':'array','items':_obj({
        'name':_STR,'description':_STR,
        'fields':{'type':'array','items':_obj({'key':{'type':'string','enum':list(PROFILE_FIELDS)},'value':_STR})},
        'evidence':_STR})},
})


def split_source(source: str, size: int = CHUNK_CHARS) -> list[str]:
    """Keep headings and paragraphs intact where possible, within a character budget."""
    source = source.strip()
    if not source or len(source) > MAX_SOURCE:
        raise ValueError('Нужен непустой .md/.txt не длиннее 155 000 символов.')
    if size < 350:
        raise ValueError('Слишком маленький размер порции.')
    units = re.split(r'(?=^#{1,3}\s)', source, flags=re.M)
    result, part = [], ''
    for unit in units:
        if not unit.strip():
            continue
        if len(unit) > size:
            sections = re.split(r'(?<=\n)\n+', unit)
            pieces = []
            for sect in sections:
                while len(sect) > size:
                    boundary = sect.rfind(' ', 0, size)
                    boundary = boundary if boundary > size//2 else size
                    pieces.append(sect[:boundary])
                    sect = sect[boundary:].lstrip()
                if sect.strip():
                    pieces.append(sect)
        else:
            pieces = [unit]
        for piece in pieces:
            if len(part) + len(piece) + 2 > size and part:
                result.append(part.strip())
                part = ''
            part += ('\n\n' if part else '') + piece.strip()
    if part.strip():
        result.append(part.strip())
    return result


def _decode(text: str) -> dict:
    text = (text or '').strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.I)
        text = re.sub(r'\s*```\s*$', '', text)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        # Some providers wrap valid JSON in a short preamble. Do not attempt to
        # repair incomplete/truncated JSON: a retry is safer than lost facts.
        start=text.find('{')
        if start >= 0:
            try:
                obj, consumed=json.JSONDecoder().raw_decode(text[start:])
                if text[start+consumed:].strip().strip('`'):
                    raise ValueError('После JSON обнаружен дополнительный текст.')
            except (ValueError,json.JSONDecodeError) as parse_exc:
                raise ValueError('ИИ вернул невалидный или оборванный JSON.') from parse_exc
        else:
            raise ValueError('ИИ вернул невалидный или оборванный JSON.') from exc
    if not isinstance(obj,dict):
        raise ValueError('Ответ ИИ должен быть JSON-объектом.')
    if not all(isinstance(obj.get(key),list) for key in ('summary','articles','people')):
        raise ValueError('В ответе ИИ отсутствуют массивы summary/articles/people.')
    return obj


def _validate_piece(obj: dict, raw: str):
    res={'summary':[],'articles':[],'people':[]}
    for s in obj.get('summary',[])[:3] if isinstance(obj.get('summary'),list) else []:
        if not isinstance(s,dict): continue
        statement=str(s.get('text','')).strip()
        evidence=str(s.get('evidence','')).strip()
        if 12<=len(statement)<=340 and 8<=len(evidence)<=600 and evidence in raw:
            res['summary'].append(statement)
    for row in obj.get('articles',[])[:5] if isinstance(obj.get('articles'),list) else []:
        if not isinstance(row,dict): continue
        title=str(row.get('title','')).strip()
        body=str(row.get('body','')).strip()
        evidence=str(row.get('evidence','')).strip()
        if not (2<=len(title)<=100 and 12<=len(body)<=3000 and 8<=len(evidence)<=600
                and evidence in raw): continue
        certainty=str(row.get('certainty','unknown'))
        kind=str(row.get('kind','other'))
        if certainty not in ('canon','legend','unknown'): certainty='unknown'
        if kind not in ('place','history','rule','organization','culture','rumour','other'): kind='other'
        res['articles'].append({'title':title,'body':body,'evidence':evidence,
                                'certainty':certainty,'kind':kind})
    for row in obj.get('people',[])[:5] if isinstance(obj.get('people'),list) else []:
        if not isinstance(row,dict): continue
        name=str(row.get('name','')).strip()
        desc=str(row.get('description','')).strip()
        evidence=str(row.get('evidence','')).strip()
        if not (2<=len(name)<=80 and 14<=len(desc)<=1900 and 8<=len(evidence)<=600
                and evidence in raw): continue
        fields={}
        values = row.get('fields',{})
        if isinstance(values,list):
            values={r['key']:r['value'] for r in values if isinstance(r,dict)
                    and isinstance(r.get('key'),str) and isinstance(r.get('value'),str)}
        if isinstance(values,dict):
            for key,val in values.items():
                if key in PROFILE_FIELDS and isinstance(val,str) and 1<=len(val.strip())<=500 \
                        and val.strip().casefold() in raw.casefold():
                    fields[key]=val.strip()
        res['people'].append({'name':name,'description':desc,'fields':fields,'evidence':evidence})
    return res


def _atomic_json(path: Path, obj: dict):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.lore_',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(obj,f,ensure_ascii=False,indent=2)
            f.write('\n');f.flush();os.fsync(f.fileno())
        os.chmod(name,0o600)
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)


class ImportDraft:
    """Persistent progress per (world, source). Never writes to the game DB."""
    def __init__(self, root: str|Path, wid: int, digest: str):
        self.root=Path(root)
        self.wid=int(wid)
        if not re.fullmatch(r'[0-9a-f]{16}',digest):
            raise ValueError('Некорректный идентификатор импорта.')
        self.digest=digest
        self.json_path=self.root/f'world_{wid}_{digest}.json'
        self.source_path=self.root/f'world_{wid}_{digest}.md'
        self.data=json.loads(self.json_path.read_text(encoding='utf-8'))
        if self.data.get('wid')!=self.wid or self.data.get('digest')!=digest:
            raise ValueError('Черновик импорта не соответствует миру.')
        if hashlib.sha256(self.source.encode('utf-8')).hexdigest()[:16]!=digest:
            raise ValueError('Источник черновика повреждён; сохранение запрещено.')

    @property
    def source(self):
        return self.source_path.read_text(encoding='utf-8')

    @property
    def model(self):
        return self.data['model']

    @property
    def complete(self):
        return bool(self.data.get('complete'))

    def save(self):
        _atomic_json(self.json_path,self.data)

    def completed_root_parts(self, parts):
        def done(key):
            if key in self.data['chunks']: return True
            return key in self.data['splits'] and all(done(child) for child in self.data['splits'][key])
        return sum(done(str(i)) for i in range(1,len(parts)+1))

    def collect(self, parts):
        def descend(key):
            if key in self.data['chunks']:
                return [self.data['chunks'][key]]
            if key in self.data['splits']:
                return [piece for child in self.data['splits'][key] for piece in descend(child)]
            raise ValueError('Не все части обработаны; сначала продолжи импорт.')
        return [piece for i in range(1,len(parts)+1) for piece in descend(str(i))]

    @classmethod
    def create(cls,root: str|Path,wid:int,source:str,model:str):
        parts=split_source(source)
        if len(parts)>100:
            raise ValueError('Документ слишком большой для одного импорта; раздели его на два файла.')
        root=Path(root)
        root.mkdir(parents=True,exist_ok=True)
        digest=hashlib.sha256(source.encode('utf-8')).hexdigest()[:16]
        json_path=root/f'world_{wid}_{digest}.json'
        source_path=root/f'world_{wid}_{digest}.md'
        if json_path.exists():
            # Reuse partial work. An intentionally cancelled/applied import can
            # be started again by uploading the file again.
            old=cls(root,wid,digest)
            if not old.data.get('cancelled') and not old.data.get('applied'):
                return old
            old.data={'version':2,'wid':int(wid),'digest':digest,'model':model,
                      'chunks':{},'splits':{},'complete':False,'applied':False}
            old.save()
            return old
        if not source_path.exists():
            fd,tmp=tempfile.mkstemp(prefix='.lore_source_',suffix='.tmp',dir=root)
            try:
                with os.fdopen(fd,'w',encoding='utf-8') as f:
                    f.write(source);f.flush();os.fsync(f.fileno())
                os.chmod(tmp,0o600);os.replace(tmp,source_path)
            finally:
                if os.path.exists(tmp):os.unlink(tmp)
        _atomic_json(json_path,{'version':2,'wid':int(wid),'digest':digest,'model':model,
                                'chunks':{},'splits':{},'complete':False,'applied':False})
        return cls(root,wid,digest)

    @classmethod
    def latest(cls,root: str|Path,wid:int):
        root=Path(root)
        for path in sorted(root.glob(f'world_{int(wid)}_*.json'),key=lambda p:p.stat().st_mtime,reverse=True):
            try:
                obj=json.loads(path.read_text(encoding='utf-8'))
                if not obj.get('applied') and not obj.get('cancelled'):
                    return cls(root,wid,obj['digest'])
            except (OSError,ValueError,KeyError,json.JSONDecodeError):
                log.warning('Unreadable import draft at %s',path)
        return None

    def cancel(self):
        self.data['cancelled']=True
        self.save()

    def mark_applied(self):
        self.data['applied']=True
        self.save()


def draft_root(db):
    return Path(db.path).parent/'import_drafts'


def _supports_response_format(create):
    try:
        sig=inspect.signature(create)
        return 'response_format' in sig.parameters or any(p.kind==inspect.Parameter.VAR_KEYWORD
                                                         for p in sig.parameters.values())
    except (TypeError,ValueError):
        return True


def _unsupported_format(exc):
    return getattr(exc,'status_code',None) in (400,422) and any(
        token in str(exc).lower() for token in ('response_format','json_schema','json_object','unsupported','not supported'))


async def _extract(client,model,raw,part_id,format_state):
    last_exc=None
    for attempt in range(1,MAX_ATTEMPTS+1):
        kwargs={'model':model,
                'messages':[{'role':'system','content':IMPORT_INSTRUCTIONS},
                            {'role':'user','content':f'ЧАСТЬ {part_id}. Верни только полный JSON:\n\n{raw}'}],
                'max_tokens':2800}
        if format_state['mode']=='schema':
            kwargs['response_format']={'type':'json_schema', 'json_schema':{
                'name':'lore_fragment','strict':True,'schema':_RESPONSE_SCHEMA}}
        elif format_state['mode']=='json_object':
            kwargs['response_format']={'type':'json_object'}
        if attempt>1:
            kwargs['messages'].append({'role':'user','content':
                'Предыдущий ответ был неполным или не прошёл проверку. '
                'Нужен короткий завершённый JSON-объект с массивами summary, articles, people. '
                'Не добавляй пояснений. Сократи количество записей при необходимости.'})
        try:
            answer=await client.chat.completions.create(**kwargs)
            choice=answer.choices[0]
            if getattr(choice,'finish_reason',None)=='length':
                raise ValueError('Ответ оборвался по лимиту выходных токенов.')
            payload=_decode(choice.message.content or '')
            parsed=_validate_piece(payload,raw)
            if (sum(len(payload[k]) for k in ('summary','articles','people'))>0
                    and not any(parsed[k] for k in ('summary','articles','people'))):
                raise ValueError('Все записи отклонены: цитаты не подтверждаются исходником.')
            # An intentionally empty result is legitimate, but makes sense only
            # for truly sparse chunks; do not retry a valid empty response.
            return parsed
        except Exception as exc:
            if _unsupported_format(exc) and format_state['mode']!='plain':
                format_state['mode']='json_object' if format_state['mode']=='schema' else 'plain'
                log.info('Lore importer: response format unavailable, falling back to %s',format_state['mode'])
                continue
            # Auth / payments / permissions cannot be fixed by retries or splitting.
            if getattr(exc,'status_code',None) in (401,402,403,404):
                raise
            last_exc=exc
            log.warning('Lore fragment %s attempt %s/%s failed: %s',part_id,attempt,MAX_ATTEMPTS,
                        type(exc).__name__)
            if attempt<MAX_ATTEMPTS:
                await asyncio.sleep(min(2**(attempt-1),4))
    if last_exc is not None and not isinstance(last_exc, ValueError):
        raise last_exc
    raise ValueError(f'Фрагмент {part_id}: {last_exc}')


def _split_failed(raw):
    """Split at a nearby paragraph/sentence boundary, else safely at midpoint."""
    mid=len(raw)//2
    options=[raw.rfind('\n\n',0,mid+150),raw.find('\n\n',max(0,mid-150)),
             raw.rfind('. ',0,mid+100),raw.find('. ',max(0,mid-100))]
    choices=[c for c in options if c!=-1 and len(raw)//4<=c<=len(raw)*3//4]
    at=min(choices,key=lambda c:abs(c-mid)) if choices else mid
    if raw[at:at+2] in ('\n\n','. '): at+=2
    return raw[:at].strip(),raw[at:].strip()


async def analyze_bible(client,model:str,text:str,*,max_parts:int=100,on_progress=None,
                        draft:ImportDraft|None=None) -> dict:
    """Resume validated pieces from draft; never call Polza for completed pieces."""
    parts=split_source(text)
    if len(parts)>max_parts:
        raise ValueError(f'Документ требует {len(parts)} частей; лимит {max_parts}.')
    if draft is not None:
        if draft.source!=text or draft.model!=model:
            raise ValueError('Исходный файл или модель отличаются от сохранённого черновика.')
        if draft.data.get('applied') or draft.data.get('cancelled'):
            raise ValueError('Этот импорт уже завершён или отменён.')
    state={'mode':'schema' if _supports_response_format(client.chat.completions.create) else 'plain'}
    local_chunks={};local_splits={}
    chunks=draft.data['chunks'] if draft else local_chunks
    splits=draft.data['splits'] if draft else local_splits

    async def part(raw,key,depth=0):
        if key in chunks:
            return
        if key in splits:
            left,right=_split_failed(raw)
            if not left or not right:
                raise ValueError('Нельзя восстановить части сохранённого фрагмента.')
            a,b=splits[key]
            await part(left,a,depth+1)
            await part(right,b,depth+1)
            return
        try:
            parsed=await _extract(client,model,raw,key,state)
        except ValueError:
            if depth>=MAX_SPLIT_DEPTH or len(raw)<800:
                raise
            left,right=_split_failed(raw)
            if not left or not right:
                raise
            a,b=f'{key}.1',f'{key}.2'
            splits[key]=[a,b]
            if draft: draft.save()
            await part(left,a,depth+1)
            await part(right,b,depth+1)
            return
        chunks[key]=parsed
        if draft: draft.save()

    for idx,chunk in enumerate(parts,1):
        await part(chunk,str(idx))
        if on_progress:
            await on_progress(idx,len(parts))
    pieces=draft.collect(parts) if draft else _collect_local(chunks,splits,len(parts))
    result=_combine(pieces,len(parts))
    if draft:
        draft.data['complete']=True
        draft.save()
    return result


def _collect_local(chunks,splits,n):
    def rec(k):
        if k in chunks:return [chunks[k]]
        return [x for child in splits[k] for x in rec(child)]
    return [x for i in range(1,n+1) for x in rec(str(i))]


def _combine(pieces,part_count):
    result={'summary':[],'articles':[],'people':[],'parts':part_count}
    for piece in pieces:
        for key in ('summary','articles','people'):
            result[key].extend(piece[key])
    summaries=[];seen=set()
    for s in result['summary']:
        if s.casefold() not in seen:
            summaries.append(s);seen.add(s.casefold())
    result['summary']=summaries[:50]
    for key,field in (('articles','title'),('people','name')):
        merged={}
        for item in result[key]:
            k=item[field].casefold()
            if k not in merged:
                merged[k]=dict(item)
                continue
            old=merged[k]
            if key=='articles':
                if item['body'] not in old['body'] and len(old['body'])+len(item['body'])<13000:
                    old['body']+='\n\n'+item['body']
                # Do not promote legend/unknown to canon.
                if item['certainty']!='canon':old['certainty']=item['certainty']
            else:
                if item['description'] not in old['description'] and len(old['description'])+len(item['description'])<5500:
                    old['description']+='\n'+item['description']
                for f,v in item['fields'].items():
                    old['fields'].setdefault(f,v)
        result[key]=list(merged.values())
    if not result['articles'] and not result['people'] and not result['summary']:
        raise ValueError('ИИ не выделил проверяемых сведений; черновик сохранён.')
    return result


def plan_from_draft(draft:ImportDraft):
    parts=split_source(draft.source)
    return _combine(draft.collect(parts),len(parts))


def render_import_preview(plan: dict, world_name: str) -> str:
    output=[f'# ИИ-импорт: {world_name}',
            '', '**Черновик для проверки. До подтверждения не сохраняется в энциклопедию.**',
            '', f'Фрагментов: {plan.get("parts",0)}',
            '', '## Основные правила мира']
    output.extend(f'- {s}' for s in plan.get('summary',[]))
    output.extend(['','## Энциклопедия'])
    for art in plan.get('articles',[]):
        output.extend(['',f'### {art["title"]}',
                       f'Категория: {art["kind"]} · Достоверность: {art["certainty"]}',
                       '',art['body'],'',f'> Основание: {art["evidence"]}'])
    output.extend(['','## Персонажи'])
    for person in plan.get('people',[]):
        output.extend(['',f'### {person["name"]}','',person['description']])
        for field,val in person.get('fields',{}).items():
            output.append(f'- {PROFILE_FIELDS.get(field,field)}: {val}')
        output.extend(['',f'> Основание: {person["evidence"]}'])
    output.extend(['','---','Оригинал источника хранится отдельно. Описания ИИ необходимо проверять: '
                   'цитата свидетельствует только об исходном тексте, не о корректности всех выводов.'])
    return '\n'.join(output)


def apply_bible(db,wid:int,source:str,plan:dict)->dict:
    """One SQLite transaction; never clobber author's existing profile/canon."""
    if not db.get_world(wid):
        raise ValueError('Мир не найден.')
    if len(source)>MAX_SOURCE:
        raise ValueError('Документ слишком большой.')
    digest=hashlib.sha256(source.encode('utf-8')).hexdigest()[:16]
    directory=Path(db.path).parent/'imports'
    directory.mkdir(parents=True,exist_ok=True)
    archive=directory/f'world_{wid}_{digest}.md'
    if not archive.exists():
        tmp=archive.with_suffix('.tmp')
        tmp.write_text(source,encoding='utf-8');tmp.replace(archive)
    counts={'articles':0,'characters':0,'fields':0,'summary':0,'skipped':0,'source_file':str(archive)}
    with db.connect() as conn:
        w=conn.execute('SELECT description FROM worlds WHERE id=?',(wid,)).fetchone()
        description=w['description']
        for s in plan.get('summary',[]):
            if not isinstance(s,str) or s.casefold() in description.casefold():continue
            if len(description)+len(s)+3>22000:counts['skipped']+=1;continue
            description+=('\n\n' if description else '')+s
            counts['summary']+=1
        conn.execute('UPDATE worlds SET description=? WHERE id=?',(description,wid))
        existing_articles={r['title'].casefold() for r in conn.execute('SELECT title FROM world_lore WHERE world_id=?',(wid,))}
        for article in plan.get('articles',[]):
            name=article['title'].strip()
            if name.casefold() in existing_articles:counts['skipped']+=1;continue
            existing_articles.add(name.casefold())
            certainty=article['certainty']
            tag={'canon':'Подтверждённый канон','legend':'Легенда / слух','unknown':'Неизвестно / не установлено'}[certainty]
            body=f'Статус: {tag}\nКатегория: {article["kind"]}\n\n{article["body"]}'
            if len(body)>15000:counts['skipped']+=1;continue
            conn.execute('INSERT INTO world_lore(world_id,title,body) VALUES (?,?,?)',(wid,name,body))
            counts['articles']+=1
        existing_chars={r['name'].casefold():r['id'] for r in conn.execute('SELECT id,name FROM characters WHERE world_id=?',(wid,))}
        for person in plan.get('people',[]):
            name=person['name'].strip();cid=existing_chars.get(name.casefold())
            if cid is None:
                cid=conn.execute('''INSERT INTO characters(session_id,name,role,world_id,description)
                    VALUES (NULL,?,'npc',?,?)''',(name,wid,person['description'][:6000])).lastrowid
                existing_chars[name.casefold()]=cid;counts['characters']+=1
            else:
                row=conn.execute('SELECT description FROM characters WHERE id=?',(cid,)).fetchone()
                if not row['description'].strip():
                    conn.execute('UPDATE characters SET description=? WHERE id=?',(person['description'][:6000],cid))
            for field,value in person.get('fields',{}).items():
                if field not in PROFILE_FIELDS:continue
                row=conn.execute('SELECT 1 FROM character_fields WHERE character_id=? AND field=? AND session_id=0',(cid,field)).fetchone()
                if row:continue
                conn.execute('''INSERT INTO character_fields(character_id,field,session_id,value,source,certainty,evidence)
                    VALUES (?,?,?,?,?,?,?)''',(cid,field,0,value[:1500],'import','confirmed',person['evidence'][:350]))
                counts['fields']+=1
    return counts
