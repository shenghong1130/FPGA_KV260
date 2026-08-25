from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from .db_models import Base


class Database:
    def __init__(self, url: str) -> None:
        self.engine: Engine = create_engine(url)
        self.sessions = sessionmaker(self.engine, class_=Session, expire_on_commit=False)

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)

    def close(self) -> None:
        self.engine.dispose()
