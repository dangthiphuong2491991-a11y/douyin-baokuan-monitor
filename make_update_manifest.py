# -*- coding: utf-8 -*-
"""
差量更新·打包端工具。

生成"更新清单"(每个文件的 sha256)+ 抽取"改动文件"(内容寻址)供上传。
客户端启动时比对清单,只下载改动过的 backend 文件、就地替换 —— 不再每次拉 580MB 整包。

结构约定(Electron win-unpacked)：
  <win-unpacked>/
    ├─ resources/app.asar          ← Electron 外壳(main.js/publish.js)。变了→客户端回退整包安装
    └─ resources/backend/**        ← 后端 onedir(backend.exe + _internal/含Python代码/static/Chromium)
                                     真正每次改的就这里，差量只管它

用法：
  # ① 生成清单
  python make_update_manifest.py gen <win-unpacked> <version> <out_manifest.json>
  # ② 对比上一版清单，把"改动/新增的 backend 文件"按 sha256 内容寻址拷到 delta 目录(供上传到内容寻址池)
  python make_update_manifest.py delta <old_manifest.json|-> <new_manifest.json> <win-unpacked> <out_delta_dir>
"""
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path


def _sha256(fp: Path) -> str:
    h = hashlib.sha256()
    with open(fp, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def gen_manifest(win_unpacked: Path, version: str) -> dict:
    """遍历 resources/backend 算每文件 sha256 + app.asar 的 shell_hash。"""
    res = win_unpacked / "resources"
    asar = res / "app.asar"
    shell_hash = _sha256(asar) if asar.exists() else ""

    backend_root = res / "backend"
    files = {}
    total = 0
    if backend_root.exists():
        for fp in backend_root.rglob("*"):
            if not fp.is_file():
                continue
            rel = fp.relative_to(backend_root).as_posix()   # 统一用 / 分隔
            sz = fp.stat().st_size
            files[rel] = {"sha256": _sha256(fp), "size": sz}
            total += sz
    return {
        "version": version,
        "shell_hash": shell_hash,
        "backend": files,
        "_stat": {"file_count": len(files), "total_bytes": total},
    }


def cmd_gen(argv):
    win_unpacked, version, out = Path(argv[0]), argv[1], Path(argv[2])
    m = gen_manifest(win_unpacked, version)
    out.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    st = m["_stat"]
    print(f"[manifest] v{version}: {st['file_count']} 个 backend 文件, "
          f"{st['total_bytes']/1048576:.1f}MB, shell_hash={m['shell_hash'][:12]}…")
    print(f"[manifest] → {out}")


def cmd_delta(argv):
    old_arg, new_path, win_unpacked, out_dir = argv[0], Path(argv[1]), Path(argv[2]), Path(argv[3])
    new = json.loads(new_path.read_text(encoding="utf-8"))
    old = {"backend": {}}
    if old_arg != "-" and Path(old_arg).exists():
        old = json.loads(Path(old_arg).read_text(encoding="utf-8"))
    old_b, new_b = old.get("backend", {}), new.get("backend", {})

    out_dir.mkdir(parents=True, exist_ok=True)
    backend_root = win_unpacked / "resources" / "backend"
    changed, total = [], 0
    seen_sha = set()
    for rel, meta in new_b.items():
        sha = meta["sha256"]
        if old_b.get(rel, {}).get("sha256") == sha:
            continue                      # 没变 → 客户端本地已有,跳过
        changed.append(rel)
        total += meta["size"]
        if sha in seen_sha:               # 同内容文件只需上传一份(内容寻址)
            continue
        seen_sha.add(sha)
        src = backend_root / rel
        if src.exists():
            shutil.copy2(src, out_dir / sha)    # 文件名=sha256(内容寻址)
    # shell(app.asar)是否变了 —— 变了客户端会回退整包,不走差量
    shell_changed = old.get("shell_hash") != new.get("shell_hash")
    print(f"[delta] 改动 backend 文件 {len(changed)} 个 / 去重内容块 {len(seen_sha)} 个, {total/1048576:.2f}MB")
    print(f"[delta] shell(app.asar)变了? {shell_changed}  → {'客户端将回退整包安装' if shell_changed else '纯差量即可'}")
    print(f"[delta] 内容寻址文件已拷到 → {out_dir}  (共 {len(list(out_dir.iterdir()))} 块)")
    for r in changed[:30]:
        print("   ~", r)
    if len(changed) > 30:
        print(f"   … 还有 {len(changed)-30} 个")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("gen", "delta"):
        print(__doc__)
        sys.exit(1)
    cmd, argv = sys.argv[1], sys.argv[2:]
    if cmd == "gen":
        cmd_gen(argv)
    else:
        cmd_delta(argv)


if __name__ == "__main__":
    main()
