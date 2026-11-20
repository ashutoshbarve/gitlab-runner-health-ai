from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.schemas import RecurringJobOut
from app.trend_analyzer import top_recurring_jobs

router = APIRouter(prefix="/trends", tags=["trends"])


@router.get("/recurring-jobs", response_model=list[RecurringJobOut])
def recurring_jobs(db: Session = Depends(get_db), min_count: int = 2, limit: int = 20):
    settings = get_settings()
    return top_recurring_jobs(db, window_days=settings.trend_window_days, min_count=min_count, limit=limit)
