// 视频号发布·Electron 原生实现（照抄小V猫）：隐藏 BrowserWindow(backgroundThrottling:false)
// + webContents.debugger(CDP) 驱动上传/填表/发表。无独立浏览器窗口、不被"窗口不可见"节流。
// 逻辑对齐 channels.py 的 upload()，但换成不被节流的 Electron webContents。
const { BrowserWindow, session, screen, app } = require('electron');
const path = require('path');
const fs = require('fs');
const os = require('os');

const ROOT = path.join(__dirname, '..');

// 【关键·和后端 app.py 完全一致】打包版数据在 %LOCALAPPDATA%\爆款监控\data,不在安装目录旁。
// 之前 publish.js 用 ROOT/data 找 channels/*.json → 打包机上找不到 → 取会话报"账号未登录"。
function _dataDir() {
  try {
    if (app && app.isPackaged) {
      const local = process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local');
      return path.join(local, '爆款监控', 'data');
    }
  } catch (e) {}
  return path.join(ROOT, 'data');   // 开发态：项目目录下 data/
}
function _chJson(aid) { return path.join(_dataDir(), 'channels', aid + '.json'); }
const CREATE_URL = 'https://channels.weixin.qq.com/platform/post/create';
// 【关键】正常 Chrome UA——Electron 默认 UA 带"Electron/爆款监控"会被视频号二维码/接口拒绝。
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36';

// 深度穿透 shadow DOM（wujie 微前端，控件在 shadowRoot 里）——对齐 channels.py _DEEP_JS
const DEEP_JS = `
  var _dall=function(){ var out=[]; (function walk(r){ var els=r.querySelectorAll('*');
    for(var i=0;i<els.length;i++){ out.push(els[i]); if(els[i].shadowRoot) walk(els[i].shadowRoot); } })(document); return out; };
  var _dqa=function(sel){ return _dall().filter(function(e){ try{return e.matches(sel);}catch(_){return false;} }); };
`;

function log(msg) {
  try { fs.appendFileSync(path.join(__dirname, 'publish.log'), '[' + new Date().toISOString() + '] ' + msg + '\n'); } catch (e) {}
}

