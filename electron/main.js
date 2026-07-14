// 爆款监控 Electron 外壳：拉起 Python 后端 + 主窗口 + <webview> 内嵌视频号后台（会话隔离 + cookie 注入）
const { app, BrowserWindow, ipcMain, session, dialog } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const net = require('net');
const http = require('http');
const https = require('https');
const os = require('os');
const { publishOne, electronLogin, electronCheckLogin, captureFlow, dumpUploadSdk, dumpAuthMat } = require('./publish');

const ROOT = path.join(__dirname, '..');   // 项目根（app.py 所在）
const PORT = 8790;
const PUB_PORT = 8791;                      // Electron 原生发布服务端口（Python 后端调它）
let backend = null;
let win = null;

// ============ 原生发布 HTTP 服务：Python 上传 worker POST /publish 过来，Electron 用隐藏
// webContents(不节流)驱动发布，跑完返回 {ok,msg}。这就是"照抄小V猫"的落地入口。============
function startPublishServer() {
  const srv = http.createServer((req, res) => {
    if (req.method === 'POST' && req.url === '/publish') {
      let buf = '';
      req.on('data', (d) => { buf += d; });
      req.on('end', async () => {
        let params;
        try { params = JSON.parse(buf); } catch (e) {
          res.writeHead(400); res.end(JSON.stringify({ ok: false, msg: 'bad json' })); return;
        }
        try {
          const r = await publishOne(params, (m) => {
            // 进度回传给 Python(可选,失败不影响)
            try {
              const body = Buffer.from(JSON.stringify({ tid: params.tid, stage: m }));
              const rq = http.request({ host: '127.0.0.1', port: PORT, path: '/api/channels/pubprogress', method: 'POST',
                headers: { 'content-type': 'application/json', 'content-length': body.length } });
              rq.on('error', () => {}); rq.write(body); rq.end();
            } catch (e) {}
          });
          res.writeHead(200, { 'content-type': 'application/json' });
          res.end(JSON.stringify(r));
        } catch (e) {
          res.writeHead(200, { 'content-type': 'application/json' });
          res.end(JSON.stringify({ ok: false, msg: 'publish异常: ' + String(e).slice(0, 120) }));
        }
      });
    } else if (req.method === 'POST' && req.url === '/capture') {
      // 抓包模式:只丢文件触发上传,把 preupload/分片/post_clip 报文记录到 capture.jsonl(零发布风险)
      let buf = ''; req.on('data', (d) => { buf += d; });
      req.on('end', async () => {
        let params; try { params = JSON.parse(buf); } catch (e) { res.writeHead(400); res.end('{}'); return; }
        try {
          const r = await captureFlow(params, () => {});
          res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify(r));
        } catch (e) { res.writeHead(200); res.end(JSON.stringify({ ok: false, msg: String(e).slice(0, 160) })); }
      });
    } else if (req.method === 'POST' && req.url === '/authmat') {
      let buf = ''; req.on('data', (d) => { buf += d; });
      req.on('end', async () => {
        let params; try { params = JSON.parse(buf); } catch (e) { res.writeHead(400); res.end('{}'); return; }
        try { const r = await dumpAuthMat(params, () => {}); res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify(r)); }
        catch (e) { res.writeHead(200); res.end(JSON.stringify({ ok: false, msg: String(e).slice(0, 160) })); }
      });
    } else if (req.method === 'POST' && req.url === '/dumpsdk') {
      // 抠上传SDK源码到 sdk_dump/(逆向依据,不上传不发布)
      let buf = ''; req.on('data', (d) => { buf += d; });
      req.on('end', async () => {
        let params; try { params = JSON.parse(buf); } catch (e) { res.writeHead(400); res.end('{}'); return; }
        try { const r = await dumpUploadSdk(params, () => {}); res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify(r)); }
        catch (e) { res.writeHead(200); res.end(JSON.stringify({ ok: false, msg: String(e).slice(0, 160) })); }
      });
    } else if (req.method === 'POST' && req.url === '/login') {
      let buf = ''; req.on('data', (d) => { buf += d; });
      req.on('end', async () => {
        let params; try { params = JSON.parse(buf); } catch (e) { res.writeHead(400); res.end('{}'); return; }
        try {
          const r = await electronLogin(params.aid, () => {});
          res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify(r));
        } catch (e) { res.writeHead(200); res.end(JSON.stringify({ ok: false, msg: String(e).slice(0, 120) })); }
      });
    } else if (req.method === 'GET' && req.url.startsWith('/checklogin')) {
      const aid = new URL(req.url, 'http://x').searchParams.get('aid') || '';
      electronCheckLogin(aid).then((online) => {
        res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify({ online }));
      }).catch(() => { res.writeHead(200); res.end(JSON.stringify({ online: false })); });
    } else if (req.url === '/ping') {
      res.writeHead(200); res.end('ok');
    } else {
      res.writeHead(404); res.end();
    }
  });
  // 端口被占（比如已有一个实例在跑）不要让主进程崩——优雅忽略即可
  srv.on('error', (e) => console.log('[pub] 发布服务端口占用/出错(已忽略，不崩溃):', String(e).slice(0, 100)));
  srv.listen(PUB_PORT, '127.0.0.1', () => console.log('[pub] 原生发布服务 :' + PUB_PORT));
}

