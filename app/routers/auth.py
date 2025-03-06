# app/routers/auth.py
from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.security import OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from datetime import datetime, timedelta
import psycopg2
from dotenv import load_dotenv
import os

# Cargar variables de entorno
load_dotenv()

router = APIRouter(
    prefix="/auth",
    tags=["auth"],
)

# Configuración para JWT y hashing
SECRET_KEY = os.getenv("SECRET_KEY", "A5DD9F4F87075741044F604C552C31ED32E5BD246066A765A4D18DE8D8D83F12")  # Reemplaza por una clave segura
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain_password: str, stored_password: str) -> bool:
    """
    Si la contraseña almacenada es corta, asumimos que está en texto plano.
    De lo contrario, se verifica con bcrypt.
    """
    if stored_password is None:
        return False
    if len(stored_password) < 30:
        return plain_password == stored_password
    return pwd_context.verify(plain_password, stored_password)

def create_access_token(data: dict, expires_delta: timedelta = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

# Función para obtener la conexión a la base de datos usando el Pooler de Supabase
def get_db_connection():
    try:
        conn = psycopg2.connect(
            dbname=os.getenv("DBNAME", "postgres"),
            user=os.getenv("USER", "postgres.apnfioxjddccokgkljvd"),
            password=os.getenv("PASSWORD", "Pachamama190"),
            host=os.getenv("HOST", "aws-0-sa-east-1.pooler.supabase.com"),
            port=5432,  # Fijo en 5432
            sslmode="require"
        )
        return conn
    except Exception as e:
        raise Exception(f"Error en la conexión a la base de datos: {e}")

@router.post("/login")
def login(form_data: OAuth2PasswordRequestForm = Depends()):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Consulta: usar el nombre de la tabla según la base de datos actual.
        cur.execute('SELECT id, email, password FROM "User" WHERE email = %s', (form_data.username,))
        user = cur.fetchone()
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuario no encontrado")
        user_id, email, stored_password = user
        if not verify_password(form_data.password, stored_password):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Contraseña incorrecta")
        access_token = create_access_token(data={"sub": email})
        return {"access_token": access_token, "token_type": "bearer"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en la autenticación: {str(e)}")
    finally:
        cur.close()
        conn.close()
