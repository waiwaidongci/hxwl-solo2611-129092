import logging
from datetime import date, timedelta
from sqlalchemy import and_, or_, select
from src.config.database import Session
from src.constants.enums import Status
from src.domain.analysis import analyze, reissue
from src.domain.spec import SPEC
from src.models.jobs import Delivery, Inbox, Run, Schedule
from src.models.source import Dataset
from src.services.analysis import DISPATCH_TEAM
from src.services.audit import record
from src.services.inbox import publish
from src.utils.exports import write_report
from src.utils.time import now

log = logging.getLogger(__name__)


def materialize_due():
    if not SPEC.get('scheduled'):
        return
    stamp = now()
    with Session.begin() as db:
        schedules = db.scalars(select(Schedule).where(Schedule.enabled.is_(True), Schedule.next_at <= stamp)
                                .order_by(Schedule.next_at).limit(20).with_for_update(skip_locked=True))
        for item in schedules:
            due = item.next_at
            dataset_id = item.dataset_id
            params = dict(item.params)
            if item.repeat_daily:
                elapsed_days = (stamp - due).days
                due += timedelta(days=elapsed_days)
                period_key = SPEC.get('schedule_date_param')
                if period_key:
                    report_day = date.fromisoformat(params[period_key]) + timedelta(days=elapsed_days)
                    params[period_key] = report_day.isoformat()
                    item.params = {**params, period_key: (report_day + timedelta(days=1)).isoformat()}
                    dataset_id = db.scalar(select(Dataset.id).order_by(Dataset.id.desc()).limit(1)) or dataset_id
                item.next_at = due + timedelta(days=1)
            else:
                item.enabled = False
            db.add(Run(dataset_id=dataset_id, schedule_id=item.id, scheduled_at=due,
                       name=item.name, params=params, author=item.author))


def claim():
    stamp = now()
    with Session.begin() as db:
        query = select(Run).where(or_(Run.status == Status.QUEUED,
                                      and_(Run.status == Status.RUNNING, Run.lease_until < stamp)))
        job = db.scalar(query.order_by(Run.id).limit(1).with_for_update(skip_locked=True))
        if job is None:
            return None
        if job.attempt >= 3:
            job.status = Status.FAILED
            job.error = '后台进程多次中断，请发起新的运行'
            job.finished_at = stamp
            record(db, 'worker', 'analysis.failed', job.id, reason=job.error)
            return None
        job.status = Status.RUNNING
        job.attempt += 1
        job.lease_until = stamp + timedelta(minutes=3)
        params = dict(job.params)
        if params.get('reissue'):
            # 首次认领时选定台账批次并固定下来，中断重试沿用同一批，期间导入的台账留给下次补发。
            if params.get('batch_ids') is None:
                params['batch_ids'] = list(db.scalars(select(Dataset.id).order_by(Dataset.id)))
                job.params = params
            return 'reissue', job.id, job.attempt, params
        records = db.get(Dataset, job.dataset_id).rows
        return 'analysis', job.id, job.attempt, records, params


def execute(lease):
    if lease[0] == 'reissue':
        return execute_reissue(lease)
    _, job_id, attempt, rows, params = lease
    result = export = failure = None
    try:
        result = analyze(rows, params)
        export = write_report(job_id, attempt, result)
    except ValueError as error:
        failure = str(error)[:500]
    except Exception:
        log.exception('Analysis %s failed', job_id)
        failure = '计算失败，请检查字段口径后重试'
    with Session.begin() as db:
        job = db.get(Run, job_id, with_for_update=True)
        if job.attempt != attempt or job.status != Status.RUNNING:
            return
        job.lease_until = None
        job.finished_at = now()
        job.status = Status.FAILED if failure else Status.COMPLETE
        job.result = None if failure else result
        job.export_file = export
        job.error = failure
        record(db, 'worker', f'analysis.{job.status}', job.id, attempt=attempt, reason=failure)
        if job.schedule_id:
            plan = db.get(Schedule, job.schedule_id)
            ready = not failure and result.get('ready', True)
            detail = '已保存 CSV 到站内收件箱' if ready else (failure or result['headline'])
            receipt = Delivery(run_id=job.id, recipient=plan.recipient, state='delivered' if ready else 'blocked', detail=detail[:500])
            db.add(receipt)
            record(db, 'worker', f'delivery.{receipt.state}', job.id, recipient=plan.recipient)
            if ready:
                publish(db, job, plan.recipient)


def execute_reissue(lease):
    _, job_id, attempt, params = lease
    result = export = failure = None
    try:
        with Session() as db:
            base = db.get(Run, params.get('base_run_id'))
            base_rows = db.get(Dataset, base.dataset_id).rows if base else None
            if base_rows is None:
                raise ValueError('找不到初版运行或台账，无法补发')
            sources = []
            for dataset_id in params.get('batch_ids') or []:
                dataset = db.get(Dataset, dataset_id)
                if dataset is not None:
                    sources.append(dataset.rows)
        result = reissue(base_rows, sources, params)
        export = write_report(job_id, attempt, result)
    except ValueError as error:
        failure = str(error)[:500]
    except Exception:
        log.exception('Reissue %s failed', job_id)
        failure = '计算失败，请检查字段口径后重试'
    with Session.begin() as db:
        job = db.get(Run, job_id, with_for_update=True)
        if job.attempt != attempt or job.status != Status.RUNNING:
            return
        job.lease_until = None
        job.finished_at = now()
        job.status = Status.FAILED if failure else Status.COMPLETE
        job.result = None if failure else result
        job.export_file = export
        job.error = failure
        record(db, 'worker', f'reissue.{job.status}', job.id, attempt=attempt, reason=failure)
        if failure:
            return
        # 达标后在一个事务里切换收件箱当前版与下载；拦截、冲突或已有更晚版时保留旧版。
        day, cutoff = params.get('report_date'), params.get('as_of_time')
        entry = db.get(Inbox, day, with_for_update=True)
        ready = result.get('ready', False)
        if entry is None:
            state, recipient = 'blocked', DISPATCH_TEAM
            detail = '找不到该营业日的站内收件箱，未切换当前版'
        elif not ready:
            state, recipient, detail = 'blocked', entry.recipient, result['headline']
        elif entry.cutoff < cutoff:
            entry.run_id = job.id
            entry.cutoff = cutoff
            entry.published_at = now()
            state, recipient = 'delivered', entry.recipient
            detail = f'补发版已进入站内收件箱（{day} 截点 {cutoff}）'
        else:
            state, recipient = 'superseded', entry.recipient
            detail = f'收件箱当前版截点为 {entry.cutoff}，本次截点 {cutoff} 的结果保留为历史版本'
        db.add(Delivery(run_id=job.id, recipient=recipient, state=state, detail=detail[:500]))
        record(db, 'worker', f'delivery.{state}', job.id, recipient=recipient)
