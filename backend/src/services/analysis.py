import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date


REGION_RECIPIENTS = {
    '东区': '东区收货协调组',
    '北区': '北区收货协调组',
    '南区': '南区收货协调组',
}
DISPATCH_TEAM = '早班调度室'
PLAN = '应报计划'
RECEIPT = '到货回执'


@dataclass(frozen=True, order=True)
class ReportingSlot:
    report_date: str
    region: str
    depot: str
    category: str

    def label(self):
        return f'{self.report_date} {self.region}{self.depot} {self.category}'


@dataclass(frozen=True)
class Receipt:
    received_at: str
    arrived_lots: int


@dataclass
class Totals:
    expected: int = 0
    received: int = 0
    arrived_lots: int = 0

    def add(self, receipt):
        self.expected += 1
        if receipt is not None:
            self.received += 1
            self.arrived_lots += receipt.arrived_lots

    @property
    def missing(self):
        return self.expected - self.received

    @property
    def completeness(self):
        return round(self.received * 100 / self.expected, 2) if self.expected else None

    def passes(self, threshold):
        return self.expected > 0 and self.received * 100 >= threshold * self.expected


def _date(value):
    try:
        if date.fromisoformat(value).isoformat() == value:
            return value
    except (TypeError, ValueError):
        pass
    raise ValueError('业务日期须使用 YYYY-MM-DD 格式并为有效日期')


def _time(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value):
        raise ValueError(f'{label}须使用北京时间 HH:MM 格式')
    return value


def _text(row, key, label):
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label}不能为空')
    return value.strip()


def _index_rows(rows):
    plans = set()
    receipts = {}
    for line, row in enumerate(rows, start=2):
        try:
            slot = ReportingSlot(
                _date(row.get('report_date')),
                _text(row, 'region', '片区'),
                _text(row, 'depot', '收货仓点'),
                _text(row, 'category', '到货品类'),
            )
            if slot.region not in REGION_RECIPIENTS:
                raise ValueError('片区须为东区、北区或南区')
            record_type = _text(row, 'record_type', '记录类型')
            received_at, arrived_lots = row.get('received_at'), row.get('arrived_lots')
            if record_type == PLAN:
                if slot in plans:
                    raise ValueError(f'应报计划重复（{slot.label()}）')
                if received_at not in (None, '') or arrived_lots not in (None, ''):
                    raise ValueError('应报计划的回执接收时间和到货批次须留空')
                plans.add(slot)
            elif record_type == RECEIPT:
                if slot in receipts:
                    raise ValueError(f'到货回执重复（{slot.label()}）')
                received_at = _time(received_at, '回执接收时间')
                if type(arrived_lots) is not int or arrived_lots < 0:
                    raise ValueError('回执到货批次须为非负整数，零到货填 0')
                receipts[slot] = Receipt(received_at, arrived_lots)
            else:
                raise ValueError('记录类型须为应报计划或到货回执')
        except ValueError as error:
            raise ValueError(f'台账第 {line} 行 · {error}') from None
    for slot in receipts:
        if slot not in plans:
            raise ValueError(f'回执缺少对应应报计划（{slot.label()}）')
    return plans, receipts


def validate_rows(rows):
    _index_rows(rows)


def _recommendations(ready, totals, by_region):
    if not totals.expected:
        return [[DISPATCH_TEAM, '核对台账', '确认当天班表已导入，再运行日报']]
    if ready:
        rows = [[DISPATCH_TEAM, '接收日报', '接收全场收报明细和缺报名单']]
    else:
        rows = [[DISPATCH_TEAM, '暂缓分发', '待缺报补齐或台账核对完成后重新计算']]
    for region, counts in sorted(by_region.items()):
        if counts.missing:
            action = '接收缺报提醒' if ready else '优先催报'
            rows.append([REGION_RECIPIENTS[region], action, f'按名单跟进{region} {counts.missing} 条缺报'])
    return rows


