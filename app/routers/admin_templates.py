# app/routers/admin_templates.py
"""
Gestión de plantillas de e-mail para propuestas y matchings.

Tipos admitidos:
    • automatic  – para e-mails de match automáticos (candidato → oferta)
    • manual     – para propuestas enviadas manualmente desde el panel
    • empleado   – para avisos dirigidos al EMPLEADO (nuevas ofertas para él)

Los endpoints permiten:
    • GET    ''                         → listado
    • POST   ''                         → crear
    • PUT    '/{tpl_id}'                → actualizar
    • DELETE '/{tpl_id}'                → eliminar
    • POST   '/{tpl_id}/set-default'    → marcar como predeterminada
"""

from __future__ import annotations

import os
import traceback
from datetime import datetime
from typing import Any, Dict, Tuple

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from psycopg2.extras import RealDictCursor

from app.database import get_db_connection

load_dotenv()

# ────────────────────── Config/constantes ──────────────────────
SECRET_KEY = os.getenv("SECRET_KEY", "")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

ALLOWED_TYPES = {"automatic", "manual", "empleado"}

# ───────────────────────── Helpers Auth ────────────────────────
def get_current_admin(token: str = Depends(oauth2)) -> str:
    try:
        sub = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("sub")
        if not sub:
            raise ValueError
        return sub
    except (JWTError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido o requerido")

# ───────────────────────── Helpers varios ──────────────────────
def _now() -> datetime:
    return datetime.utcnow()

def _row_to_dict(row: Tuple[Any, ...]) -> Dict[str, Any]:
    keys = (
        "id",
        "name",
        "type",
        "subject",
        "body",
        "is_default",
        "created_at",
        "updated_at",
    )
    return dict(zip(keys, row))

def _validate_payload(data: dict) -> Tuple[str, str, str, str, bool]:
    """
    Valida y normaliza el JSON de entrada.
    Devuelve: name, type, subject, body, is_default
    """
    name = data.get("name", "").strip()
    tpl_type = data.get("type", "").strip()
    subject = data.get("subject", "").strip()
    body = data.get("body", "").strip()
    is_default = bool(data.get("is_default", False))

    if not name or tpl_type not in ALLOWED_TYPES or not subject or not body:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"name, type {ALLOWED_TYPES}, subject y body son obligatorios",
        )
    return name, tpl_type, subject, body, is_default

# ───────────────────────── Router ──────────────────────────────
router = APIRouter(
    tags=["admin_templates"],
    dependencies=[Depends(get_current_admin)],
)

# ───────────────────────── ENDPOINTS ───────────────────────────
@router.get("")
def list_templates():
    """Lista completa de plantillas ordenadas por tipo y prioridad."""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(
            """
            SELECT id, name, type, subject, body,
                   is_default, created_at, updated_at
              FROM proposal_templates
             ORDER BY type, is_default DESC, updated_at DESC;
            """
        )
        return {"templates": cur.fetchall()}
    finally:
        cur.close()
        conn.close()

# ───────────────────────── CREAR ───────────────────────────────
@router.post("", status_code=201)
async def create_template(request: Request):
    name, tpl_type, subject, body, is_default = _validate_payload(await request.json())
    now = _now()

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        if is_default:
            # si se marca como default, desmarcamos otros del mismo tipo
            cur.execute(
                "UPDATE proposal_templates SET is_default = FALSE WHERE type = %s;",
                (tpl_type,),
            )
        cur.execute(
            """
            INSERT INTO proposal_templates
                   (name, type, subject, body, content,
                    is_default, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, name, type, subject, body,
                      is_default, created_at, updated_at;
            """,
            (name, tpl_type, subject, body, body, is_default, now, now),
        )
        tpl = cur.fetchone()
        conn.commit()
        return {"template": _row_to_dict(tpl)}
    except Exception:
        conn.rollback()
        traceback.print_exc()
        raise HTTPException(500, "Error al crear plantilla")
    finally:
        cur.close()
        conn.close()

# ───────────────────────── ACTUALIZAR ──────────────────────────
@router.put("/{tpl_id}")
async def update_template(tpl_id: int, request: Request):
    name, tpl_type, subject, body, is_default = _validate_payload(await request.json())
    now = _now()

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        if is_default:
            cur.execute(
                "UPDATE proposal_templates SET is_default = FALSE WHERE type = %s;",
                (tpl_type,),
            )
        cur.execute(
            """
            UPDATE proposal_templates
               SET name = %s,
                   type = %s,
                   subject = %s,
                   body = %s,
                   content = %s,
                   is_default = %s,
                   updated_at = %s
             WHERE id = %s
         RETURNING id, name, type, subject, body,
                   is_default, created_at, updated_at;
            """,
            (name, tpl_type, subject, body, body, is_default, now, tpl_id),
        )
        tpl = cur.fetchone()
        if not tpl:
            raise HTTPException(404, "Plantilla no encontrada")
        conn.commit()
        return {"template": _row_to_dict(tpl)}
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        traceback.print_exc()
        raise HTTPException(500, "Error al actualizar plantilla")
    finally:
        cur.close()
        conn.close()

# ───────────────────────── ELIMINAR ────────────────────────────
@router.delete("/{tpl_id}")
def delete_template(tpl_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "DELETE FROM proposal_templates WHERE id = %s RETURNING id;", (tpl_id,)
        )
        if not cur.fetchone():
            raise HTTPException(404, "Plantilla no encontrada")
        conn.commit()
        return {"message": "Plantilla eliminada"}
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        traceback.print_exc()
        raise HTTPException(500, "Error al eliminar plantilla")
    finally:
        cur.close()
        conn.close()

# ───────────────────────── SET DEFAULT ─────────────────────────
@router.post("/{template_id}/set-default")
def set_default_template(template_id: int):
    """Marca la plantilla indicada como predeterminada dentro de su tipo."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT type FROM proposal_templates WHERE id = %s;", (template_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Plantilla no encontrada")
        tpl_type = row[0]

        cur.execute(
            "UPDATE proposal_templates SET is_default = FALSE WHERE type = %s;",
            (tpl_type,),
        )
        cur.execute(
            "UPDATE proposal_templates SET is_default = TRUE  WHERE id = %s;",
            (template_id,),
        )
        conn.commit()
        return {
            "message": f"Plantilla {template_id} ahora es default para tipo '{tpl_type}'"
        }
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        traceback.print_exc()
        raise HTTPException(500, "Error interno")
    finally:
        cur.close()
        conn.close()
