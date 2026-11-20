from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Runner, RunnerStatusEvent
from app.schemas import RunnerOut

router = APIRouter(prefix="/runners", tags=["runners"])


@router.get("", response_model=list[RunnerOut])
def list_runners(db: Session = Depends(get_db)):
    runners = db.execute(select(Runner).order_by(Runner.health_score.asc())).scalars().all()
    return runners


@router.get("/{runner_id}", response_model=RunnerOut)
def get_runner(runner_id: int, db: Session = Depends(get_db)):
    runner = db.get(Runner, runner_id)
    if not runner:
        raise HTTPException(status_code=404, detail="Runner not found")
    return runner


@router.get("/{runner_id}/history")
def get_runner_history(runner_id: int, db: Session = Depends(get_db)):
    events = (
        db.execute(
            select(RunnerStatusEvent)
            .where(RunnerStatusEvent.runner_id == runner_id)
            .order_by(RunnerStatusEvent.changed_at.desc())
        )
        .scalars()
        .all()
    )
    return [
        {
            "previous_status": e.previous_status,
            "new_status": e.new_status,
            "changed_at": e.changed_at,
            "notified": e.notified,
        }
        for e in events
    ]
