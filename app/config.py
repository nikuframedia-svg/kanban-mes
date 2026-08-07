"""Configuração central. Tudo vem do ambiente (.env carregado pelo docker compose
ou exportado manualmente); defaults seguros para desenvolvimento local."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    # Postgres — plano de produção (leitura) + mes_kanban (escrita de validados)
    pg_host: str = field(default_factory=lambda: _env("MES_PG_HOST", "127.0.0.1"))
    pg_port: int = field(default_factory=lambda: int(_env("MES_PG_PORT", "5432")))
    pg_db: str = field(default_factory=lambda: _env("MES_PG_DB", "dataresearchmtg"))
    pg_user: str = field(default_factory=lambda: _env("MES_PG_USER", "mes_kanban_app"))
    pg_password: str = field(default_factory=lambda: _env("MES_PG_PASSWORD"))

    # Staging local — trabalho em curso nunca toca no Postgres
    data_dir: Path = field(default_factory=lambda: Path(_env("MES_DATA_DIR", str(BASE_DIR / "data"))))

    # OCR — Gemini (free tier UE: dados não usados para treino). Sem chave, modo manual.
    # Primário com quota free decente; o provider tem fallbacks se esgotar.
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY"))
    ocr_model: str = field(default_factory=lambda: _env("MES_OCR_MODEL", "gemini-3-flash-preview"))

    host: str = field(default_factory=lambda: _env("MES_HOST", "127.0.0.1"))
    # 8000 é da bridge do PP1 neste servidor — o MES vive na 8100
    port: int = field(default_factory=lambda: int(_env("MES_PORT", "8100")))
    admin_token: str = field(default_factory=lambda: _env("MES_ADMIN_TOKEN"))

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def pg_dsn(self) -> str:
        return (
            f"host={self.pg_host} port={self.pg_port} dbname={self.pg_db} "
            f"user={self.pg_user} password={self.pg_password}"
        )


settings = Settings()
