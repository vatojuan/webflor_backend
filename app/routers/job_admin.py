# app/routers/job_admin.py
import os
import traceback
from datetime import datetime, timezone
from typing import Dict, Optional

import psycopg2
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt

load_dotenv()

# ────────────────────────────
# JWT helpers
# ────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")


def get_current_admin(token: str = Depends(oauth2_scheme)) -> str:
    if not token:
        raise HTTPException(401, "Token no proporcionado")
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("sub") or ""
    except JWTError:
        raise HTTPException(401, "Token inválido o expirado")


# ────────────────────────────
# DB helpers
# ────────────────────────────
def get_db_connection():
    try:
        return psycopg2.connect(
            dbname=os.getenv("DBNAME"),
            user=os.getenv("USER"),
            password=os.getenv("PASSWORD"),
            host=os.getenv("HOST"),
            port=int(os.getenv("DB_PORT", 5432)),
            sslmode="require",
        )
    except Exception as e:
        raise HTTPException(500, f"Error conexión BD: {e}")


def get_admin_config() -> Dict[str, bool]:
    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM admin_config;")
        return {k: v.lower() == "true" for k, v in cur.fetchall()}
    except Exception:
        traceback.print_exc()
        return {}
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


def get_admin_id(email: str) -> Optional[int]:
    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT id FROM "User" WHERE email=%s LIMIT 1;', (email,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


# ────────────────────────────
# Router
# ────────────────────────────
router = APIRouter(
    prefix="/api/job",
    tags=["job_admin"],
    dependencies=[Depends(get_current_admin)],
)

# ────────────────────────────
# GET /admin_offers
# ────────────────────────────
@router.get("/admin_offers")
def get_admin_offers(admin_sub: str = Depends(get_current_admin)):
    cfg = get_admin_config()
    show_admin_exp = cfg.get("show_expired_admin_offers", False)
    show_employer_exp = cfg.get("show_expired_employer_offers", False)

    admin_id = get_admin_id(admin_sub)

    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, title, description, requirements,
                   "expirationDate", "userId", source, label,
                   "contactEmail", "contactPhone"
              FROM public."Job"
          ORDER BY id DESC;
            """
        )
        cols = [d[0] for d in cur.description]

        now = datetime.now(timezone.utc)
        offers = []
        for row in cur.fetchall():
            offer = dict(zip(cols, row))
            exp = offer["expirationDate"]

            expired = False
            if exp:
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                expired = exp < now
                offer["expirationDate"] = exp.isoformat()

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
        raise HTTPException(500, f"Error al obtener ofertas: {e}")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


# ────────────────────────────
# PUT /update-admin
# ────────────────────────────
@router.put("/update-admin")
async def update_admin_offer(request: Request):
    body = await request.json()

    job_id = int(body.get("id") or 0)
    title = body.get("title")
    description = body.get("description")
    requirements = body.get("requirements", "")
    expiration = body.get("expirationDate")
    user_id = int(body.get("userId") or 0)
    contact_email = body.get("contactEmail")  # opcional
    contact_phone = body.get("contactPhone")  # opcional
    source = body.get("source", "admin")
    label = body.get("label", "automatic")

    if not (job_id and title and description and user_id):
        raise HTTPException(400, "Faltan campos obligatorios")

    # Si la oferta es del admin, debe tener un email de contacto
    if source == "admin" and not contact_email:
        raise HTTPException(400, "Las ofertas del administrador requieren contactEmail")

    exp_date = None
    if expiration:
        try:
            exp_date = datetime.fromisoformat(expiration)
        except ValueError:
            raise HTTPException(400, "Formato de fecha inválido")

    # Embedding OpenAI
    from openai import OpenAI

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    text_to_embed = f"{title} {description} {requirements}"
    embedding = client.embeddings.create(
        input=text_to_embed, model="text-embedding-ada-002"
    ).data[0].embedding

    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
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
                   "contactEmail"   = %s,
                   "contactPhone"   = %s,
                   embedding        = %s
             WHERE id = %s
         RETURNING id, title, description, requirements,
                   "expirationDate", "userId", source, label,
                   "contactEmail", "contactPhone";
            """,
            (
                title,
                description,
                requirements,
                exp_date,
                user_id,
                source,
                label,
                contact_email,
                contact_phone,
                embedding,
                job_id,
            ),
        )
        upd = cur.fetchone()
        if not upd:
            raise HTTPException(404, "Oferta no encontrada")
        conn.commit()

        keys = [
            "id",
            "title",
            "description",
            "requirements",
            "expirationDate",
            "userId",
            "source",
            "label",
            "contactEmail",
            "contactPhone",
        ]
        offer = dict(zip(keys, upd))
        if offer["expirationDate"]:
            offer["expirationDate"] = offer["expirationDate"].isoformat()
        return offer
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Error interno al actualizar: {e}")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


# ────────────────────────────
# DELETE /delete-admin
# ────────────────────────────
@router.delete("/delete-admin")
async def delete_admin_offer(request: Request):
    """
    Elimina una oferta **y** sus propuestas asociadas.
    """
    body = await request.json()
    job_id = int(body.get("jobId") or 0)
    if not job_id:
        raise HTTPException(400, "jobId es requerido")

    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # 1) Cancelar propuestas activas
        cur.execute(
            """
            UPDATE proposals
               SET status='cancelled', cancelled_at=NOW()
             WHERE job_id=%s AND status IN ('waiting','pending');
            """,
            (job_id,),
        )

        # 2) Eliminar propuestas canceladas
        cur.execute(
            "DELETE FROM proposals WHERE job_id=%s AND status='cancelled';", (job_id,)
        )

        # 3) Borrar la oferta
        cur.execute('DELETE FROM public."Job" WHERE id=%s RETURNING id;', (job_id,))
        if not cur.fetchone():
            raise HTTPException(404, "Oferta no encontrada")

        conn.commit()
        return {"message": "Oferta y propuestas eliminadas", "jobId": job_id}
    except HTTPException:
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        traceback.print_exc()
        raise HTTPException(500, f"Error al eliminar oferta: {e}")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
