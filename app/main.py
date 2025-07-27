# main.py

import os
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
import logging

# --- Configuración del Logger ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# --- Carga de Routers ---
from app.routers import (
    auth as public_auth,
    cv_confirm,
    cv_upload,
    files,
    integration,
    users,
    webhooks,
    job,
    proposal,
    apply as apply_router,
    match as matchings_admin_router,
    admin_templates as admin_templates_router,
    # ... (Si faltan otros routers, asegúrate de importarlos)
)
# (Es posible que algunos routers como cv_processing, file_processing, etc.,
# necesiten ser importados aquí si no están ya incluidos en otros módulos)

# ─────────────────── Configuración de la App ───────────────────
load_dotenv()
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM  = os.getenv("ALGORITHM", "HS256")

app = FastAPI(
    title="FAP Mendoza API",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ─────────────────── Middleware de CORS ───────────────────
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
# Nota: La convención es definir el prefijo dentro del APIRouter en cada archivo,
# y no al incluirlo aquí, para evitar duplicados.

# Routers Públicos
app.include_router(public_auth.router)
app.include_router(cv_confirm.router)
app.include_router(cv_upload.router)
app.include_router(files.router)
app.include_router(integration.router)
app.include_router(users.router)
app.include_router(webhooks.router)
app.include_router(job.router) # Contiene endpoints públicos de jobs
app.include_router(apply_router)

# Routers de Administración (Protegidos)
# La protección se define ahora dentro de cada router para mayor claridad.
app.include_router(proposal.router)
app.include_router(matchings_admin_router)
app.include_router(admin_templates_router)
# app.include_router(admin_users.router) # Descomentar si tienes este router
# ... (incluir otros routers de admin aquí)


# ─────────────────── Endpoints de Raíz ───────────────────
@app.get("/")
def home():
    return {"ok": True, "message": "API de FAP Mendoza funcionando."}

@app.on_event("startup")
def list_routes():
    url_list = [{"path": route.path, "name": route.name} for route in app.routes]
    logger.info("✅ Rutas cargadas:")
    for route in url_list:
        logger.info(f"  - Path: {route['path']}")
