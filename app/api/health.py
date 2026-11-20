from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import Runner
from app.schemas import HealthSummaryOut

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", response_model=HealthSummaryOut)
def health_summary(db: Session = Depends(get_db)):
    settings = get_settings()
    runners = db.execute(select(Runner)).scalars().all()

    total = len(runners)
    online = sum(1 for r in runners if r.status == "online")
    offline = sum(1 for r in runners if r.status == "offline")
    stale = sum(1 for r in runners if r.status == "stale")
    avg_score = (sum(r.health_score for r in runners) / total) if total else 100.0
    below_threshold = [r for r in runners if r.health_score < settings.health_score_alert_threshold]

    return HealthSummaryOut(
        total_runners=total,
        online=online,
        offline=offline,
        stale=stale,
        average_score=round(avg_score, 1),
        runners_below_threshold=below_threshold,
    )