function waitPort(port, cb, tries = 80) {
  const s = net.connect({ port, host: '127.0.0.1' });
  s.on('connect', () => { s.destroy(); cb(true); });
  s.on('error', () => {
    s.destroy();
    if (tries <= 0) return cb(false);
    setTimeout(() => waitPort(port, cb, tries - 1), 400);
  });
}

// 只负责 spawn 后端进程(存到模块变量 backend)。启动 + 差量更新重启都复用它。
function _spawnBackend() {
  // 【关键】强制 UTF-8：否则后端进程会把中文路径(如"7月10日去重模板")编码成"?"，
  // 而"?"是 Windows 非法文件名字符 → 生成草稿时 [Errno 22] Invalid argument
  // BAOKUAN_VER：让后端显示 Electron 外壳的版本(app.getVersion)，界面版本号才和安装包一致
  const env = Object.assign({}, process.env, { PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8', BAOKUAN_VER: _installedVersion() || app.getVersion() });
  let cmd, args, cwd;
  if (app.isPackaged) {
    // 打包后：跑塞进 resources 的无窗口后端 exe（backend.spec 打的 onedir，含自带 Chromium）。
    const be = path.join(process.resourcesPath, 'backend', 'backend.exe');
    cmd = be; args = []; cwd = path.dirname(be);
  } else {
    cmd = process.platform === 'win32' ? 'python' : 'python3';
    args = ['-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', String(PORT)];
    cwd = ROOT;
  }
  backend = spawn(cmd, args, { cwd, windowsHide: true, env });
  backend.stdout.on('data', d => console.log('[py]', d.toString().trim()));
  backend.stderr.on('data', d => console.log('[py]', d.toString().trim()));
  backend.on('error', e => console.log('[py] spawn error', String(e)));
}

function startBackend() {
  waitPort(PORT, (up) => {
    if (up) { createWindow(); return; }          // 已有后端在跑就直接用
    _spawnBackend();
    waitPort(PORT, () => createWindow());
  });
}

// 端口"变空"再回调(和 waitPort 相反)——差量更新停后端后,等 8790 真释放再替换文件。
function _waitPortFree(port, cb, tries = 40) {
  const s = net.connect({ port, host: '127.0.0.1' });
  s.on('connect', () => { s.destroy(); if (tries <= 0) return cb(); setTimeout(() => _waitPortFree(port, cb, tries - 1), 400); });
  s.on('error', () => { s.destroy(); cb(); });
}
// 差量更新用：停后端(kill backend.exe,等端口释放,好替换被占用的文件)
function stopBackendProc() {
  return new Promise((resolve) => {
    try { if (backend && !backend.killed) backend.kill(); } catch (e) {}
    _waitPortFree(PORT, resolve);
  });
}
// 差量更新用：替换完文件后重新拉起后端(等端口起来)
function restartBackendProc() {
  return new Promise((resolve) => { _spawnBackend(); waitPort(PORT, resolve); });
}

function createWindow() {
  win = new BrowserWindow({
    width: 1280, height: 820, minWidth: 960, minHeight: 640,
    title: '爆款监控 · 抖音博主更新雷达',
    backgroundColor: '#f6f7fb',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      webviewTag: true,            // 关键：允许 <webview>（内嵌视频号后台）
      nodeIntegration: false,
      contextIsolation: true,
    },
  });
  win.setMenuBarVisibility(false);
  // 【终极防缓存】清缓存 + 清storage + URL带时间戳唯一化，彻底杜绝旧前端残留。
  const ses = win.webContents.session;
  Promise.allSettled([
    ses.clearCache(),
    ses.clearStorageData({ storages: ['cachestorage', 'serviceworkers', 'shadercache'] }),
  ]).finally(() => {
    win.loadURL(`http://127.0.0.1:${PORT}/?_=${Date.now()}`);   // 时间戳=每次启动都是全新URL
  });
  // 快捷键强制硬刷新：Ctrl+R / F5（忽略缓存）
  win.webContents.on('before-input-event', (e, input) => {
    if ((input.control && input.key.toLowerCase() === 'r') || input.key === 'F5') {
      win.webContents.reloadIgnoringCache();
      e.preventDefault();
    }
  });
  // 打包安装版：启动几秒后自动检测更新（源码 dev 不自更新）
  if (app.isPackaged) {
    setTimeout(() => { checkUpdateOnStartup().catch((e) => console.log('[upd]', String(e))); }, 4000);
  }
}

// 把某账号的 cookie（Playwright storage_state 文件）注入到它专属的 partition 会话
ipcMain.handle('inject-cookies', async (_e, aid) => {
  const f = path.join(ROOT, 'data', 'channels', aid + '.json');
  if (!fs.existsSync(f)) return { ok: false, error: '账号未登录' };
  let data;
  try { data = JSON.parse(fs.readFileSync(f, 'utf-8')); }
  catch (e) { return { ok: false, error: '读 cookie 失败' }; }
  const part = 'persist:ch_' + aid;
  const ses = session.fromPartition(part);
  let n = 0;
  const errs = [];
  for (const c of (data.cookies || [])) {
    const bareDomain = (c.domain || '').replace(/^\./, '');
    const url = (c.secure ? 'https' : 'http') + '://' + bareDomain + (c.path || '/');
    const ss = String(c.sameSite || '').toLowerCase();
    const sameSite = ss === 'strict' ? 'strict' : (ss === 'lax' ? 'lax' : 'no_restriction');
    try {
      await ses.cookies.set({
        url, name: c.name, value: c.value, domain: c.domain, path: c.path || '/',
        secure: !!c.secure, httpOnly: !!c.httpOnly, sameSite,
        expirationDate: (c.expires && c.expires > 0) ? c.expires : undefined,
      });
      n++;
    } catch (e) { errs.push(c.name + ':' + String(e).slice(0, 60)); }
  }
  // 设完读回来验证
  let got = [];
  try { got = (await ses.cookies.get({ url: 'https://channels.weixin.qq.com/' })).map(c => c.name); } catch (e) {}
  console.log('[DBG] injectCookies part=' + part + ' 设置' + n + ' 读回=' + JSON.stringify(got) + (errs.length ? ' 错误=' + JSON.stringify(errs) : ''));
  try { fs.appendFileSync(path.join(__dirname, 'dbg.log'), '[DBG] injectCookies 设置' + n + ' 读回=' + JSON.stringify(got) + (errs.length ? ' 错误=' + JSON.stringify(errs) : '') + '\n'); } catch (e) {}
  // 把 channels.weixin.qq.com 的 localStorage 一并返回（视频号登录态靠它 + cookie）
  let ls = [];
  for (const o of (data.origins || [])) {
    if ((o.origin || '').includes('channels.weixin.qq.com')) {
      ls = o.localStorage || [];
      break;
    }
  }
  return { ok: true, partition: part, count: n, localStorage: ls };
});

// 原生文件夹选择框（Electron 无 pywebview 窗口，后端弹不出，改用主进程的原生对话框）
ipcMain.handle('pick-folder', async () => {
  try {
    const r = await dialog.showOpenDialog(win, { title: '选择文件夹', properties: ['openDirectory'] });
    if (r.canceled || !r.filePaths || !r.filePaths.length) return { path: '' };
    return { path: r.filePaths[0] };
  } catch (e) { return { path: '', error: String(e) }; }
});

// 取视频文件（多选，可带默认目录）
ipcMain.handle('pick-files', async (_e, defaultPath) => {
  try {
    const opts = {
      title: '选择视频', properties: ['openFile', 'multiSelections'],
      filters: [{ name: '视频', extensions: ['mp4', 'mov', 'mkv', 'avi', 'webm'] }, { name: '所有文件', extensions: ['*'] }],
    };
    if (defaultPath) opts.defaultPath = defaultPath;
    const r = await dialog.showOpenDialog(win, opts);
    if (r.canceled || !r.filePaths || !r.filePaths.length) return { paths: [] };
    return { paths: r.filePaths };
  } catch (e) { return { paths: [], error: String(e) }; }
});

// 取目录并递归扫出里面所有视频（可带默认目录）
const _VIDEO_EXT = new Set(['.mp4', '.mov', '.mkv', '.avi', '.webm']);
function _scanVideos(dir, out) {
  let ents = [];
  try { ents = fs.readdirSync(dir, { withFileTypes: true }); } catch (e) { return; }
  for (const ent of ents) {
    const p = path.join(dir, ent.name);
    if (ent.isDirectory()) _scanVideos(p, out);
    else if (_VIDEO_EXT.has(path.extname(ent.name).toLowerCase()) && !ent.name.endsWith('.part')) out.push(p);
  }
}
ipcMain.handle('pick-video-dir', async (_e, defaultPath) => {
  try {
    const opts = { title: '选择目录', properties: ['openDirectory'] };
    if (defaultPath) opts.defaultPath = defaultPath;
    const r = await dialog.showOpenDialog(win, opts);
    if (r.canceled || !r.filePaths || !r.filePaths.length) return { paths: [] };
    const out = [];
    _scanVideos(r.filePaths[0], out);
    out.sort();
    return { paths: out };
  } catch (e) { return { paths: [], error: String(e) }; }
});

// 清空某账号后台分区的所有存储（cookie/localStorage），脏会话重登用
ipcMain.handle('clear-partition', async (_e, aid) => {
  try {
    const ses = session.fromPartition('persist:ch_' + aid);
    await ses.clearStorageData();
    return { ok: true };
  } catch (e) { return { ok: false, error: String(e) }; }
});

// 读取某账号后台分区里 weixin 域的 cookie（把 webview 登录态同步回 storage_state 文件用）
ipcMain.handle('get-partition-cookies', async (_e, aid) => {
  try {
    const ses = session.fromPartition('persist:ch_' + aid);
    const all = await ses.cookies.get({});
    const cks = all.filter(c => (c.domain || '').includes('weixin.qq.com')).map(c => ({
      name: c.name, value: c.value, domain: c.domain, path: c.path || '/',
      secure: !!c.secure, httpOnly: !!c.httpOnly,
      sameSite: c.sameSite === 'strict' ? 'Strict' : (c.sameSite === 'lax' ? 'Lax' : 'None'),
      expires: (c.expirationDate && c.expirationDate > 0) ? c.expirationDate : -1,
    }));
    return { ok: true, cookies: cks };
  } catch (e) { return { ok: false, error: String(e) }; }
});

ipcMain.on('dbg', (_e, msg) => {
  console.log('[DBG]', msg);
  try { fs.appendFileSync(path.join(__dirname, 'dbg.log'), '[DBG] ' + msg + '\n'); } catch (e) {}
});

// ============ 自动更新（安装版）：每次启动检测 version.json，有新版→下载安装包→运行→退出自己 ============
// 更新清单多源尝试：国内可达的 jsDelivr 镜像优先，再退回 GitHub raw（客户多在国内，raw 常被墙）。
const UPDATE_MANIFEST_URLS = [
  // @latest=jsDelivr 取最新 git tag(tag 不可变、永不卡缓存)——@master 分支缓存常卡在旧版导致客户更新不了,故优先它
  'https://cdn.jsdelivr.net/gh/dangthiphuong2491991-a11y/douyin-baokuan-monitor@latest/version.json',
  'https://cdn.jsdelivr.net/gh/dangthiphuong2491991-a11y/douyin-baokuan-monitor@master/version.json',
  'https://raw.githubusercontent.com/dangthiphuong2491991-a11y/douyin-baokuan-monitor/master/version.json',
];

function _httpGetText(url, redirects = 0) {
  return new Promise((resolve, reject) => {
    if (redirects > 5) return reject(new Error('too many redirects'));
    const lib = url.startsWith('https') ? https : http;
    const req = lib.get(url, { timeout: 15000, headers: { 'User-Agent': 'baokuan-updater' } }, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        res.resume();
        return resolve(_httpGetText(new URL(res.headers.location, url).toString(), redirects + 1));
      }
      if (res.statusCode !== 200) { res.resume(); return reject(new Error('HTTP ' + res.statusCode)); }
      let buf = '';
      res.on('data', (d) => { buf += d; });
      res.on('end', () => resolve(buf));
    });
    req.on('error', reject);
    req.on('timeout', () => req.destroy(new Error('timeout')));
  });
}

