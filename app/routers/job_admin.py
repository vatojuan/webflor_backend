import os
import traceback
from datetime import datetime, timezone
from typing import Dict, Optional

import psycopg2
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError

load_dotenv()

# ───────────────────  JWT  ───────────────────
SECRET_KEY    = os.getenv("SECRET_KEY", "")
ALGORITHM     = os.getenv("ALGORITHM", "HS256")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

from fastapi import Header

def get_current_admin(authorization: str = Header(...)) -> str:
    try:
        token = authorization.replace("Bearer ", "")
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sub = payload.get("sub")
        if not sub:
            raise JWTError()
        return sub
    except JWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token admin inválido o expirado")

# ───────────────────  DB  ───────────────────
def get_db_connection():
    try:
        return psycopg2.connect(
            dbname   = os.getenv("DBNAME"),
            user     = os.getenv("USER"),
            password = os.getenv("PASSWORD"),
            host     = os.getenv("HOST"),
            port     = int(os.getenv("DB_PORT", 5432)),
            sslmode  = "require",
        )
    except Exception as e:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Error conexión BD: {e}")

def get_admin_config() -> Dict[str, bool]:
    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("SELECT key, value FROM admin_config;")
        return {k: v.lower() == "true" for k, v in cur.fetchall()}
    except Exception:
        traceback.print_exc()
        return {}
    finally:
        if cur:  cur.close()
        if conn: conn.close()

def get_admin_id(email: str) -> Optional[int]:
    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute('SELECT id FROM "User" WHERE email=%s LIMIT 1;', (email,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        if cur:  cur.close()
        if conn: conn.close()


# ───────────────────  Router  ───────────────────
router = APIRouter(
    prefix="/api/job",
    tags=["job_admin"],
)


# ════════════════════════════════════════
# GET /api/job/admin_offers
# (REQUIERE ADMIN TOKEN)
# ════════════════════════════════════════
@router.get(
    "/admin_offers",
    dependencies=[Depends(get_current_admin)],
    status_code=status.HTTP_200_OK,
)
def get_admin_offers(admin_sub: str = Depends(get_current_admin)):
    """
    Devuelve todas las ofertas, filtrando expiradas según configuración.
    Requiere token admin.
    """
    cfg               = get_admin_config()
    show_admin_exp    = cfg.get("show_expired_admin_offers", False)
    show_employer_exp = cfg.get("show_expired_employer_offers", False)

    # ✅ Asegurarse de que admin_sub puede ser ID o email
    if admin_sub.isdigit():
        admin_id = int(admin_sub)
    else:
        admin_id = get_admin_id(admin_sub)

    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute(
            """
            SELECT
              id,
              title,
              description,
              requirements,
              "expirationDate",
              "userId",
              source,
              label,
              contact_email   AS "contactEmail",
              contact_phone   AS "contactPhone"
            FROM public."Job"
            ORDER BY id DESC;
            """
        )
        cols   = [d[0] for d in cur.description]
        now    = datetime.now(timezone.utc)
        offers = []
        for row in cur.fetchall():
            offer = dict(zip(cols, row))
            exp = offer["expirationDate"]
            if exp:
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                offer["expirationDate"] = exp.isoformat()
                expired = exp < now
            else:
                expired = False

            is_admin_offer = admin_id is not None and offer["userId"] == admin_id
            if expired:
                if is_admin_offer and not show_admin_exp:
                    continue
                if not is_admin_offer and not show_employer_exp:
                    continue

            offers.append(offer)

        return {"offers": offers}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Error al obtener ofertas: {e}")
    finally:
        if cur:  cur.close()
        if conn: conn.close()


# ════════════════════════════════════════
# PUT /api/job/update-admin
# (REQUIERE ADMIN TOKEN)
# ════════════════════════════════════════
@router.put(
    "/update-admin",
    dependencies=[Depends(get_current_admin)],
    status_code=status.HTTP_200_OK,
)
async def update_admin_offer(request: Request):
    body          = await request.json()
    job_id        = int(body.get("id") or 0)
    title         = body.get("title")
    description   = body.get("description")
    requirements  = body.get("requirements", "")
    expiration    = body.get("expirationDate")
    user_id       = int(body.get("userId") or 0)
    contact_email = body.get("contactEmail") or body.get("contact_email")
    contact_phone = body.get("contactPhone") or body.get("contact_phone")
    source        = body.get("source", "admin")
    label         = body.get("label", "automatic")

    if not (job_id and title and description and user_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Faltan campos obligatorios")
    if source == "admin" and not contact_email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Las ofertas del administrador requieren contactEmail")

    exp_date = None
    if expiration:
        try:
            exp_date = datetime.fromisoformat(expiration)
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Formato de fecha inválido")

    # Generar embedding con OpenAI (opcional)
    from openai import OpenAI
    client    = OpenAI(api_key=os.getenv("OPENAI_API_KEY",""))
    embedding = client.embeddings.create(
        input=f"{title} {description} {requirements}",
        model="text-embedding-ada-002"
    ).data[0].embedding

    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute(
            """
            UPDATE public."Job"
               SET title            = %s,
                   description      = %s,
                   requirements     = %s,
                   "expirationDate" = %s,
                   "userId"         = %s,
                   source           = %s,
                   label            = %s,
                   contact_email    = %s,
                   contact_phone    = %s,
                   embedding        = %s
             WHERE id = %s
         RETURNING
                   id,
                   title,
                   description,
                   requirements,
                   "expirationDate",
                   "userId",
                   source,
                   label,
                   contact_email   AS "contactEmail",
                   contact_phone   AS "contactPhone";
            """,
            (
                title, description, requirements, exp_date,
                user_id, source, label,
                contact_email, contact_phone,
                embedding, job_id,
            ),
        )
        upd = cur.fetchone()
        if not upd:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Oferta no encontrada")
        conn.commit()

        keys  = [
            "id","title","description","requirements",
            "expirationDate","userId","source","label",
            "contactEmail","contactPhone"
        ]
        offer = dict(zip(keys, upd))
        if offer["expirationDate"]:
            offer["expirationDate"] = offer["expirationDate"].isoformat()
        return offer

    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Error interno al actualizar: {e}")
    finally:
        if cur:  cur.close()
        if conn: conn.close()


# ════════════════════════════════════════
# DELETE /api/job/delete-admin
# (REQUIERE ADMIN TOKEN)
# ════════════════════════════════════════
@router.delete(
    "/delete-admin",
    dependencies=[Depends(get_current_admin)],
    status_code=status.HTTP_200_OK,
)
async def delete_admin_offer(request: Request):
    body   = await request.json()
    job_id = int(body.get("jobId") or 0)
    if not job_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "jobId es requerido")

    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()

        # 1) cancelar propuestas activas
        cur.execute(
            """
            UPDATE proposals
               SET status='cancelled',
                   cancelled_at = NOW()
             WHERE job_id = %s
               AND status IN ('waiting','pending');
            """,
            (job_id,),
        )
        # 2) eliminar todas las propuestas asociadas
        cur.execute("DELETE FROM proposals WHERE job_id = %s;", (job_id,))
        # 3) borrar la oferta
        cur.execute(
            'DELETE FROM public."Job" WHERE id = %s RETURNING id;',
            (job_id,),
        )
        if not cur.fetchone():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Oferta no encontrada")

        conn.commit()
        return {"message": "Oferta y propuestas eliminadas", "jobId": job_id}

    except HTTPException:
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        traceback.print_exc()
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Error al eliminar oferta: {e}")
    finally:
        if cur:  cur.close()
        if conn: conn.close()
