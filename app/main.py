# main.py

import os
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import logging

# --- Configuración del Logger ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# --- Carga de Routers ---
# Importamos cada módulo de router directamente.
from app.routers import (
    auth,
    cv_confirm,
    cv_upload,
    files,
    integration,
    users,
    webhooks,
    job,
    proposal,
    apply,
    match,
    admin_templates,
    # Asegúrate de que todos tus archivos de router estén importados aquí.
    # Por ejemplo: admin_users, admin_config, etc.
)

# ─────────────────── Configuración de la App ───────────────────
load_dotenv()
app = FastAPI(
    title="FAP Mendoza API",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ─────────────────── Middleware de CORS ───────────────────
# Asegúrate de que tu variable de entorno FRONTEND_ORIGINS esté bien configurada.
# Ejemplo: FRONTEND_ORIGINS="http://localhost:3000,https://fapmendoza.online"
origins_env = os.getenv("FRONTEND_ORIGINS", "http://localhost:3000,https://fapmendoza.online")
origins = [o.strip() for o in origins_env.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────── Middleware de Logging ───────────────────
@app.middleware("http")
async def log_request(request: Request, call_next):
    logger.info(f"📥 {request.method} {request.url.path}")
    response = await call_next(request)
    logger.info(f"📤 {response.status_code}")
    return response

# ─────────────────── Inclusión de Routers ───────────────────
# Ahora incluimos el objeto 'router' de cada módulo importado.
# Esto soluciona el error 'AttributeError: module has no attribute routes'.
app.include_router(auth.router)
app.include_router(cv_confirm.router)
app.include_router(cv_upload.router)
app.include_router(files.router)
app.include_router(integration.router)
app.include_router(users.router)
app.include_router(webhooks.router)
app.include_router(job.router)
app.include_router(apply.router)
app.include_router(proposal.router)
app.include_router(match.router)
app.include_router(admin_templates.router)
# ... (incluye aquí el .router de cada módulo de admin que falte)

# ─────────────────── Endpoints de Raíz ───────────────────
@app.get("/")
def home():
    return {"ok": True, "message": "API de FAP Mendoza funcionando."}

@app.on_event("startup")
def list_routes():
    url_list = [{"path": route.path, "name": route.name} for route in app.routes]
    logger.info("✅ Rutas cargadas exitosamente:")
    for route in url_list:
        logger.info(f"  - Path: {route['path']}")

