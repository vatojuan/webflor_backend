# app/routers/auth.py

from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.security import OAuth2PasswordRequestForm
from jose import jwt
from passlib.context import CryptContext
from datetime import datetime, timedelta
from pydantic import BaseModel
import psycopg2
from dotenv import load_dotenv
import os
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests

# Cargar variables de entorno (asegúrate de que se llame una sola vez en el proyecto)
load_dotenv()

router = APIRouter(
    prefix="/auth",
    tags=["auth"],
)

# ───────────────────────── Configuración JWT y hashing ─────────────────────────
SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "A5DD9F4F87075741044F604C552C31ED32E5BD246066A765A4D18DE8D8D83F12",
)  # Reemplazar por clave segura en producción
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_password(plain_password: str, stored_password: str) -> bool:
    """
    Verifica la contraseña.
    Si la contraseña almacenada es corta (<30), asumimos que está en texto plano.
    De lo contrario, comparamos con bcrypt.
    """
    if not stored_password:
        return False
    if len(stored_password) < 30:
        return plain_password == stored_password
    return pwd_context.verify(plain_password, stored_password)


def create_access_token(data: dict, expires_delta: timedelta = None) -> str:
    """
    Genera un JWT con el payload `data` y tiempo de expiración opcional.
    """
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


# ─────────────────────────── Conexión a la base de datos ──────────────────────────
def get_db_connection():
    """
    Retorna una conexión psycopg2 a la base, usando variables de entorno.
    """
    try:
        conn = psycopg2.connect(
            dbname=os.getenv("DBNAME", "postgres"),
            user=os.getenv("USER", "postgres"),
            password=os.getenv("PASSWORD", "postgres"),
            host=os.getenv("HOST", "localhost"),
            port=int(os.getenv("DB_PORT", "5432")),
            sslmode="require",
        )
        return conn
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en la conexión a BD: {e}")


# ───────────────────────────── Modelo para login-google ───────────────────────────
class GoogleLoginIn(BaseModel):
    id_token: str


# ──────────────────────────────── Endpoint: /auth/login ────────────────────────────
@router.post("/login", summary="Login con email y contraseña")
def login(form_data: OAuth2PasswordRequestForm = Depends()):
    """
    Autentica al usuario con email y contraseña.
    Retorna un JSON con {"access_token": "...", "token_type": "bearer"}.
    El campo `sub` del JWT será el ID numérico del usuario en la BD.
    """
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Buscar usuario por email
        cur.execute(
            'SELECT id, email, password FROM "User" WHERE email = %s',
            (form_data.username,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Usuario no encontrado",
            )

        user_id, email, stored_password = row

        # Verificar contraseña
        if not verify_password(form_data.password, stored_password):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Contraseña incorrecta",
            )

        # Crear token con "sub" = user_id (string)
        access_token = create_access_token(data={"sub": str(user_id)})
        return {"access_token": access_token, "token_type": "bearer"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error en la autenticación: {e}"
        )
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


# ─────────────────────────── Endpoint: /auth/login-google ──────────────────────────
@router.post("/login-google", summary="Login con Google ID token")
def login_google(payload: GoogleLoginIn):
    """
    Recibe un `id_token` de Google, lo valida y devuelve un JWT de FastAPI.
    El campo `sub` del JWT será el ID numérico del usuario en la BD.
    """
    try:
        # 1) Verificar ID token con Google
        idinfo = google_id_token.verify_oauth2_token(
            payload.id_token,
            google_requests.Request(),
            audience=os.getenv("GOOGLE_CLIENT_ID"),
        )
        email = idinfo.get("email")
        if not email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email no válido en el token de Google",
            )

        # 2) Buscar usuario en BD por email
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT id FROM "User" WHERE email = %s', (email,))
        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Usuario no registrado",
            )

        user_id = row[0]

        # 3) Generar JWT de FastAPI con "sub" = user_id
        access_token = create_access_token(data={"sub": str(user_id)})
        return {"access_token": access_token, "token_type": "bearer"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error verificando ID token de Google: {e}"
        )
