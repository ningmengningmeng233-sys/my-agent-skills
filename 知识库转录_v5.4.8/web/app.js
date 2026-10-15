
document.getElementById('tabNav').addEventListener('click', function(e){
  const btn = e.target.closest('.tab-btn');
  if(!btn) return;
  const name = btn.getAttribute('data-tab');
  if(!name) return;
  document.querySelectorAll('.tab-content').forEach(el=>el.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(el=>el.classList.remove('active'));
  btn.classList.add('active');
  const content = document.getElementById('tab-'+name);
  if(content) content.classList.add('active');
  if(name==='manage') loadStats();
  if(name==='settings') loadSettings();
});
const form = document.getElementById('searchForm');
const queryInput = document.getElementById('query');
const resultsDiv = document.getElementById('results');
const searchStats = document.getElementById('searchStats');
const params = new URLSearchParams(window.location.search);
// 进页面只同步搜索框文本，不自动发请求（避免收藏链接一打开就自带上次查询）。
// 结果区保留引导语，用户回车/点搜索/点快捷按钮才真正检索。
if(params.get('q')) queryInput.value = params.get('q');
resultsDiv.innerHTML = '<div class="loading" id="backendStatus">正在检测后端状态...</div>';
fetch('/api/health/ready').then(r=>r.json()).then(d=>{
  const el = document.getElementById('backendStatus');
  if(!el) return;
  const st = d.state || (d.ready ? 'ready' : 'warming');
  const prog = (d.progress != null) ? ('（进度 ' + Math.round(d.progress*100) + '%，阶段 ' + esc(d.stage||'?') + '）') : '';
  if(st === 'ready'){ el.innerHTML = '后端就绪（build ' + esc(d.build_id||d.cache_build_id||'?') + '），输入问题后按回车或点搜索。'; }
  else if(st === 'degraded'){ el.innerHTML = '⚠️ 向量模型离线，已降级为纯词法搜索：' + esc(d.user_action||d.probe_error||'') + ' 输入问题仍可搜，结果会标注 degraded。'; }
  else if(st === 'blocked'){ el.innerHTML = '❌ 索引不可用（blocked）：' + esc(d.user_action||d.error_code||'') + ' 请按终端提示修复后重建。'; }
  else if(d.probe_error){ el.innerHTML = '⚠️ 后端状态 ' + esc(st) + prog + '：' + esc(d.probe_error); }
  else { el.innerHTML = '⚠️ 后端' + esc(st) + prog + '：加载向量快照 → 加载词法快照 → 连接模型；输入问题后按回车或点搜索。'; }
}).catch(()=>{ const el = document.getElementById('backendStatus'); if(el) el.innerHTML = '⚠️ 后端未启动或无法连接：请运行 start_web.ps1 start 一键启动（会检查库/Ollama/端口并轮询 ready）。'; });
var searchBtn = document.getElementById('btnSearch');
if (searchBtn) {
  searchBtn.setAttribute('type', 'button');
  searchBtn.addEventListener('click', function(){ doSearch(); });
}
queryInput.addEventListener('keydown', function(e){
  if (e.key === 'Enter') { e.preventDefault(); doSearch(); }
});
function quickAsk(q){ queryInput.value = q; doSearch(); }
function esc(s){ const d=document.createElement('div'); d.textContent=(s==null?'':String(s)); return d.innerHTML; }
function fmtScore(r){
  const v = (r && typeof r === 'object') ? r : {score: r};
  const f = Number(v.final_score != null ? v.final_score : v.score || 0);
  const vs = Number(v.vector_score != null ? v.vector_score : NaN);
  const ls = Number(v.lexical_score != null ? v.lexical_score : NaN);
  const kb = Number(v.keyword_bonus != null ? v.keyword_bonus : NaN);
  if(!isFinite(f)) return '综合排序分 0.000（非概率）';
  let detail = '综合排序分: ' + f.toFixed(3) + '（非概率，仅用于排序）';
  if(isFinite(vs) && isFinite(ls) && isFinite(kb)) detail += ' ｜ 向量 ' + vs.toFixed(3) + ' / 词法 ' + ls.toFixed(3) + ' / 关键词+' + kb.toFixed(2);
  return detail;
}
function typeTag(name){
  if(name==='概念') return '<span class="tag tag-concept">[概念]</span>';
  if(name==='方法') return '<span class="tag tag-method">[方法]</span>';
  if(name==='数据') return '<span class="tag tag-data">[数据]</span>';
  if(name==='观点') return '<span class="tag tag-view">[观点]</span>';
  return '<span class="tag tag-other">[' + esc(name) + ']</span>';
}
function sectionHtml(title, query, segPath){
  return '<h4>' + decorateInline(title, query, segPath) + '</h4>';
}
function decorateInline(s, query, segPath){
  let e = esc(s);
  e = e.replace(/\[概念\]/g, typeTag('概念')).replace(/\[方法\]/g, typeTag('方法')).replace(/\[数据\]/g, typeTag('数据')).replace(/\[观点\]/g, typeTag('观点'));
  const linkify = function(mm,a,b){
    const line = Number(a);
    const label = '行 ' + a + (b?'~'+b:'');
    if(segPath){
      return '<a class="tag tag-line" style="cursor:pointer;text-decoration:underline;text-underline-offset:2px;" data-seg="' + esc(segPath) + '" data-line="' + line + '" title="点击直达原文第' + line + '行">📍' + label + '</a>';
    }
    return '<span class="tag tag-line">📍' + label + '</span>';
  };
  e = e.replace(/（行\s*(\d+)(?:\s*[~～\-—]\s*(\d+))?[^）]*）/g, linkify);
  e = e.replace(/\(行\s*(\d+)(?:\s*[~～\-—]\s*(\d+))?[^)]*\)/g, linkify);
  return e;
}
function highlightHtml(html, query){
  const words = Array.from(new Set((query||'').split(/[\s,，。、；;：:？?！!「」“”"']+/).filter(w=>w && w.length>=2)));
  words.sort((a,b)=>b.length-a.length);
  let out = html;
  words.slice(0,8).forEach(w=>{
    try{
      const re = new RegExp('(' + w.replace(/[.*+?^${}()|[\]\\]/g,'\\$&') + ')', 'g');
      out = out.replace(re, '<em>$1</em>');
    }catch(e){}
  });
  return out;
}
function stripMd(s){
  return String(s||'').replace(/\*\*/g,'').replace(/^[#"“”]+|[#"“”]+$/g,'').trim();
}
function parseKnowledgeItems(summary){
  const lines = String(summary||'').split('\n');
  const items = [];
  let section = '';
  for(let raw of lines){
    const line = raw.trim();
    let m;
    if((m=line.match(/^#{2,4}\s+(.*)/))){ section = stripMd(m[1]); continue; }
    if((m=line.match(/^-\s+\[(概念|方法|数据|观点)\]\s*(.*)/))){
      const body = m[2].trim();
      const t = body.match(/^(.*?)[（(]行\s*(\d+)(?:\s*[~～\-—]\s*(\d+))?/);
      items.push({
        type: m[1],
        text: t ? t[1].trim() : body.replace(/[（(]行.*$/,'').trim(),
        lineStart: t ? Number(t[2]) : null,
        lineEnd: t && t[3] ? Number(t[3]) : (t ? Number(t[2]) : null),
        section: section
      });
    }
  }
  return items;
}
function parseQuotes(summary){
  const lines = String(summary||'').split('\n');
  const out = [];
  let inQuote = false;
  for(let raw of lines){
    const line = raw.trim();
    if(/^#{2,4}\s+关键引用/.test(line)){ inQuote = true; continue; }
    if(/^#{2,4}\s+/.test(line) && inQuote){ break; }
    if(!inQuote) continue;
    const m = line.match(/^-\s+["“](.+?)["”]\s*[（(]行\s*(\d+)/);
    if(m) out.push({ text: m[1].trim(), line: Number(m[2]) });
  }
  return out.slice(0,3);
}
function lineBadge(lineStart, lineEnd, segPath){
  const label = '行 ' + lineStart + ((lineEnd && lineEnd !== lineStart) ? ('~'+lineEnd) : '');
  if(segPath){
    return '<a class="tag tag-line" style="cursor:pointer;text-decoration:underline;text-underline-offset:2px;" data-seg="' + esc(segPath) + '" data-line="' + lineStart + '" title="点击直达原文第' + lineStart + '行">📍' + label + '</a>';
  }
  return '<span class="tag tag-line">📍' + label + '</span>';
}
function buildTldr(summary, items, quotes, query, segPath){
  const t = stripMd(String(summary||'').split('\n').find(l=>l.trim() && !/^[#\-|>]/.test(l.trim()) && l.trim().length>20) || '');
  const scored = items.map(it=>{
    let s = 0;
    const words = (query||'').split(/[\s,，。、；;：:？?！!「」“”"']+/).filter(w=>w && w.length>=2);
    words.forEach(w=>{ if(it.text.includes(w)) s += 2; });
    if(/幻觉|胡说|造假/.test(it.text) && /幻觉|胡说|造假|真实|正确/.test(query||'')) s += 3;
    if(it.type==='观点' || it.type==='数据') s += 1;
    return { it, s };
  }).sort((a,b)=>b.s-a.s);
  const top = (scored.length && scored[0].s>0 ? scored : items.map(it=>({it, s:0}))).slice(0,3).map(x=>x.it);
  let h = '<div class="tldr"><div class="tldr-title">⚡ 速读梗概</div>';
  if(t) h += '<div class="tldr-text">' + decorateInline(t, query, segPath) + '</div>';
  if(top.length){
    h += '<ul class="tldr-points">' + top.map(it=>'<li>' + typeTag(it.type) + ' ' + decorateInline(it.text, query, segPath) + (it.lineStart?(' ' + lineBadge(it.lineStart, it.lineEnd, segPath)):'') + '</li>').join('') + '</ul>';
  }
  if(quotes.length){
    h += '<div style="margin-top:8px;font-size:13px;color:#888;">关键原句：' + quotes.map(q=>'“'+decorateInline(q.text, query, segPath)+'” ' + lineBadge(q.line, q.line, segPath)).join('　') + '</div>';
  }
  h += '</div>';
  return h;
}
function knowledgeListHtml(items, query, segPath){
  const order = { '概念':0, '方法':1, '数据':2, '观点':3 };
  const sorted = items.slice().sort((a,b)=>(order[a.type]-order[b.type]));
  const counts = {};
  sorted.forEach(it=>{ counts[it.type]=(counts[it.type]||0)+1; });
  const chips = Object.keys(counts).sort((a,b)=>order[a]-order[b]).map(k=>typeTag(k)+'×'+counts[k]).join(' ');
  let h = '<div class="kb-counts">' + chips + '<span style="font-size:12px;color:#666">共 ' + sorted.length + ' 条</span></div>';
  const first = sorted.slice(0,4), rest = sorted.slice(4);
  h += '<div class="kb-list">' + first.map(itemHtml(query, segPath)).join('') + '</div>';
  if(rest.length){
    h += '<details class="kb-more"><summary>展开剩余 ' + rest.length + ' 条知识点（概念 / 方法 / 数据 / 观点）</summary><div class="inner"><div class="kb-list">' + rest.map(itemHtml(query, segPath)).join('') + '</div></div></details>';
  }
  return h;
}
function itemHtml(query, segPath){
  return function(it){
    return '<div class="kb-item">' + typeTag(it.type) + '<span class="txt">' + decorateInline(it.text, query, segPath) + (it.lineStart?(' ' + lineBadge(it.lineStart, it.lineEnd, segPath)):'') + '</span></div>';
  };
}
function renderV5Card(summary, query, segPath){
  const lines = String(summary||'').split('\n');
  let html = '', inTable = false, inList = false;
  const closeTable = ()=>{ if(inTable){ html += '</tbody></table>'; inTable=false; } };
  const closeList = ()=>{ if(inList){ html += '</ul>'; inList=false; } };
  for(let raw of lines){
    const line = raw.trim();
    if(!line){ closeTable(); closeList(); continue; }
    if(/^\|.*\|$/.test(line)){
      const cells = line.split('|').slice(1,-1).map(c=>c.trim());
      if(cells.every(c=>/^:?-{2,}:?$/.test(c))) continue;
      if(!inTable){ closeList(); html += '<table><tbody>'; inTable=true; }
      const head = /实体|类型|作用|定义|首次/.test(line);
      html += '<tr>' + cells.map(c=> head ? '<th>'+esc(c)+'</th>' : '<td>'+decorateInline(c,query,segPath)+'</td>').join('') + '</tr>';
      continue;
    }
    closeTable();
    let m;
    if((m=line.match(/^####\s+(.*)/))){ closeList(); html += sectionHtml(m[1], query, segPath); continue; }
    if((m=line.match(/^###\s+(.*)/))){ closeList(); html += sectionHtml(m[1], query, segPath); continue; }
    if((m=line.match(/^##\s+(.*)/))){ closeList(); html += sectionHtml(m[1], query, segPath); continue; }
    if(/^---+$/.test(line)){ closeList(); continue; }
    if((m=line.match(/^-\s+(.*)/))){
      if(!inList){ html += '<ul>'; inList=true; }
      const body = m[1];
      const isQuote = /^["“]/.test(body);
      html += '<li class="'+(isQuote?'quote':'')+'">'+decorateInline(body,query,segPath)+'</li>';
      continue;
    }
    closeList();
    html += '<div style="margin:6px 0;">'+decorateInline(line,query,segPath)+'</div>';
  }
  closeTable(); closeList();
  return highlightHtml(html, query);
}
function cleanTitle(t){ t=(t||'').trim().replace(/^《+|》+$/g,''); return t ? ('《'+t+'》') : '《独立文档集》'; }
let activeSearchController = null;
let searchRequestId = 0;
async function doSearch(){
  const q = queryInput.value.trim();
  if(!q || q.length > 500){ if(q.length > 500) resultsDiv.innerHTML = '<div class="loading" style="color:#e74c3c;">❌ 查询过长（最多 500 字）</div>'; return; }
  if(window.__searching){ if(activeSearchController){ try{ activeSearchController.abort(); }catch(e){} } }
  window.__searching = true;
  const myId = ++searchRequestId;
  const btn = document.getElementById('btnSearch');
  document.querySelectorAll('#tab-search button').forEach(b=>{ b.disabled = true; });
  btn.textContent = '搜索中...';
  const url = new URL(window.location);
  url.searchParams.set('q', q);
  window.history.replaceState({}, '', url);
  resultsDiv.innerHTML = '<div class="loading" id="searchProgress">搜索中...<br><small style="color:#555;" id="searchElapsed">正在请求后端...</small></div>';
  searchStats.textContent = '';
  const ctrl = new AbortController();
  activeSearchController = ctrl;
  const timer = setTimeout(()=>ctrl.abort(), 120000);
  const t0 = Date.now();
  const tick = setInterval(()=>{
    if(myId !== searchRequestId) { clearInterval(tick); return; }
    const s = ((Date.now()-t0)/1000).toFixed(0);
    const el = document.getElementById('searchElapsed');
    if(el) el.textContent = '仍在处理，已耗时 ' + s + ' 秒' + (Number(s) >= 10 ? '｜可打开管理页查看后端状态' : '');
  }, 1000);
  try{
    const resp = await fetch('/api/search?q=' + encodeURIComponent(q) + '&k=4', {signal: ctrl.signal});
    const data = await resp.json().catch(()=>({}));
    if(myId !== searchRequestId) return;
    if(resp.status === 429) throw new Error('后端搜索并发已满（429），请 3 秒后重试');
    if(resp.status === 503) throw new Error('检索失败：' + (data.error || '后端 embedding 不可用'));
    if(!resp.ok) throw new Error(data.error || ('HTTP '+resp.status));
    renderSearch(data, q, (Date.now()-t0)/1000);
  }catch(err){
    if(myId !== searchRequestId) return;
    const isTimeout = (err && err.name === 'AbortError');
    const msg = (err && err.message) || String(err);
    resultsDiv.innerHTML = '<div class="loading" style="color:#e74c3c;">'
      + (isTimeout
        ? '❌ 搜索超时（120秒）：后端可能正在预热模型或被长请求占满<br><small>请 <button class="btn btn-primary" style="padding:6px 14px;font-size:13px;" onclick="doSearch()">点此重试</button>，重试一般秒回；若连续超时请重启后端服务</small>'
        : '❌ 搜索失败：' + esc(msg) + '<br><small>HTTP 状态与建议见上；Ollama 离线/索引损坏/端口占用请到“管理/设置”页查看 <button class="btn btn-primary" style="padding:6px 14px;font-size:13px;" onclick="doSearch()">点此重试</button></small>')
      + '</div>';
  }finally{
    clearTimeout(timer);
    clearInterval(tick);
    if(myId === searchRequestId){ window.__searching = false; activeSearchController = null; }
    document.querySelectorAll('#tab-search button').forEach(b=>{ b.disabled = false; });
    btn.textContent = '搜索';
  }
}
function renderSearch(data, q, elapsed){
  if(data.error){ resultsDiv.innerHTML = '<div class="loading" style="color:#e74c3c;">❌ ' + esc(data.error) + '</div>'; return; }
  if(!data.results || data.results.length===0){
    resultsDiv.innerHTML = '<div class="loading">😴 没有找到相关结果<br><small style="color:#555;">试试换个说法，或加更多关键词</small></div>';
    searchStats.textContent = '';
    return;
  }
  let html = '';
  // 定义查询过滤提示：后端已淘汰不含核心词的卡；若触发兜底（filtered=false 的卡），
  // 如实告知用户“库内含词内容不足，为你补了相关结果”，不撒谎。
  if(data.filtered && data.must_terms && data.must_terms.length){
    const hasFallback = (data.results||[]).some(r=>r.filtered === false);
    html += '<div class="result-range" style="margin-bottom:12px;">'
      + (data.degraded ? '⚠️ 当前为纯词法降级结果（向量离线）：' : '🔍 定义查询已过滤：')
      + '仅保留含「' + esc(data.must_terms.join(' / ')) + '」的卡片'
      + (hasFallback ? '（库内含词内容不足，已为你补了相关结果）' : '') + '</div>';
  } else if((data.results||[]).some(r=>r.filtered === false)){
    html += '<div class="result-range" style="margin-bottom:12px;">📚 库内无含词内容，为你显示相关结果</div>';
  }
  // 跨段同句消歧：同一查询返回多卡时，若某卡关键引用在其他卡的段文件中也逐字出现，
  // 在该引用下方标注“本文还有 X 处相同表述”，点击展开定位，避免误认唯一出处。
  const quoteOwners = {};
  data.results.forEach(r=>{
    parseQuotes(r.summary||'').forEach(qq=>{
      const key = qq.text.replace(/\s+/g,'').slice(0,40);
      if(key.length < 10) return;
      (quoteOwners[key] = quoteOwners[key] || new Set()).add(r.segment_path || r.index_path);
    });
  });
  data.results.forEach(r=>{
    const segPath = r.segment_path || '';
    const dupNotes = {};
    parseQuotes(r.summary||'').forEach(qq=>{
      const key = qq.text.replace(/\s+/g,'').slice(0,40);
      if(key.length < 10) return;
      const owners = quoteOwners[key] || new Set();
      const others = [...owners].filter(o=>o && o !== (r.segment_path || r.index_path));
      if(others.length) dupNotes[qq.text] = others;
    });
    const dupBanner = Object.keys(dupNotes).length
      ? '<div class="result-range">⚠️ 本卡有 ' + Object.keys(dupNotes).length + ' 句关键引用在其他段文件中也有相同表述：'
        + Object.entries(dupNotes).map(([txt, owners])=>'“' + esc(txt.slice(0,24)) + '…”→ ' + owners.map(o=>'<a data-seg="' + esc(o) + '" data-line="1" style="color:#6c5ce7;cursor:pointer;">' + esc(String(o).split('/').pop()) + '</a>').join('、')).join('；')
        + '</div>'
      : '';
    const loc = (r.segment_line_start && r.segment_line_end) ? ('行 ' + r.segment_line_start + ' ~ ' + r.segment_line_end) : '段落级卡片';
    html += '<div class="result-card">';
    html += '<div class="result-meta"><span class="tag tag-note">📘 结构化卡片</span>';
    html += '<a class="tag tag-line" style="cursor:pointer;text-decoration:underline;text-underline-offset:2px;" data-seg="' + esc(r.segment_path||'') + '" data-line="' + (r.segment_line_start||1) + '" title="点击直达原文">📍 ' + esc(loc) + '</a>';
    html += '<span class="tag tag-score">' + esc(fmtScore(r)) + '</span>';
    if(r.author) html += ' 👤 ' + esc(r.author);
    html += '</div>';
    html += '<div class="result-title">' + esc(cleanTitle(r.title)) + '</div>';
    if(r.chapter_title) html += '<div class="result-range">小节：' + esc(r.chapter_title) + ' ｜ 范围：' + esc(r.chapter_range||'全文') + '</div>';
    html += dupBanner;
    const kbItems = parseKnowledgeItems(r.summary||'');
    const kbQuotes = parseQuotes(r.summary||'');
    if(kbItems.length){
      html += buildTldr(r.summary||'', kbItems, kbQuotes, q, segPath);
      html += '<div class="result-text">' + knowledgeListHtml(kbItems, q, segPath) + '</div>';
      html += '<details class="kb-more"><summary>查看完整卡片（含实体表 / 交叉引用 / 全部原文小节）</summary><div class="inner"><div class="result-text">' + renderV5Card(r.summary||'', q, segPath) + '</div></div></details>';
    } else {
      html += '<div class="result-text">' + renderV5Card(r.summary||'', q, segPath) + '</div>';
    }
    html += '<div class="result-score">' + esc(fmtScore(r)) + '</div>';
    html += '<div class="result-links"><code>卡片: ' + esc(r.index_path||'') + '</code>';
    if(r.segment_path) html += '<a data-seg="' + esc(r.segment_path) + '" data-line="' + esc(r.segment_line_start||1) + '">原文: ' + esc(r.segment_path) + '（点击查看行号上下文）</a>';
    html += '</div></div>';
  });
  resultsDiv.innerHTML = html;
  searchStats.textContent = '📊 共 ' + data.results.length + ' 条结果'
    + ((elapsed != null && isFinite(elapsed)) ? (' ｜ 耗时 ' + Number(elapsed).toFixed(1) + 's') : '');
  resultsDiv.querySelectorAll('a[data-seg]').forEach(a=>{
    a.addEventListener('click', ()=>openSource(a.getAttribute('data-seg'), Number(a.getAttribute('data-line')||1)));
  });
  try{
    if(form && queryInput && queryInput.value){ /* keep */ }
  }catch(e){}
}
async function openSource(path, line){
  resultsDiv.innerHTML = '<div class="loading">正在读取原文行号上下文…</div>';
  try{
    const res = await fetch('/api/source?path=' + encodeURIComponent(path) + '&line=' + encodeURIComponent(line));
    const data = await res.json();
    if(!res.ok) throw new Error(data.error || '原文读取失败');
    let h = '<div class="result-card"><div class="result-meta"><span class="tag tag-note">📄 原文上下文</span><span class="tag tag-line">当前行 ' + data.line + '</span></div>';
    h += '<div class="result-range">文件 ' + esc(data.path) + ' ｜ 显示 ' + data.start + '~' + data.end + ' ｜ <a id="backLink" style="color:#6c5ce7;cursor:pointer;">← 返回检索结果</a></div>';
    h += '<div class="viewer">' + data.lines.map(function(x){
      return '<div class="ln' + (x.number===data.line?' hit':'') + '"><span class="no">' + String(x.number).padStart(4,' ') + '</span><span>' + highlightHtml(esc(x.text), queryInput.value) + '</span></div>';
    }).join('') + '</div></div>';
    resultsDiv.innerHTML = h;
    document.getElementById('backLink').addEventListener('click', doSearch);
    const hit = resultsDiv.querySelector('.hit');
    if(hit) hit.scrollIntoView({block:'center'});
  }catch(e){
    resultsDiv.innerHTML = '<div class="loading" style="color:#e74c3c;">❌ 原文读取失败：' + esc(e.message) + '</div>';
  }
}
try{
  // 进页面不同步发请求：URL 的 ?q= 只做搜索框回显，避免一打开就自带上次查询。
  // 用户回车/点搜索/点快捷按钮/点返回检索结果时才真正检索。
  if(form && queryInput && params.get('autosearch') === '1' && queryInput.value){ form.dispatchEvent(new Event('submit')); }
}catch(e){}
async function loadStats(){
  const statsArea = document.getElementById('statsArea');
  const fileListArea = document.getElementById('fileListArea');
  const indexStatus = document.getElementById('indexStatus');
  statsArea.innerHTML = '<div class="loading">加载中...</div>';
  try{
    const resp = await fetch('/api/stats');
  const _filesResp = await fetch('/api/files?offset=0&limit=100');
  const _filesData = await _filesResp.json().catch(()=>({}));
    const data = await resp.json();
    if(data.error){ statsArea.innerHTML = '<div class="loading" style="color:#e74c3c;">❌ ' + esc(data.error) + '</div>'; return; }
    statsArea.innerHTML = '<div class="stat-grid">' +
      '<div class="stat-card"><div class="num">' + (data.vector_count||0) + '</div><div class="label">📄 向量知识块</div></div>' +
      '<div class="stat-card"><div class="num">' + (data.index_count||0) + '</div><div class="label">📇 段索引卡片</div></div>' +
      '<div class="stat-card"><div class="num">' + (data.raw_count||0) + '</div><div class="label">📁 原文段文件</div></div>' +
      '</div>';
    const vec = data.vector_status || '未知';
    indexStatus.innerHTML = '<div style="font-size:14px;color:#888;margin-bottom:16px;">本地向量：' + esc(vec) + ' ｜ 库：' + esc(data.lib||'') + '</div>';
    const files = _filesData.files || []; const _total = _filesData.total || files.length;
    if(!files.length){ fileListArea.innerHTML = '<div class="loading">暂无文件</div>'; return; }
    let flist = '<div class="file-list" style="margin-top:8px;">';
    files.forEach(f=>{
      flist += '<div class="file-row">';
      flist += '<span class="source-tag tag-note">独立文档集</span>';
      flist += '<span class="fname">' + esc(f.name) + '</span>';
      flist += '<span class="fsize">' + (f.size > 1024 ? (f.size/1024).toFixed(1)+'KB' : f.size+'B') + '</span>';
      flist += '<span class="indexed">✅ 已索引</span>';
      flist += '</div>';
    });
    flist += '</div>';
    flist += '<div style="font-size:13px;color:#666;margin-top:6px;">共 ' + _total + ' 个文件（分页显示前 ' + files.length + ' 个）</div>';
    fileListArea.innerHTML = flist;
  }catch(err){
    statsArea.innerHTML = '<div class="loading" style="color:#e74c3c;">❌ 加载失败</div>';
  }
}
async function loadSettings(){
  try{
    const resp = await fetch('/api/status');
    const data = await resp.json();
    const stats = data.stats || {};
    document.getElementById('knowledgeDir').value = (data.diagnostics && data.diagnostics.library_status && data.diagnostics.library_status.path) || '';
    const ol = data.diagnostics ? data.diagnostics.ollama : null;
    let dot = '<span class="status-dot offline"></span> ❌ 无法连接';
    if(ol && ol.online){
      dot = '<span><span class="status-dot online"></span> ✅ 在线' + (ol.matched_model ? ('，' + esc(ol.matched_model) + ' 就绪') : '，但未检测到推荐向量模型') + '</span>';
      if(ol.models && ol.models.length) dot += '<div style="font-size:13px;color:#666;margin-top:6px;">已安装模型: ' + esc(ol.models.join(', ')) + '</div>';
    } else if(ol && ol.error){
      dot = '<span><span class="status-dot offline"></span> ❌ 离线：' + esc(ol.error) + '</span><div style="font-size:13px;color:#666;margin-top:6px;">已自动降级为本地离线向量，零 Token 可用</div>';
    }
    document.getElementById('ollamaStatus').innerHTML = dot;
    const lib = data.diagnostics ? data.diagnostics.library_status : null;
    document.getElementById('libHealth').innerHTML = lib
      ? '<div style="font-size:14px;color:#888;">原文 ' + (lib.has_raw?'✅':'❌') + ' ｜ 段索引 ' + (lib.has_segment_index?'✅':'❌') + ' ｜ 书导航 ' + (lib.has_book_nav?'✅':'❌') + ' ｜ 语义索引 ' + (lib.has_semantic_index?'✅':'❌') + '</div><div style="font-size:13px;color:#666;margin-top:6px;">向量状态：' + esc(stats.vector_status||'-') + ' ｜ 原文 ' + (stats.raw_count||0) + ' ｜ 索引 ' + (stats.index_count||0) + ' ｜ 导航 ' + (stats.nav_count||0) + '</div>'
      : '<div class="loading">❌ 无法读取知识库状态</div>';
  }catch(err){
    document.getElementById('ollamaStatus').innerHTML = '<span class="status-dot offline"></span> ❌ 无法连接';
  }
}
function toast(msg, type){
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(()=>{ el.style.opacity='0'; setTimeout(()=>el.remove(),300); },3000);
}
window.__knowledgeAppReady = true;
if (window.__knowledgeWatchdog) { clearTimeout(window.__knowledgeWatchdog); }
