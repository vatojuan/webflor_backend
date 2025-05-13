import os
import traceback
from datetime import datetime
from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
import psycopg2
from dotenv import load_dotenv

load_dotenv()

# — JWT admin —
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

def get_current_admin(token: str = Depends(oauth2_scheme)):
    if not token:
        raise HTTPException(status_code=401, detail="Token no proporcionado")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if not payload.get("sub"):
            raise HTTPException(status_code=401, detail="Token inválido o expirado")
    except JWTError:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")
    return payload["sub"]

# — DB helper —
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
        raise HTTPException(status_code=500, detail=f"Error en la conexión a la BD: {e}")

def get_admin_config():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM admin_config")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return {k: v.lower() == "true" for k, v in rows}
    except Exception as e:
        print("❌ Error leyendo configuración:", e)
        return {}

# — Router protegido —
router = APIRouter(
    prefix="/api/job",
    tags=["job_admin"],
    dependencies=[Depends(get_current_admin)],
)

@router.get("/admin_offers")
async def get_admin_offers():
    """
    Todas las ofertas de la tabla "Job", filtradas según configuración admin_config.
    """
    config = get_admin_config()
    show_exp_admin = config.get("show_expired_admin_offers", False)
    show_exp_employer = config.get("show_expired_employer_offers", False)

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        base_query = """
            SELECT j.id,
                   j.title,
                   j.description,
                   j.requirements,
                   j."expirationDate",
                   j."userId",
                   j.source,
                   j.label,
                   u.role
            FROM public."Job" j
            LEFT JOIN public."User" u ON j."userId" = u.id
        """
        conditions = []
        if not show_exp_admin:
            conditions.append("""
                NOT (u.role = 'admin' AND j."expirationDate" IS NOT NULL AND j."expirationDate" < CURRENT_DATE)
            """)
        if not show_exp_employer:
            conditions.append("""
                NOT (u.role != 'admin' AND j."expirationDate" IS NOT NULL AND j."expirationDate" < CURRENT_DATE)
            """)
        if conditions:
            base_query += " WHERE " + " AND ".join(conditions)
        base_query += " ORDER BY j.id DESC;"

        cur.execute(base_query)
        cols = [desc[0] for desc in cur.description]
        offers = []
        for row in cur.fetchall():
            offer = dict(zip(cols, row))
            if offer["expirationDate"]:
                offer["expirationDate"] = offer["expirationDate"].isoformat()
            offers.append(offer)

        return {"offers": offers}

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error al obtener ofertas: {e}")
    finally:
        cur.close()
        conn.close()

# — Actualizar oferta —
@router.put("/update-admin")
async def update_admin_offer(request: Request):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        data = await request.json()
        job_id      = int(data.get("id") or 0)
        title       = data.get("title")
        description = data.get("description")
        requirements= data.get("requirements", "")
        expiration  = data.get("expirationDate")
        user_id     = int(data.get("userId") or 0)

        if not (job_id and title and description and user_id):
            raise HTTPException(status_code=400, detail="Faltan campos obligatorios")

        exp_date = None
        if expiration:
            try:
                exp_date = datetime.fromisoformat(expiration)
            except ValueError:
                raise HTTPException(status_code=400, detail="Formato de fecha inválido")

        # Recalcular embedding
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        text_to_embed = f"{title} {description} {requirements}"
        resp = client.embeddings.create(input=text_to_embed, model="text-embedding-ada-002")
        embedding = resp.data[0].embedding

        cur.execute("""
            UPDATE public."Job"
            SET title            = %s,
                description      = %s,
                requirements     = %s,
                "expirationDate" = %s,
                "userId"         = %s,
                embedding        = %s
            WHERE id = %s
            RETURNING id, title, description, requirements, "expirationDate", "userId", source, label;
        """, (title, description, requirements, exp_date, user_id, embedding, job_id))

        updated = cur.fetchone()
        if not updated:
            raise HTTPException(status_code=404, detail="Oferta no encontrada")

        conn.commit()
        offer = dict(zip(
            ["id", "title", "description", "requirements", "expirationDate", "userId", "source", "label"],
            updated
        ))
        if offer["expirationDate"]:
            offer["expirationDate"] = offer["expirationDate"].isoformat()
        return offer

    except HTTPException:
        raise
    except Exception:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Error interno al actualizar")
    finally:
        cur.close()
        conn.close()

# — Eliminar oferta —
@router.delete("/delete-admin")
async def delete_admin_offer(request: Request):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        data = await request.json()
        job_id = int(data.get("jobId") or 0)
        if not job_id:
            raise HTTPException(status_code=400, detail="jobId es requerido")

        cur.execute('DELETE FROM public."Job" WHERE id = %s RETURNING id;', (job_id,))
        deleted = cur.fetchone()
        if not deleted:
            raise HTTPException(status_code=404, detail="Oferta no encontrada")

        conn.commit()
        return {"message": "Oferta eliminada", "jobId": job_id}

    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error al eliminar oferta: {e}")
    finally:
        cur.close()
        conn.close()
