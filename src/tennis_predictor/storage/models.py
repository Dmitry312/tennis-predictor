import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, Date, DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (
        UniqueConstraint("nbbet_match_id", name="uq_matches_nbbet_match_id"),
        UniqueConstraint("championat_match_id", name="uq_matches_championat_match_id"),
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
    created_at = Column(DateTime, default=datetime.utcnow)