def _compile(plans, receipts, report_date, cutoff, threshold, context='', extra_notes=()):
    total = Totals()
    by_region = defaultdict(Totals)
    missing_by_category = defaultdict(int)
    details, missing = [], []
    for slot in sorted(plans):
        receipt = receipts.get(slot)
        if receipt is not None and receipt.received_at > cutoff:
            receipt = None
        total.add(receipt)
        by_region[slot.region].add(receipt)
        missing_by_category[slot.category] += int(receipt is None)
        details.append([
            slot.region, slot.depot, slot.category, 1, int(receipt is not None),
            receipt.received_at if receipt else None, receipt.arrived_lots if receipt else None,
        ])
        if receipt is None:
            missing.append([slot.region, slot.depot, slot.category, REGION_RECIPIENTS[slot.region]])

    below = [region for region, counts in sorted(by_region.items()) if not counts.passes(threshold)]
    ready = total.passes(threshold) and not below
    stamp = f'{report_date} {cutoff}'
    status = '可分发' if ready else '拦截发布'
    if total.expected:
        summary = (f'{context}{stamp} 应报 {total.expected} 条，实报 {total.received} 条，缺报 {total.missing} 条，'
                   f'全场完整率 {total.completeness:.2f}%。{threshold}% 门槛下{status}。')
        if below:
            summary += f'未达标片区为{"、".join(below)}。'
        elif total.missing:
            summary += '请对应片区协调组按名单催报。'
    else:
        summary = f'{context}{stamp} 无当天应报计划，拦截发布。请调度室核对并导入当天班表。'

    return {
        'ready': ready,
        'summary': summary,
        'metrics': [
            {'label': '应报条数', 'value': total.expected, 'detail': '当天仓点与品类的计划总数'},
            {'label': '截点实报条数', 'value': total.received, 'detail': '含截点当分钟和零到货回执'},
            {'label': '缺报条数', 'value': total.missing, 'detail': '截点仍未收到回执的计划项'},
            {'label': '全场完整率（%）', 'value': total.completeness, 'detail': f'{threshold}% 为全场与片区共同门槛'},
            {'label': '发布闸门', 'value': status, 'detail': '当天有计划且全场与各有计划片区达标方可分发'},
        ],
        'tables': [
            {'title': '品类与仓点收报对账',
             'columns': ['片区', '仓点', '品类', '应报条数', '实报条数', '回执时间（北京时间）', '到货批次'],
             'rows': details},
            {'title': '片区收报完整率',
             'columns': ['片区', '应报条数', '实报条数', '缺报条数', '到货批次合计', '完整率（%）', '门槛结果'],
             'rows': [[region, counts.expected, counts.received, counts.missing, counts.arrived_lots,
                       counts.completeness, '达标' if counts.passes(threshold) else '未达标']
                      for region, counts in sorted(by_region.items())]},
            {'title': '截点缺报名单', 'columns': ['片区', '仓点', '品类', '跟进团队'], 'rows': missing},
            {'title': '区域收件建议', 'columns': ['团队', '建议', '处理事项'],
             'rows': _recommendations(ready, total, by_region)},
        ],
        'notes': [
            '按北京时间业务日和截点统计。空白到货批次表示截点缺报，0 代表已回执且零到货。',
            '发布判断使用原始收报比例。源台账漏排的计划需由调度室核对原始班表。',
            *extra_notes,
        ],
        'series_title': '各品类缺报条数（最多 16 类）',
        'series': [{'label': category, 'value': count}
                   for category, count in sorted(missing_by_category.items(), key=lambda item: (-item[1], item[0]))[:16]],
    }


