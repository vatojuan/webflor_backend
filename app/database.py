import os
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Leer URL desde env var, con fallback
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:Pachamama190@db.apnfioxjddccokgkljvd.supabase.co:5432/postgres"
)

# Asegurar SSL
if "?" not in DATABASE_URL:
    DATABASE_URL += "?sslmode=require"

engine = create_engine(
    DATABASE_URL,
    connect_args={"sslmode": "require"}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
