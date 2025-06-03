# app/routers/admin_templates.py
from __future__ import annotations

import os, traceback
from datetime import datetime
from typing import Dict, Any, Tuple, List

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from psycopg2.extras import RealDictCursor

from app.database import get_db_connection            #  ← unificamos helper

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY", "")
ALGORITHM  = os.getenv("ALGORITHM", "HS256")
oauth2     = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

# ───────────────────────── helpers ──────────────────────────
def get_current_admin(token: str = Depends(oauth2)) -> str:
    try:
        sub = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("sub")
        if not sub:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido")
        return sub
    except JWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido")

def _now() -> datetime:               # evita múltiples llamadas
    return datetime.utcnow()

def _row_to_dict(row: Tuple[Any, ...]) -> Dict[str, Any]:
    keys = ("id","name","type","subject","body","is_default","created_at","updated_at")
    return dict(zip(keys, row))

# ───────────────────────── router ───────────────────────────
router = APIRouter(
    tags=["admin_templates"],
    dependencies=[Depends(get_current_admin)]
)

# ───────────────────────── End-points ───────────────────────
@router.get("")
def list_templates():
    """
    Devuelve TODAS las plantillas ordenadas por tipo y prioridad
    (la predeterminada de cada tipo primero).
    """
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT id,name,type,subject,body,is_default,created_at,updated_at
              FROM proposal_templates
             ORDER BY type, is_default DESC, updated_at DESC;
        """)
        return {"templates": cur.fetchall()}
    finally:
        cur.close(); conn.close()

# ───────────────────────── helpers validación ───────────────
def _validate_payload(data: dict) -> Tuple[str,str,str,str]:
    name     = data.get("name","").strip()
    tpl_type = data.get("type","").strip()
    subject  = data.get("subject","").strip()
    body     = data.get("body","").strip()

    if not name or tpl_type not in ("automatic","manual") \
       or not subject or not body:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "name, type (automatic|manual), subject y body son obligatorios"
        )
    return name, tpl_type, subject, body

# ───────────────────────── Crear ────────────────────────────
@router.post("", status_code=201)
async def create_template(request: Request):
    name, tpl_type, subject, body = _validate_payload(await request.json())
    now = _now()

    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO proposal_templates
                   (name,type,subject,body,content,is_default,created_at,updated_at)
            VALUES (%s,%s,%s,%s,%s,FALSE,%s,%s)
            RETURNING id,name,type,subject,body,is_default,created_at,updated_at;
        """, (name,tpl_type,subject,body,body,now,now))
        tpl = cur.fetchone(); conn.commit()
        return {"template": _row_to_dict(tpl)}
    except Exception:
        conn.rollback(); traceback.print_exc()
        raise HTTPException(500,"Error al crear plantilla")
    finally:
        cur.close(); conn.close()

# ───────────────────────── Actualizar ───────────────────────
@router.put("/{tpl_id}")
async def update_template(tpl_id:int, request:Request):
    name, tpl_type, subject, body = _validate_payload(await request.json())
    now = _now()

    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE proposal_templates
               SET name=%s,type=%s,subject=%s,body=%s,content=%s,updated_at=%s
             WHERE id=%s
         RETURNING id,name,type,subject,body,is_default,created_at,updated_at;
        """,(name,tpl_type,subject,body,body,now,tpl_id))
        tpl = cur.fetchone()
        if not tpl:
            raise HTTPException(404,"Plantilla no encontrada")
        conn.commit()
        return {"template": _row_to_dict(tpl)}
    except HTTPException: raise
    except Exception:
        conn.rollback(); traceback.print_exc()
        raise HTTPException(500,"Error al actualizar plantilla")
    finally:
        cur.close(); conn.close()

# ───────────────────────── Eliminar ─────────────────────────
@router.delete("/{tpl_id}")
def delete_template(tpl_id:int):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("DELETE FROM proposal_templates WHERE id=%s RETURNING id;",(tpl_id,))
        if not cur.fetchone():
            raise HTTPException(404,"Plantilla no encontrada")
        conn.commit()
        return {"message":"Plantilla eliminada"}
    except HTTPException: raise
    except Exception:
        conn.rollback(); traceback.print_exc()
        raise HTTPException(500,"Error al eliminar plantilla")
    finally:
        cur.close(); conn.close()

# ───────────────────────── Set default ──────────────────────
@router.post("/{template_id}/set-default")
def set_default_template(template_id:int):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        # tipo de la plantilla que queremos setear como default
        cur.execute("SELECT type FROM proposal_templates WHERE id=%s",(template_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404,"Plantilla no encontrada")
        tpl_type = row[0]

        # deshabilitamos las anteriores y marcamos la elegida
        cur.execute("UPDATE proposal_templates SET is_default=FALSE WHERE type=%s;",(tpl_type,))
        cur.execute("UPDATE proposal_templates SET is_default=TRUE  WHERE id=%s;",(template_id,))
        conn.commit()
        return {"message": f"Plantilla {template_id} → default '{tpl_type}'"}
    except HTTPException: raise
    except Exception:
        conn.rollback(); traceback.print_exc()
        raise HTTPException(500,"Error interno")
    finally:
        cur.close(); conn.close()
