import os
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, HTTPException, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt

# Routers públicos
from app.routers import (
    auth as public_auth,
    cv_confirm,
    cv_upload,
    cv_processing,
    files,
    file_processing,
    integration,
    users,
    webhooks,
)
# Auth Admin (login)
from backend.auth import router as admin_router
# Routers de administración
from app.routers import (
    cv_admin_upload,
    job,
    job_admin,
    admin_users,
    proposal,
)
# Router de matchings
from app.routers.matchings_admin import router as matchings_admin_router
# Router de configuración
from app.routers.admin_config import router as admin_config_router

# --------------------------------------------------
# Cargar variables de entorno
# --------------------------------------------------
load_dotenv()
SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "A5DD9F4F87075741044F604C552C31ED32E5BD246066A765A4D18DE8D8D83F12"
)
ALGORITHM = os.getenv("ALGORITHM", "HS256")

# --------------------------------------------------
# Inicializar FastAPI
# --------------------------------------------------
app = FastAPI(
    proxy_headers=True,
    redirect_slashes=False,
    docs_url="/docs",
    redoc_url="/redoc",
    root_path="/",
)

# --------------------------------------------------
# Configuración CORS
# --------------------------------------------------
origins = os.getenv("FRONTEND_ORIGINS", "").split(",")
if not origins or origins == [""]:
    origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------------------------------
# Middleware de logging
# --------------------------------------------------
@app.middleware("http")
async def log_request(request: Request, call_next):
    print(
        "📥",
        request.method,
        request.url.path,
        "proto=", request.headers.get("x-forwarded-proto"),
        "host=", request.headers.get("host"),
    )
    response = await call_next(request)
    print("📤", response.status_code)
    return response

# --------------------------------------------------
# Routers públicos
# --------------------------------------------------
for router in (
    public_auth.router,
    cv_confirm.router,
    cv_upload.router,
    cv_processing.router,
    files.router,
    file_processing.router,
    integration.router,
    users.router,
    webhooks.router,
):
    app.include_router(router)

# --------------------------------------------------
# Auth Admin (login)
# --------------------------------------------------
app.include_router(admin_router, prefix="/auth", tags=["admin"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")


def get_current_admin(token: str = Depends(oauth2_scheme)):
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Token no proporcionado")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sub = payload.get("sub")
        if not sub:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Token inválido o expirado")
    except JWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Token inválido o expirado")
    return sub

# --------------------------------------------------
# Routers protegidos (admin)
# --------------------------------------------------
app.include_router(
    cv_admin_upload.router,
    tags=["cv_admin"],
    dependencies=[Depends(get_current_admin)],
)

app.include_router(
    job.router,
    prefix="/api/job",
    tags=["job"],
)

app.include_router(
    job_admin.router,
    prefix="/api/job",
    tags=["job_admin"],
)

app.include_router(
    admin_users.router,
    tags=["admin_users"],
)

app.include_router(
    proposal.router,
    tags=["proposals"],
)

# --------------------------------------------------
# Router de matchings
# --------------------------------------------------
app.include_router(
    matchings_admin_router,
    prefix="/api/admin",
    tags=["matchings"],
    dependencies=[Depends(get_current_admin)],
)

# --------------------------------------------------
# Router de configuración
# --------------------------------------------------
app.include_router(
    admin_config_router,
    prefix="/api/admin/config",
    tags=["admin_config"],
    dependencies=[Depends(get_current_admin)],
)

# --------------------------------------------------
# Endpoints adicionales
# --------------------------------------------------
@app.get("/admin/protected", tags=["admin"])
def admin_protected(user=Depends(get_current_admin)):
    return {"message": f"Bienvenido, {user}"}


@app.get("/")
def home():
    return {"ok": True, "message": "API viva y en HTTPS"}


@app.on_event("startup")
def list_routes():
    for route in app.routes:
        print("✅ Ruta cargada:", route.path)
