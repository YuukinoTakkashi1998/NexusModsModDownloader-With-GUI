all_files = "!All!"
logs      = "./output.log"
crash     = "./crash.txt"
profile   = "./profile/"
configs   = "./config.json"
downloads = "./downloads/"

ANALYSIS_THREADS = 15   # parallel API calls during analysis


def safeimport(package, pip_name=None):
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", pip_name or package])
    return __import__(package)


if __name__ == "__main__":
    try:
        import os, re, sys, stat, time, json, shutil, ctypes, logging
        import traceback, subprocess, threading, webbrowser
        from queue import Queue, Empty

        tqdm               = safeimport("tqdm")
        requests_lib       = safeimport("requests")
        playwright         = safeimport("playwright")
        playwright_stealth = safeimport("playwright_stealth")
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "firefox"])

        from playwright_stealth import Stealth
        from playwright.sync_api import sync_playwright

        import tkinter as tk
        from tkinter import ttk, filedialog, scrolledtext

        # ══════════════════════════════════════════════════════════════════════
        # Global state
        # ══════════════════════════════════════════════════════════════════════
        session        = requests_lib.Session()
        processedfiles = []
        failed_flag    = False
        apikey         = ""
        game           = ""
        mods_dir       = ""
        modlist        = []
        input_path     = ""
        raw_data       = []
        ln             = 0
        log_queue      = Queue()
        mod_title_cache = {}
        stop_event     = threading.Event()   # set → abort current download run
        progress_queue = Queue()             # (idx, done_mb, total_mb, speed_mb, eta_s) | (idx, "done") | (idx, "failed") | (idx, "skipped")

        # Playwright globals — initialised inside _pw_thread_main via _pw_run
        pw_instance  = None
        pw_context   = None
        pw_page      = None

        # ══════════════════════════════════════════════════════════════════════
        # Logging
        # ══════════════════════════════════════════════════════════════════════
        if os.path.exists(logs):
            os.remove(logs)

        logger = logging.getLogger("nmmd")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        logger.addHandler(logging.FileHandler(logs, encoding="utf-8"))

        class _QH(logging.Handler):
            def emit(self, r):
                log_queue.put(self.format(r))
        logger.addHandler(_QH())

        def log(msg):
            logger.info(msg)

        # ══════════════════════════════════════════════════════════════════════
        # Utilities
        # ══════════════════════════════════════════════════════════════════════
        def fold(d, clean=True):
            def err(func, p, _):
                try:
                    os.chmod(p, stat.S_IWRITE)
                    func(p)
                except Exception:
                    pass
            if clean and os.path.exists(d):
                shutil.rmtree(d, onexc=err)
            os.makedirs(d, exist_ok=True)

        def retry(func, *args, **kwargs):
            global failed_flag
            for attempt in range(5):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    log(f"{e}\nFailed attempt {attempt+1}/5")
                    time.sleep(1 + attempt)
            failed_flag = True
            return None

        def api_call(mod_id, cat="", fileid=""):
            def req():
                url = (f"https://api.nexusmods.com/v1/games/{game}/mods/{mod_id}/files"
                       f"{'.json?category='+cat if not fileid else '/'+fileid+'.json'}")
                r = session.get(url, headers={"accept": "application/json", "apikey": apikey})
                if r.status_code == 200:
                    return r.json()
                raise Exception(f"API error mod {mod_id}:{fileid} -> {r.status_code}")
            return retry(req)

        def mod_title_for(mod_id):
            if mod_id in mod_title_cache:
                return mod_title_cache[mod_id]

            def req():
                url = f"https://api.nexusmods.com/v1/games/{game}/mods/{mod_id}.json"
                r = session.get(url, headers={"accept": "application/json", "apikey": apikey})
                if r.status_code == 200:
                    data = r.json()
                    title = data.get("name") or data.get("title") or ""
                    mod_title_cache[mod_id] = title
                    return title
                raise Exception(f"API error mod {mod_id} -> {r.status_code}")

            try:
                return retry(req) or ""
            except Exception:
                return ""

        # ══════════════════════════════════════════════════════════════════════
        # Playwright — single dedicated thread owns ALL browser calls forever.
        #
        # Rule: nothing Playwright-related is ever called from any other thread.
        # The pw-thread receives jobs via pw_job_queue and posts results back via
        # per-job result queues.  fetch_bytes (pure requests/HTTP) runs on the
        # caller's thread and is the only part that is truly off-thread.
        # ══════════════════════════════════════════════════════════════════════
        pw_instance  = None
        pw_context   = None
        pw_page      = None   # set once inside the pw-thread, read-only afterwards
        pw_job_queue = Queue()   # (fn, result_q) | None=shutdown

        def _pw_run(fn):
            """Post fn to the pw-thread, block until done, return result or raise."""
            rq = Queue()
            pw_job_queue.put((fn, rq))
            tag, val = rq.get()
            if tag == "err":
                raise val
            return val

        def _pw_thread_main():
            """Owns the Playwright event loop for the lifetime of the process."""
            global pw_instance, pw_context, pw_page
            # initialised by the first _pw_run call from load_config_and_browser
            while True:
                item = pw_job_queue.get()
                if item is None:
                    break
                fn, rq = item
                try:
                    rq.put(("ok", fn()))
                except Exception as exc:
                    rq.put(("err", exc))

        _pw_thread = threading.Thread(target=_pw_thread_main, daemon=True, name="pw-thread")
        _pw_thread.start()

        # ══════════════════════════════════════════════════════════════════════
        # Download core
        # ══════════════════════════════════════════════════════════════════════
        def fetch_bytes(dl_url, dl_suggested, filename, label, dl_index=None):
            """Pure HTTP download — no Playwright involved, safe on any thread.
            Pushes (dl_index, done_mb, total_mb, speed_mb, eta_s) tuples to
            progress_queue so the GUI can render a live progress bar.
            Respects stop_event: aborts mid-download cleanly.
            """
            if not filename:
                filename = os.path.splitext(dl_suggested)[0]
            clean = re.sub(r'[\\/:*?"<>|.]', "", filename).strip()
            processedfiles.append(clean)
            if clean in modlist:
                log(f"{label} | Already downloaded, skipping: {clean}")
                return "skipped"
            ext       = os.path.splitext(dl_suggested)[1]
            full_name = clean + ext
            log(f"{label} | Fetching: {full_name}")
            p        = os.path.join(downloads, full_name)
            existing = os.path.getsize(p) if os.path.exists(p) else 0
            hdrs     = {"Range": f"bytes={existing}-"} if existing else {}
            resp     = session.get(dl_url, allow_redirects=True, stream=True,
                                   timeout=30, headers=hdrs)
            if resp.status_code not in (200, 206):
                log(f"{label} | Fetch failed: {resp.status_code}")
                return "failed"
            mb          = 1024 * 1024
            cl          = int(resp.headers.get("content-length", 0))
            total_mb    = (cl + existing) / mb
            done_bytes  = existing
            t_start     = time.time()
            speed_alpha = 0.1          # EMA smoothing factor for speed
            speed_mb    = 0.0

            with open(p, "ab" if existing else "wb") as f:
                with tqdm.tqdm(total=total_mb, initial=existing / mb, unit="MB",
                               bar_format="{l_bar}{bar}| {n:.1f}/{total:.1f} {unit} {rate_fmt} {remaining}") as pb:
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        if stop_event.is_set():
                            log(f"{label} | Stopped by user")
                            resp.close()
                            return "stopped"
                        if chunk:
                            f.write(chunk)
                            chunk_mb  = len(chunk) / mb
                            done_bytes += len(chunk)
                            elapsed   = time.time() - t_start or 1e-9
                            inst_mb   = chunk_mb / (time.time() - t_start - (elapsed - chunk_mb / max(speed_mb, 1e-9)) or 1e-9)
                            # EMA speed (MB/s) using wall-clock
                            new_speed = (done_bytes - existing) / mb / max(elapsed, 1e-9)
                            speed_mb  = speed_alpha * new_speed + (1 - speed_alpha) * speed_mb if speed_mb else new_speed
                            remain_mb = max(total_mb - done_bytes / mb, 0)
                            eta_s     = remain_mb / speed_mb if speed_mb > 0 else 0
                            pb.update(chunk_mb)
                            if dl_index is not None:
                                progress_queue.put((dl_index, done_bytes / mb,
                                                    total_mb, speed_mb, eta_s))
            shutil.move(p, os.path.join(mods_dir, full_name))
            log(f"{label} | Done: {full_name}")
            return "done"

        def _browser_trigger_id(mod_id, file_id):
            """Runs on the pw-thread. Returns (url, suggested_filename) or raises."""
            url = (f"https://www.nexusmods.com/{game}/mods/{mod_id}"
                   f"?tab=files&file_id={file_id}")
            pw_page.goto(url, wait_until="domcontentloaded")
            pw_page.wait_for_selector("mod-file-download", timeout=15000)
            pw_page.evaluate("""() => {
                const c = document.querySelector('mod-file-download');
                if (c) c.dispatchEvent(
                    new CustomEvent('slowDownload', {bubbles:true, composed:true}));
            }""")
            slow = pw_page.get_by_role("button", name="Slow download", exact=True)
            try:
                slow.wait_for(timeout=10000)
            except Exception:
                slow = pw_page.get_by_text("Slow download", exact=True)
                slow.wait_for(timeout=10000)
            with pw_page.expect_download(timeout=60000) as dl_info:
                slow.click()
            dl = dl_info.value
            result = (dl.url, dl.suggested_filename)
            dl.cancel()
            return result

        def _browser_trigger_url(url):
            """Runs on the pw-thread. Returns (url, suggested_filename) or raises."""
            with pw_page.expect_download(timeout=60000) as dl_info:
                try:
                    pw_page.goto(url, wait_until="domcontentloaded")
                except Exception:
                    pass
            dl = dl_info.value
            result = (dl.url, dl.suggested_filename)
            dl.cancel()
            return result

        def download_by_id(mod_id, file_id, filename, label, on_done, dl_index=None):
            url = (f"https://www.nexusmods.com/{game}/mods/{mod_id}"
                   f"?tab=files&file_id={file_id}")
            def attempt():
                log(f"Navigating: {url}")
                return _pw_run(lambda: _browser_trigger_id(mod_id, file_id))
            result = retry(attempt)
            if result:
                dl_url, dl_suggested = result
                on_done(fetch_bytes(dl_url, dl_suggested, filename, label, dl_index))
            else:
                log(f"{label} | May not be logged in or mod not public")
                on_done("failed")

        def download_by_url(url, label, on_done, dl_index=None):
            def attempt():
                log(f"Navigating: {url}")
                return _pw_run(lambda: _browser_trigger_url(url))
            result = retry(attempt)
            if result:
                dl_url, dl_suggested = result
                on_done(fetch_bytes(dl_url, dl_suggested, None, label, dl_index))
            else:
                on_done("failed")

        def run_downloads(sel_entries, on_status):
            """Sequential downloads, one at a time. Browser calls stay on pw-thread."""
            total = len(sel_entries)
            for i, entry in enumerate(sel_entries):
                if stop_event.is_set():
                    log("Download run aborted by user.")
                    break
                label = f"{i+1}/{total}"
                on_status(i, "downloading")
                try:
                    if entry["type"] == "url":
                        download_by_url(entry["nexus_url"], label,
                                        lambda s, _i=i: on_status(_i, s),
                                        dl_index=i)
                    else:
                        download_by_id(entry["mod_id"], entry["file_id"],
                                       entry["filename"], label,
                                       lambda s, _i=i: on_status(_i, s),
                                       dl_index=i)
                except Exception as ex:
                    log(f"{label} | Unexpected error: {ex}")
                    on_status(i, "failed")

            if not stop_event.is_set():
                for f in os.listdir(mods_dir):
                    if os.path.splitext(f)[0] not in processedfiles:
                        log(f"Removing unlisted installed file: {f}")
                        fp = os.path.join(mods_dir, f)
                        if os.path.exists(fp):
                            os.remove(fp)
                log("All downloads finished.")
            else:
                log("Downloads stopped. Partial files left in downloads/ folder have been cleared.")

        # ══════════════════════════════════════════════════════════════════════
        # Analysis
        # ══════════════════════════════════════════════════════════════════════
        def analyze_line(line):
            line = line.strip()
            if not line:
                return []
            if line.startswith("https://"):
                return [{"mod_id": None, "file_id": None, "filename": line,
                         "display_name": line, "mod_title": "",
                         "nexus_url": line, "type": "url"}]
            split  = line.split(";")
            first  = split[0].split(":")
            mod_id = first[0]
            main   = first[1:]
            if not main and ":" not in split[0]:
                main = [all_files]
            optional = split[1:]
            if optional and optional[0] == "":
                optional[0] = all_files
            nexus_url = f"https://www.nexusmods.com/{game}/mods/{mod_id}"
            entries = []

            def collect(types, cat):
                if not types:
                    return
                resp = api_call(mod_id, cat)
                if not resp:
                    return
                mod_title = mod_title_for(mod_id)
                for nf in types:
                    for e in resp.get("files", []):
                        if nf == all_files or nf.lower() == e["name"].lower():
                            entries.append({
                                "mod_id":       mod_id,
                                "file_id":      str(e["file_id"]),
                                "filename":     os.path.splitext(e["file_name"])[0],
                                "display_name": e["name"],
                                "mod_title":    mod_title,
                                "nexus_url":    nexus_url,
                                "type":         "nexus",
                            })

            collect(main, "main")
            collect(optional, "optional")
            collect(optional, "update")
            return entries

        def run_analysis(lines, done_cb):
            """15 parallel API threads for speed, results sorted to match txt order."""
            indexed   = [(i, l) for i, l in enumerate(lines) if l.strip()]
            results   = {}          # line_index -> list of entries
            lock      = threading.Lock()
            sem       = threading.Semaphore(ANALYSIS_THREADS)

            def worker(idx, line):
                with sem:
                    try:
                        e = analyze_line(line)
                        with lock:
                            results[idx] = e
                        log(f"Analyzed: {line.strip()}")
                    except Exception as ex:
                        log(f"Error analyzing '{line.strip()}': {ex}")

            threads = [threading.Thread(target=worker, args=(i, l), daemon=True)
                       for i, l in indexed]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # Flatten in original line order
            ordered = []
            for i, _ in indexed:
                ordered.extend(results.get(i, []))
            done_cb(ordered)

        def build_collection_entries(data):
            entries = []
            for item in data.get("externalResources", []):
                url = item["resourceUrl"]
                entries.append({"mod_id": None, "file_id": None, "filename": url,
                                 "display_name": url, "nexus_url": url, "type": "url"})
            for item in data.get("modFiles", []):
                fi  = item["file"]
                mod = fi["mod"]
                fname = (f"{fi['name']}-{mod['modId']}-{fi['fileId']}"
                         f"-{mod['version']}-{fi['version']}")
                entries.append({
                    "mod_id":       str(mod["modId"]),
                    "file_id":      str(item["fileId"]),
                    "filename":     fname,
                    "display_name": fi["name"],
                    "mod_title":    mod.get("name") or mod.get("modName") or mod.get("title") or "",
                    "nexus_url":    f"https://www.nexusmods.com/{game}/mods/{mod['modId']}",
                    "type":         "nexus",
                })
            return entries

        # ══════════════════════════════════════════════════════════════════════
        # Config + browser init
        # ══════════════════════════════════════════════════════════════════════
        def load_config_and_browser():
            global apikey, game, mods_dir, input_path, raw_data, ln, modlist
            global pw_instance, pw_context, pw_page

            input_path = path_var.get().strip().strip('"')

            with open(configs, "r") as f:
                config = json.loads(re.sub(r"\\+", "/", f.read()))

            if not isinstance(config.get("hide"), bool):
                log("Invalid 'hide' in config.json")
                return False

            apikey = config["apikey"]
            r = session.get("https://api.nexusmods.com/v1/users/validate.json",
                            headers={"accept": "application/json", "apikey": apikey})
            if r.status_code != 200:
                log("Invalid 'apikey' in config.json")
                return False

            firefox_path = config["firefox"] + "/"
            if not os.path.isdir(firefox_path):
                log(f"Invalid 'firefox' path: {firefox_path}")
                return False

            if pw_context is None:
                fold(profile, False)
                fold(downloads, False)

                def copy(name):
                    src = firefox_path + name
                    dst = profile + name
                    if os.path.isfile(src):
                        shutil.copy(src, dst)
                    elif os.path.isdir(src):
                        shutil.copytree(src, dst, dirs_exist_ok=True)

                for n in ["cookies.sqlite", "extensions.json", "extension-settings.json",
                          "extension-preferences.json", "extensions"]:
                    copy(n)

                # Launch browser INSIDE the pw-thread via _pw_run
                hide   = config["hide"]
                dpath  = downloads
                prof   = profile
                def _init_browser():
                    global pw_instance, pw_context, pw_page
                    pw_instance = sync_playwright().start()
                    pw_context  = pw_instance.firefox.launch_persistent_context(
                        prof, headless=hide, accept_downloads=True,
                        downloads_path=dpath, viewport={"width": 1920, "height": 1080}
                    )
                    Stealth().apply_stealth_sync(pw_context)
                    pw_page = pw_context.pages[0] if pw_context.pages else pw_context.new_page()
                _pw_run(_init_browser)

            # Parse input
            if (input_path.startswith("https://www.nexusmods.com/games/")
                    and "/collections/" in input_path):
                nav = input_path if input_path.endswith("/mods") else input_path + "/mods"
                try:
                    def _load_collection():
                        with pw_page.expect_response(
                            lambda r: (r.request.headers.get("x-graphql-operationname")
                                       == "CollectionRevisionMods" and r.status == 200)
                        ) as resp_info:
                            pw_page.goto(nav, wait_until="domcontentloaded")
                        resp = resp_info.value
                        title = pw_page.text_content(
                            ".typography-heading-md.sm\\:typography-heading-lg"
                            ".text-neutral-strong.break-words.font-semibold")
                        return resp.json(), title, resp.status

                    log(f"Navigating: {nav}")
                    col_json, title, status = _pw_run(_load_collection)
                    if status == 200:
                        col      = col_json["data"]["collectionRevision"]
                        mods_dir = re.sub(r'[\\/:*?"<>|.]', "", title or "collection").strip()
                        raw_data = col
                        ln       = len(col["externalResources"]) + len(col["modFiles"])
                        game     = re.search(r'/games/([^/]+)/', input_path).group(1)
                    else:
                        log("Failed to parse collection")
                        return False
                except Exception as exc:
                    log(f"Collection load error: {exc}")
                    return False

            elif os.path.isfile(input_path) and input_path.endswith(".txt"):
                base_name = os.path.splitext(os.path.basename(input_path))[0]
                mods_dir  = re.sub(r'[\\/:*?"<>|.]', "", base_name).strip()
                with open(input_path, "r") as f:
                    lines = f.readlines()
                game     = lines[0].strip()
                raw_data = lines[1:]
                ln       = len(raw_data)
            else:
                log(f"Invalid input: {input_path}")
                return False

            if not os.path.isdir(mods_dir):
                os.mkdir(mods_dir)
            modlist = [os.path.splitext(i)[0] for i in os.listdir(mods_dir)]
            return True

        # ══════════════════════════════════════════════════════════════════════
        # GUI
        # ══════════════════════════════════════════════════════════════════════
        root = tk.Tk()
        root.title("Nexus Mods Downloader")
        root.configure(bg="#0f0f0f")
        root.geometry("1280x820")
        root.minsize(960, 620)

        DARK   = "#0f0f0f"
        PANEL  = "#161616"
        PANEL2 = "#1b1b1b"
        BORDER = "#272727"
        ACCENT = "#d97706"
        FG     = "#ddd8cc"
        FG2    = "#5e5a52"
        GREEN  = "#22c55e"
        AMBER  = "#f59e0b"
        RED    = "#ef4444"
        BLUE   = "#60a5fa"
        SEL_BG = "#251f12"
        MONO   = "Consolas"

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame",         background=DARK)
        style.configure("TLabel",         background=DARK, foreground=FG,     font=("Segoe UI", 10))
        style.configure("Head.TLabel",    background=DARK, foreground=FG,     font=("Segoe UI", 13, "bold"))
        style.configure("Sub.TLabel",     background=DARK, foreground=FG2,    font=("Segoe UI", 9))
        style.configure("Accent.TLabel",  background=DARK, foreground=ACCENT, font=("Segoe UI", 10, "bold"))
        style.configure("TButton",        background=PANEL, foreground=FG,    font=("Segoe UI", 10),
                        relief="flat", borderwidth=1, focusthickness=0)
        style.map("TButton",
                  background=[("active", "#252525"), ("pressed", "#1a1a1a"), ("disabled", "#141414")],
                  foreground=[("active", ACCENT), ("disabled", FG2)])
        style.configure("Accent.TButton", background=ACCENT, foreground="#0a0a0a",
                        font=("Segoe UI", 10, "bold"))
        style.map("Accent.TButton",
                  background=[("active", "#f59e0b"), ("pressed", "#b45309"), ("disabled", "#2e2210")],
                  foreground=[("active", "#0a0a0a"), ("disabled", "#6b5a3a")])
        style.configure("Download.Horizontal.TProgressbar",
                troughcolor=PANEL2,
                background=ACCENT,
                bordercolor=BORDER,
                lightcolor=ACCENT,
                darkcolor=ACCENT)
        style.configure("TEntry",         fieldbackground=PANEL, background=PANEL,
                        foreground=FG, insertcolor=FG, relief="flat", font=("Segoe UI", 10))
        style.configure("Vertical.TScrollbar", background=PANEL2, troughcolor=DARK,
                        bordercolor=DARK, arrowcolor=FG2, relief="flat")
        style.configure("TPanedwindow",   background=BORDER)

        # ── Header ────────────────────────────────────────────────────────────
        hdr = ttk.Frame(root)
        hdr.pack(fill="x", padx=20, pady=(16, 0))
        ttk.Label(hdr, text="NEXUS MODS DOWNLOADER", style="Head.TLabel").pack(side="left")
        status_var = tk.StringVar(value="Ready")
        ttk.Label(hdr, textvariable=status_var, style="Sub.TLabel").pack(side="right")
        tk.Frame(root, height=1, bg=BORDER).pack(fill="x", padx=20, pady=10)

        # ── Input ─────────────────────────────────────────────────────────────
        inp = ttk.Frame(root)
        inp.pack(fill="x", padx=20, pady=(0, 6))
        ttk.Label(inp, text="Mod list (.txt) or collection URL", style="Sub.TLabel").pack(anchor="w")
        irow = ttk.Frame(inp)
        irow.pack(fill="x", pady=4)
        path_var = tk.StringVar()
        ttk.Entry(irow, textvariable=path_var).pack(side="left", fill="x", expand=True, ipady=6)

        def browse():
            f = filedialog.askopenfilename(filetypes=[("Text files", "*.txt"), ("All", "*.*")])
            if f:
                path_var.set(f)

        ttk.Button(irow, text="Browse…", command=browse).pack(side="left", padx=(8, 0), ipady=4)
        analyze_btn = ttk.Button(irow, text="Analyze Mods ▶", style="Accent.TButton",
                                 command=lambda: start_analysis())
        analyze_btn.pack(side="left", padx=(8, 0), ipady=4)
        tk.Frame(root, height=1, bg=BORDER).pack(fill="x", padx=20, pady=8)

        # ── Three-pane layout ─────────────────────────────────────────────────
        main_pane = ttk.PanedWindow(root, orient="horizontal")
        main_pane.pack(fill="both", expand=True, padx=20, pady=(0, 8))

        # ┌─────────────────────────────────────────────────────────────────────
        # │ LEFT — Mod selection
        # └─────────────────────────────────────────────────────────────────────
        left = tk.Frame(main_pane, bg=DARK)
        main_pane.add(left, weight=3)

        ltop = tk.Frame(left, bg=DARK)
        ltop.pack(fill="x", pady=(0, 5))
        tk.Label(ltop, text="Mods", bg=DARK, fg=ACCENT,
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        mod_count_var = tk.StringVar(value="0 found")
        tk.Label(ltop, textvariable=mod_count_var, bg=DARK, fg=FG2,
                 font=("Segoe UI", 9)).pack(side="left", padx=8)
        tk.Label(ltop, text="Shift/Ctrl-click · drag · right-click", bg=DARK, fg=FG2,
                 font=("Segoe UI", 8)).pack(side="left")
        for txt, val in (("All", True), ("None", False)):
            ttk.Button(ltop, text=txt, width=5,
                       command=lambda v=val: toggle_all(v)).pack(side="right", padx=2)

        # Canvas list
        cf = tk.Frame(left, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        cf.pack(fill="both", expand=True)
        canvas = tk.Canvas(cf, bg=PANEL, highlightthickness=0, bd=0)
        vscroll = ttk.Scrollbar(cf, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=PANEL)
        cwin  = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(cwin, width=e.width))

        def _scroll_list(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        canvas.bind("<MouseWheel>", _scroll_list)
        inner.bind("<MouseWheel>", _scroll_list)

        dl_btn = ttk.Button(left, text="Download Selected  ▶", style="Accent.TButton",
                            command=lambda: start_download(), state="disabled")
        dl_btn.pack(fill="x", pady=(8, 0), ipady=6)

        stop_btn = ttk.Button(left, text="⏹  Stop & Clear Temp",
                              command=lambda: stop_download(), state="disabled")
        stop_btn.pack(fill="x", pady=(4, 0), ipady=4)

        # ┌─────────────────────────────────────────────────────────────────────
        # │ MIDDLE — Download status
        # └─────────────────────────────────────────────────────────────────────
        mid = tk.Frame(main_pane, bg=DARK)
        main_pane.add(mid, weight=2)

        tk.Label(mid, text="Download Status", bg=DARK, fg=ACCENT,
             font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 5))
        scf = tk.Frame(mid, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        scf.pack(fill="both", expand=True)
        sc2 = tk.Canvas(scf, bg=PANEL, highlightthickness=0, bd=0)
        vs2 = ttk.Scrollbar(scf, orient="vertical", command=sc2.yview)
        sc2.configure(yscrollcommand=vs2.set)
        vs2.pack(side="right", fill="y")
        sc2.pack(side="left", fill="both", expand=True)
        status_inner = tk.Frame(sc2, bg=PANEL)
        sw = sc2.create_window((0, 0), window=status_inner, anchor="nw")
        status_inner.bind("<Configure>", lambda e: sc2.configure(scrollregion=sc2.bbox("all")))
        sc2.bind("<Configure>", lambda e: sc2.itemconfig(sw, width=e.width))

        # ┌─────────────────────────────────────────────────────────────────────
        # │ RIGHT — Log
        # └─────────────────────────────────────────────────────────────────────
        right = tk.Frame(main_pane, bg=DARK)
        main_pane.add(right, weight=2)

        tk.Label(right, text="Log", bg=DARK, fg=ACCENT,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 5))
        log_box = scrolledtext.ScrolledText(
            right, bg=PANEL, fg="#6b8060", insertbackground=FG,
            font=(MONO, 9), relief="flat", state="disabled",
            wrap="word", highlightthickness=1, highlightbackground=BORDER
        )
        log_box.pack(fill="both", expand=True)
        log_box.tag_config("err",  foreground=RED)
        log_box.tag_config("ok",   foreground=GREEN)
        log_box.tag_config("warn", foreground=AMBER)
        log_box.tag_config("info", foreground="#6b8060")

        def append_log(msg):
            log_box.configure(state="normal")
            lo  = msg.lower()
            tag = ("err"  if any(w in lo for w in ("fail", "error", "invalid", "exception")) else
                   "ok"   if any(w in lo for w in ("done", "finish", "downloaded", "complete")) else
                   "warn" if any(w in lo for w in ("skip", "remov", "not public")) else "info")
            log_box.insert("end", msg + "\n", tag)
            log_box.see("end")
            log_box.configure(state="disabled")

        def poll_log():
            for _ in range(40):
                try:
                    append_log(log_queue.get_nowait())
                except Empty:
                    break
            root.after(80, poll_log)
        root.after(80, poll_log)

        def _fmt_eta(secs):
            secs = int(secs)
            if secs < 60:
                return f"{secs}s"
            m, s = divmod(secs, 60)
            return f"{m}m{s:02d}s"

        def _fmt_size_from_mb(size_mb):
            size_bytes = size_mb * 1024 * 1024
            if size_bytes < 1024 * 1024:
                return f"{size_bytes / 1024:.1f} KB"
            if size_bytes < 1024 * 1024 * 1024:
                return f"{size_bytes / (1024 * 1024):.1f} MB"
            return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"

        def poll_progress():
            """Drain progress_queue and update per-file progress bars."""
            for _ in range(80):
                try:
                    item = progress_queue.get_nowait()
                except Empty:
                    break
                dl_pos, done_mb, total_mb, speed_mb, eta_s = item
                if dl_pos < len(progress_bars):
                    pvar, pstats = progress_bars[dl_pos]
                    pct = (done_mb / total_mb * 100) if total_mb > 0 else 0
                    pvar.set(min(pct, 100.0))
                    speed_str = f"{speed_mb:.2f} MB/s" if speed_mb > 0 else ""
                    size_str  = f"{_fmt_size_from_mb(done_mb)}/{_fmt_size_from_mb(total_mb)}"
                    eta_str   = f"ETA {_fmt_eta(eta_s)}" if eta_s > 0 else ""
                    pstats.configure(text=f"{size_str}  {speed_str}  {eta_str}".strip())
            root.after(100, poll_progress)

        root.after(100, poll_progress)

        # ══════════════════════════════════════════════════════════════════════
        # Mod list data
        # ══════════════════════════════════════════════════════════════════════
        check_vars    = []   # [(BooleanVar, entry_dict)]
        row_widgets   = []   # [tk.Frame]
        status_labels = []   # [tk.Label] in status panel
        progress_bars = []   # [(DoubleVar, tk.Label)] — progress bar + stats text
        selected_set  = set()
        last_clicked  = [None]

        STATUS_COLORS = {
            "pending":    FG2,
            "queued":     BLUE,
            "done":       GREEN,
            "skipped":    AMBER,
            "failed":     RED,
            "stopped":    RED,
            "downloading": ACCENT,
        }

        def row_bg(i, hi=False):
            return SEL_BG if hi else (PANEL if i % 2 == 0 else PANEL2)

        def refresh_row(i):
            bg = row_bg(i, i in selected_set)
            rw = row_widgets[i]
            rw.configure(bg=bg)
            for ch in rw.winfo_children():
                try:
                    ch.configure(bg=bg)
                except Exception:
                    pass

        def refresh_all_rows():
            for i in range(len(row_widgets)):
                refresh_row(i)

        def clear_mod_list():
            for w in inner.winfo_children():
                w.destroy()
            for w in status_inner.winfo_children():
                w.destroy()
            check_vars.clear()
            row_widgets.clear()
            status_labels.clear()
            progress_bars.clear()
            selected_set.clear()
            last_clicked[0] = None
            mod_count_var.set("0 found")
            dl_btn.configure(state="disabled")

        def populate_download_status(entries):
            for w in status_inner.winfo_children():
                w.destroy()
            status_labels.clear()
            progress_bars.clear()

            for i, entry in enumerate(entries):
                sbg = PANEL if i % 2 == 0 else PANEL2
                sf  = tk.Frame(status_inner, bg=sbg)
                sf.pack(fill="x")

                name = entry.get("display_name") or entry.get("filename") or "Unknown"

                tk.Label(
                    sf,
                    text=name,
                    bg=sbg,
                    fg=FG2,
                    font=("Segoe UI", 11),
                    anchor="w",
                ).pack(side="left", padx=(8, 4), pady=(4, 2), fill="x", expand=True)

                sl = tk.Label(sf, text="pending", bg=sbg, fg=FG2,
                              font=(MONO, 10), width=12, anchor="e")
                sl.pack(side="right", padx=(0, 8), pady=(4, 2))
                status_labels.append(sl)

                pf = tk.Frame(status_inner, bg=sbg)
                pf.pack(fill="x", padx=8, pady=(0, 4))

                pbar_var = tk.DoubleVar(value=0.0)
                pbar = ttk.Progressbar(pf, variable=pbar_var, maximum=100,
                                       length=100, mode="determinate",
                                       style="Download.Horizontal.TProgressbar")
                pbar.pack(side="left", fill="x", expand=True)

                pstats = tk.Label(pf, text="", bg=sbg, fg=FG2,
                                  font=(MONO, 9), anchor="w", width=28)
                pstats.pack(side="left", padx=(6, 0))

                progress_bars.append((pbar_var, pstats))

        # ── Drag-box selection ────────────────────────────────────────────────
        drag_start = [None]
        drag_rect  = [None]

        def drag_begin(e):
            drag_start[0] = canvas.canvasy(e.y)
            if drag_rect[0]:
                canvas.delete(drag_rect[0])
                drag_rect[0] = None
            if not (e.state & 0x4):   # no Ctrl → clear
                selected_set.clear()

        def drag_move(e):
            if drag_start[0] is None:
                return
            cy = canvas.canvasy(e.y)
            y0, y1 = sorted([drag_start[0], cy])
            if drag_rect[0] is None:
                drag_rect[0] = canvas.create_rectangle(
                    2, y0, canvas.winfo_width() - 2, y1,
                    outline=ACCENT, fill="", width=1, dash=(4, 2))
            else:
                canvas.coords(drag_rect[0], 2, y0, canvas.winfo_width() - 2, y1)
            for i, rw in enumerate(row_widgets):
                wy = rw.winfo_y()
                wh = rw.winfo_height()
                if wy + wh >= y0 and wy <= y1:
                    selected_set.add(i)
                else:
                    if not (e.state & 0x4):
                        selected_set.discard(i)
                refresh_row(i)

        def drag_end(e):
            if drag_rect[0]:
                canvas.delete(drag_rect[0])
                drag_rect[0] = None
            drag_start[0] = None

        canvas.bind("<ButtonPress-1>",   drag_begin)
        canvas.bind("<B1-Motion>",       drag_move)
        canvas.bind("<ButtonRelease-1>", drag_end)

        # ── Right-click context menu ──────────────────────────────────────────
        ctx = tk.Menu(root, tearoff=0, bg=PANEL2, fg=FG,
                      activebackground=ACCENT, activeforeground="#000",
                      font=("Segoe UI", 10), bd=0, relief="flat")

        def ctx_enable():
            for i in selected_set:
                check_vars[i][0].set(True)
        def ctx_disable():
            for i in selected_set:
                check_vars[i][0].set(False)
        def ctx_toggle():
            for i in selected_set:
                v = check_vars[i][0]
                v.set(not v.get())
        def ctx_sel_enabled():
            selected_set.clear()
            for i, (v, _) in enumerate(check_vars):
                if v.get():
                    selected_set.add(i)
            refresh_all_rows()
        def ctx_open_links():
            for i in selected_set:
                url = check_vars[i][1].get("nexus_url", "")
                if url:
                    webbrowser.open(url)

        ctx.add_command(label="Enable selected",        command=ctx_enable)
        ctx.add_command(label="Disable selected",       command=ctx_disable)
        ctx.add_command(label="Toggle selected",        command=ctx_toggle)
        ctx.add_separator()
        ctx.add_command(label="Select all enabled",     command=ctx_sel_enabled)
        ctx.add_separator()
        ctx.add_command(label="Open links in browser",  command=ctx_open_links)

        def show_ctx(e):
            try:
                ctx.tk_popup(e.x_root, e.y_root)
            finally:
                ctx.grab_release()

        # ── Row click (Shift / Ctrl) ──────────────────────────────────────────
        def row_click(e, idx):
            shift = bool(e.state & 0x1)
            ctrl  = bool(e.state & 0x4)
            if shift and last_clicked[0] is not None:
                lo, hi = sorted([last_clicked[0], idx])
                for i in range(lo, hi + 1):
                    selected_set.add(i)
            elif ctrl:
                if idx in selected_set:
                    selected_set.discard(idx)
                else:
                    selected_set.add(idx)
            else:
                selected_set.clear()
                selected_set.add(idx)
            last_clicked[0] = idx
            refresh_all_rows()

        def row_rclick(e, idx):
            if idx not in selected_set:
                selected_set.clear()
                selected_set.add(idx)
                refresh_all_rows()
            show_ctx(e)

        # ── Populate list ─────────────────────────────────────────────────────
        def populate_mod_list(entries):
            clear_mod_list()
            if not entries:
                tk.Label(inner, text="No mods found.", bg=PANEL, fg=FG2,
                         font=("Segoe UI", 9)).pack(padx=12, pady=12)
                return

            mod_count_var.set(f"{len(entries)} found")

            for i, entry in enumerate(entries):
                bg = row_bg(i)

                # ── List row ──────────────────────────────────────────────────
                rw = tk.Frame(inner, bg=bg)
                rw.pack(fill="x")
                row_widgets.append(rw)

                var = tk.BooleanVar(value=True)
                check_vars.append((var, entry))

                cb = tk.Checkbutton(rw, variable=var, bg=bg, fg=FG,
                                    activebackground=bg, activeforeground=ACCENT,
                                    selectcolor=PANEL, highlightthickness=0, bd=0)
                cb.pack(side="left", padx=(8, 2), pady=5)

                name = entry.get("display_name") or entry.get("filename") or "Unknown"
                lbl  = tk.Label(rw, text=name, bg=bg, fg=FG,
                                font=("Segoe UI", 10), anchor="w", wraplength=230)
                lbl.pack(side="left", fill="x", expand=True, padx=(0, 6))

                url = entry.get("nexus_url", "")
                if url:
                    lnk = tk.Label(rw, text="Mod's Link ↗", bg=bg, fg=ACCENT,
                                   font=("Segoe UI", 9, "underline"), cursor="hand2")
                    lnk.pack(side="right", padx=(0, 8))
                    lnk.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))

                mod_title = entry.get("mod_title") or ""
                if mod_title:
                    mt = tk.Label(rw, text=mod_title, bg=bg, fg=FG2,
                                  font=("Segoe UI", 9), anchor="e")
                    mt.pack(side="right", padx=(0, 8))

                for w in (rw, lbl, cb):
                    w.bind("<Button-1>", lambda e, ii=i: row_click(e, ii))
                    w.bind("<Button-3>", lambda e, ii=i: row_rclick(e, ii))
                    w.bind("<MouseWheel>", _scroll_list)

            dl_btn.configure(state="normal")

        def set_mod_status(idx, status):
            if idx < len(status_labels):
                sl = status_labels[idx]
                sl.configure(text=status, fg=STATUS_COLORS.get(status, FG2))

        def toggle_all(state):
            for v, _ in check_vars:
                v.set(state)

        # ══════════════════════════════════════════════════════════════════════
        # Analysis flow
        # ══════════════════════════════════════════════════════════════════════
        _cur_entries = []

        def start_analysis():
            if not path_var.get().strip():
                append_log("Enter a path or URL first.")
                return
            analyze_btn.configure(state="disabled")
            dl_btn.configure(state="disabled")
            clear_mod_list()
            status_var.set("Analyzing…")
            append_log("Starting analysis…")

            def worker():
                try:
                    ok = load_config_and_browser()
                    if not ok:
                        root.after(0, lambda: [status_var.set("Config error"),
                                               analyze_btn.configure(state="normal")])
                        return
                    if isinstance(raw_data, list):
                        run_analysis(raw_data,
                                     lambda e: root.after(0, lambda: finish_analysis(e)))
                    else:
                        e = build_collection_entries(raw_data)
                        root.after(0, lambda: finish_analysis(e))
                except Exception as ex:
                    log(f"Analysis error: {ex}\n{traceback.format_exc()}")
                    root.after(0, lambda: [status_var.set("Error"),
                                           analyze_btn.configure(state="normal")])

            threading.Thread(target=worker, daemon=True).start()

        def finish_analysis(entries):
            global _cur_entries
            _cur_entries = entries
            populate_mod_list(entries)
            status_var.set(f"Found {len(entries)} mod file(s) — ready to download")
            append_log(f"Analysis complete — {len(entries)} file(s) found.")
            analyze_btn.configure(state="normal")
            if entries:
                dl_btn.configure(state="normal")

        # ══════════════════════════════════════════════════════════════════════
        # Download flow
        # ══════════════════════════════════════════════════════════════════════
        def _clear_downloads_folder():
            """Wipe the ./downloads/ temp folder (partial/in-progress files only)."""
            if os.path.exists(downloads):
                for fname in os.listdir(downloads):
                    fp = os.path.join(downloads, fname)
                    try:
                        if os.path.isfile(fp):
                            os.remove(fp)
                    except Exception:
                        pass
            log("Temporary downloads folder cleared.")

        def stop_download():
            stop_event.set()
            log("Stop requested — finishing current chunk…")
            stop_btn.configure(state="disabled")

        def start_download():
            pairs    = [(i, e) for i, (v, e) in enumerate(check_vars) if v.get()]
            if not pairs:
                append_log("No mods selected.")
                return

            stop_event.clear()    # reset from any previous run

            orig_idx  = [i for i, _ in pairs]
            sel_ents  = [e for _, e in pairs]
            n         = len(pairs)

            populate_download_status(sel_ents)

            for i in range(n):
                root.after(0, lambda ii=i: set_mod_status(ii, "queued"))
                if i < len(progress_bars):
                    pvar, pstats = progress_bars[i]
                    pvar.set(0.0)
                    pstats.configure(text="")

            dl_btn.configure(state="disabled")
            analyze_btn.configure(state="disabled")
            stop_btn.configure(state="normal")
            status_var.set(f"Downloading {n} file(s)…")
            append_log(f"Starting {n} download(s), one at a time.")

            def on_status(pos, status):
                root.after(0, lambda: set_mod_status(pos, status))

            def _finish(stopped=False):
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
                if stopped:
                    _clear_downloads_folder()
                    root.after(0, lambda: [
                        status_var.set("Stopped"),
                        dl_btn.configure(state="normal"),
                        analyze_btn.configure(state="normal"),
                        stop_btn.configure(state="disabled"),
                    ])
                else:
                    root.after(0, lambda: [
                        status_var.set("Done"),
                        dl_btn.configure(state="normal"),
                        analyze_btn.configure(state="normal"),
                        stop_btn.configure(state="disabled"),
                    ])

            def worker():
                try:
                    ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
                    run_downloads(sel_ents, on_status)
                    _finish(stopped=stop_event.is_set())
                except Exception as ex:
                    log(f"Download error: {ex}\n{traceback.format_exc()}")
                    root.after(0, lambda: [
                        status_var.set("Download error — check log"),
                        dl_btn.configure(state="normal"),
                        analyze_btn.configure(state="normal"),
                        stop_btn.configure(state="disabled"),
                    ])

            threading.Thread(target=worker, daemon=True).start()

        # ── Start ─────────────────────────────────────────────────────────────
        try:
            ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
        except Exception:
            pass

        root.mainloop()

    except Exception as e:
        import traceback
        with open(crash, "w") as f:
            f.write(f"Failure:\n{str(e)}\n\n{traceback.format_exc()}")
        try:
            os.startfile(os.path.abspath(crash))
        except Exception:
            pass