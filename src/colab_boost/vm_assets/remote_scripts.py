"""Parameterized remote Python script builders executed inside Colab VMs.

Centralizes shared VM-side helper snippets (PID liveness, atomic file writes,
symlink-safe tar filtering, and directory stat walking) so ``orchestrator.py``
never duplicates inline remote Python code and scripts can be unit-tested
directly.
"""

from __future__ import annotations

from typing import Any, Optional

from colab_boost.backends.base import RESULT_MARKER

VM_PID_ALIVE_SNIPPET = (
    "def _pid_alive(pid):\n"
    "    if not pid or pid <= 0:\n"
    "        return False\n"
    "    stat_path = f'/proc/{pid}/stat'\n"
    "    if not os.path.exists(stat_path):\n"
    "        return False\n"
    "    try:\n"
    "        raw_stat = open(stat_path, encoding='utf-8', errors='replace').read()\n"
    "        rparen = raw_stat.rfind(')')\n"
    "        if rparen != -1:\n"
    "            fields = raw_stat[rparen + 1:].split()\n"
    "            if fields and fields[0] in ('Z', 'X', 'x'):\n"
    "                return False\n"
    "        return True\n"
    "    except OSError:\n"
    "        return False\n"
)

VM_ATOMIC_WRITE_SNIPPET = (
    "def _atomic_w(dst_path, txt):\n"
    "    fd, tmp_p = tempfile.mkstemp(dir=wd_dir, prefix='.wd.', suffix='.tmp')\n"
    "    try:\n"
    "        with os.fdopen(fd, 'w', encoding='utf-8') as fh:\n"
    "            fh.write(txt)\n"
    "            fh.flush()\n"
    "        os.replace(tmp_p, dst_path)\n"
    "    except Exception:\n"
    "        try:\n"
    "            os.remove(tmp_p)\n"
    "        except OSError:\n"
    "            pass\n"
    "        raise\n"
)

VM_TAR_FILTER_SNIPPET = (
    "def _tar_filter(ti):\n"
    "    if ti.issym() and (ti.linkname.startswith('/') or '..' in ti.linkname.split('/')):\n"
    "        return None\n"
    "    if not (ti.isfile() or ti.isdir() or ti.issym()):\n"
    "        return None\n"
    "    return ti\n"
)

VM_PATH_STATS_SNIPPET = (
    "def _path_stats(target_path):\n"
    "    fc, tb = 0, 0\n"
    "    if os.path.isfile(target_path) and not os.path.islink(target_path):\n"
    "        return 1, os.path.getsize(target_path)\n"
    "    if os.path.isdir(target_path) and not os.path.islink(target_path):\n"
    "        for r, _, fs in os.walk(target_path, followlinks=False):\n"
    "            for fn in fs:\n"
    "                fp = os.path.join(r, fn)\n"
    "                if os.path.islink(fp):\n"
    "                    continue\n"
    "                try:\n"
    "                    if os.path.isfile(fp):\n"
    "                        fc += 1\n"
    "                        tb += os.path.getsize(fp)\n"
    "                except OSError:\n"
    "                    pass\n"
    "    return fc, tb\n"
)


