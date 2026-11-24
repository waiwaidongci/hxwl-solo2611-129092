from sqlalchemy import select
from src.constants.enums import Status
from src.models.jobs import Inbox, Run
from src.utils.time import iso, now


def publish(db, run, recipient):
    """已投递的运行进入站内收件箱；同营业日只保留截点更晚的版本。"""
    params = run.params or {}
    day, cutoff = params.get('report_date'), params.get('as_of_time')
    if not day or not cutoff:
        return
    entry = db.get(Inbox, day, with_for_update=True)
    if entry is None:
        db.add(Inbox(report_date=day, base_run_id=run.id, run_id=run.id, cutoff=cutoff,
                     recipient=recipient, published_at=now()))
    elif entry.cutoff < cutoff:
        entry.run_id = run.id
        entry.cutoff = cutoff
        entry.published_at = now()


def versions(db, entry):
    """一个营业日的初版与全部补发结果，当前版由收件箱指针标记。"""
    runs = db.scalars(select(Run).where(Run.status == Status.COMPLETE,
                                        Run.params['report_date'].as_string() == entry.report_date)
                      .order_by(Run.id)).all()
    items = []
    for run in runs:
        params = run.params or {}
        if run.id != entry.base_run_id and not params.get('reissue'):
            continue
        items.append({'id': run.id, 'name': run.name, 'cutoff': params.get('as_of_time'),
                      'kind': 'reissue' if params.get('reissue') else 'initial',
                      'ready': bool((run.result or {}).get('ready')),
                      'current': run.id == entry.run_id,
                      'created_at': iso(run.created_at), 'has_export': bool(run.export_file)})
    return sorted(items, key=lambda item: (item['cutoff'] or '', item['id']))
