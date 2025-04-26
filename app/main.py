from fastapi import FastAPI, Depends, HTTPException, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt

from app.routers import (
    auth as public_auth,
    cv_confirm, cv_upload, cv_processing,
    files, file_processing, integration,
    users, webhooks
)
from backend.auth import router as admin_router
from app.routers import cv_admin_upload, job, job_admin, admin_users, proposal

SECRET_KEY = "A5DD9F4F87075741044F604C552C31ED32E5BD246066A765A4D18DE8D8D83F12"
ALGORITHM  = "HS256"

app = FastAPI(
    proxy_headers=True,
    redirect_slashes=False,
    docs_url="/docs", redoc_url="/redoc", root_path="/"
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://fapmendoza.online",
        "https://webfloradmin-vatojuans-projects.vercel.app"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def log_req(request: Request, call_next):
    print("📥", request.method, request.url.path,
          "proto=", request.headers.get("x-forwarded-proto"),
          "host=", request.headers.get("host"))
    resp = await call_next(request)
    print("📤", resp.status_code)
    return resp

# Routers públicos
for r in (
    public_auth.router, cv_confirm.router, cv_upload.router,
    cv_processing.router, files.router, file_processing.router,
    integration.router, users.router, webhooks.router
):
    app.include_router(r)

# Auth admin
app.include_router(admin_router, prefix="/auth", tags=["admin"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")
def get_current_admin(token: str = Depends(oauth2_scheme)):
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token no proporcionado")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if not payload.get("sub"):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido o expirado")
    except JWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido o expirado")
    return payload["sub"]

# Routers admin
app.include_router(cv_admin_upload.router, tags=["cv_admin"], dependencies=[Depends(get_current_admin)])
app.include_router(job.router,       prefix="/api/job", tags=["job"])
app.include_router(job_admin.router, prefix="/api/job", tags=["job_admin"])
app.include_router(admin_users.router)
app.include_router(proposal.router)   # /api/proposals, GET/POST/PATCH sin 404

@app.get("/admin/protected", tags=["admin"])
def admin_protected(user=Depends(get_current_admin)):
    return {"message": f"Bienvenido, {user}"}

@app.get("/")
def home():
    return {"ok": True, "message": "API viva y en HTTPS"}

@app.on_event("startup")
def list_routes():
    for r in app.routes:
        print("✅ Ruta cargada:", r.path)