def build_keep_winddown_hook_script(
    run_id: str,
    session_name: str,
    specs: list[dict[str, str]],
) -> str:
    """Builds the VM-side watchdog wind-down hook script that archives ``[[keep]]`` paths."""
    return (
        "import json, os, shutil, tarfile, time\n"
        f"specs = {specs!r}\n"
        "keep_root = os.environ.get('BOOST_KEEP_STAGE_DIR', '/root/.colab-boost/keep')\n"
        f"stage_root = os.path.join(keep_root, {run_id!r})\n"
        "os.makedirs(stage_root, exist_ok=True)\n"
        "drive_mount = os.environ.get('BOOST_DRIVE_MOUNT', '/content/drive/MyDrive')\n"
        f"drive_base = os.path.join(drive_mount, 'ColabBoost', {session_name!r}, {run_id!r})\n"
        "has_drive = os.path.isdir(drive_mount)\n"
        "if has_drive:\n"
        "    os.makedirs(drive_base, exist_ok=True)\n"
        f"{VM_TAR_FILTER_SNIPPET}"
        f"{VM_PATH_STATS_SNIPPET}"
        f"manifest = {{'run_id': {run_id!r}, 'saved_at': time.time(), 'items': []}}\n"
        "for item in specs:\n"
        "    p = item['path']\n"
        "    slug = item['slug']\n"
        "    entry = {'path': p, 'exists': os.path.lexists(p), 'file_count': 0, 'total_bytes': 0, 'drive_synced': False}\n"
        "    if entry['exists']:\n"
        "        fc, tb = _path_stats(p)\n"
        "        entry['file_count'] = fc\n"
        "        entry['total_bytes'] = tb\n"
        "        arc_path = os.path.join(stage_root, slug + '.tar.gz')\n"
        "        tmp_arc = arc_path + '.tmp'\n"
        "        try:\n"
        "            with tarfile.open(tmp_arc, 'w:gz') as tf:\n"
        "                tf.add(p, arcname=os.path.basename(p.rstrip('/')) or slug, recursive=True, filter=_tar_filter)\n"
        "            os.replace(tmp_arc, arc_path)\n"
        "            entry['archive'] = arc_path\n"
        "            if has_drive:\n"
        "                shutil.copy2(arc_path, os.path.join(drive_base, slug + '.tar.gz'))\n"
        "                entry['drive_synced'] = True\n"
        "        except Exception as exc:\n"
        "            try:\n"
        "                os.remove(tmp_arc)\n"
        "            except OSError:\n"
        "                pass\n"
        "            entry['error'] = str(exc)\n"
        "    manifest['items'].append(entry)\n"
        "with open(os.path.join(stage_root, 'keep_manifest.json'), 'w', encoding='utf-8') as fh:\n"
        "    json.dump(manifest, fh)\n"
    )


def build_keep_sync_script(
    run_id: str,
    session_name: str,
    specs: list[dict[str, str]],
    *,
    result_marker: str = RESULT_MARKER,
    inline_limit_bytes: int = 8 * 1024 * 1024,
) -> str:
    """Builds the VM-side script that archives ``[[keep]]`` paths, hashes them, and returns metadata/inline payload."""
    return (
        "import base64, hashlib, json, os, shutil, tarfile\n"
        f"specs = {specs!r}\n"
        "keep_root = os.environ.get('BOOST_KEEP_STAGE_DIR', '/root/.colab-boost/keep')\n"
        f"stage_root = os.path.join(keep_root, {run_id!r})\n"
        "os.makedirs(stage_root, exist_ok=True)\n"
        "stage_manifest = os.path.join(stage_root, 'keep_manifest.json')\n"
        "drive_map = {}\n"
        "if os.path.exists(stage_manifest):\n"
        "    try:\n"
        "        for it in json.load(open(stage_manifest, encoding='utf-8')).get('items', []):\n"
        "            drive_map[it.get('path')] = bool(it.get('drive_synced'))\n"
        "    except Exception:\n"
        "        pass\n"
        "drive_mount = os.environ.get('BOOST_DRIVE_MOUNT', '/content/drive/MyDrive')\n"
        f"drive_root = os.path.join(drive_mount, 'ColabBoost', {session_name!r}, {run_id!r})\n"
        "has_drive = os.path.isdir(drive_mount)\n"
        "if has_drive:\n"
        "    os.makedirs(drive_root, exist_ok=True)\n"
        f"{VM_TAR_FILTER_SNIPPET}"
        f"{VM_PATH_STATS_SNIPPET}"
        "results = []\n"
        "for sp in specs:\n"
        "    p = sp['path']\n"
        "    slug = sp['slug']\n"
        "    fc = 0\n"
        "    tb = 0\n"
        "    b64 = ''\n"
        "    stage_arc = ''\n"
        "    arc_bytes = 0\n"
        "    arc_sha = ''\n"
        "    err_msg = ''\n"
        "    ds = bool(drive_map.get(p, False))\n"
        "    exists = os.path.lexists(p)\n"
        "    if exists:\n"
        "        fc, tb = _path_stats(p)\n"
        "        stage_arc = os.path.join(stage_root, slug + '.tar.gz')\n"
        "        tmp_arc = stage_arc + '.tmp'\n"
        "        try:\n"
        "            with tarfile.open(tmp_arc, mode='w:gz') as tf:\n"
        "                tf.add(p, arcname=os.path.basename(p.rstrip('/')) or slug, recursive=True, filter=_tar_filter)\n"
        "            os.replace(tmp_arc, stage_arc)\n"
        "            arc_bytes = os.path.getsize(stage_arc)\n"
        "            h = hashlib.sha256()\n"
        "            with open(stage_arc, 'rb') as hfh:\n"
        "                for blk in iter(lambda: hfh.read(4 * 1024 * 1024), b''):\n"
        "                    h.update(blk)\n"
        "            arc_sha = h.hexdigest()\n"
        "            if has_drive:\n"
        "                try:\n"
        "                    shutil.copy2(stage_arc, os.path.join(drive_root, slug + '.tar.gz'))\n"
        "                    ds = True\n"
        "                except OSError:\n"
        "                    pass\n"
        f"            if arc_bytes <= {inline_limit_bytes}:\n"
        "                raw_tar = open(stage_arc, 'rb').read()\n"
        "                b64 = base64.b64encode(raw_tar).decode('ascii')\n"
        "        except Exception as exc:\n"
        "            try:\n"
        "                os.remove(tmp_arc)\n"
        "            except OSError:\n"
        "                pass\n"
        "            err_msg = str(exc)\n"
        "    results.append({'path': p, 'slug': slug, 'exists': exists, 'file_count': fc, 'total_bytes': tb, 'b64': b64, 'stage_archive': stage_arc, 'archive_bytes': arc_bytes, 'archive_sha256': arc_sha, 'drive_synced': ds, 'error': err_msg})\n"
        f"print({result_marker!r} + json.dumps({{'items': results}}), flush=True)\n"
    )