// 差量更新会更新 resources/installed_manifest.json 里的 version —— 它才是"当前真实版本"。
// (app.getVersion 读 asar 内 package.json,差量替换不了 asar,会永远停在装包时的版本 → 差量判断失效)
function _installedVersion() {
  try {
    const m = JSON.parse(fs.readFileSync(path.join(process.resourcesPath, 'installed_manifest.json'), 'utf-8'));
    if (m && m.version) return String(m.version);
  } catch (e) {}
  return null;
}

function _verGt(a, b) {
  const pa = String(a).replace(/^v/i, '').split('.').map((n) => parseInt(n, 10) || 0);
  const pb = String(b).replace(/^v/i, '').split('.').map((n) => parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    if ((pa[i] || 0) > (pb[i] || 0)) return true;
    if ((pa[i] || 0) < (pb[i] || 0)) return false;
  }
  return false;
}

async function checkUpdateOnStartup() {
  let info = null;
  for (const u of UPDATE_MANIFEST_URLS) {
    try { info = JSON.parse(await _httpGetText(u)); if (info) break; } catch (e) { console.log('[upd] manifest 失败', u, String(e).slice(0, 60)); }
  }
  if (!info) { console.log('[upd] 拉不到更新信息（网络/被墙）'); return; }
  const cur = _installedVersion() || app.getVersion();   // 差量更新后的真实版本(见 _installedVersion)
  // desktop_* 是 Electron 安装版专用字段（和 pywebview 老版的 version/exe_url 分开，互不干扰）
  const latest = info.desktop_version || info.version;
  if (!_verGt(latest, cur)) { console.log('[upd] 已是最新', cur, 'vs', latest); return; }

  // 整包安装(差量不满足/失败时回退):弹框征询→下整包→静默重装
  const onFullInstaller = async (reason) => {
    console.log('[upd] 整包安装, 原因:', reason);
    const setupUrl = info.desktop_setup_url || info.setup_url;
    if (!setupUrl) return { mode: 'full-skip' };
    const r = await dialog.showMessageBox(win, {
      type: 'info', buttons: ['现在更新', '以后再说'], defaultId: 0, cancelId: 1,
      title: '发现新版本', message: `发现新版本 v${latest}（当前 v${cur}）`,
      detail: (info.desktop_notes || info.notes || '') + '\n\n点“现在更新”会下载安装包并自动重装，你的下载视频/设置/登录都不受影响。',
    });
    if (r.response !== 0) return { mode: 'full-cancel' };
    try { await downloadAndRunInstaller(setupUrl, latest); }
    catch (e) { dialog.showMessageBox(win, { type: 'error', title: '更新失败', message: '自动更新失败', detail: String(e).slice(0, 200) + '\n\n可稍后重启再试，或到发布页手动下载安装。' }); }
    return { mode: 'full' };
  };

  // 差量优先:只下改动的 backend 文件、就地替换、重启后端(不用整包重装)。任何环节不满足→onFullInstaller 兜底。
  try {
    const updater = require('./updater');
    const res = await updater.checkAndApply({
      versionInfo: info, curVersion: cur, verGt: _verGt,
      stopBackend: stopBackendProc, startBackend: restartBackendProc,
      reloadUI: () => { try { win && win.webContents.reloadIgnoringCache(); } catch (e) {} },
      onFullInstaller, log: (m) => console.log('[upd]', m),
    });
    if (res && res.mode === 'diff' && res.files > 0) {
      try { dialog.showMessageBox(win, { type: 'info', title: '更新完成', message: `已增量更新到 v${latest}`, detail: `只下载替换了 ${res.files} 个改动文件，已就地生效，无需重装。` }); } catch (e) {}
    }
  } catch (e) {
    console.log('[upd] 更新器异常 → 整包', String(e));
    await onFullInstaller('updater-exception');
  }
}

