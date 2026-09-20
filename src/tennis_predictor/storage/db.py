"""SQLAlchemy engine и фабрика сессий.

ORM-модели живут в models.py (Base, Match); target_metadata в
migrations/env.py указывает на Base.metadata.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tennis_predictor.config import get_settings

settings = get_settings()

engine = create_engine(settings.postgres_dsn, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
