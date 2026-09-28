"""Configuração central. Tudo vem do ambiente (.env carregado pelo docker compose
ou exportado manualmente); defaults seguros para desenvolvimento local."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    """Var vazia = var ausente: uma linha `MES_PG_PORT=` no .env devolvia ""
    e o int("") rebentava o arranque — com Restart=always, em ciclo."""
    return os.environ.get(name, "").strip() or default


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

    # OCR local — Ollama a servir um Qwen de visão na GPU do PC da fábrica.
    # Com MES_QWEN_URL definido passa a ser o motor PRINCIPAL (sem quotas,
    # imagens nunca saem da infraestrutura); vazio = inativo. Ativa-se na
    # migração para o PC da empresa: MES_QWEN_URL=http://localhost:11434.
    qwen_url: str = field(default_factory=lambda: _env("MES_QWEN_URL"))
    qwen_model: str = field(default_factory=lambda: _env("MES_QWEN_MODEL", "qwen3.5:9b"))
    # 600 s: uma GPU fria a carregar o modelo demora; o keep_alive=-1 do
    # provider mantém-no residente a partir da primeira folha.
    qwen_timeout_s: float = field(
        default_factory=lambda: float(_env("MES_QWEN_TIMEOUT_S", "600")))
    # Memória de trabalho do Qwen (imagem + instruções + resposta, em tokens).
    # Sem isto vale o valor por omissão do Ollama (2048 nas versões antigas):
    # medido numa folha real de 15 linhas, a resposta parava na 1.ª linha.
    qwen_num_ctx: int = field(
        default_factory=lambda: int(_env("MES_QWEN_NUM_CTX", "8192")))
    qwen_no_think: bool = field(
        default_factory=lambda: _env("MES_QWEN_NO_THINK", "1").lower() in ("1", "true", "yes"))

    # OCR — Gemini (free tier UE: dados não usados para treino). Sem chave, modo manual.
    # Fallback cloud quando o Qwen local está inativo ou em baixo.
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY"))
    ocr_model: str = field(default_factory=lambda: _env("MES_OCR_MODEL", "gemini-3-flash-preview"))

    # Último recurso pago: API Claude quando TODA a cadeia Gemini falha.
    # Sem chave, não existe — o comportamento fica exatamente o de hoje.
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    claude_ocr_model: str = field(default_factory=lambda: _env("MES_CLAUDE_OCR_MODEL", "claude-haiku-4-5"))

    # Página com fração de tinta abaixo disto é um verso em branco do scanner:
    # não se gasta OCR nela (e evita-se a folha alucinada). Medido: brancas
    # ficam ≈0.0001, a folha real mais rala ≈0.002 — ver imaging.ink_fraction.
    blank_ink_threshold: float = field(
        default_factory=lambda: float(_env("MES_BLANK_INK_THRESHOLD", "0.0008")))

    # Pasta onde o sync do Drive (DATARESEARCHMTG) deixa os PDFs de kanban;
    # o POST /ingest/drive vai lá buscá-los sozinho.
    drive_dir: Path = field(default_factory=lambda: Path(
        _env("MES_DRIVE_DIR", str(Path.home() / "projects" / "DATARESEARCHMTG"))))

    # Que PDFs do Drive são deste setor. Desde 14-08 o scanner deixou de pôr a
    # máquina no nome («18-08-2026.PDF») e o padrão antigo (…rapid…) deixou de
    # apanhar os lotes novos — o default aceita qualquer PDF datado que NÃO
    # esteja marcado como do setor de perfis (serrote/vanguard/mtg2, que são da
    # app kanban-mes-mtg2 na porta 8101).
    kanban_pdf_re: str = field(default_factory=lambda: _env(
        "MES_KANBAN_PDF_RE",
        r"^\d{2}-\d{2}-\d{4}(?!.*(serrote|vanguard|mtg2)).*\.pdf$"))

    host: str = field(default_factory=lambda: _env("MES_HOST", "127.0.0.1"))
    # 8000 é da bridge do PP1 neste servidor — o MES vive na 8100
    port: int = field(default_factory=lambda: int(_env("MES_PORT", "8100")))
    admin_token: str = field(default_factory=lambda: _env("MES_ADMIN_TOKEN"))

    cross_engine: str = field(default_factory=lambda: _env("MES_CROSS_ENGINE", "legacy").lower())

    # Validada logo, grava depois (25/09). «background»: o clique grava só no
    # SQLite e o sync_worker leva a folha ao Postgres por trás. «sync»: o
    # mesmo passo do trabalhador corre dentro do pedido (para diagnóstico ou
    # para voltar atrás; antes de instalar uma versão antiga, esperar que o
    # /health mostre sync_pending=0).
    validation_mode: str = field(
        default_factory=lambda: _env("MES_VALIDATION_MODE", "background").lower())
    # Folhas validadas COM avisos esperam isto antes de irem para o histórico
    # (que só aceita INSERT): janela para «Reabrir» se foi engano.
    sync_delay_with_warnings_s: float = field(
        default_factory=lambda: float(_env("MES_SYNC_DELAY_WITH_WARNINGS_S", "600")))

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
