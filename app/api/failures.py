from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.job_collector import rediagnose_failure
from app.config import get_settings
from app.database import get_db
from app.models import JobFailure
from app.notifier import TeamsNotifier
from app.schemas import JobFailureOut

router = APIRouter(prefix="/failures", tags=["failures"])


@router.get("", response_model=list[JobFailureOut])
def list_failures(db: Session = Depends(get_db), limit: int = 50):
    failures = (
        db.execute(select(JobFailure).order_by(JobFailure.recorded_at.desc()).limit(limit)).scalars().all()
    )
    return failures


async def _notify_rediagnosis(db: Session, settings, failure: JobFailure, diagnosis) -> None:
    notifier = TeamsNotifier(settings)
    job_payload = {
        "id": failure.id,
        "name": failure.job_name,
        "project_id": failure.project_id,
        "stage": failure.stage,
        "ref": failure.ref,
        "failure_reason": failure.failure_reason,
        "dependency_analysis": failure.dependency_analysis,
        "web_url": failure.web_url,
    }
    await notifier.send_job_failure_alert(db, job_payload, failure.runner_id, diagnosis)


@router.post("/{failure_id}/rediagnose", response_model=JobFailureOut)
async def rediagnose_one(failure_id: int, notify: bool = False, db: Session = Depends(get_db)):
    """Re-run AI diagnosis for one already-recorded failure - e.g. after a
    bug fix in the AI reasoning pipeline. Re-fetches the job's log from
    GitLab fresh. Set ?notify=true to also post a fresh Teams card."""
    failure = db.get(JobFailure, failure_id)
    if not failure:
        raise HTTPException(status_code=404, detail="Failure not found")

    settings = get_settings()
    diagnosis = await rediagnose_failure(db, settings, failure)

    if notify:
        await _notify_rediagnosis(db, settings, failure, diagnosis)

    return failure


@router.post("/rediagnose-errors")
async def rediagnose_errors(notify: bool = False, limit: int = 50, db: Session = Depends(get_db)):
    """Bulk re-run diagnosis for every failure whose last AI attempt errored
    out (ai_error non-empty) - the direct fix-forward for failures caught by
    a since-fixed bug in the AI reasoning pipeline. Does not touch failures
    that already have a successful diagnosis."""
    settings = get_settings()
    failures = db.execute(select(JobFailure).where(JobFailure.ai_error != "").limit(limit)).scalars().all()

    results = []
    for failure in failures:
        diagnosis = await rediagnose_failure(db, settings, failure)
        if notify:
            await _notify_rediagnosis(db, settings, failure, diagnosis)
        results.append(
            {
                "id": failure.id,
                "job_name": failure.job_name,
                "succeeded": diagnosis.succeeded,
                "category": diagnosis.category,
                "confidence": diagnosis.confidence,
            }
        )

    return {"processed": len(results), "results": results}
