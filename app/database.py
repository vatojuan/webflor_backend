from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Configura la URL de conexión a PostgreSQL (ajusta usuario, contraseña, host y nombre de BD)
SQLALCHEMY_DATABASE_URL = "postgresql://postgres:Juanchi190@localhost/webflor_db"

engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()
