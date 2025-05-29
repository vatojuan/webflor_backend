# app/database.py

import os
import psycopg2
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

load_dotenv()

# ───────────── SQLAlchemy ORM setup ─────────────
# URL de conexión para SQLAlchemy (puedes ajustarla en .env o usar este valor por defecto)
SQLALCHEMY_DATABASE_URL = os.getenv(
    "SQLALCHEMY_DATABASE_URL",
    "postgresql://postgres:Juanchi190@localhost/webflor_db"
)
engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ──────────── Conexión cruda con psycopg2 ────────────
# Para los routers que usan SQL crudo (job.py, match.py, etc.)
def get_db_connection():
    try:
        conn = psycopg2.connect(
            dbname   = os.getenv("DBNAME",   os.getenv("POSTGRES_DB")),
            user     = os.getenv("USER",     os.getenv("POSTGRES_USER")),
            password = os.getenv("PASSWORD", os.getenv("POSTGRES_PASSWORD")),
            host     = os.getenv("HOST",     "localhost"),
            port     = int(os.getenv("DB_PORT", 5432)),
            sslmode  = os.getenv("DB_SSLMODE", "require"),
        )
        return conn
    except Exception as e:
        # Si algo falla aquí, todos los routers que dependan de psycopg2 lanzarán excepción
        raise Exception(f"Error en la conexión a la base de datos: {e}")