function downloadAndRunInstaller(url, ver) {
  return new Promise((resolve, reject) => {
    const dest = path.join(os.tmpdir(), `爆款监控_Setup_${ver}.exe`);
    const pw = new BrowserWindow({
      width: 460, height: 200, frame: false, resizable: false, alwaysOnTop: true,
      backgroundColor: '#12141c', webPreferences: {},
    });
    pw.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(
      `<body style="margin:0;background:#12141c;color:#fff;font:14px system-ui;display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;gap:14px">
      <div id="t">正在下载新版 v${ver} …</div>
      <div style="width:82%;height:12px;background:#333;border-radius:6px;overflow:hidden"><div id="b" style="height:100%;width:0;background:linear-gradient(90deg,#7c6eff,#a78bfa);transition:width .2s"></div></div>
      <div id="p" style="font-size:12px;color:#aaa">0%</div>
      <script>function set(pct,txt){document.getElementById('b').style.width=pct+'%';document.getElementById('p').textContent=txt}</script></body>`));
    const setP = (pct, txt) => { try { pw.webContents.executeJavaScript(`set(${pct},${JSON.stringify(txt)})`); } catch (e) {} };
    const fail = (e) => { try { pw.destroy(); } catch (_) {} reject(e instanceof Error ? e : new Error(String(e))); };

    const doGet = (u, redirects) => {
      if (redirects > 6) return fail(new Error('too many redirects'));
      const lib = u.startsWith('https') ? https : http;
      lib.get(u, { headers: { 'User-Agent': 'baokuan-updater' } }, (res) => {
        if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
          res.resume(); return doGet(new URL(res.headers.location, u).toString(), redirects + 1);
        }
        if (res.statusCode !== 200) { res.resume(); return fail(new Error('HTTP ' + res.statusCode)); }
        const total = parseInt(res.headers['content-length'] || '0', 10);
        let got = 0;
        const f = fs.createWriteStream(dest);
        res.on('data', (d) => {
          got += d.length;
          if (total) setP(Math.floor(got / total * 100), `${(got / 1048576).toFixed(0)} / ${(total / 1048576).toFixed(0)} MB`);
        });
        res.pipe(f);
        f.on('finish', () => f.close(() => {
          try { if (fs.statSync(dest).size < 1000000) return fail(new Error('下载文件异常(过小)，可能网络中断')); } catch (e) { return fail(e); }
          setP(100, '下载完成，正在静默安装并重启…');
          // 更新场景用静默安装 /S（不再弹向导、装到原位置），--force-run 装完自动重启。
          // 首次安装是用户手动双击 setup（那时才走选位置的向导）。
          try { spawn(dest, ['/S', '--force-run'], { detached: true, stdio: 'ignore' }).unref(); } catch (e) { return fail(e); }
          setTimeout(() => { try { pw.destroy(); } catch (_) {} resolve(); app.quit(); }, 1500);
        }));
        f.on('error', fail);
      }).on('error', fail);
    };
    doGet(url, 0);
  });
}

// 【单实例保护】已经有一个在跑就别再开第二个——否则第二个抢 8790/8791 端口会崩
// (EADDRINUSE)。第二次点图标 → 直接把已开的窗口拉到前面，不再启动新进程。
const gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (win) { try { if (win.isMinimized()) win.restore(); win.show(); win.focus(); } catch (e) {} }
  });
  app.whenReady().then(() => { startBackend(); startPublishServer(); });
  app.on('window-all-closed', () => {
    if (backend) { try { backend.kill(); } catch (e) {} }
    app.quit();
  });
}
