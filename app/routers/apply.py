# app/routers/apply.py

from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import OAuth2PasswordBearer
from datetime import datetime
from jose import JWTError
import traceback
import logging

from app.database import get_db_connection
from app.routers.auth import create_access_token

logger = logging.getLogger(__name__)

# Montamos bajo /api/job para que coincida con el frontend
router = APIRouter(prefix="/api/job", tags=["apply"])

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

@router.get("/apply/{token}", summary="Confirmar postulación")
def apply_with_token(token: str, auth: str = Depends(oauth2_scheme)):
    """
    1. Verifica que el token exista en matches con status 'sent' y no aplicado.
    2. Inserta propuesta pending si no existe.
    3. Marca matching como applied.
    4. Incrementa applicants y actualiza last_application.
    5. Devuelve { success: True, token: JWT }.
    """
    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()

        # 1) Matching vigente
        logger.debug(f"Buscando matching para token: {token}")
        cur.execute(
            """
            SELECT id, job_id, user_id
              FROM matches
             WHERE trim(apply_token) = trim(%s)
               AND status = 'sent'
               AND applied_at IS NULL
            """,
            (token,),
        )
        row = cur.fetchone()
        if not row:
            logger.warning(f"Token inválido o expirado: {token}")
            raise HTTPException(404, "Token inválido o expirado")

        match_id, job_id, user_id = row

        # 2) Crear propuesta si no existe
        cur.execute(
            """
            INSERT INTO proposals (job_id, applicant_id, label, status, created_at)
            SELECT %s, %s,
                   COALESCE((SELECT label FROM "Job" WHERE id = %s), 'manual'),
                   'pending', NOW()
            WHERE NOT EXISTS (
              SELECT 1 FROM proposals
               WHERE job_id = %s AND applicant_id = %s
            );
            """,
            (job_id, user_id, job_id, job_id, user_id),
        )

        # 3) Marcar matching como aplicado
        cur.execute(
            """
            UPDATE matches
               SET applied_at = NOW(),
                   status     = 'applied'
             WHERE id = %s;
            """,
            (match_id,),
        )

        # 4) Incrementar contador de postulantes
        cur.execute(
            """
            UPDATE "Job"
               SET applicants        = COALESCE(applicants, 0) + 1,
                   last_application = %s
             WHERE id = %s;
            """,
            (datetime.utcnow(), job_id),
        )

        conn.commit()

        # 5) Generar y devolver JWT
        access_token = create_access_token({"sub": str(user_id)})
        return {"success": True, "token": access_token}

    except HTTPException:
        # ya tiene código y mensaje apropiado
        raise
    except JWTError:
        # en caso de fallo de creación del token
        logger.exception("Error generando JWT de usuario")
        raise HTTPException(500, "Error interno de autenticación")
    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception("Error interno confirmando postulación")
        raise HTTPException(500, f"Error interno: {e}")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
