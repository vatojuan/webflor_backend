# app/routers/admin_templates.py

import os
import traceback
from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.security import OAuth2PasswordBearer
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from jose import jwt, JWTError

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM  = os.getenv("ALGORITHM", "HS256")
oauth2     = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

def get_current_admin(token: str = Depends(oauth2)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sub = payload.get("sub")
        if not sub:
            raise HTTPException(status_code=401, detail="Token inválido")
        return sub
    except JWTError:
        raise HTTPException(status_code=401, detail="Token inválido")

def get_db():
    return psycopg2.connect(
        dbname=os.getenv("DBNAME"),
        user=os.getenv("USER"),
        password=os.getenv("PASSWORD"),
        host=os.getenv("HOST"),
        port=int(os.getenv("DB_PORT", 5432)),
        sslmode="require"
    )

router = APIRouter(
    prefix="/api/admin/templates",
    tags=["admin_templates"],
    dependencies=[Depends(get_current_admin)]
)

@router.get("", summary="Listar todas las plantillas")
def list_templates():
    conn = get_db()
    cur  = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT
              id,
              name,
              type,
              subject,
              body,
              is_default,
              created_at,
              updated_at
            FROM proposal_templates
            ORDER BY updated_at DESC;
        """)
        templates = cur.fetchall()
        return {"templates": templates}
    except Exception:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Error al listar plantillas")
    finally:
        cur.close()
        conn.close()

@router.post("", status_code=201, summary="Crear nueva plantilla")
async def create_template(request: Request):
    data    = await request.json()
    name    = data.get("name", "").strip()
    tpl_type= data.get("type", "").strip()
    subject = data.get("subject", "").strip()
    body    = data.get("body", "").strip()

    if not name or tpl_type not in ("automatic", "manual") or not subject or not body:
        raise HTTPException(
            status_code=400,
            detail="name, type (automatic|manual), subject y body son obligatorios"
        )

    now = datetime.utcnow()
    conn = get_db()
    cur  = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO proposal_templates
              (name, type, subject, body, is_default, created_at, updated_at)
            VALUES (%s, %s, %s, %s, FALSE, %s, %s)
            RETURNING id, name, type, subject, body, is_default, created_at, updated_at;
        """, (name, tpl_type, subject, body, now, now))
        tpl = cur.fetchone()
        conn.commit()
        return {
            "template": {
                "id": tpl[0],
                "name": tpl[1],
                "type": tpl[2],
                "subject": tpl[3],
                "body": tpl[4],
                "is_default": tpl[5],
                "created_at": tpl[6].isoformat(),
                "updated_at": tpl[7].isoformat()
            }
        }
    except Exception:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Error al crear plantilla")
    finally:
        cur.close()
        conn.close()

@router.put("/{tpl_id}", summary="Actualizar plantilla")
async def update_template(tpl_id: int, request: Request):
    data    = await request.json()
    name    = data.get("name", "").strip()
    tpl_type= data.get("type", "").strip()
    subject = data.get("subject", "").strip()
    body    = data.get("body", "").strip()

    if not name or tpl_type not in ("automatic", "manual") or not subject or not body:
        raise HTTPException(
            status_code=400,
            detail="name, type, subject y body son obligatorios"
        )

    now = datetime.utcnow()
    conn = get_db()
    cur  = conn.cursor()
    try:
        cur.execute("""
            UPDATE proposal_templates
            SET name = %s,
                type = %s,
                subject = %s,
                body = %s,
                updated_at = %s
            WHERE id = %s
            RETURNING id, name, type, subject, body, is_default, created_at, updated_at;
        """, (name, tpl_type, subject, body, now, tpl_id))
        tpl = cur.fetchone()
        if not tpl:
            raise HTTPException(status_code=404, detail="Plantilla no encontrada")
        conn.commit()
        return {
            "template": {
                "id": tpl[0],
                "name": tpl[1],
                "type": tpl[2],
                "subject": tpl[3],
                "body": tpl[4],
                "is_default": tpl[5],
                "created_at": tpl[6].isoformat(),
                "updated_at": tpl[7].isoformat()
            }
        }
    except HTTPException:
        raise
    except Exception:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Error al actualizar plantilla")
    finally:
        cur.close()
        conn.close()

@router.delete("/{tpl_id}", summary="Eliminar plantilla")
def delete_template(tpl_id: int):
    conn = get_db()
    cur  = conn.cursor()
    try:
        cur.execute("DELETE FROM proposal_templates WHERE id = %s RETURNING id;", (tpl_id,))
        deleted = cur.fetchone()
        if not deleted:
            raise HTTPException(status_code=404, detail="Plantilla no encontrada")
        conn.commit()
        return {"message": "Plantilla eliminada"}
    except HTTPException:
        raise
    except Exception:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Error al eliminar plantilla")
    finally:
        cur.close()
        conn.close()
