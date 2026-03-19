from fastapi import APIRouter, Depends
from web.auth.jwt_utils import require_auth
from web.core.database import get_last_audit_run, get_db

router = APIRouter()

@router.get("/latest-audit")
async def get_latest_audit(uid: int = Depends(require_auth)):
    audit = get_last_audit_run(uid)

    # Count pending research_proposal actions (inline — no dedicated helper exists)
    proposal_count = 0
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM pending_actions WHERE user_id = %s AND status = 'pending' AND action_type = 'research_proposal'",
                (uid,)
            )
            row = cursor.fetchone()
            proposal_count = row[0] if row else 0
    except Exception:
        pass

    if audit is None:
        return {
            "has_data": False,
            "fidelity_score": None,
            "run_at": None,
            "negative_unabsorbed_count": None,
            "suppression_gap_count": None,
            "proposal_count": proposal_count,
        }

    return {
        "has_data": True,
        "fidelity_score": audit.get("fidelity_score"),
        "run_at": str(audit.get("run_at")) if audit.get("run_at") else None,
        "negative_unabsorbed_count": audit.get("negative_unabsorbed_count"),
        "suppression_gap_count": audit.get("suppression_gap_count"),
        "proposal_count": proposal_count,
    }
