import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (
        UniqueConstraint("nbbet_match_id", name="uq_matches_nbbet_match_id"),
        UniqueConstraint("championat_match_id", name="uq_matches_championat_match_id"),
        Index("ix_matches_duplicate_of", "duplicate_of"),
        CheckConstraint("odds_open_p1 > 1 AND odds_open_p2 > 1", name="ck_matches_odds_open_gt1"),
        CheckConstraint("(odds_open_p1 IS NULL) = (odds_open_p2 IS NULL)", name="ck_matches_odds_open_pair"),
    )

    match_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    gender = Column(String(1))  # M / F
    level = Column(String(20))  # tour / challenger / itf / exhibition
    tournament_name = Column(String)
    match_date = Column(Date)
    player1 = Column(String)
    player2 = Column(String)
    surface = Column(String, nullable=True)
    # UniqueConstraint на nullable-колонке — в Postgres обычный UNIQUE
    # constraint уже nullable-safe (несколько NULL разрешены, конфликтуют
    # только реальные значения), поэтому отдельный partial index не нужен.
    # Это то, чего не хватало для настоящего ON CONFLICT DO UPDATE на
    # уровне БД — раньше идемпотентность держалась только на
    # check-then-insert в приложении (см. backfill_historical_pbp.py/
    # ingestion/nbbet). Имена заданы явно (см. __table_args__) — иначе
    # alembic autogenerate не может сгенерировать рабочий downgrade().
    nbbet_match_id = Column(Integer, nullable=True)
    championat_match_id = Column(Integer, nullable=True)
    has_pbp = Column(Boolean, default=False)
    pbp_s3_path = Column(String, nullable=True)
    pbp_completeness = Column(Float, nullable=True)  # NULL = pbp нет вовсе (has_pbp=False); 0.0 = pbp есть, но ни один гейм не завершён; иначе доля complete-геймов
    # Статус nb-bet (ingestion/nbbet/status.py, NbBetStatus) — на листинге
    # поле "1", на странице матча "23". NULL — статус ещё не известен
    # (строка пришла из sqlite-манифеста, страницу/листинг не смотрели).
    status = Column(SmallInteger, nullable=True)
    # Числовой nbbet_match_id без слага не работает ни на странице
    # (/tennis/events/{id} -> 404), ни в points API (200 с пустым
    # errorCode:404 внутри тела) — проверено отдельно. Слаг обязателен.
    nbbet_slug = Column(String, nullable=True)
    details_fetched_at = Column(DateTime, nullable=True)
    # Счётчик попыток detail_job и время последней — без них failed/
    # retry_later (страница 404, свежий матч без очков) не выходили бы
    # из очереди (details_fetched_at IS NULL) и перекачивались бы на
    # каждом запуске cron. server_default="0" — чтобы у старых строк
    # (sweep уже их создал) сразу было корректное значение, а не NULL.
    details_attempted_at = Column(DateTime, nullable=True)
    details_attempts = Column(SmallInteger, nullable=False, server_default="0")
    # Реальный таймстемп старта матча (поле "4" листинга), UTC без tzinfo
    # (как и остальные datetime-колонки здесь). match_date — производная
    # от него же дата (.date()), посчитанная отдельно в discovery.py;
    # start_ts не меняет и не перепривязывает match_date — это отдельное,
    # более точное поле для анализа по часу/дню, а не замена существующего.
    start_ts = Column(DateTime, nullable=True)
    # Дубль (один реальный матч под двумя nbbet_match_id — подтверждено:
    # идентичный start_ts, и когда у обеих есть pbp — общий pbp_s3_path,
    # т.е. дубль уже в исходной sqlite, не наша ошибка). Удалять строку
    # нельзя — следующий sweep пересоздаст её по nbbet_match_id. Вместо
    # этого duplicate_of у "лишней" строки указывает на оставляемую;
    # detail_job её пропускает (fetch_pending: duplicate_of IS NULL), а
    # при обучении берутся только duplicate_of IS NULL. pipeline/
    # mark_duplicates.py — скрипт, который эту колонку заполняет.
    duplicate_of = Column(UUID(as_uuid=True), nullable=True)
    # Версия набора полей, записанных в nbbet_details при последней
    # обработке detail_job (ingestion/nbbet/details.py: DETAILS_VERSION).
    # server_default="1" — уже обработанные строки (версия 1, без
    # рейтинга) не трогаем молча; detail_job --refresh-version 2
    # находит их по details_version < 2 и перекачивает страницу заново
    # ради новых полей (рейтинг появился только во 2 версии).
    details_version = Column(SmallInteger, nullable=False, server_default="1")
    # Начальные (предматчевые) коэффициенты — пишутся ОДИН РАЗ, при первом
    # наблюдении (sweep из листинга статус 1/2, либо detail_job из
    # match["30"] на странице как запасной путь, если sweep не успел).
    # Повторные проходы не перезаписывают уже заполненные значения — это
    # не "текущая" цена (она движется до начала матча), а именно первая
    # увиденная нами, поэтому источник/статус/время относятся к тому же
    # моменту, что и сами цены. Букмекер не хранится — в данных страницы
    # не указан явно (см. docs/decisions.md про coefficients: "30"/"31" —
    # два набора чисел без текстовых названий, предположительно LEON по
    # интерфейсу источника, не проверено).
    odds_open_p1 = Column(Numeric(7, 3), nullable=True)
    odds_open_p2 = Column(Numeric(7, 3), nullable=True)
    odds_open_captured_at = Column(DateTime, nullable=True)
    odds_open_source = Column(String(10), nullable=True)  # 'listing' | 'page'
    odds_open_status = Column(SmallInteger, nullable=True)  # статус матча в момент захвата
    created_at = Column(DateTime, default=datetime.utcnow)


class JobRun(Base):
    """Журнал запусков регулярных джоб: одна строка на запуск, heartbeat
    по ходу работы. См. ingestion/job_runs.py."""

    __tablename__ = "job_runs"
    __table_args__ = (Index("ix_job_runs_job_started", "job_name", "started_at"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_name = Column(String, nullable=False)
    started_at = Column(DateTime, nullable=False)
    heartbeat_at = Column(DateTime, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String(10), nullable=False)  # running | ok | error
    inserted = Column(Integer, nullable=False, server_default="0")
    updated = Column(Integer, nullable=False, server_default="0")
    skipped = Column(Integer, nullable=False, server_default="0")
    errors_count = Column(Integer, nullable=False, server_default="0")
    error_sample = Column(Text, nullable=True)
    exit_code = Column(SmallInteger, nullable=True)