def build_worker_poll_script(
    vm_run_dir: str,
    keep_paths: Optional[list[str]] = None,
    *,
    result_marker: str = RESULT_MARKER,
) -> str:
    """Builds the VM-side worker status poll script, optionally collecting ``[[keep]]`` stats."""
    paths = list(keep_paths or [])
    base = (
        "import json, os\n"
        f"{VM_PID_ALIVE_SNIPPET}"
        f"{VM_PATH_STATS_SNIPPET if paths else ''}"
        f"d = {vm_run_dir!r}\n"
        "ex = os.path.isdir(d)\n"
        "st = json.load(open(d + '/status.json')) if os.path.exists(d + '/status.json') else None\n"
        "pid = int(open(d + '/worker.pid').read().strip()) if os.path.exists(d + '/worker.pid') else 0\n"
        "alive = _pid_alive(pid)\n"
        "lines = open(d + '/run.log', errors='replace').readlines()[-35:] if os.path.exists(d + '/run.log') else []\n"
    )
    if not paths:
        return (
            base
            + f"print({result_marker!r} + json.dumps({{'exists': ex, 'status': st, 'alive': alive, 'tail': [x.rstrip() for x in lines]}}), flush=True)\n"
        )
    return (
        base
        + "ks = {}\n"
        + f"for kp in {paths!r}:\n"
        + "    fc, tb = _path_stats(kp)\n"
        + "    ks[kp] = {'file_count': fc, 'total_bytes': tb}\n"
        + f"print({result_marker!r} + json.dumps({{'exists': ex, 'status': st, 'alive': alive, 'tail': [x.rstrip() for x in lines], 'keep_stats': ks}}), flush=True)\n"
    )


def build_watchdog_install_script(
    watchdog_src: str,
    cfg_json: str,
    hooks_json: str,
    *,
    result_marker: str = RESULT_MARKER,
) -> str:
    """Builds the VM-side script that atomically writes and launches the watchdog daemon."""
    return (
        "import json, os, subprocess, sys, tempfile\n"
        "wd_dir = '/root/.colab-boost/watchdog'\n"
        "os.makedirs(wd_dir, exist_ok=True)\n"
        "hb_file = '/root/.colab-boost/heartbeat'\n"
        "os.makedirs(os.path.dirname(hb_file) or '/', exist_ok=True)\n"
        "open(hb_file, 'a', encoding='utf-8').close()\n"
        "os.utime(hb_file, None)\n"
        f"{VM_ATOMIC_WRITE_SNIPPET}"
        f"_atomic_w(wd_dir + '/watchdog.py', {watchdog_src!r})\n"
        f"_atomic_w(wd_dir + '/watchdog.json', {cfg_json!r})\n"
        "p = subprocess.Popen("
        "[sys.executable, wd_dir + '/watchdog.py', '--config', wd_dir + '/watchdog.json', '--hooks-stdin'], "
        "stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, start_new_session=True)\n"
        "if p.stdin:\n"
        f"    p.stdin.write({hooks_json!r})\n"
        "    p.stdin.close()\n"
        f"print({result_marker!r} + json.dumps({{'pid': p.pid}}), flush=True)\n"
    )


