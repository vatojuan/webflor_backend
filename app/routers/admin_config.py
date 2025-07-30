import os
from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
import psycopg2
from dotenv import load_dotenv

load_dotenv()

# — JWT admin —
SECRET_KEY    = os.getenv("SECRET_KEY")
ALGORITHM     = os.getenv("ALGORITHM", "HS256")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

def get_current_admin(token: str = Depends(oauth2_scheme)):
    if not token:
        raise HTTPException(401, "Token no proporcionado")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if not payload.get("sub"):
            raise HTTPException(401, "Token inválido o expirado")
    except JWTError:
        raise HTTPException(401, "Token inválido o expirado")
    return payload["sub"]

def get_db_connection():
    try:
        return psycopg2.connect(
            dbname   = os.getenv("DBNAME"),
            user     = os.getenv("USER"),
            password = os.getenv("PASSWORD"),
            host     = os.getenv("HOST"),
            port     = int(os.getenv("DB_PORT", 5432)),
            sslmode  = "require"
        )
    except Exception as e:
        raise HTTPException(500, f"Error en la conexión a la BD: {e}")

# --- CORRECCIÓN ---
# Se quita "/api" del prefijo. main.py se encargará de añadirlo.
router = APIRouter(
    prefix="/admin/config",
    tags=["admin_config"],
    dependencies=[Depends(get_current_admin)]
)

@router.get("/")
def get_config():
    conn = get_db_connection()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT key, value FROM admin_config;")
        rows = cur.fetchall()
        return {
            key: (value.lower() == "true")
            for key, value in rows
        }
    except Exception as e:
        raise HTTPException(500, f"Error fetching config: {e}")
    finally:
        cur.close()
        conn.close()

@router.post("/")
def update_config(payload: dict):
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        raise HTTPException(400, "El campo 'settings' debe ser un objeto clave→valor")
    conn = get_db_connection()
    cur  = conn.cursor()
    try:
        for key, val in settings.items():
            str_val = "true" if bool(val) else "false"
            cur.execute(
                """
                INSERT INTO admin_config(key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                (key, str_val)
            )
        conn.commit()
        return {"message": "Configuración actualizada"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Error updating config: {e}")
    finally:
        cur.close()
        conn.close()
