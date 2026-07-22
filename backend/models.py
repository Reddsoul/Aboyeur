"""SQLAlchemy models + SQLite init for Aboyeur.

Schema source of truth: task.md "SQLite Schema" + "IngestJob Table" sections.
"""
from sqlalchemy import (
    create_engine, Column, String, Text, Integer, Float, DateTime, JSON, ForeignKey,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime
from pathlib import Path
import os

Base = declarative_base()

# Database path — env override (DB_PATH) wins, else local data/aboyeur.db.
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = os.getenv("DB_PATH", str(DATA_DIR / "aboyeur.db"))


class Book(Base):
    __tablename__ = "books"
    id = Column(String, primary_key=True)            # slug, e.g. "food-of-sichuan"
    title = Column(String, nullable=False)
    author = Column(String, default="")
    cuisine = Column(JSON, default=list)             # ["Chinese", "Sichuan"]
    tier = Column(Integer, default=2)                # 1=everyday 2=occasional 3=special
    flavor_profiles = Column(JSON, default=list)     # ["spicy", "umami"]
    key_ingredients = Column(JSON, default=list)
    complexity = Column(String, default="intermediate")
    source_path = Column(String)
    ingested_at = Column(DateTime, default=datetime.utcnow)
    recipes = relationship("Recipe", back_populates="book", cascade="all, delete-orphan")


class Recipe(Base):
    __tablename__ = "recipes"
    id = Column(String, primary_key=True)            # "{book_id}::{slug}"
    book_id = Column(String, ForeignKey("books.id"), nullable=False)
    title = Column(String, nullable=False)
    chunk_text = Column(Text)
    ingredients = Column(JSON, default=list)         # names only, no qty
    technique = Column(String, default="")           # stir_fry|braise|roast|steam|raw|bake
    calories = Column(Float, default=0)
    protein_g = Column(Float, default=0)
    carb_g = Column(Float, default=0)
    fat_g = Column(Float, default=0)
    # Audit-trail extras (mirrors recipe frontmatter; not in the minimal spec but useful).
    usda_status = Column(String, default="skipped")  # success|failed|skipped
    chroma_id = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    book = relationship("Book", back_populates="recipes")


class IngestJob(Base):
    __tablename__ = "ingest_jobs"
    id = Column(String, primary_key=True)            # book_id slug
    filename = Column(String, nullable=False)
    md5 = Column(String)
    stage = Column(String, default="detected")       # detected|hashing|extracting|chunking|indexing|done|failed
    stage_error = Column(Text)
    failed_stage = Column(String)
    extractor = Column(String)                       # "calibre" or "marker"
    raw_md_path = Column(String)
    chunks_found = Column(Integer, default=0)
    chunks_indexed = Column(Integer, default=0)
    chunks_skipped = Column(Integer, default=0)
    detected_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)


class PantryItem(Base):
    __tablename__ = "pantry"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    quantity = Column(String, default="")            # "2 cups" — freeform
    category = Column(String, default="")            # produce|protein|dairy|pantry|spice|frozen
    added_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)


class HouseholdMember(Base):
    __tablename__ = "household"
    id = Column(String, primary_key=True)            # "adult_1", "kid_1"
    label = Column(String, nullable=False)
    age = Column(Integer, nullable=True)
    sex = Column(String, nullable=True)              # "m"|"f"
    height_cm = Column(Float, nullable=True)
    weight_kg = Column(Float, nullable=True)
    activity = Column(String, default="moderate")    # sedentary|light|moderate|active|very_active
    daily_kcal = Column(Integer, default=2000)       # fallback if no anthropometric data


class Menu(Base):
    __tablename__ = "menus"
    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    approved_at = Column(DateTime, nullable=True)
    week_of = Column(String)                         # ISO date of Monday, "2025-06-09"
    plan_json = Column(JSON)                         # full menu structure
    overlap_score = Column(Integer, default=0)       # 0–100


# SQLite engine + session
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    """Create data dir + all tables. Idempotent."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI dependency — yields a session, always closes."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
