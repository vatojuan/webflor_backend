# app/database.py
"""
Centraliza la configuración de BD para:
  • psycopg2 (raw_connection)
  • SQLAlchemy (ORM y engine)

Todas las variables se leen de .env:
  DBNAME, POSTGRES_DB
  USER, POSTGRES_USER
  PASSWORD, POSTGRES_PASSWORD
  HOST
  DB_PORT
  DB_SSLMODE
  SQLALCHEMY_DATABASE_URL (opcional)
"""
import os
from dotenv import load_dotenv
import psycopg2
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

load_dotenv()

# ─────────────────── Parámetros de conexión ───────────────────
DB_NAME     = os.getenv("DBNAME",        os.getenv("POSTGRES_DB", "postgres"))
DB_USER     = os.getenv("USER",          os.getenv("POSTGRES_USER", "postgres"))
DB_PASS     = os.getenv("PASSWORD",      os.getenv("POSTGRES_PASSWORD", ""))
DB_HOST     = os.getenv("HOST",          os.getenv("DB_HOST", "localhost"))
DB_PORT     = os.getenv("DB_PORT",       "5432")
DB_SSLMODE  = os.getenv("DB_SSLMODE",    "require")

# ─────────────────── SQLAlchemy ORM setup ───────────────────
# Puede sobreescribirse completamente con SQLALCHEMY_DATABASE_URL en .env
SQLALCHEMY_DATABASE_URL = os.getenv(
    "SQLALCHEMY_DATABASE_URL",
    f"postgresql+psycopg2://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)
Base = declarative_base()

# ─────────────────── Conexión cruda con psycopg2 ───────────────────
def get_db_connection():
    """
    Retorna una conexión psycopg2 usando las mismas variables de entorno.
    Util para queries crudos (pgvector, embebidos, etc.).
    """
    try:
        return psycopg2.connect(
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASS,
            host=DB_HOST,
            port=int(DB_PORT),
            sslmode=DB_SSLMODE,
        )
    except Exception as e:
        raise Exception(f"Error conectando a la base de datos: {e}")
