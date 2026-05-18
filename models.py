from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, index=True, nullable=False)
    username = Column(String, nullable=True)

    access_granted = Column(Boolean, default=False)
    is_admin = Column(Boolean, default=False)
    is_banned = Column(Boolean, default=False)

    # настройки парсинга
    country = Column(String, nullable=True)  # ch | fi | null
    json_limit = Column(Integer, default=50)
    last_account_token = Column(Text, nullable=True)

    total_parses = Column(Integer, default=0)
    total_listings = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)
    last_active_at = Column(DateTime, nullable=True)

    proxies = relationship("Proxy", back_populates="user", cascade="all, delete-orphan")
    categories = relationship("UserCategory", back_populates="user", cascade="all, delete-orphan")
    parse_runs = relationship("ParseRun", back_populates="user", cascade="all, delete-orphan")


class Proxy(Base):
    __tablename__ = "proxies"

    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    host = Column(String, nullable=False)
    port = Column(Integer, nullable=False)
    username = Column(String, nullable=True)
    password = Column(String, nullable=True)
    proxy_type = Column(String, default="socks5")
    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="proxies")


class UserCategory(Base):
    __tablename__ = "user_categories"
    __table_args__ = (UniqueConstraint("user_id", "category_key", name="uq_user_category"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    category_key = Column(String, nullable=False)
    label = Column(String, nullable=False)
    url_path = Column(String, nullable=False)
    is_preset = Column(Boolean, default=True)

    user = relationship("User", back_populates="categories")


class ParseRun(Base):
    __tablename__ = "parse_runs"

    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    status = Column(String, default="running")  # running | done | stopped | error
    listings_count = Column(Integer, default=0)
    categories_used = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)

    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="parse_runs")
