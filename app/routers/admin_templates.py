# app/routers/admin_templates.py
"""
Módulo para la gestión de todas las plantillas de correo electrónico del sistema.

Permite a los administradores crear, leer, actualizar y eliminar plantillas
para cada tipo de comunicación transaccional.
"""
from __future__ import annotations

import os
import logging
from datetime import datetime

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from psycopg2.extras import RealDictCursor

from app.database import get_db_connection

load_dotenv()
logger = logging.getLogger(__name__)

# ────────────────────── Configuración y Constantes ──────────────────────
SECRET_KEY = os.getenv("SECRET_KEY", "")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

# Se expanden los tipos de plantillas para cubrir todas las comunicaciones
ALLOWED_TYPES = {
    "empleado",                 # Notificación de match a candidato
    "automatic",                # Propuesta automática a empleador
    "manual",                   # Propuesta manual a empleador
    "application_confirmation", # Confirmación de postulación a candidato
    "cancellation_warning",     # Aviso de 5 mins para cancelar
}

# ───────────────────────── Helpers de Autenticación ────────────────────────
def get_current_admin(token: str = Depends(oauth2)) -> str:
    """
    Decodifica el token JWT para obtener el 'subject' (ID del admin)
    y lo usa como dependencia para proteger los endpoints.
    """
    try:
        sub = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("sub")
        if not sub:
            raise ValueError("El token no contiene el 'subject'.")
        return sub
    except (JWTError, ValueError) as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Token inválido o requerido: {e}")

# ───────────────────────── Router de FastAPI ───────────────────────────
# El router se define DESPUÉS de sus dependencias, como get_current_admin.
router = APIRouter(
    prefix="/api/admin/templates",
    tags=["admin_templates"],
    dependencies=[Depends(get_current_admin)],
)

# ───────────────────────── Lógica de la API ───────────────────────────

@router.get("", summary="Listar todas las plantillas")
def list_templates():
    """Devuelve una lista de todas las plantillas, ordenadas por tipo y prioridad."""
    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT id, name, type, subject, body, is_default, created_at, updated_at
              FROM proposal_templates
             ORDER BY type, is_default DESC, updated_at DESC;
            """
        )
        return {"templates": cur.fetchall()}
    finally:
        if cur: cur.close()
        if conn: conn.close()

@router.post("", status_code=status.HTTP_201_CREATED, summary="Crear una nueva plantilla")
async def create_template(request: Request):
    """Crea una nueva plantilla y la devuelve."""
    data = await request.json()
    name = data.get("name", "").strip()
    tpl_type = data.get("type", "").strip()
    subject = data.get("subject", "").strip()
    body = data.get("body", "").strip()
    is_default = bool(data.get("is_default", False))

    if not all([name, tpl_type, subject, body]) or tpl_type not in ALLOWED_TYPES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"Campos 'name', 'type', 'subject', 'body' son obligatorios. El tipo debe ser uno de: {ALLOWED_TYPES}")

    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # Si se marca como default, desmarcar las otras del mismo tipo
        if is_default:
            cur.execute("UPDATE proposal_templates SET is_default = FALSE WHERE type = %s;", (tpl_type,))

        cur.execute(
            """
            INSERT INTO proposal_templates (name, type, subject, body, is_default, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
            RETURNING *;
            """,
            (name, tpl_type, subject, body, is_default),
        )
        new_template = cur.fetchone()
        conn.commit()
        return {"template": new_template}
    except Exception as e:
        if conn: conn.rollback()
        logger.exception("Error al crear la plantilla.")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error interno al crear la plantilla.")
    finally:
        if cur: cur.close()
        if conn: conn.close()
        
# Los endpoints PUT, DELETE y set-default se mantienen con una lógica similar,
# asegurando que operan sobre la tabla `proposal_templates`.
# (Aquí iría la implementación completa de los otros endpoints)

@router.put("/{tpl_id}", summary="Actualizar una plantilla existente")
async def update_template(tpl_id: int, request: Request):
    # Esta es una implementación de ejemplo, puedes completarla según tus necesidades.
    data = await request.json()
    name = data.get("name", "").strip()
    tpl_type = data.get("type", "").strip()
    subject = data.get("subject", "").strip()
    body = data.get("body", "").strip()
    is_default = bool(data.get("is_default", False))

    if not all([name, tpl_type, subject, body]) or tpl_type not in ALLOWED_TYPES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Todos los campos son obligatorios.")

    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if is_default:
            cur.execute("UPDATE proposal_templates SET is_default = FALSE WHERE type = %s AND id != %s;", (tpl_type, tpl_id))
        
        cur.execute(
            """
            UPDATE proposal_templates
               SET name = %s, type = %s, subject = %s, body = %s, is_default = %s, updated_at = NOW()
             WHERE id = %s
         RETURNING *;
            """, (name, tpl_type, subject, body, is_default, tpl_id)
        )
        updated_template = cur.fetchone()
        if not updated_template:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Plantilla no encontrada.")
        conn.commit()
        return {"template": updated_template}
    except Exception as e:
        if conn: conn.rollback()
        logger.exception(f"Error al actualizar la plantilla {tpl_id}.")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error interno al actualizar.")
    finally:
        if cur: cur.close()
        if conn: conn.close()


@router.delete("/{tpl_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Eliminar una plantilla")
def delete_template(tpl_id: int):
    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM proposal_templates WHERE id = %s RETURNING id;", (tpl_id,))
        if not cur.fetchone():
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Plantilla no encontrada para eliminar.")
        conn.commit()
    except Exception as e:
        if conn: conn.rollback()
        logger.exception(f"Error al eliminar la plantilla {tpl_id}.")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error interno al eliminar.")
    finally:
        if cur: cur.close()
        if conn: conn.close()


@router.post("/{tpl_id}/set-default", summary="Marcar una plantilla como predeterminada")
def set_default_template(tpl_id: int):
    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT type FROM proposal_templates WHERE id = %s;", (tpl_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Plantilla no encontrada.")
        
        tpl_type = row[0]
        
        conn.autocommit = False # Iniciar transacción
        cur.execute("UPDATE proposal_templates SET is_default = FALSE WHERE type = %s;", (tpl_type,))
        cur.execute("UPDATE proposal_templates SET is_default = TRUE WHERE id = %s;", (tpl_id,))
        conn.commit()
        
        return {"message": f"Plantilla {tpl_id} ahora es la predeterminada para el tipo '{tpl_type}'."}
    except Exception as e:
        if conn: conn.rollback()
        logger.exception(f"Error al marcar como predeterminada la plantilla {tpl_id}.")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al marcar como predeterminada.")
    finally:
        if cur: cur.close()
        if conn: conn.close()
