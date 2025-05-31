import os
from dotenv import load_dotenv
import psycopg2
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

load_dotenv()

# ─────────────────── Variables de entorno para BD ───────────────────
DB_NAME     = os.getenv("DBNAME",        os.getenv("POSTGRES_DB", "postgres"))
DB_USER     = os.getenv("USER",          os.getenv("POSTGRES_USER", "postgres"))
DB_PASS     = os.getenv("PASSWORD",      os.getenv("POSTGRES_PASSWORD", ""))
DB_HOST     = os.getenv("HOST",          os.getenv("DB_HOST", "localhost"))
DB_PORT     = os.getenv("DB_PORT",       os.getenv("DB_PORT", "5432"))
DB_SSLMODE  = os.getenv("DB_SSLMODE",    os.getenv("DB_SSLMODE", "require"))

# ─────────────────── URL completa (Heroku/Render/etc) ───────────────────
# Render, Heroku, Vercel, etc., suelen exponer algo como DATABASE_URL
DATABASE_URL = os.getenv("DATABASE_URL")

# ─────────────────── SQLAlchemy ORM setup ───────────────────
# Si existe DATABASE_URL, úsala directamente.
if DATABASE_URL:
    SQLALCHEMY_DATABASE_URL = DATABASE_URL
else:
    # En local, construye con psycopg2+host/puerto/credenciales
    SQLALCHEMY_DATABASE_URL = (
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
    Retorna una conexión psycopg2. Si detecta la variable DATABASE_URL,
    la usa directamente (útil en Render/Heroku). Si no existe, cae al
    método “clásico” con DB_HOST, DB_USER, etc.
    """
    try:
        if DATABASE_URL:
            # Si DATABASE_URL está presente, úsala (Render, Heroku, etc.)
            # En muchos entornos ya incluye sslmode=require, pero puedes
            # forzarlo aquí si fuera necesario:
            return psycopg2.connect(DATABASE_URL, sslmode="require")
        else:
            # En local
            return psycopg2.connect(
                dbname=DB_NAME,
                user=DB_USER,
                password=DB_PASS,
                host=DB_HOST,
                port=int(DB_PORT),
                sslmode=DB_SSLMODE,
            )
    except Exception as e:
        # Si falla, arroja excepción para que el endpoint sepa que no hay BD
        raise Exception(f"Error conectando a la base de datos: {e}")
