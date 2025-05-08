# app/routers/admin_config.py

from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.security import OAuth2PasswordBearer
import os
import psycopg2
from dotenv import load_dotenv
from jose import JWTError, jwt

load_dotenv()

router = APIRouter(prefix="/api/admin/config", tags=["admin_config"])

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM  = os.getenv("ALGORITHM", "HS256")


def get_current_admin(token: str = Depends(oauth2_scheme)):
    if not token:
        raise HTTPException(status_code=401, detail="Token no proporcionado")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sub = payload.get("sub")
        if not sub:
            raise HTTPException(status_code=401, detail="Token inválido o expirado")
        return sub
    except JWTError:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")


def get_db_connection():
    try:
        return psycopg2.connect(
            dbname=os.getenv("DBNAME"),
            user=os.getenv("USER"),
            password=os.getenv("PASSWORD"),
            host=os.getenv("HOST"),
            port=int(os.getenv("DB_PORT", 5432)),
            sslmode="require"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB connection error: {e}")


# GET con y sin slash final
@router.get("", dependencies=[Depends(get_current_admin)])
@router.get("/", dependencies=[Depends(get_current_admin)])
def get_config():
    conn = get_db_connection()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT key, value FROM admin_config;")
        rows = cur.fetchall()
        return { k: v for k, v in rows }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching config: {e}")
    finally:
        cur.close()
        conn.close()


@router.post("", dependencies=[Depends(get_current_admin)])
@router.post("/", dependencies=[Depends(get_current_admin)])
async def update_config(request: Request):
    payload = await request.json()
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        raise HTTPException(status_code=400, detail="settings debe ser un objeto clave->valor")
    conn = get_db_connection()
    cur  = conn.cursor()
    try:
        for key, value in settings.items():
            cur.execute(
                """
                INSERT INTO admin_config (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
                """,
                (key, str(value))
            )
        conn.commit()
        return {"message": "Configuración actualizada"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Error updating config: {e}")
    finally:
        cur.close()
        conn.close()
