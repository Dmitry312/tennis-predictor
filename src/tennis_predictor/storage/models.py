from datetime import datetime
import uuid

from sqlalchemy import Boolean, Column, Date, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Match(Base):
    __tablename__ = "matches"

    match_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    gender = Column(String(1))  # M / F
    level = Column(String(20))  # tour / challenger / itf / exhibition
    tournament_name = Column(String)
    match_date = Column(Date)
    player1 = Column(String)
    player2 = Column(String)
    surface = Column(String, nullable=True)
    nbbet_match_id = Column(Integer, nullable=True)
    championat_match_id = Column(Integer, nullable=True)
    has_pbp = Column(Boolean, default=False)
    pbp_s3_path = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
