// 视频号发布·Electron 原生实现（照抄小V猫）：隐藏 BrowserWindow(backgroundThrottling:false)
// + webContents.debugger(CDP) 驱动上传/填表/发表。无独立浏览器窗口、不被"窗口不可见"节流。
// 逻辑对齐 channels.py 的 upload()，但换成不被节流的 Electron webContents。
const { BrowserWindow, session, screen } = require('electron');
const path = require('path');
const fs = require('fs');

const ROOT = path.join(__dirname, '..');
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
  const f = path.join(ROOT, 'data', 'channels', aid + '.json');
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
    const f = path.join(ROOT, 'data', 'channels', aid + '.json');
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

module.exports = { publishOne, electronLogin, electronCheckLogin };
