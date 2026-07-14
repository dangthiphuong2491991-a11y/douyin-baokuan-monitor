// ============ 差量更新器（增量更新，仿字字动画）============
// 启动时拉远端 update_manifest.json，和本地 installed_manifest.json 比对，
// 只下载"改动过的 backend 文件"(内容寻址 by sha256)、就地替换、重启后端 + 刷新前端。
// 不再每次拉 580MB 整包。任何一步出问题 / Electron 外壳(app.asar)变了 → 回退整包安装(onFullInstaller)。
const { BrowserWindow } = require('electron');
const fs = require('fs');
const path = require('path');
const https = require('https');
const http = require('http');
const crypto = require('crypto');

function _get(url, redirects, cb) {                       // cb(err, res)
  if (redirects > 6) return cb(new Error('too many redirects'));
  const lib = url.startsWith('https') ? https : http;
  const req = lib.get(url, { timeout: 20000, headers: { 'User-Agent': 'baokuan-updater' } }, (res) => {
    if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
      res.resume(); return _get(new URL(res.headers.location, url).toString(), redirects + 1, cb);
    }
    if (res.statusCode !== 200) { res.resume(); return cb(new Error('HTTP ' + res.statusCode)); }
    cb(null, res);
  });
  req.on('error', cb);
  req.on('timeout', () => req.destroy(new Error('timeout')));
}

function httpGetText(url) {
  return new Promise((resolve, reject) => _get(url, 0, (err, res) => {
    if (err) return reject(err);
    let buf = ''; res.on('data', (d) => { buf += d; }); res.on('end', () => resolve(buf));
  }));
}

function downloadTo(url, dest, onProgress) {
  return new Promise((resolve, reject) => _get(url, 0, (err, res) => {
    if (err) return reject(err);
    const total = parseInt(res.headers['content-length'] || '0', 10);
    let got = 0;
    const f = fs.createWriteStream(dest);
    res.on('data', (d) => { got += d.length; if (total && onProgress) onProgress(got / total); });
    res.pipe(f);
    f.on('finish', () => f.close(() => resolve()));
    f.on('error', reject);
  }));
}

function sha256File(fp) {
  const h = crypto.createHash('sha256');
  h.update(fs.readFileSync(fp));
  return h.digest('hex');
}

// —— 路径（打包后 process.resourcesPath = .../resources） ——
function resourcesDir() { return process.resourcesPath; }
function backendDir() { return path.join(resourcesDir(), 'backend'); }
function asarPath() { return path.join(resourcesDir(), 'app.asar'); }
function installedManifestPath() { return path.join(resourcesDir(), 'installed_manifest.json'); }

function loadInstalled() {
  try { return JSON.parse(fs.readFileSync(installedManifestPath(), 'utf-8')); } catch (e) { return null; }
}

function makeProgressWin() {
  const pw = new BrowserWindow({
    width: 460, height: 210, frame: false, resizable: false, alwaysOnTop: true,
    backgroundColor: '#12141c', skipTaskbar: true, webPreferences: {},
  });
  pw.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(
    `<body style="margin:0;background:#12141c;color:#fff;font:14px system-ui;display:flex;flex-direction:column;justify-content:center;height:100vh;padding:0 26px;box-sizing:border-box;gap:12px">
    <div style="font-size:16px;font-weight:600">📦 爆款监控更新</div>
    <div id="t" style="font-size:12px;color:#7c8cff">准备中…</div>
    <div id="f" style="font-size:12px;color:#aaa">下载中…</div>
    <div style="width:100%;height:10px;background:#333;border-radius:6px;overflow:hidden"><div id="b" style="height:100%;width:0;background:linear-gradient(90deg,#7c6eff,#a78bfa);transition:width .15s"></div></div>
    <script>function set(t,f,pct){if(t!=null)document.getElementById('t').textContent=t;if(f!=null)document.getElementById('f').textContent=f;if(pct!=null)document.getElementById('b').style.width=pct+'%'}</script></body>`));
  return pw;
}

