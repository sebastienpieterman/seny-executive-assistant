import logging
from typing import Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from web.auth.jwt_utils import require_auth, require_screen_agent
from web.core.database import (
    get_lcd_layer1, set_lcd_layer1,
    get_recent_lcd_observations, set_lcd_synthesis, get_db,
    append_lcd_observation
)

logger = logging.getLogger(__name__)
router = APIRouter()


class Layer1Update(BaseModel):
    content: str


class IngestPayload(BaseModel):
    source: str
    project: Optional[str] = None
    content: str


@router.get("/")
async def get_layer1(user_id: str = Depends(require_auth)):
    row = get_lcd_layer1(int(user_id))
    if row is None:
        return {"content": "", "layer2_synthesis": "", "layer2_synthesized_at": None, "updated_at": None}
    return row


@router.put("/")
async def update_layer1(body: Layer1Update, user_id: str = Depends(require_auth)):
    set_lcd_layer1(int(user_id), body.content)
    row = get_lcd_layer1(int(user_id))
    if row is None:
        return {"content": body.content, "layer2_synthesis": "", "layer2_synthesized_at": None, "updated_at": None}
    return row


@router.get("/observations")
async def list_observations(limit: int = 20, user_id: str = Depends(require_auth)):
    return get_recent_lcd_observations(int(user_id), min(limit, 50))


@router.get("/synthesis")
async def get_synthesis(user_id: str = Depends(require_auth)):
    row = get_lcd_layer1(int(user_id))
    if row is None:
        return {"layer2_synthesis": "", "layer2_synthesized_at": None}
    return {"layer2_synthesis": row.get("layer2_synthesis", ""), "layer2_synthesized_at": row.get("layer2_synthesized_at")}


@router.post("/synthesis/refresh")
async def refresh_synthesis(user_id: str = Depends(require_auth)):
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE lcd_layer1 SET layer2_synthesis='', layer2_synthesized_at=NULL WHERE user_id=%s",
                (int(user_id),)
            )
    except Exception as e:
        logger.error("lcd synthesis refresh error: %s", repr(e))
    return {"status": "synthesis cache cleared — will re-synthesize on next chat"}


@router.post("/ingest")
async def ingest_observation(body: IngestPayload, user_id: str = Depends(require_screen_agent)):
    """
    Accept an LCD observation from any external signal source (screen agent, claude-code sessions, etc.).

    Auth: X-Screen-Agent-Key header (same key used by screen agent).
    Fail-open: always returns 200 with status "ok", even if DB write fails.
    """
    source = f"{body.source}:{body.project}" if body.project else body.source
    obs_id = None
    try:
        obs_id = append_lcd_observation(int(user_id), source=source, content=body.content)
    except Exception as e:
        logger.warning("lcd ingest append error: %s", repr(e))
    return {"observation_id": obs_id, "status": "ok"}
