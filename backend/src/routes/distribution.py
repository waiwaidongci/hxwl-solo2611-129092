from datetime import UTC, date, timedelta
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from src.config.database import connection
from src.constants.enums import Role, Status
from src.controllers.auth import allow, current
from src.domain.spec import SPEC
from src.models.identity import User
from src.models.jobs import Delivery, Inbox, Run, Schedule
from src.models.source import Dataset
from src.schemas.requests import ReissueRequest, ScheduleRequest
from src.services.audit import record
from src.services.inbox import versions
from src.services.validation import normalize_params
from src.utils import views
from src.utils.time import now
from src.utils.wakeup import wake

router = APIRouter()


@router.get('/schedules')
def schedules(db: Session = Depends(connection), actor: User = Depends(current)):
    return [views.schedule(item) for item in db.scalars(select(Schedule).order_by(Schedule.id.desc()).limit(100))]


@router.post('/schedules', status_code=201)
def add(body: ScheduleRequest, db: Session = Depends(connection), actor: User = Depends(allow(Role.ADMIN))):
    if not db.get(Dataset, body.dataset_id):
        raise HTTPException(404, '找不到指定的数据批次')
    if body.recipient not in SPEC['recipients']:
        raise ValueError('请选择当前业务的收件团队')
    if body.first_at.utcoffset() is None:
        raise ValueError('首次运行时间需要明确时区')
    first = body.first_at.astimezone(UTC).replace(tzinfo=None)
    stamp = now()
    if first < stamp - timedelta(seconds=30) or first > stamp + timedelta(days=366):
        raise ValueError('请选择从现在起一年内的首次运行时间')
    item = Schedule(dataset_id=body.dataset_id, params=normalize_params(body.params), name=body.name,
                    recipient=body.recipient, next_at=first, repeat_daily=body.repeat_daily, author=actor.username)
    db.add(item)
    db.flush()
    record(db, actor.username, 'schedule.created', item.id, recipient=item.recipient)
    db.commit()
    wake()
    return views.schedule(item)


@router.post('/schedules/{schedule_id}/disable')
def disable(schedule_id: int, db: Session = Depends(connection), actor: User = Depends(allow(Role.ADMIN))):
    item = db.get(Schedule, schedule_id, with_for_update=True)
    if not item:
        raise HTTPException(404, '找不到该分发计划')
    if item.enabled:
        item.enabled = False
        record(db, actor.username, 'schedule.disabled', item.id)
        db.commit()
    return views.schedule(item)


@router.get('/deliveries')
def outbox(db: Session = Depends(connection), actor: User = Depends(current)):
    return [views.delivery(item) for item in db.scalars(select(Delivery).order_by(Delivery.id.desc()).limit(100))]


@router.get('/inbox')
def inbox(db: Session = Depends(connection), actor: User = Depends(current)):
    entries = db.scalars(select(Inbox).order_by(Inbox.report_date.desc()).limit(60)).all()
    return [views.inbox(entry, versions(db, entry)) for entry in entries]


@router.post('/reissues', status_code=202)
def reissue(body: ReissueRequest, db: Session = Depends(connection),
            actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    try:
        day = date.fromisoformat(body.report_date).isoformat()
    except ValueError:
        day = None
    if day != body.report_date:
        raise ValueError('业务日期须使用 YYYY-MM-DD 格式并为有效日期')
    entry = db.get(Inbox, day)
    if entry is None:
        raise HTTPException(409, '该营业日还没有已发出的日报，无法补发')
    base = db.get(Run, entry.base_run_id)
    if base is None:
        raise HTTPException(409, '找不到该营业日的初版运行，无法补发')
    base_params = base.params or {}
    base_cutoff = base_params.get('as_of_time')
    if base_cutoff and body.cutoff <= base_cutoff:
        raise ValueError(f'补发截点须晚于初版截点 {base_cutoff}')
    pending = db.scalars(select(Run).where(Run.status.in_([Status.QUEUED, Status.RUNNING]),
                                           Run.params['report_date'].as_string() == day,
                                           Run.params['as_of_time'].as_string() == body.cutoff)).all()
    for item in pending:
        if (item.params or {}).get('reissue'):
            return views.run(item, detail=True)
    params = {'report_date': day, 'as_of_time': body.cutoff,
              'min_completeness_pct': base_params.get('min_completeness_pct', 85),
              'reissue': True, 'base_run_id': entry.base_run_id}
    item = Run(name=f'{day} 日报补发（截点 {body.cutoff}）', dataset_id=base.dataset_id,
               params=params, author=actor.username)
    db.add(item)
    db.flush()
    record(db, actor.username, 'reissue.queued', item.id, report_date=day, cutoff=body.cutoff)
    db.commit()
    wake()
    return views.run(item, detail=True)
