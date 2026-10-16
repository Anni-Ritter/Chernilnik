"""AI-assisted, non-destructive import of a world bible into shared lore and library.

The model proposes *evidence-backed* entries; a human confirms a preview before
anything is written to SQLite. The original source is kept outside the DB.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from worlds import PROFILE_FIELDS

MAX_SOURCE = 155_000
IMPORT_INSTRUCTIONS = '''Ты библиотекарь по авторскому фэнтези-канону. Получаешь ФРАГМЕНТ библии мира.
Извлеки факты ДЛЯ БАЗЫ ЗНАНИЙ, не сочиняй сюжет. Ответ СТРОГО JSON-объект:
{"summary":[{"text":"короткое устойчивое мировое правило", "evidence":"дословная непрерывная цитата из источника"}],
 "articles":[{"title":"Название сущности или раздела", "kind":"place|history|rule|organization|culture|rumour|other", "body":"сжатое, но содержательное описание на русском, только по источнику", "certainty":"canon|legend|unknown", "evidence":"дословный непрерывный фрагмент источника"}],
 "people":[{"name":"точное имя персонажа", "description":"только прямо установленная биография/должность/вид", "fields":{"species":"вид, если явно известен", "occupation":"роль, если явно известна"}, "evidence":"дословная цитата из источника"}]}.
Максимум 11 статей и 11 персонажей на фрагмент. Отбирай описанные сущности, не создавай анкету для одного лишь имени в перечне.
Страна, культура, магический закон, артефакт, академия, историческое событие -> articles.
Установленное конкретное лицо с описанием -> people, НЕ общая энциклопедическая статья, если только не нужна отдельная историческая справка.
В summary помести только базовые постоянные правила (максимум 4 на фрагмент), с дословной цитатой evidence.
Каждая статья и персонаж ОБЯЗАТЕЛЬНО требуют evidence: точную цитату из фрагмента (не пересказ). Если не уверен, пропусти.
Обязательно различай объективный факт и слух/легенду/невыясненное: certainty=legend/unknown; не утверждай истинность слуха.
Устаревшие и разрушенные территории помечай историческими, не записывай в текущие обитаемые страны.
Персонажи разных вселенных не объединяются автоматически по именам. Не заполняй скрытые мысли/возраст/семью, если источник не говорит.
В fields допустимы только ключи age/gender/species/origin/residence/occupation/family/values/goals/fears/defenses/attachments/boundaries/conflicts/habits/self_image.
Не включай Markdown-ограждения вокруг JSON.''' 


def split_source(source: str, size: int = 6500) -> list[str]:
    """Whole paragraphs and headings survive across bounded model requests."""
    source = source.strip()
    if not source or len(source) > MAX_SOURCE:
        raise ValueError('Нужен непустой .md/.txt не длиннее 155 000 символов.')
    units = re.split(r'(?=^#{1,3}\s)', source, flags=re.M)
    result, part = [], ''
    for unit in units:
        if not unit.strip():
            continue
        if len(unit) > size:
            sections = re.split(r'(?<=\n)\n+', unit)
            # Any single giant paragraph is split at the physical character limit.
            pieces = []
            for sect in sections:
                while len(sect)>size:
                    boundary=sect.rfind(' ',0,size)
                    boundary=boundary if boundary>size//2 else size
                    pieces.append(sect[:boundary])
                    sect=sect[boundary:].lstrip()
                if sect.strip():
                    pieces.append(sect)
        else:
            pieces=[unit]
        for piece in pieces:
            if len(part)+len(piece)>size and part:
                result.append(part.strip()); part=''
            part += ('\n\n' if part else '')+piece.strip()
    if part.strip():
        result.append(part.strip())
    return result


def _decode(text: str) -> dict:
    text=text.strip()
    if text.startswith('```'):
        text=re.sub(r'^```(?:json)?\s*','',text,flags=re.I)
        text=re.sub(r'\s*```\s*$','',text)
    try:
        obj=json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError('ИИ вернул невалидный JSON; попробуй импорт ещё раз.') from exc
    if not isinstance(obj,dict):
        raise ValueError('Ответ ИИ должен быть JSON-объектом.')
    return obj


def _validate_piece(obj: dict, raw: str):
    res={'summary':[],'articles':[],'people':[]}
    for s in obj.get('summary',[])[:4] if isinstance(obj.get('summary'),list) else []:
        if not isinstance(s,dict): continue
        statement=str(s.get('text','')).strip()
        evidence=str(s.get('evidence','')).strip()
        if 12<=len(statement)<=340 and 8<=len(evidence)<=600 and evidence in raw:
            res['summary'].append(statement)
    for row in obj.get('articles',[])[:11] if isinstance(obj.get('articles'),list) else []:
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
    for row in obj.get('people',[])[:11] if isinstance(obj.get('people'),list) else []:
        if not isinstance(row,dict): continue
        name=str(row.get('name','')).strip()
        desc=str(row.get('description','')).strip()
        evidence=str(row.get('evidence','')).strip()
        if not (2<=len(name)<=80 and 14<=len(desc)<=1900 and 8<=len(evidence)<=600
                and evidence in raw): continue
        fields={}
        if isinstance(row.get('fields'),dict):
            for key,val in row['fields'].items():
                if key in PROFILE_FIELDS and isinstance(val,str) and 1<=len(val.strip())<=500 \
                        and val.strip().casefold() in raw.casefold():
                    fields[key]=val.strip()
        res['people'].append({'name':name,'description':desc,'fields':fields,'evidence':evidence})
    return res


async def analyze_bible(client, model: str, text: str, *, max_parts: int = 26, on_progress=None) -> dict:
    parts=split_source(text)
    if len(parts)>max_parts:
        raise ValueError(f'Документ требует {len(parts)} частей; лимит {max_parts}. Раздели на два файла.')
    result={'summary':[],'articles':[],'people':[],'parts':len(parts)}
    for idx,chunk in enumerate(parts,1):
        response=await client.chat.completions.create(
            model=model,
            messages=[{'role':'system','content':IMPORT_INSTRUCTIONS},
                      {'role':'user','content':f'ФРАГМЕНТ {idx} ИЗ {len(parts)}:\n\n{chunk}'}],
            max_tokens=4300,
        )
        payload=_decode(response.choices[0].message.content or '')
        output=_validate_piece(payload,chunk)
        if on_progress is not None:
            await on_progress(idx,len(parts))
        for key in ('summary','articles','people'):
            result[key].extend(output[key])
    # Casefold, including Cyrillic, rather than SQLite NOCASE, which is ASCII-only.
    summaries=[]; seen=set()
    for s in result['summary']:
        if s.casefold() not in seen:
            summaries.append(s); seen.add(s.casefold())
    result['summary']=summaries[:30]
    for key,field in (('articles','title'),('people','name')):
        merged={}
        for item in result[key]:
            k=item[field].casefold()
            if k not in merged:
                merged[k]=item
                continue
            old=merged[k]
            if key=='articles':
                if item['body'] not in old['body'] and len(old['body'])+len(item['body'])<13000:
                    old['body']+='\n\n'+item['body']
                # A legend cannot be promoted to confirmed when sections disagree.
                if item['certainty']!='canon': old['certainty']=item['certainty']
            else:
                if item['description'] not in old['description'] and len(old['description'])+len(item['description'])<5500:
                    old['description']+='\n'+item['description']
                for f,v in item['fields'].items():
                    old['fields'].setdefault(f,v)
        result[key]=list(merged.values())
    if not result['articles'] and not result['people'] and not result['summary']:
        raise ValueError('ИИ не выделил проверяемых сведений; исходный документ не изменён.')
    return result



def render_import_preview(plan: dict, world_name: str) -> str:
    """Complete, human-readable model proposal. It is NOT yet canonical."""
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


def apply_bible(db, wid: int, source: str, plan: dict) -> dict:
    """One SQLite transaction; never clobbers author's existing profile/canon."""
    if not db.get_world(wid):
        raise ValueError('Мир не найден.')
    if len(source)>MAX_SOURCE:
        raise ValueError('Документ слишком большой.')
    # Persist the complete original for audit/re-import, independently of AI summaries.
    digest=hashlib.sha256(source.encode('utf-8')).hexdigest()[:16]
    directory=Path(db.path).parent/'imports'
    directory.mkdir(parents=True,exist_ok=True)
    archive=directory/f'world_{wid}_{digest}.md'
    if not archive.exists():
        tmp=archive.with_suffix('.tmp')
        tmp.write_text(source,encoding='utf-8')
        tmp.replace(archive)
    counts={'articles':0,'characters':0,'fields':0,'summary':0,'skipped':0,'source_file':str(archive)}
    with db.connect() as conn:
        w=conn.execute('SELECT description FROM worlds WHERE id=?',(wid,)).fetchone()
        description=w['description']
        for s in plan.get('summary',[]):
            if not isinstance(s,str) or s.casefold() in description.casefold(): continue
            if len(description)+len(s)+3>22000: counts['skipped']+=1; continue
            description+= ('\n\n' if description else '')+s
            counts['summary']+=1
        conn.execute('UPDATE worlds SET description=? WHERE id=?',(description,wid))
        existing_articles={r['title'].casefold() for r in conn.execute('SELECT title FROM world_lore WHERE world_id=?',(wid,))}
        for article in plan.get('articles',[]):
            name=article['title'].strip()
            if name.casefold() in existing_articles: counts['skipped']+=1; continue
            existing_articles.add(name.casefold())
            certainty=article['certainty']
            tag={'canon':'Подтверждённый канон', 'legend':'Легенда / слух', 'unknown':'Неизвестно / не установлено'}[certainty]
            body=f'Статус: {tag}\nКатегория: {article["kind"]}\n\n{article["body"]}'
            if len(body)>15000: counts['skipped']+=1; continue
            conn.execute('INSERT INTO world_lore(world_id,title,body) VALUES (?,?,?)',(wid,name,body))
            counts['articles']+=1
        existing_chars={r['name'].casefold():r['id'] for r in conn.execute('SELECT id,name FROM characters WHERE world_id=?',(wid,))}
        for person in plan.get('people',[]):
            name=person['name'].strip()
            cid=existing_chars.get(name.casefold())
            if cid is None:
                # v0.6.1 schema: world-library characters may have no story yet.
                cid=conn.execute('''INSERT INTO characters(session_id,name,role,world_id,description)
                    VALUES (NULL,?,'npc',?,?)''',(name,wid,person['description'][:6000])).lastrowid
                existing_chars[name.casefold()]=cid
                counts['characters']+=1
            else:
                row=conn.execute('SELECT description FROM characters WHERE id=?',(cid,)).fetchone()
                if not row['description'].strip():
                    conn.execute('UPDATE characters SET description=? WHERE id=?',
                                 (person['description'][:6000],cid))
            for field,value in person.get('fields',{}).items():
                if field not in PROFILE_FIELDS: continue
                row=conn.execute('SELECT 1 FROM character_fields WHERE character_id=? AND field=? AND session_id=0',
                                 (cid,field)).fetchone()
                if row: continue
                conn.execute('''INSERT INTO character_fields(character_id,field,session_id,value,source,certainty,evidence)
                    VALUES (?,?,?,?,?,?,?)''',
                    (cid,field,0,value[:1500],'import','confirmed',person['evidence'][:350]))
                counts['fields']+=1
    return counts