def build_watchdog_patch_respawn_script(
    watchdog_src: str,
    *,
    deadline_epoch: Optional[float] = None,
    remaining_s: Optional[float] = None,
    default_idle_s: Optional[int] = None,
    touch_heartbeat: bool = False,
    append_hooks: Optional[list[dict[str, Any]]] = None,
    respawn_only_if_guarded: bool = False,
    result_marker: str = RESULT_MARKER,
) -> str:
    """Builds the VM-side script that patches ``watchdog.json`` atomically and respawns the daemon if dead."""
    parts: list[str] = [
        "import json, os, subprocess, sys, tempfile, time\n",
        VM_PID_ALIVE_SNIPPET,
        "wd_dir = '/root/.colab-boost/watchdog'\n",
        "os.makedirs(wd_dir, exist_ok=True)\n",
        "cfg_path = wd_dir + '/watchdog.json'\n",
        "pid_path = wd_dir + '/watchdog.pid'\n",
        "script_path = wd_dir + '/watchdog.py'\n",
        "cfg = {}\n",
        "if os.path.exists(cfg_path):\n",
        "    try:\n",
        "        cfg = json.load(open(cfg_path, encoding='utf-8'))\n",
        "    except Exception:\n",
        "        cfg = {}\n",
    ]

    if deadline_epoch is not None:
        parts.extend(
            [
                f"cfg['deadline_epoch'] = {deadline_epoch!r}\n",
                f"cfg['remaining_s'] = {remaining_s!r}\n",
                "cfg.pop('max_runtime_s', None)\n",
                "cfg['cancelled'] = False\n",
                f"cfg.setdefault('idle_timeout_s', {default_idle_s!r})\n",
                "cfg.setdefault('runtime_addr', os.environ.get('TBE_RUNTIME_ADDR'))\n",
                "cfg.setdefault('heartbeat_file', '/root/.colab-boost/heartbeat')\n",
                "cfg.setdefault('log_file', wd_dir + '/watchdog.log')\n",
                "cfg.setdefault('poll_s', 15)\n",
                "cfg.setdefault('grace_s', 180)\n",
                "cfg.setdefault('started_at', time.time())\n",
            ]
        )

    if touch_heartbeat:
        parts.extend(
            [
                "hb_file = cfg.get('heartbeat_file') or '/root/.colab-boost/heartbeat'\n",
                "os.makedirs(os.path.dirname(hb_file) or '/', exist_ok=True)\n",
                "open(hb_file, 'a', encoding='utf-8').close()\n",
                "os.utime(hb_file, None)\n",
            ]
        )

    if append_hooks is not None:
        parts.extend(
            [
                "existing_hooks = cfg.get('hooks') if isinstance(cfg.get('hooks'), list) else []\n",
                f"for h in {append_hooks!r}:\n",
                "    if h not in existing_hooks:\n",
                "        existing_hooks.append(h)\n",
                "cfg['hooks'] = existing_hooks\n",
            ]
        )

    parts.extend(
        [
            VM_ATOMIC_WRITE_SNIPPET,
            "_atomic_w(cfg_path, json.dumps(cfg))\n",
            "pid = 0\n",
            "if os.path.exists(pid_path):\n",
            "    try:\n",
            "        pid = int(open(pid_path).read().strip())\n",
            "    except Exception:\n",
            "        pid = 0\n",
            "alive = _pid_alive(pid)\n",
        ]
    )

    if respawn_only_if_guarded:
        parts.append(
            "has_guard = bool(cfg.get('deadline_epoch') or cfg.get('remaining_s') or cfg.get('idle_timeout_s'))\n"
            "if has_guard and not alive:\n"
        )
    else:
        parts.append("if not alive:\n")

    parts.extend(
        [
            f"    _atomic_w(script_path, {watchdog_src!r})\n",
            "    p = subprocess.Popen([sys.executable, script_path, '--config', cfg_path], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)\n",
            "    pid = p.pid\n",
        ]
    )

    out_fields: list[str] = ["'pid': pid"]
    if deadline_epoch is not None:
        out_fields.extend(["'deadline_epoch': cfg['deadline_epoch']", "'remaining_s': cfg['remaining_s']"])
    if append_hooks is not None:
        out_fields.append("'hooks_count': len(existing_hooks)")
    payload_expr = "{" + ", ".join(out_fields) + "}"
    parts.append(f"print({result_marker!r} + json.dumps({payload_expr}), flush=True)\n")
    return "".join(parts)