def analyze(rows, params):
    report_date = _date(params.get('report_date', '2026-11-24'))
    cutoff = _time(params.get('as_of_time', '06:30'), '早班统计截点')
    if not '04:00' <= cutoff <= '09:00':
        raise ValueError('早班统计截点须在 04:00 至 09:00（含）')
    threshold = params.get('min_completeness_pct', 85)
    if type(threshold) is not int or not 0 <= threshold <= 100:
        raise ValueError('最低收报完整率须为 0 至 100 的整数')

    plans, receipts = _index_rows(rows)
    daily = {slot for slot in plans if slot.report_date == report_date}
    return _compile(daily, receipts, report_date, cutoff, threshold)


def merge_receipts(plans, sources, report_date, cutoff):
    """合并各台账批次在补发截点前接收的当日回执。

    同一仓点品类按更正时间（回执接收时间）取最新，与批次导入先后无关；
    同一时刻内容一致视为重复导入，不一致则判定冲突。返回 (有效回执, 冲突, 排班外回执数)。
    """
    candidates = defaultdict(list)
    ignored = 0
    for rows in sources:
        _, receipts = _index_rows(rows)
        for slot, receipt in receipts.items():
            if slot.report_date != report_date:
                continue
            if slot not in plans:
                ignored += 1
                continue
            if receipt.received_at > cutoff:
                continue
            candidates[slot].append(receipt)
    merged, conflicts = {}, []
    for slot, entries in candidates.items():
        latest = max(entry.received_at for entry in entries)
        lots = {entry.arrived_lots for entry in entries if entry.received_at == latest}
        if len(lots) > 1:
            conflicts.append([slot.region, slot.depot, slot.category, latest,
                              '、'.join(str(item) for item in sorted(lots))])
        else:
            merged[slot] = Receipt(latest, lots.pop())
    return merged, conflicts, ignored


def reissue_analyze(base_rows, sources, params):
    """按初版排班和门槛，对合并后的当日回执重算补发版。"""
    report_date = _date(params.get('report_date'))
    cutoff = _time(params.get('as_of_time'), '补发截点')
    threshold = params.get('min_completeness_pct')
    if type(threshold) is not int or not 0 <= threshold <= 100:
        raise ValueError('最低收报完整率须为 0 至 100 的整数')
    base_plans, _ = _index_rows(base_rows)
    plans = {slot for slot in base_plans if slot.report_date == report_date}
    merged, conflicts, ignored = merge_receipts(plans, sources, report_date, cutoff)
    context = f'补发版（沿用初版排班与 {threshold}% 门槛）· '
    if conflicts:
        return {
            'ready': False,
            'summary': (f'{context}{report_date} {cutoff} 有 {len(conflicts)} 组回执在同一更正时刻内容不一致，'
                        f'拦截发布。请相关仓点核对后重新导入台账，再发起补发。'),
            'metrics': [
                {'label': '应报条数', 'value': len(plans), 'detail': '初版排班中当天仓点与品类的计划总数'},
                {'label': '同时刻冲突回执', 'value': len(conflicts), 'detail': '同一仓点品类在同一更正时刻的到货批次不一致'},
                {'label': '发布闸门', 'value': '拦截发布', 'detail': '收件箱保留旧版，排除冲突后重新补发'},
            ],
            'tables': [
                {'title': '同一时刻冲突回执',
                 'columns': ['片区', '仓点', '品类', '更正时间（北京时间）', '冲突到货批次'],
                 'rows': sorted(conflicts)},
            ],
            'notes': [
                '同一仓点品类以更正时间最新的一条为准，与批次导入先后无关。',
                '同一更正时刻出现不同到货批次时无法判定有效回执，本次补发被拦截，收件箱保留旧版。',
            ],
            'series': [],
        }
    notes = [f'本次补发合并 {len(sources)} 个台账批次在截点 {cutoff} 前接收的当日回执，同一仓点品类取更正时间最新者。']
    if ignored:
        notes.append(f'另有 {ignored} 条回执不在初版排班内，未纳入本次补发。')
    return _compile(plans, merged, report_date, cutoff, threshold, context=context, extra_notes=notes)
