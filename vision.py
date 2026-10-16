"""Visually grounded character references; never invent hidden identity or attributes."""
from __future__ import annotations
import base64
from io import BytesIO
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_IMAGE_BYTES = 12 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 32_000_000


def image_info(raw: bytes) -> str:
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError('Изображение слишком большое (максимум 12 МБ).')
    try:
        with Image.open(BytesIO(raw)) as im:
            im.verify()
        with Image.open(BytesIO(raw)) as im:
            if im.format not in ('JPEG', 'PNG', 'WEBP'):
                raise ValueError('Поддерживаются JPG, PNG и WEBP.')
            if im.width * im.height > 32_000_000:
                raise ValueError('Слишком высокое разрешение.')
            return {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp'}[im.format]
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError('Не удалось прочитать изображение.') from exc


def image_data_url(raw: bytes) -> str:
    # Compress only the request to OpenAI; the stored reference is untouched.
    image_info(raw)
    with Image.open(BytesIO(raw)) as image:
        image = ImageOps.exif_transpose(image).convert('RGB')
        image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        out = BytesIO()
        image.save(out, format='JPEG', quality=85, optimize=True)
    return 'data:image/jpeg;base64,' + base64.b64encode(out.getvalue()).decode('ascii')


VISION_GUIDANCE = ("Опиши ТОЛЬКО визуально наблюдаемую внешность персонажа на референсе. "
                   "Пиши по-русски, нейтрально, компактно и конкретно, около 80–150 слов. "
                   "Укажи только устойчивые особенности: тип существа/силуэт, естественный цвет/длину волос, "
                   "глаза (если цвет различим), кожу, особенности лица, шрамы, рога, крылья, особые приметы. "
                   "НЕ описывай одежду, украшения, аксессуары, макияж, позу, фон или временную укладку волос: "
                   "они относятся к референсу, а не к постоянному облику. "
                   "Если изображение стилизовано, отметь это; не считай стилизацию объективной анатомией. "
                   "Не приписывай характер, биографию, диагноз, возраст с точностью, намерения, личность, "
                   "расу/этничность, пол или секреты, если они не даны автором. "
                   "Не описывай то, чего не видно. Если на фото несколько фигур, опиши центральную, "
                   "но предупреди, что выбор неоднозначен. Это справочная карточка для повествования, "
                   "а не знание, доступное автоматически всем персонажам.")


async def describe_image(client, model: str, raw: bytes, name: str):
    extra = {'reasoning': {'effort': 'none'}} if model == 'gpt-6-luna' else {}
    response = await client.responses.create(
        model=model,
        instructions=VISION_GUIDANCE,
        input=[{'role': 'user', 'content': [
            {'type': 'input_text', 'text': f'Визуальный референс персонажа «{name[:80]}». Опиши внешность.'},
            {'type': 'input_image', 'image_url': image_data_url(raw), 'detail': 'auto'},
        ]}],
        max_output_tokens=600,
        store=False,
        **extra,
    )
    text = (response.output_text or '').strip()
    if not text:
        raise RuntimeError('Модель не смогла описать изображение.')
    usage = getattr(response, 'usage', None)
    return text, getattr(usage, 'input_tokens', 0) or 0, getattr(usage, 'output_tokens', 0) or 0
