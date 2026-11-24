import {feedback, field} from '../components/common/fields.js';
import {panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {DELIVERY, REVIEW} from '../constants/enums.js';
import {handler} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, link, p} from '../utils/dom.js';
import {date} from '../utils/format.js';
import {runPage} from './run.js';

function inboxPanel(spec, entries, message) {
  return panel('站内收件箱当前版',
    p('全场与各片区均达标才一次性切换当前版和下载；拦截或失败时保留旧版。','subtle'),
    table([
      {key:'report_date',label:'营业日'},
      {key:'cutoff',label:'当前截点'},
      {label:'当前版本',render:row=>link(`运行 ${row.run_id}`,`#results/${row.run_id}`)},
      {key:'recipient',label:'收件团队'},
      {label:'切换时间',render:row=>date(row.published_at)},
      {label:'下载',render:row=>{
        const download = button('下载当前版',{class:'quiet'});
        download.addEventListener('click', handler(download,message,() =>
          workspace.download(`/runs/${row.run_id}/export`,`${spec.short_name}-${row.run_id}.csv`)));
        return download;
      }},
    ], entries, {caption:'站内收件箱当前版', empty:'定时分发首次投递后生成当前版。'}));
}

function versionsPanel(spec, entries, message) {
  return panel('收件箱版本', p('旧版仍可打开查看缺报名单并下载 CSV。','subtle'),
    entries.map(entry => el('div',{class:'version-group'},
      p(`${entry.report_date} · 当前截点 ${entry.cutoff}`,'eyeline'),
      table([
        {label:'版本',render:row=>link(row.name,`#results/${row.id}`)},
        {label:'类型',render:row=>row.kind==='reissue'?'补发':'初版'},
        {key:'cutoff',label:'截点'},
        {label:'状态',render:row=>pill(row.current?'当前版':row.ready?'历史版':'未发布',
          row.current?'delivered':row.ready?'':'blocked')},
        {label:'计算时间',render:row=>date(row.created_at)},
        {label:'下载',render:row=>{
          if (!row.has_export) return '—';
          const download = button('下载 CSV',{class:'quiet'});
          download.addEventListener('click', handler(download,message,() =>
            workspace.download(`/runs/${row.id}/export`,`${spec.short_name}-${row.id}.csv`)));
          return download;
        }},
      ], entry.versions, {caption:`${entry.report_date} 收件箱版本`}))));
}

function reissuePanel(entries) {
  const message = feedback();
  const submit = button('发起补发',{type:'submit',class:'primary','data-testid':'start-reissue'});
  const form = el('form',{class:'fields',onSubmit:handler(submit,message,async event=>{
    const data = new FormData(event.target);
    const item = await workspace.reissue({report_date:data.get('report_date'),cutoff:data.get('cutoff')});
    location.hash = `#results/${item.id}`;
  })},
    el('label',{class:'field',for:'reissue-date'},el('span',{},'营业日'),
      el('select',{id:'reissue-date',name:'report_date',required:true},
        entries.map(item=>el('option',{value:item.report_date},item.report_date)))),
    field({key:'cutoff',label:'补发截点（北京时间）',type:'time',help:'须晚于初版截点，合并截至该时刻导入的当日回执。'}),
    el('div',{class:'actions'},submit), message);
  return panel('按营业日补发',
    p('沿用初版排班与门槛，合并各台账批次中截点前的当日回执；同一仓点品类取更正时间最新者，同一时刻内容冲突即拦截。','subtle'),
    form);
}

export async function deliveryPage(route, refresh) {
  const {spec, runs} = workspace;
  const message = feedback();
  const artifacts = table([
    {label:'报告', render:row=>link(row.name,`#results/${row.id}`)},
    {label:'复核', render:row=>pill(REVIEW[row.review],row.review)},
    {label:'计算时间', render:row=>date(row.finished_at)},
    {label:'下载', render:row=>{
      const download = button('下载 CSV',{class:'quiet'});
      download.addEventListener('click', handler(download,message,() =>
        workspace.download(`/runs/${row.id}/export`,`${spec.short_name}-${row.id}.csv`)));
      return download;
    }},
  ], runs.filter(row=>row.status==='complete'), {caption:'可导出的结果', empty:'完成计算后会生成对应的报告。'});
  const entries = spec.scheduled ? await workspace.inbox() : [];
  const delivery = spec.scheduled ? panel('站内分发回执', table([
    {key:'recipient',label:'收件团队'},
    {label:'回执',render:row=>pill(DELIVERY[row.state]||row.state,row.state)},
    {key:'detail',label:'说明'}, {label:'时间',render:row=>date(row.created_at)},
    {label:'查看',render:row=>link(`运行 ${row.run_id}`,`#results/${row.run_id}`)},
  ],await workspace.deliveries(),{caption:'站内收件箱',empty:'计划到时执行，回执将在此更新。'})) : null;
  return el('div',{}, panel(spec.pages[5], p('按运行批次下载 CSV，复核状态随报告保留。','subtle'), artifacts, message),
    delivery,
    spec.scheduled && inboxPanel(spec, entries, message),
    spec.scheduled && entries.length > 0 && versionsPanel(spec, entries, message),
    spec.scheduled && session.canWrite && entries.length > 0 && reissuePanel(entries),
    spec.scheduled && await runPage({...route,scheduling:true},refresh));
}
