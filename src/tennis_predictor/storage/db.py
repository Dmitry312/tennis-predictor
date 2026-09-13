"""SQLAlchemy engine и фабрика сессий.

Модели (ORM-классы) появятся здесь же, когда дойдём до схемы БД —
тогда target_metadata в migrations/env.py перестанет быть None.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tennis_predictor.config import get_settings

settings = get_settings()

engine = create_engine(settings.postgres_dsn, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