// 把账号 cookie 播种进它专属 partition（和 main.js inject-cookies 一样）
async function injectCookies(part, aid) {
  const f = _chJson(aid);
  if (!fs.existsSync(f)) throw new Error('账号未登录');
  const data = JSON.parse(fs.readFileSync(f, 'utf-8'));
  const ses = session.fromPartition(part);
  for (const c of (data.cookies || [])) {
    const bare = (c.domain || '').replace(/^\./, '');
    const url = (c.secure ? 'https' : 'http') + '://' + bare + (c.path || '/');
    const ss = String(c.sameSite || '').toLowerCase();
    const sameSite = ss === 'strict' ? 'strict' : (ss === 'lax' ? 'lax' : 'no_restriction');
    try {
      await ses.cookies.set({
        url, name: c.name, value: c.value, domain: c.domain, path: c.path || '/',
        secure: !!c.secure, httpOnly: !!c.httpOnly, sameSite,
        expirationDate: (c.expires && c.expires > 0) ? c.expires : undefined,
      });
    } catch (e) {}
  }
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// 挂剧集：拦到 post_create 请求 → 把 objectDesc.component={id:exportId,type:8,title} 注进去再放行；
// 其它请求(含非 post_create)一律原样放行。和 channels.py 的 _setup_drama_injection 同逻辑。
async function injectDrama(dbg, p, params) {
  const rid = p.requestId;
  const cont = (extra) => dbg.sendCommand('Fetch.continueRequest', Object.assign({ requestId: rid }, extra || {}));
  try {
    const u = (p.request && p.request.url) || '';
    if (u.indexOf('/post/post_create') < 0 || u.indexOf('micro/content') >= 0) return cont();
    const raw = (p.request && p.request.postData) || '';
    if (!raw) return cont();
    let data;
    try { data = JSON.parse(raw); } catch (e) { return cont(); }
    let od = data.objectDesc, odWasStr = false;
    if (typeof od === 'string') { try { od = JSON.parse(od); odWasStr = true; } catch (e) { od = null; } }
    if (od && typeof od === 'object') {
      od.component = { id: params.drama, type: 8, title: params.drama_title || params.drama };
      data.objectDesc = odWasStr ? JSON.stringify(od) : od;
      const nb = JSON.stringify(data);
      log('挂剧集注入成功: ' + (params.drama_title || params.drama));
      return cont({ postData: Buffer.from(nb, 'utf8').toString('base64') });
    }
    log('post_create 里没有 objectDesc(dict)，跳过挂剧');
    return cont();
  } catch (e) {
    log('挂剧集注入异常: ' + e);
    try { return cont(); } catch (_) {}
  }
}

// 发布一条视频。params: {aid, video_path, title, tags[], desc, location, drama, ...}
// onStatus(msg) 进度回调。返回 {ok, msg}。
async function publishOne(params, onStatus) {
  const st = (m) => { try { onStatus && onStatus(m); } catch (e) {} log('STAGE ' + m); };
  const { aid, video_path } = params;
  const title = params.title || '';
  const tags = params.tags || [];
  const desc = params.desc || '';
  const part = 'persist:ch_' + aid;

  // 护栏：空文件
  try {
    const sz = fs.statSync(video_path).size;
    if (sz < 10240) return { ok: false, msg: '视频文件是空的(' + sz + '字节)，去重导出失败的空壳，请重新导出' };
  } catch (e) { return { ok: false, msg: '视频文件不存在：' + video_path }; }

  log('publishOne start aid=' + aid + ' video=' + video_path);
  try { session.fromPartition(part).setUserAgent(UA); } catch (e) {}   // 正常Chrome UA
  await injectCookies(part, aid);
  log('cookies injected');

  // 【关键·2026-07-13实测定案】方案对比:
  //  - show:false 普通隐藏窗口→被合成器节流,上传102秒(慢)。
  //  - offscreen:true 离屏渲染→上传8秒(不节流,快),但 Input 点击坐标空间和页面CSS坐标错位,发表点不中。
  //  - ✅ 真实窗口贴屏幕右下角只露1px→在屏幕上=不节流(快),真实窗口=坐标正常(点得中),1px+不上任务栏=用户几乎无感。
  // 这是又快、又能点中发表、又几乎零窗口的唯一可行解(Electron 版贴角方案)。
  const win = new BrowserWindow({
    show: false, width: 1280, height: 1000, skipTaskbar: true,
    webPreferences: { partition: part, backgroundThrottling: false, sandbox: false },
  });
  const wc = win.webContents;
  try { wc.setAudioMuted(true); } catch (e) {}   // 静音:发布页会自动播放视频预览声音,后台别外放
  // 移到主屏右下角外,只露1px在屏内(在屏幕上=不节流,用户几乎看不到),不抢焦点显示
  try {
    const b = screen.getPrimaryDisplay().bounds;
    win.setPosition(b.x + b.width - 1, b.y + b.height - 1);
    win.showInactive();
  } catch (e) { log('corner pos err ' + e); }
  const dbg = wc.debugger;
  const sig = { clip_ready: false, create_done: false, create_ok: false, msg: '' };
  let cleanup = () => { try { win.destroy(); } catch (e) {} };
  log('window created');

  try {
    dbg.attach('1.3');
    log('debugger attached');
  } catch (e) {
    cleanup();
    return { ok: false, msg: 'debugger attach 失败: ' + String(e).slice(0, 80) };
  }

  // 监听后端响应：post_clip_video_result=CDN登记完成；post_create=发布结果
  dbg.on('message', (_e, method, p) => {
    try {
      if (method === 'Network.responseReceived') {
        const u = (p.response && p.response.url) || '';
        if (u.indexOf('post_clip_video_result') >= 0) sig.clip_ready = true;
        else if (u.indexOf('/post/post_create') >= 0 && u.indexOf('micro/content') < 0) {
          sig.create_done = true;
          dbg.sendCommand('Network.getResponseBody', { requestId: p.requestId }).then((r) => {
            try {
              const j = JSON.parse(r.body);
              const ec = (j.errCode !== undefined) ? j.errCode : (j.errcode !== undefined ? j.errcode : -1);
              sig.create_ok = (ec === 0);
              sig.msg = j.errMsg || j.errmsg || '';
            } catch (_) { sig.create_ok = true; }
          }).catch(() => { sig.create_ok = true; });
        }
      } else if (method === 'Fetch.requestPaused') {
        // 【挂剧集】拦 post_create 请求，把 objectDesc.component={id,type:8,title} 注进去再放行
        injectDrama(dbg, p, params).catch(() => {});
      }
    } catch (err) {}
  });

  const evalJS = async (expr) => {
    const r = await dbg.sendCommand('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    if (r && r.result) return r.result.value;
    return undefined;
  };
  // 可信点击：用 debugger Input.dispatchMouseEvent —— 它的坐标是页面 CSS 像素,和
  // getBoundingClientRect 同一坐标空间(sendInputEvent 用窗口设备像素,OSR下与页面缩放不一致会点空)。
  const trustedClick = async (x, y) => {
    try {
      await dbg.sendCommand('Input.dispatchMouseEvent', { type: 'mouseMoved', x: x, y: y });
      await dbg.sendCommand('Input.dispatchMouseEvent', { type: 'mousePressed', x: x, y: y, button: 'left', buttons: 1, clickCount: 1 });
      await dbg.sendCommand('Input.dispatchMouseEvent', { type: 'mouseReleased', x: x, y: y, button: 'left', buttons: 0, clickCount: 1 });
    } catch (e) { log('click err ' + e); }
  };

  st('打开发表页');
  // 【关键】必须先 loadURL(触发渲染进程创建),否则 Network.enable 等命令会在没有渲染进程时挂死。
  // loadURL 不 await(视频号 SPA 长连接会让 did-finish-load 迟迟不触发)。
  wc.loadURL(CREATE_URL).catch((e) => log('loadURL rejected(忽略) ' + e));
  await new Promise((res) => {
    let done = false;
    const fin = () => { if (!done) { done = true; res(); } };
    wc.once('dom-ready', fin);
    setTimeout(fin, 10000);   // 兜底:最多等10秒
  });
  await sleep(1500);
  // 渲染进程已就绪 → 现在 enable CDP 域(上传响应在很久之后,不会漏)
  try {
    await dbg.sendCommand('Network.enable');
    await dbg.sendCommand('Runtime.enable');
    await dbg.sendCommand('DOM.enable');
    // 挂剧集：开 Fetch 拦截 post_create 请求（只拦它这一个，别影响别的）
    if (params.drama) {
      await dbg.sendCommand('Fetch.enable', { patterns: [{ urlPattern: '*post/post_create*', requestStage: 'Request' }] });
      log('Fetch 拦截已开(挂剧集 ' + params.drama_title + ')');
    }
    log('domains enabled');
  } catch (e) { log('enable domains err ' + e); }
  const url = wc.getURL() || '';
  log('page loaded url=' + url);
  if (url.toLowerCase().indexOf('login') >= 0) { cleanup(); return { ok: false, msg: '登录已失效，请重新扫码登录该账号' }; }

  // 找上传输入框的 objectId → DOM.setFileInputFiles 塞路径（大文件不受限）
  st('上传视频文件…');
  let setOk = false;
  for (let i = 0; i < 25 && !setOk; i++) {
    try {
      const r = await dbg.sendCommand('Runtime.evaluate', {
        expression: '(function(){' + DEEP_JS + ' var a=_dqa(\'input[type=file]\');'
          + ' return a.find(function(x){return (x.accept||\'\').indexOf(\'video\')>=0;})||a[0]||null;})()',
        returnByValue: false,
      });
      const oid = r && r.result && r.result.objectId;
      if (oid) {
        await dbg.sendCommand('DOM.setFileInputFiles', { files: [video_path], objectId: oid });
        setOk = true;
        break;
      }
    } catch (e) {}
    await sleep(1000);
  }
  if (!setOk) { cleanup(); return { ok: false, msg: '没找到上传输入框（页面可能已改版）' }; }

  // 等 CDN 登记完成（post_clip_video_result）。不中途重塞、不跑重扫描（对齐 channels.py 教训）。
  st('上传视频到CDN(约1分钟)…');
  for (let w = 0; w < 240 && !sig.clip_ready; w++) await sleep(2000);
  if (!sig.clip_ready) { cleanup(); return { ok: false, msg: '视频上传超时(>8分钟未收到CDN登记完成)' }; }
  st('视频已上传完成');

  // 填标题/话题/简介：聚焦编辑器 + Input.insertText（可信输入）
  st('填标题/话题/简介');
  try {
    const rect = await evalJS('(function(){' + DEEP_JS
      + ' var ed=document.querySelector(\'.text-editor-content\')||document.querySelector(\'div.input-editor\');'
      + ' if(!ed){var a=_dqa(\'.text-editor-content,div.input-editor\');ed=a[0];}'
      + ' if(!ed) return null; ed.focus(); var r=ed.getBoundingClientRect();'
      + ' return {x:r.left+8,y:r.top+8};})()');
    if (rect) {
      await dbg.sendCommand('Input.dispatchMouseEvent', { type: 'mousePressed', x: rect.x, y: rect.y, button: 'left', clickCount: 1 });
      await dbg.sendCommand('Input.dispatchMouseEvent', { type: 'mouseReleased', x: rect.x, y: rect.y, button: 'left', clickCount: 1 });
      await sleep(200);
      let text = (title || '').slice(0, 30);
      if (tags && tags.length) { for (const tg of tags) text += ' #' + String(tg).replace(/^#/, ''); }
      if (desc) text += '\n' + desc;
      if (text) await dbg.sendCommand('Input.insertText', { text });
    }
  } catch (e) { log('fill err ' + e); }

  // 发表：等按钮真启用（禁用绝不点），启用后 Input 派发可信点击
  st('发表');
  const deadline = Date.now() + 480000;
  let waited = 0;
  while (Date.now() < deadline && !sig.create_done) {
    let bs;
    try {
      bs = await evalJS('(function(){' + DEEP_JS
        + ' var b=_dall().find(function(e){return e.tagName===\'BUTTON\'&&(e.innerText||\'\').trim()===\'发表\';});'
        + ' if(!b) return {found:false};'
        + ' var c=(b.className||\'\').toString(); var dis=!!b.disabled||b.getAttribute(\'aria-disabled\')===\'true\'||/disable/.test(c);'
        + ' try{ b.scrollIntoView({block:\'center\',inline:\'center\'}); }catch(_){}'   // 滚到视口中间,否则坐标在窗口外点空
        + ' var r=b.getBoundingClientRect(); return {found:true,disabled:dis,x:r.left+r.width/2,y:r.top+r.height/2,iw:innerWidth,ih:innerHeight};})()');
    } catch (e) { bs = { found: false, err: String(e).slice(0, 60) }; }
    waited++;
    if (waited % 3 === 1) log('btn ' + JSON.stringify(bs));
    if (!bs || !bs.found) { await sleep(2000); continue; }
    if (bs.disabled) {
      if (waited % 5 === 1) st('视频号处理视频中，等发表按钮就绪…(' + (waited * 3) + '秒)');
      await sleep(3000);
      continue;
    }
    // 按钮启用 → 用 DOM.getContentQuads 拿真实可点击坐标(权威方法,自动处理 OSR 缩放),再 Input 点击
    try {
      const rr = await dbg.sendCommand('Runtime.evaluate', {
        expression: '(function(){' + DEEP_JS
          + ' var b=_dall().find(function(e){return e.tagName===\'BUTTON\'&&(e.innerText||\'\').trim()===\'发表\';});'
          + ' if(b){ try{b.scrollIntoView({block:\'center\'});}catch(_){} } return b;})()',
        returnByValue: false,
      });
      const oid = rr && rr.result && rr.result.objectId;
      if (oid) {
        const qd = await dbg.sendCommand('DOM.getContentQuads', { objectId: oid });
        const q = qd && qd.quads && qd.quads[0];
        if (q) {
          const cx = (q[0] + q[2] + q[4] + q[6]) / 4, cy = (q[1] + q[3] + q[5] + q[7]) / 4;
          // 诊断:这个点上到底是不是发表按钮(有没有遮罩拦截)
          try {
            const hit = await evalJS('(function(){var e=document.elementFromPoint(' + Math.round(cx) + ',' + Math.round(cy) + ');'
              + ' if(!e) return "null"; var s=getComputedStyle(e);'
              + ' return e.tagName+"|"+(e.innerText||"").trim().slice(0,8)+"|"+(e.className||"").toString().slice(0,40)+"|z="+s.zIndex+"|pe="+s.pointerEvents;})()');
            log('点位命中元素: ' + hit);
          } catch (e) {}
          log('发表按钮真实坐标(quads) ' + Math.round(cx) + ',' + Math.round(cy) + ' 点击');
          // 坐标点击(移到点上→按→抬,带小延时,模拟真人)
          await dbg.sendCommand('Input.dispatchMouseEvent', { type: 'mouseMoved', x: cx, y: cy });
          await sleep(30);
          await dbg.sendCommand('Input.dispatchMouseEvent', { type: 'mousePressed', x: cx, y: cy, button: 'left', buttons: 1, clickCount: 1 });
          await sleep(60);
          await dbg.sendCommand('Input.dispatchMouseEvent', { type: 'mouseReleased', x: cx, y: cy, button: 'left', buttons: 0, clickCount: 1 });
          // 兜底:直接在 shadow 里对按钮派发完整鼠标事件序列(wujie shadow 里坐标点击可能送不达)
          await sleep(100);
          await evalJS('(function(){' + DEEP_JS
            + ' var b=_dall().find(function(e){return e.tagName===\'BUTTON\'&&(e.innerText||\'\').trim()===\'发表\';});'
            + ' if(!b) return false; var r=b.getBoundingClientRect(); var cx=r.left+r.width/2, cy=r.top+r.height/2;'
            + ' var o={bubbles:true,cancelable:true,view:window,clientX:cx,clientY:cy,button:0};'
            + ' [\'pointerover\',\'pointerenter\',\'mouseover\',\'pointerdown\',\'mousedown\',\'pointerup\',\'mouseup\',\'click\'].forEach(function(t){'
            + '   try{ b.dispatchEvent(new (t.indexOf(\'pointer\')===0?PointerEvent:MouseEvent)(t,o)); }catch(e){} });'
            + ' try{ b.click(); }catch(e){} return true;})()');
        } else { log('getContentQuads 无 quad'); }
      } else { log('拿不到发表按钮 objectId'); }
    } catch (e) { log('quad click err ' + e); }
    await sleep(1800);
    // 【诊断】点完发表后 dump 页面可见按钮+弹窗文字,看到底出了什么
    try {
      const dump = await evalJS('(function(){' + DEEP_JS
        + ' var btns=_dall().filter(function(e){return e.tagName===\'BUTTON\';}).map(function(e){var r=e.getBoundingClientRect();'
        + '   return {t:(e.innerText||\'\').trim().slice(0,10),c:(e.className||\'\').toString().slice(0,40),w:Math.round(r.width),x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2)};})'
        + '   .filter(function(b){return b.w>0;});'
        + ' var dlg=_dall().filter(function(e){var c=(e.className||\'\').toString();return /dialog|modal|toast|tips|weui-desktop-dialog/.test(c);})'
        + '   .map(function(e){var r=e.getBoundingClientRect();return {c:(e.className||\'\').toString().slice(0,40),t:(e.innerText||\'\').trim().slice(0,60),w:Math.round(r.width)};}).filter(function(d){return d.w>50;});'
        + ' return {btns:btns.slice(0,15),dlg:dlg.slice(0,6)};})()');
      log('DUMP after 发表: ' + JSON.stringify(dump));
    } catch (e) { log('dump err ' + e); }
    // 二次确认框：找它的主按钮(确定/确认/发表),滚进视口,可信点击
    try {
      const cf = await evalJS('(function(){' + DEEP_JS
        + ' var btns=_dall().filter(function(e){ if(e.tagName!==\'BUTTON\') return false;'
        + '   var r=e.getBoundingClientRect(); if(r.width<=0||r.height<=0) return false;'   // 必须可见(有宽高),排除隐藏模板按钮
        + '   var s=getComputedStyle(e); if(s.display===\'none\'||s.visibility===\'hidden\'||parseFloat(s.opacity)===0) return false;'
        + '   return true; });'
        + ' var b=btns.find(function(e){var t=(e.innerText||\'\').trim();var c=(e.className||\'\').toString();'
        + '   return /^(确定|确认|发表|继续发表|确认发表)$/.test(t)&&/primary|confirm|btn_primary/.test(c);});'
        + ' if(!b){ b=btns.find(function(e){var t=(e.innerText||\'\').trim(); return /^(确定|确认)$/.test(t);}); }'
        + ' if(!b) return null; try{b.scrollIntoView({block:\'center\'});}catch(_){}'
        + ' var r=b.getBoundingClientRect(); return {t:(b.innerText||\'\').trim(),x:r.left+r.width/2,y:r.top+r.height/2};})()');
      if (cf && (cf.x !== bs.x || cf.y !== bs.y)) {   // 确认框按钮和发表按钮不同位置才点(否则是同一个,没有确认框)
        log('确认框主按钮=' + JSON.stringify(cf) + ' 可信点击');
        await trustedClick(cf.x, cf.y);
      } else {
        log('无二次确认框(直接发表)');
      }
    } catch (e) { log('confirm err ' + e); }
    for (let k = 0; k < 10 && !sig.create_done; k++) await sleep(1000);
  }

  let ok = false, msg = '';
  if (sig.create_done) {
    ok = sig.create_ok;
    msg = ok ? '发表成功' : ('视频号拒绝发布：' + (sig.msg || ''));
  } else {
    msg = '点了发表但未收到发布响应（超时）';
  }
  cleanup();
  return { ok, msg };
}

// ============ Electron 扫码登录（照抄小V猫：登录直接发生在账号分区里，会话完整）============
// 打开可见窗口让用户扫码，登录态(cookie+localStorage+IndexedDB)全落进 persist:ch_<aid> 分区。
// 之后 publishOne 用同一分区=天生有活会话,不用注入、不跳登录。成功后把 cookie 同步回
// data/channels/<aid>.json 供 Python 记录/在线判断。
async function electronLogin(aid, onStatus) {
  const st = (m) => { try { onStatus && onStatus(m); } catch (e) {} log('LOGIN ' + m); };
  const part = 'persist:ch_' + aid;
  const ses = session.fromPartition(part);
  try { ses.setUserAgent(UA); } catch (e) {}     // 正常Chrome UA,否则视频号二维码接口拒绝
  // 清空分区旧数据(可能有半登录的脏 cookie 卡住二维码)→ 强制全新扫码
  try { await ses.clearStorageData(); log('cleared partition before login'); } catch (e) {}
  const win = new BrowserWindow({
    show: true, width: 1000, height: 760, title: '扫码登录视频号（' + aid + '）',
    alwaysOnTop: true, center: true,
    webPreferences: { partition: part },
  });
  st('打开视频号登录页，请用微信扫码…');
  try { win.setAlwaysOnTop(true, 'screen-saver'); win.moveTop(); win.focus(); win.show(); } catch (e) {}
  win.loadURL('https://channels.weixin.qq.com/').catch(() => {});
  let closed = false;
  win.on('closed', () => { closed = true; });
  try {
    for (let i = 0; i < 150; i++) {   // 最多等 5 分钟
      await sleep(2000);
      if (closed) return { ok: false, msg: '登录窗口被关闭' };
      let cks = [];
      try { cks = await ses.cookies.get({ url: 'https://channels.weixin.qq.com/' }); } catch (e) {}
      const hasSession = cks.some((c) => c.name === 'sessionid' && c.value && c.value.length > 8);
      let url = '';
      try { url = win.webContents.getURL() || ''; } catch (e) {}
      if (hasSession && url.indexOf('login') < 0 && url.indexOf('channels.weixin.qq.com/platform') >= 0) {
        st('登录成功，保存会话…');
        await syncCookiesToJson(aid, ses);
        try { win.close(); } catch (e) {}
        return { ok: true };
      }
    }
  } finally {
    try { if (!closed) win.close(); } catch (e) {}
  }
  return { ok: false, msg: '超时未检测到登录（5分钟内没扫码或没登录成功）' };
}

// 把分区里 weixin 域的 cookie 存回 data/channels/<aid>.json（供 Python 在线判断/记录；发布仍用分区）
async function syncCookiesToJson(aid, ses) {
  try {
    const all = await ses.cookies.get({});
    const cks = all.filter((c) => (c.domain || '').includes('weixin.qq.com')).map((c) => ({
      name: c.name, value: c.value, domain: c.domain, path: c.path || '/',
      secure: !!c.secure, httpOnly: !!c.httpOnly,
      sameSite: c.sameSite === 'strict' ? 'Strict' : (c.sameSite === 'lax' ? 'Lax' : 'None'),
      expires: (c.expirationDate && c.expirationDate > 0) ? c.expirationDate : -1,
    }));
    const f = _chJson(aid);
    try { fs.mkdirSync(path.dirname(f), { recursive: true }); } catch (e) {}
    let data = {};
    try { data = JSON.parse(fs.readFileSync(f, 'utf-8')); } catch (e) {}
    data.cookies = cks;
    data.origins = data.origins || [{ origin: 'https://channels.weixin.qq.com', localStorage: [] }];
    fs.writeFileSync(f, JSON.stringify(data), 'utf-8');
    log('synced ' + cks.length + ' cookies to json');
  } catch (e) { log('syncCookies err ' + e); }
}

// 检查某账号分区是否仍在线(有 sessionid + 打开 platform 不跳 login)
async function electronCheckLogin(aid) {
  const part = 'persist:ch_' + aid;
  const ses = session.fromPartition(part);
  let cks = [];
  try { cks = await ses.cookies.get({ url: 'https://channels.weixin.qq.com/' }); } catch (e) {}
  return cks.some((c) => c.name === 'sessionid' && c.value && c.value.length > 8);
}

// ============ 导出鉴权材料：cookies + localStorage(设备指纹) + uin,用于测试纯Python直调 mmfinderassistant ============
async function dumpAuthMat(params, onStatus) {
  const st = (m) => { try { onStatus && onStatus(m); } catch (e) {} log('AUTH ' + m); };
  const { aid } = params;
  const part = 'persist:ch_' + aid;
  const ses = session.fromPartition(part);
  try { ses.setUserAgent(UA); } catch (e) {}
  // 尽力从 json 补种 cookie(找不到不致命)——真正的活登录本来就在 webview 分区里(用户扫码登录处),
  // 之前这里读不到 json 就抛"账号未登录"、把整个取会话搞挂 = 打包机发布卡死的根因。
  try { await injectCookies(part, aid); } catch (e) { st('injectCookies跳过(用分区活会话): ' + String(e).slice(0, 40)); }
  // cookies(全量)——直接取分区里的(扫码登录后就有)
  let cookies = [];
  try { cookies = (await ses.cookies.get({ domain: 'weixin.qq.com' })).map((c) => ({ name: c.name, value: c.value, domain: c.domain, path: c.path })); } catch (e) {}
  // localStorage 需要加载一个同源页面才能读
  const win = new BrowserWindow({ show: false, webPreferences: { partition: part, backgroundThrottling: false, sandbox: false } });
  const wc = win.webContents;
  const cleanup = () => { try { win.destroy(); } catch (e) {} };
  let ls = {};
  try {
    wc.loadURL('https://channels.weixin.qq.com/platform').catch(() => {});
    await new Promise((res) => { let d = false; const f = () => { if (!d) { d = true; res(); } }; wc.once('dom-ready', f); setTimeout(f, 12000); });
    await sleep(1500);
    ls = await wc.executeJavaScript('(function(){var o={};for(var i=0;i<localStorage.length;i++){var k=localStorage.key(i);if(/finger|device|uin|token|_did|fpd/i.test(k))o[k]=localStorage.getItem(k);}return o;})()').catch(() => ({}));
  } catch (e) { st('ls读取失败:' + e); }
  cleanup();
  st('导出完成 cookies=' + cookies.length + ' lsKeys=' + Object.keys(ls || {}).length);
  // 分区里没有有效 sessionid = 这个号在这台机器上没真正登录过(或已失效)→ 给个清楚提示,别再含糊报"账号未登录"
  const hasSession = cookies.some((c) => c.name === 'sessionid' && c.value && c.value.length > 8);
  if (!hasSession) {
    return { ok: false, msg: '该账号在本机没有有效登录态,请在软件里重新扫码登录这个视频号后再发布', cookies, localStorage: ls };
  }
  return { ok: true, cookies, localStorage: ls };
}

// ============ 抠SDK：打开发表页,用 CDP Debugger 把含上传逻辑(uploadpartdfs/preuploadvideo/UploadID)============
// 的脚本源码 dump 到 sdk_dump/,作为后端逆向复刻上传签名的权威依据。不上传、不发布。
async function dumpUploadSdk(params, onStatus) {
  const st = (m) => { try { onStatus && onStatus(m); } catch (e) {} log('SDK ' + m); };
  const { aid } = params;
  const part = 'persist:ch_' + aid;
  const outDir = path.join(__dirname, 'sdk_dump');
  try { fs.mkdirSync(outDir, { recursive: true }); } catch (e) {}
  try { session.fromPartition(part).setUserAgent(UA); } catch (e) {}
  await injectCookies(part, aid);
  const win = new BrowserWindow({ show: false, width: 1280, height: 1000, skipTaskbar: true,
    webPreferences: { partition: part, backgroundThrottling: false, sandbox: false } });
  const wc = win.webContents;
  try { wc.setAudioMuted(true); } catch (e) {}
  try { const b = screen.getPrimaryDisplay().bounds; win.setPosition(b.x + b.width - 1, b.y + b.height - 1); win.showInactive(); } catch (e) {}
  const dbg = wc.debugger;
  const cleanup = () => { try { win.destroy(); } catch (e) {} };
  try { dbg.attach('1.3'); } catch (e) { cleanup(); return { ok: false, msg: 'attach失败:' + e }; }

  // 用 Network 抓所有 .js 响应体(不碰 Debugger,避免把重型SPA挂死)
  const KEYS = ['uploadpartdfs', 'preuploadvideo', 'UploadID', 'X-Arguments', 'getUploadNode', 'MultipartUpload', 'Content-MD5', 'filekey'];
  const jsReqs = new Map();   // requestId -> url
  const idx = [];
  let hitN = 0;
  dbg.on('message', (_e, method, p) => {
    try {
      if (method === 'Network.responseReceived') {
        const u = (p.response && p.response.url) || '';
        const mime = (p.response && p.response.mimeType) || '';
        if (/\.js(\?|$)/i.test(u) || /javascript/i.test(mime)) jsReqs.set(p.requestId, u);
      } else if (method === 'Network.loadingFinished') {
        if (!jsReqs.has(p.requestId)) return;
        const surl = jsReqs.get(p.requestId);
        dbg.sendCommand('Network.getResponseBody', { requestId: p.requestId }).then((r) => {
          const src = (r && r.body) || '';
          if (!src || (r && r.base64Encoded)) return;
          const hits = KEYS.filter((k) => src.indexOf(k) >= 0);
          idx.push({ url: surl, len: src.length, hits });
          if (hits.length >= 2) {
            hitN++;
            const fn = 'sdk_' + hitN + '_' + ((surl.split('?')[0].split('/').pop()) || ('s' + hitN)).replace(/[^\w.\-]/g, '_').slice(0, 40);
            try { fs.writeFileSync(path.join(outDir, fn), '// URL: ' + surl + '\n// hits: ' + hits.join(',') + '\n' + src, 'utf-8'); } catch (e) {}
            log('SDK 命中脚本 → ' + fn + ' (' + hits.join(',') + ', ' + src.length + '字节)');
          }
        }).catch(() => {});
      }
    } catch (e) {}
  });

  st('打开发表页,抓所有.js…');
  try { await dbg.sendCommand('Network.enable'); } catch (e) {}
  wc.loadURL(CREATE_URL).catch(() => {});
  await new Promise((res) => { let d = false; const f = () => { if (!d) { d = true; res(); } }; wc.once('dom-ready', f); setTimeout(f, 12000); });
  await sleep(10000);   // 等异步chunk都加载完 + getResponseBody 回调落盘
  const url = wc.getURL() || '';
  if (url.toLowerCase().indexOf('login') >= 0) { cleanup(); return { ok: false, msg: '登录失效' }; }
  await sleep(3000);
  try { fs.writeFileSync(path.join(outDir, '_index.json'), JSON.stringify(idx, null, 1), 'utf-8'); } catch (e) {}
  cleanup();
  st('抠SDK完成:命中 ' + hitN + ' 个脚本(共扫 ' + idx.length + '个js)');
  return { ok: hitN > 0, msg: 'dumped ' + hitN + ' scripts of ' + idx.length };
}

// ============ 抓包模式：只丢文件触发上传,全量记录 preupload/分片/post_clip 的请求+响应到 ============
// capture.jsonl。目的=照真实报文复刻"纯API上传"(小V猫方式)。不填标题、不点发表、不发布——零风险。
// params: {aid, video_path}。可选 params.publish=true 时才走到发表并抓 post_create(需用户授权)。
async function captureFlow(params, onStatus) {
  const st = (m) => { try { onStatus && onStatus(m); } catch (e) {} log('CAP ' + m); };
  const { aid, video_path } = params;
  const part = 'persist:ch_' + aid;
  const capFile = path.join(__dirname, 'capture.jsonl');
  const cap = (o) => { try { fs.appendFileSync(capFile, JSON.stringify(o) + '\n'); } catch (e) {} };
  try { fs.writeFileSync(capFile, ''); } catch (e) {}   // 清空上次
  try { const sz = fs.statSync(video_path).size; if (sz < 10240) return { ok: false, msg: '视频空' }; }
  catch (e) { return { ok: false, msg: '视频不存在:' + video_path }; }

  const finish = !!params.finish;   // finish=true:上传后填标题+点发表,拦下 post_create 记全请求体再中止(不发布)
  try { session.fromPartition(part).setUserAgent(UA); } catch (e) {}
  await injectCookies(part, aid);
  const win = new BrowserWindow({
    show: finish, width: 1280, height: 1000, skipTaskbar: !finish,
    webPreferences: { partition: part, backgroundThrottling: false, sandbox: false },
  });
  const wc = win.webContents;
  try { wc.setAudioMuted(true); } catch (e) {}
  if (finish) {
    try { win.center(); win.show(); win.focus(); } catch (e) {}   // 可见+聚焦:自动点不中时你能手点发表
  } else {
    try { const b = screen.getPrimaryDisplay().bounds; win.setPosition(b.x + b.width - 1, b.y + b.height - 1); win.showInactive(); } catch (e) {}
  }
  const dbg = wc.debugger;
  const cleanup = () => { try { win.destroy(); } catch (e) {} };
  try { dbg.attach('1.3'); } catch (e) { cleanup(); return { ok: false, msg: 'attach失败:' + e }; }
  let pcCaptured = false;   // 是否已抓到 post_create 请求体
  const evalJS = async (expr) => {
    try { const r = await dbg.sendCommand('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true }); return r && r.result ? r.result.value : undefined; }
    catch (e) { return undefined; }
  };

  // 目标URL:所有 CDN 上传(video.qq.com:applyuploaddfs/uploadpartdfs/completepartuploaddfs/simpleuploaddfs) + 所有 mmfinderassistant 接口
  const TARGET = /(video\.qq\.com|mmfinderassistant|uploaddfs|post_clip|getimageupload|composefinderfeed)/i;
  const reqMeta = new Map();   // requestId -> {url, method}
  let clipReady = false;
  let pdSeen = false;   // 抓到 post_draft 请求(存草稿模式)
  dbg.on('message', (_e, method, p) => {
    try {
      if (method === 'Network.requestWillBeSent') {
        const u = (p.request && p.request.url) || '';
        if (!TARGET.test(u)) return;
        if (u.indexOf('/post/post_draft') >= 0) pdSeen = true;
        reqMeta.set(p.requestId, { url: u, method: p.request.method });
        const hdrs = p.request.headers || {};
        const clen = parseInt(hdrs['Content-Length'] || hdrs['content-length'] || '0', 10);
        const ctype = (hdrs['Content-Type'] || hdrs['content-type'] || '').toLowerCase();
        const isBinary = clen > 100000 || /octet-stream|video\/|image\/|multipart\/form-data/.test(ctype);
        if (isBinary) {
          // 分片二进制体不抓(129MB),只留 headers——签名(Authorization/x-cos-*等)全在 header 里
          cap({ t: new Date().toISOString(), phase: 'req', reqId: p.requestId, url: u, method: p.request.method, headers: hdrs, body: '', note: 'BINARY skipped clen=' + clen + ' ctype=' + ctype });
        } else {
          dbg.sendCommand('Network.getRequestPostData', { requestId: p.requestId }).then((d) => {
            let body = (d && d.postData) || (p.request && p.request.postData) || '';
            let note = '';
            if (body && body.length > 6000) { note = 'TRUNCATED len=' + body.length; body = body.slice(0, 6000); }
            cap({ t: new Date().toISOString(), phase: 'req', reqId: p.requestId, url: u, method: p.request.method, headers: hdrs, hasPostData: !!p.request.hasPostData, body, note });
          }).catch(() => {
            cap({ t: new Date().toISOString(), phase: 'req', reqId: p.requestId, url: u, method: p.request.method, headers: hdrs, body: (p.request.postData || '').slice(0, 6000), note: 'nopostdata' });
          });
        }
      } else if (method === 'Network.responseReceived') {
        const u = (p.response && p.response.url) || '';
        if (u.indexOf('post_clip_video') >= 0) clipReady = true;
        if (!reqMeta.has(p.requestId) && !TARGET.test(u)) return;
        cap({ t: new Date().toISOString(), phase: 'resp-head', reqId: p.requestId, url: u, status: p.response.status, mime: p.response.mimeType, headers: p.response.headers });
      } else if (method === 'Network.loadingFinished') {
        if (!reqMeta.has(p.requestId)) return;
        const meta = reqMeta.get(p.requestId);
        dbg.sendCommand('Network.getResponseBody', { requestId: p.requestId }).then((r) => {
          if (r && r.base64Encoded) { cap({ t: new Date().toISOString(), phase: 'resp-body', reqId: p.requestId, url: meta.url, base64: true, note: 'binary resp skipped' }); return; }
          let body = (r && r.body) || '';
          let note = '';
          if (body.length > 12000) { note = 'TRUNCATED len=' + body.length; body = body.slice(0, 12000); }
          cap({ t: new Date().toISOString(), phase: 'resp-body', reqId: p.requestId, url: meta.url, body, note });
        }).catch((e) => cap({ t: new Date().toISOString(), phase: 'resp-body', reqId: p.requestId, url: meta.url, err: String(e).slice(0, 80) }));
      } else if (method === 'Fetch.requestPaused') {
        // finish模式:拦到 post_create 的 Request → 记全请求体到 capture → 中止(Failed),不真发布
        const u = (p.request && p.request.url) || '';
        if (u.indexOf('/post/post_create') >= 0 && u.indexOf('micro/content') < 0) {
          const raw = (p.request && p.request.postData) || '';
          if (raw) {
            cap({ t: new Date().toISOString(), phase: 'POST_CREATE_BODY', url: u, headers: p.request.headers, body: raw });
            pcCaptured = true;
            log('CAP 抓到 post_create 请求体 len=' + raw.length + ' → 中止(不发布)');
            dbg.sendCommand('Fetch.failRequest', { requestId: p.requestId, errorReason: 'Aborted' }).catch(() => {});
          } else {
            // 没postData(极少见)→ 记下头后直接中止
            cap({ t: new Date().toISOString(), phase: 'POST_CREATE_BODY', url: u, headers: p.request.headers, body: '', note: 'no postData on pause' });
            pcCaptured = true;
            dbg.sendCommand('Fetch.failRequest', { requestId: p.requestId, errorReason: 'Aborted' }).catch(() => {});
          }
        } else {
          dbg.sendCommand('Fetch.continueRequest', { requestId: p.requestId }).catch(() => {});
        }
      }
    } catch (err) {}
  });

  st('打开发表页(抓包)');
  wc.loadURL(CREATE_URL).catch(() => {});
  await new Promise((res) => { let d = false; const f = () => { if (!d) { d = true; res(); } }; wc.once('dom-ready', f); setTimeout(f, 10000); });
  await sleep(1500);
  try {
    await dbg.sendCommand('Network.enable'); await dbg.sendCommand('Runtime.enable'); await dbg.sendCommand('DOM.enable');
    if (finish) { await dbg.sendCommand('Fetch.enable', { patterns: [{ urlPattern: '*post/post_create*', requestStage: 'Request' }] }); log('CAP Fetch拦截post_create已开'); }
  } catch (e) {}
  const url = wc.getURL() || '';
  if (url.toLowerCase().indexOf('login') >= 0) { cleanup(); return { ok: false, msg: '登录失效,请重新扫码' }; }

  st('丢视频触发上传…');
  let setOk = false;
  for (let i = 0; i < 25 && !setOk; i++) {
    try {
      const r = await dbg.sendCommand('Runtime.evaluate', {
        returnByValue: false,
        expression: '(function(){' + DEEP_JS + ' var a=_dqa(\'input[type=file]\'); return a.find(function(x){return (x.accept||\'\').indexOf(\'video\')>=0;})||a[0]||null;})()',
      });
      const oid = r && r.result && r.result.objectId;
      if (oid) { await dbg.sendCommand('DOM.setFileInputFiles', { files: [video_path], objectId: oid }); setOk = true; break; }
    } catch (e) {}
    await sleep(1000);
  }
  if (!setOk) { cleanup(); return { ok: false, msg: '没找到上传输入框' }; }
  st('上传中,抓包(约2-3分钟)…');
  for (let w = 0; w < 150 && !clipReady; w++) await sleep(2000);
  await sleep(8000);   // clip完成后多抓8秒,把登记响应抓全

  if (finish && clipReady) {
    const savedraft = !!params.savedraft;   // true=点“保存草稿”抓 post_draft(不发布);false=点“发表”抓 post_create(中止)
    const btn = savedraft ? '保存草稿' : '发表';
    const done = () => savedraft ? pdSeen : pcCaptured;
    // 填个占位标题
    st('填标题…');
    try {
      const rect = await evalJS('(function(){' + DEEP_JS
        + ' var ed=document.querySelector(\'.text-editor-content\')||document.querySelector(\'div.input-editor\');'
        + ' if(!ed){var a=_dqa(\'.text-editor-content,div.input-editor\');ed=a[0];}'
        + ' if(!ed) return null; ed.focus(); var r=ed.getBoundingClientRect(); return {x:r.left+8,y:r.top+8};})()');
      if (rect) {
        await dbg.sendCommand('Input.dispatchMouseEvent', { type: 'mousePressed', x: rect.x, y: rect.y, button: 'left', clickCount: 1 });
        await dbg.sendCommand('Input.dispatchMouseEvent', { type: 'mouseReleased', x: rect.x, y: rect.y, button: 'left', clickCount: 1 });
        await sleep(200);
        await dbg.sendCommand('Input.insertText', { text: params.title || '抓包占位标题' });
      }
    } catch (e) { log('CAP fill err ' + e); }
    const findExpr = '(function(){' + DEEP_JS
      + ' var b=_dall().find(function(e){return e.tagName===\'BUTTON\'&&(e.innerText||\'\').trim()===\'' + btn + '\';});'
      + ' if(!b) return null; var dis=!!b.disabled||b.getAttribute(\'aria-disabled\')===\'true\'||/disable/.test((b.className||\'\')+\'\');'
      + ' if(dis) return null; try{b.scrollIntoView({block:\'center\'});}catch(_){} return b;})()';
    st('等“' + btn + '”按钮就绪并自动点(点不动就请手动点“' + btn + '”)…');
    for (let k = 0; k < 8 && !done(); k++) {
      try {
        const rr = await dbg.sendCommand('Runtime.evaluate', { expression: findExpr, returnByValue: false });
        const oid = rr && rr.result && rr.result.objectId;
        if (oid) {
          const qd = await dbg.sendCommand('DOM.getContentQuads', { objectId: oid });
          const q = qd && qd.quads && qd.quads[0];
          if (q) {
            const cx = (q[0] + q[2] + q[4] + q[6]) / 4, cy = (q[1] + q[3] + q[5] + q[7]) / 4;
            await dbg.sendCommand('Input.dispatchMouseEvent', { type: 'mouseMoved', x: cx, y: cy });
            await sleep(30);
            await dbg.sendCommand('Input.dispatchMouseEvent', { type: 'mousePressed', x: cx, y: cy, button: 'left', buttons: 1, clickCount: 1 });
            await sleep(60);
            await dbg.sendCommand('Input.dispatchMouseEvent', { type: 'mouseReleased', x: cx, y: cy, button: 'left', buttons: 0, clickCount: 1 });
            await evalJS('(function(){' + DEEP_JS
              + ' var b=_dall().find(function(e){return e.tagName===\'BUTTON\'&&(e.innerText||\'\').trim()===\'' + btn + '\';});'
              + ' if(!b) return; try{b.click();}catch(_){}})()');
          }
        }
      } catch (e) { log('CAP click err ' + e); }
      if (done()) break;
      await sleep(3000);
    }
    if (!done()) { st('自动点未触发,请在弹出的视频号窗口手动点一次“' + btn + '”…'); }
    for (let k = 0; k < 100 && !done(); k++) await sleep(3000);
    await sleep(2000);   // 多抓2秒把 post_draft 响应也落盘
    cleanup();
    const okd = done();
    st(okd ? ('✅ 已抓到 ' + (savedraft ? 'post_draft(草稿已存)' : 'post_create(未发布)')) : ('❌ 没抓到(' + btn + '没触发)'));
    return { ok: okd, msg: okd ? (savedraft ? 'post_draft captured' : 'post_create captured (not published)') : (btn + ' NOT triggered') };
  }

  cleanup();
  st(clipReady ? '抓包完成:上传登记成功' : '抓包结束:未见clip登记(仍已记录前序请求)');
  return { ok: true, msg: 'capture done clipReady=' + clipReady };
}

module.exports = { publishOne, electronLogin, electronCheckLogin, captureFlow, dumpUploadSdk, dumpAuthMat };