// 主流程。opts: { versionInfo, curVersion, verGt, stopBackend, startBackend, reloadUI, onFullInstaller, log }
// 返回 { mode:'diff'|'full'|'none', ... }。差量成功=就地更新完(不需重装);否则走 onFullInstaller。
async function checkAndApply(opts) {
  const { versionInfo, curVersion, verGt, stopBackend, startBackend, reloadUI, onFullInstaller } = opts;
  const log = opts.log || (() => {});
  const latest = versionInfo.desktop_version || versionInfo.version;
  if (!latest || !verGt(latest, curVersion)) { log('已是最新 ' + curVersion); return { mode: 'none' }; }

  const manifestUrl = versionInfo.update_manifest_url;
  const poolBase = versionInfo.update_pool_base;
  const installed = loadInstalled();
  // 没有基线清单(老版本首次装、或从整包版过渡) / 没配差量源 → 只能整包
  if (!manifestUrl || !poolBase || !installed) {
    log('无差量基线/源 → 整包 (manifest=' + !!manifestUrl + ' pool=' + !!poolBase + ' installed=' + !!installed + ')');
    return onFullInstaller('no-baseline');
  }

  let manifest;
  try { manifest = JSON.parse(await httpGetText(manifestUrl)); }
  catch (e) { log('拉 manifest 失败 → 整包: ' + e); return onFullInstaller('manifest-fetch-failed'); }

  // Electron 外壳(app.asar)变了 → 差量替换不了自己 → 整包
  try {
    if (manifest.shell_hash && fs.existsSync(asarPath()) && manifest.shell_hash !== sha256File(asarPath())) {
      log('app.asar 变了 → 整包'); return onFullInstaller('shell-changed');
    }
  } catch (e) { log('比 shell_hash 异常 → 整包: ' + e); return onFullInstaller('shell-hash-err'); }

  // 算改动的 backend 文件
  const remoteB = manifest.backend || {};
  const localB = (installed.backend) || {};
  const changed = [];
  for (const rel of Object.keys(remoteB)) {
    if ((localB[rel] && localB[rel].sha256) !== remoteB[rel].sha256) {
      changed.push({ rel, sha: remoteB[rel].sha256, size: remoteB[rel].size || 0 });
    }
  }
  if (!changed.length) {
    log('版本号新但文件无差异,直接写清单');
    try { fs.writeFileSync(installedManifestPath(), JSON.stringify(manifest)); } catch (e) {}
    return { mode: 'diff', files: 0 };
  }
  const totalMB = (changed.reduce((s, c) => s + c.size, 0) / 1048576).toFixed(1);
  log(`差量: ${changed.length} 个文件 / ${totalMB}MB → 开始下载`);

  const pw = makeProgressWin();
  const setP = (t, f, pct) => { try { pw.webContents.executeJavaScript(`set(${JSON.stringify(t)},${JSON.stringify(f)},${pct == null ? null : pct})`); } catch (e) {} };
  const os = require('os');
  const staging = path.join(os.tmpdir(), 'baokuan_update_' + latest);
  try { fs.mkdirSync(staging, { recursive: true }); } catch (e) {}

  try {
    // 内容寻址去重下载(同 sha 只下一次)
    const blobs = [...new Set(changed.map((c) => c.sha))];
    const relOf = {}; changed.forEach((c) => { relOf[c.sha] = c.rel; });
    for (let i = 0; i < blobs.length; i++) {
      const sha = blobs[i];
      const dst = path.join(staging, sha);
      setP(`下载新版 v${latest}  (${i + 1}/${blobs.length} 个文件)`, relOf[sha], Math.floor(i / blobs.length * 100));
      if (!(fs.existsSync(dst) && sha256File(dst) === sha)) {
        await downloadTo(poolBase + sha, dst, (frac) => setP(null, relOf[sha] + '  (' + Math.floor(frac * 100) + '%)', Math.floor((i + frac) / blobs.length * 100)));
      }
      if (sha256File(dst) !== sha) throw new Error('校验失败(sha不符): ' + relOf[sha]);
    }

    // 应用：停后端 → 就地替换 → 写清单 → 重启后端 → 刷新前端
    setP('正在替换文件并重启…', '请稍候', 100);
    await stopBackend();
    for (const c of changed) {
      const target = path.join(backendDir(), c.rel);
      fs.mkdirSync(path.dirname(target), { recursive: true });
      fs.copyFileSync(path.join(staging, c.sha), target);
    }
    fs.writeFileSync(installedManifestPath(), JSON.stringify(manifest));
    await startBackend();
    try { reloadUI && reloadUI(); } catch (e) {}
    try { fs.rmSync(staging, { recursive: true, force: true }); } catch (e) {}
    setTimeout(() => { try { pw.destroy(); } catch (e) {} }, 800);
    log(`差量更新完成 → v${latest} (${changed.length} 个文件)`);
    return { mode: 'diff', files: changed.length, version: latest };
  } catch (e) {
    log('差量过程出错 → 回退整包: ' + e);
    try { pw.destroy(); } catch (_) {}
    // 尽量把后端拉起来(万一停了没起)
    try { await startBackend(); } catch (_) {}
    return onFullInstaller('diff-failed:' + String(e).slice(0, 60));
  }
}

module.exports = { checkAndApply };
