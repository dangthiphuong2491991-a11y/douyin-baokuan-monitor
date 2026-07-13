// 爆款监控 Electron 外壳：拉起 Python 后端 + 主窗口 + <webview> 内嵌视频号后台（会话隔离 + cookie 注入）
const { app, BrowserWindow, ipcMain, session, dialog } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const net = require('net');
const http = require('http');
const { publishOne, electronLogin, electronCheckLogin } = require('./publish');

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

function startBackend() {
  waitPort(PORT, (up) => {
    if (up) { createWindow(); return; }          // 已有后端在跑就直接用
    const py = process.platform === 'win32' ? 'python' : 'python3';
    // 【关键】强制 UTF-8：否则后端进程会把中文路径(如"7月10日去重模板")编码成"?"，
    // 而"?"是 Windows 非法文件名字符 → 生成草稿时 [Errno 22] Invalid argument
    const env = Object.assign({}, process.env, { PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' });
    backend = spawn(py, ['-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', String(PORT)],
      { cwd: ROOT, windowsHide: true, env });
    backend.stdout.on('data', d => console.log('[py]', d.toString().trim()));
    backend.stderr.on('data', d => console.log('[py]', d.toString().trim()));
    waitPort(PORT, () => createWindow());
  });
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

app.whenReady().then(() => { startBackend(); startPublishServer(); });
app.on('window-all-closed', () => {
  if (backend) { try { backend.kill(); } catch (e) {} }
  app.quit();
});
