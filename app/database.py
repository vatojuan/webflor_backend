# app/database.py

import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# --------------------------------------------------
# Carga de variables de entorno
# --------------------------------------------------
load_dotenv()

# --------------------------------------------------
# Construcción de la URL de conexión
# --------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    # Si no está, intentamos armarla a partir de vars individuales
    user = os.getenv("DB_USER", "")
    password = os.getenv("DB_PASSWORD", "")
    host = os.getenv("DB_HOST", "localhost")
    name = os.getenv("DB_NAME")
    if not name:
        raise RuntimeError("DATABASE_URL o DB_NAME no están definidas")
    DATABASE_URL = f"postgresql://{user}:{password}@{host}/{name}"

# --------------------------------------------------
# Engine con SSL obligatorio y pool_pre_ping
# --------------------------------------------------
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"sslmode": "require"},
    # echo=True  # Descomenta para debugging de SQL
)

# --------------------------------------------------
# SessionLocal y Base declarativa
# --------------------------------------------------
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()

# --------------------------------------------------
# Dependencia para FastAPI: obtener y cerrar sesión
# --------------------------------------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
