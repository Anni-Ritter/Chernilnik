"""Persist the preferred literary model next to (but outside) the existing SQLite DB.

No SQLite migrations and no API keys stored in this JSON file.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile

POLZA_BASE_URL = "https://polza.ai/api/v1"
DEFAULT_POLZA_MODEL = "google/gemini-3.7-flash"
PRESETS = {
    "gemini37": ("polza", "google/gemini-3.7-flash", "Gemini 3.7 Flash"),
    "gemini38": ("polza", "google/gemini-3.8-flash", "Gemini 3.8 Flash"),
}
_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/+-]{1,127}$")


@dataclass(frozen=True)
class GenerationChoice:
    provider: str
    model: str

    def display(self) -> str:
        return f"{self.provider.upper()} · {self.model}"


def validate_choice(provider: str, model: str) -> GenerationChoice:
    if provider not in ("polza", "openai"):
        raise ValueError("Провайдер должен быть polza или openai.")
    if not isinstance(model, str) or not _ID.fullmatch(model):
        raise ValueError("Некорректный ID модели (2–128 символов, латиница/цифры и ./_-:+).")
    if provider == "polza" and ("/" not in model or model.startswith("/") or model.endswith("/")):
        raise ValueError("Для Polza укажи полный ID с названием провайдера: google/gemini-3.7-flash.")
    return GenerationChoice(provider, model)


class GenerationSettings:
    """A small atomic JSON config in persistent storage; never touches sqlite."""
    def __init__(self, path: str | Path, openai_model: str,
                 default_provider: str = "polza", default_model: str = DEFAULT_POLZA_MODEL):
        self.path = Path(path)
        self.openai_model = openai_model
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                stored = json.loads(self.path.read_text(encoding="utf-8"))
                self._choice = validate_choice(stored["provider"], stored["model"])
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"Не удалось прочитать выбор модели в {self.path}. "
                                   "Файл не перезаписан: сохрани его и исправь конфигурацию.") from exc
        else:
            self._choice = validate_choice(default_provider, default_model)
            self._save(self._choice)

    @property
    def choice(self) -> GenerationChoice:
        return self._choice

    def select(self, provider: str, model: str) -> GenerationChoice:
        new_choice = validate_choice(provider, model)
        self._save(new_choice)  # do not change running config if persistence fails
        self._choice = new_choice
        return new_choice

    def _save(self, choice: GenerationChoice) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=".generation_model_", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump({"provider": choice.provider, "model": choice.model}, out,
                          ensure_ascii=False, indent=2)
                out.write("\n")
                out.flush()
                os.fsync(out.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
