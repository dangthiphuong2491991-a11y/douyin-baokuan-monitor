// ============ 差量更新器（增量更新，仿字字动画）============
// 启动时拉远端 update_manifest.json，和本地 installed_manifest.json 比对，
// 只下载"改动过的 backend 文件"(内容寻址 by sha256)、就地替换、重启后端 + 刷新前端。
// 不再每次拉 580MB 整包。
//
// 【壳增量】app.asar 外壳(main.js/publish.js,才~78KB)也当内容寻址块(名字=shell_hash)下载。
//   运行时 app.asar 被自己锁着换不了 → 停程序时交给一个 PowerShell 助手:等本进程退干净后
//   用"原子改名"(Move-Item -Force,同盘=原子)把 .pending 换成正式 asar,再拉起程序。
//   pending 事先校验过 sha256,换成功前旧壳一直在 → 换崩风险极低。
//   只有连拉 manifest 都失败 / 差量下载出错 → 才回退整包(onFullInstaller)。
const { BrowserWindow } = require('electron');
const fs = require('fs');
const path = require('path');
const https = require('https');
const http = require('http');
const crypto = require('crypto');
const { spawn } = require('child_process');

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

// 拉起一个"脱离本进程独立存活"的 PowerShell 助手：等本进程(PID)退干净(app.asar 才解锁)后，
// 把 .pending 原子改名(Move -Force,同盘=原子)成正式 asar/清单,再重启程序。这是进程内换不掉自己
// 那把锁的唯一办法。
//
// 【为什么这么起】实测(Win11+Node)：
//   · spawn(powershell, {detached:true}) 直接起 → 根本不执行(node-windows 已知坑)；
//   · 非 detached → 能跑但父进程一退就被一起带走;
//   · ✅ 用 cmd 的 start 起(grandchild,完全脱离父进程),能跑又能在父退后独立存活。
// 【为什么用 -EncodedCommand】脚本整段 UTF-16 转 base64 当一个纯 ASCII 参数传,
//   彻底绕开"临时 .ps1 路径含中文/空格"的 cmd 解析地狱;路径值作为 PS 单引号字面量内嵌(PS 原生认 Unicode)。
function spawnAsarSwapHelper(pendingAsar, asarDst, pendingMan, manDst, exePath, pid, log) {
  const q = (s) => "'" + String(s).replace(/'/g, "''") + "'";
  const ps = [
    "$ErrorActionPreference='SilentlyContinue'",
    // 等主进程完全退出(asar/manifest 才解锁);兜底 90 秒后不再等,免万一 PID 复用卡死
    '$n=0; while ((Get-Process -Id ' + pid + ' -ErrorAction SilentlyContinue) -and $n -lt 225) { Start-Sleep -Milliseconds 400; $n++ }',
    'Start-Sleep -Milliseconds 500',
    // 先换壳、再换清单：万一只换成了壳(清单没换)——下次启动壳已是新的、shell_hash 相符,
    // 更新器会自然补写清单,能自愈;反过来(清单新、壳旧)才会"版本号骗人",所以顺序不能反。
    'try { Move-Item -LiteralPath ' + q(pendingAsar) + ' -Destination ' + q(asarDst) + ' -Force } catch {}',
    'try { Move-Item -LiteralPath ' + q(pendingMan) + ' -Destination ' + q(manDst) + ' -Force } catch {}',
    'try { Start-Process -FilePath ' + q(exePath) + ' } catch {}',
  ].join('\r\n');
  const b64 = Buffer.from(ps, 'utf16le').toString('base64');   // 一个纯 ASCII token,无路径解析问题
  const child = spawn('cmd.exe',
    ['/c', 'start', '', '/min', 'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
      '-WindowStyle', 'Hidden', '-EncodedCommand', b64],
    { detached: true, stdio: 'ignore', windowsHide: true });
  child.unref();
  if (log) log('壳替换助手已独立拉起(等本进程退出后原子换壳+重启)');
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
  const { versionInfo, curVersion, verGt, stopBackend, startBackend, reloadUI, onFullInstaller, quitForShellSwap } = opts;
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

  // Electron 外壳(app.asar)变了？—— 不再回退整包，改为把 asar 也当块下载、停程序时原子换掉。
  let shellChanged = false;
  try {
    shellChanged = !!(manifest.shell_hash && fs.existsSync(asarPath()) && manifest.shell_hash !== sha256File(asarPath()));
  } catch (e) { log('比 shell_hash 异常 → 整包: ' + e); return onFullInstaller('shell-hash-err'); }
  // 壳要换却缺 quitForShellSwap 能力(理论不会) → 退整包兜底
  if (shellChanged && typeof quitForShellSwap !== 'function') {
    log('shell 变了但无 quitForShellSwap → 整包'); return onFullInstaller('no-shell-swap');
  }

  // 算改动的 backend 文件
  const remoteB = manifest.backend || {};
  const localB = (installed.backend) || {};
  const changed = [];
  for (const rel of Object.keys(remoteB)) {
    if ((localB[rel] && localB[rel].sha256) !== remoteB[rel].sha256) {
      changed.push({ rel, sha: remoteB[rel].sha256, size: remoteB[rel].size || 0 });
    }
  }
  if (!changed.length && !shellChanged) {
    log('版本号新但文件无差异,直接写清单');
    try { fs.writeFileSync(installedManifestPath(), JSON.stringify(manifest)); } catch (e) {}
    return { mode: 'diff', files: 0 };
  }
  const backendMB = (changed.reduce((s, c) => s + c.size, 0) / 1048576).toFixed(1);
  log(`差量: ${changed.length} 个后端文件 / ${backendMB}MB${shellChanged ? ' + 壳(app.asar)' : ''} → 开始下载`);

  const pw = makeProgressWin();
  const setP = (t, f, pct) => { try { pw.webContents.executeJavaScript(`set(${JSON.stringify(t)},${JSON.stringify(f)},${pct == null ? null : pct})`); } catch (e) {} };
  const os = require('os');
  const staging = path.join(os.tmpdir(), 'baokuan_update_' + latest);
  try { fs.mkdirSync(staging, { recursive: true }); } catch (e) {}

  try {
    // 待下载块 = 改动后端文件 + (壳变了则)app.asar(块名=shell_hash)。内容寻址去重(同 sha 只下一次)。
    const relOf = {}; changed.forEach((c) => { relOf[c.sha] = c.rel; });
    const blobs = [...new Set(changed.map((c) => c.sha))];
    if (shellChanged) { blobs.push(manifest.shell_hash); relOf[manifest.shell_hash] = 'app.asar (程序外壳)'; }
    for (let i = 0; i < blobs.length; i++) {
      const sha = blobs[i];
      const dst = path.join(staging, sha);
      setP(`下载新版 v${latest}  (${i + 1}/${blobs.length} 个文件)`, relOf[sha], Math.floor(i / blobs.length * 100));
      if (!(fs.existsSync(dst) && sha256File(dst) === sha)) {
        await downloadTo(poolBase + sha, dst, (frac) => setP(null, relOf[sha] + '  (' + Math.floor(frac * 100) + '%)', Math.floor((i + frac) / blobs.length * 100)));
      }
      if (sha256File(dst) !== sha) throw new Error('校验失败(sha不符): ' + relOf[sha]);
    }

    // 应用：停后端 → 就地替换后端文件
    setP('正在替换文件…', '请稍候', 100);
    await stopBackend();
    for (const c of changed) {
      const target = path.join(backendDir(), c.rel);
      fs.mkdirSync(path.dirname(target), { recursive: true });
      fs.copyFileSync(path.join(staging, c.sha), target);
    }

    if (shellChanged) {
      // 壳要换：写 pending(校验过的 asar + 新清单),交给助手等退出后原子改名+重启。本进程随即退出。
      const pendingAsar = asarPath() + '.pending';
      const pendingMan = installedManifestPath() + '.pending';
      fs.copyFileSync(path.join(staging, manifest.shell_hash), pendingAsar);
      if (sha256File(pendingAsar) !== manifest.shell_hash) throw new Error('pending asar 校验失败');
      fs.writeFileSync(pendingMan, JSON.stringify(manifest));
      setP('外壳已就绪，正在重启应用…', '几秒后自动打开', 100);
      spawnAsarSwapHelper(pendingAsar, asarPath(), pendingMan, installedManifestPath(),
        process.execPath, process.pid, log);
      log(`壳增量下载完成 → v${latest}(后端${changed.length}个+壳),交助手重启`);
      // 留一点时间让助手起来,再退出整个程序(助手会等本 PID 消失后换壳+重启)
      setTimeout(() => { try { quitForShellSwap(); } catch (e) { log('quitForShellSwap err ' + e); } }, 1200);
      return { mode: 'diff-shell', files: changed.length + 1, version: latest, restart: true };
    }

    // 纯后端差量：写清单 → 重启后端 → 刷新前端(不整机重启)
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

module.exports = { checkAndApply, spawnAsarSwapHelper };
