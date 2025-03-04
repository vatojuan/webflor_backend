from fastapi import APIRouter, HTTPException
from app.clients.main_api_client import get_candidate_data

router = APIRouter(
    prefix="/integration",
    tags=["integration"],
)

@router.get("/candidate/{candidate_id}")
def integration_get_candidate(candidate_id: int):
    """
    Endpoint para obtener datos de un candidato desde la API principal.
    """
    try:
        candidate_data = get_candidate_data(candidate_id)
        return candidate_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
