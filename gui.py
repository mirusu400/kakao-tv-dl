#!/usr/bin/env python3
"""카카오TV 아카이브 — GUI

GUI 전용. 모든 다운로드 로직은 core.py에서 가져온다.
"""

import json
import logging
import queue
import re
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox

from core import (
    ROOT, STATE_DIR, DATA_DIR,
    classify_input, load_cookies,
    download_single_video, search_and_download,
    channel_download, cafe_download_all,
)

# ── 로깅 → GUI 연동 ─────────────────────────────────────────────────────

log_queue: queue.Queue = queue.Queue()


class QueueHandler(logging.Handler):
    def emit(self, record):
        log_queue.put(self.format(record))


logger = logging.getLogger("kakao-tv-dl")
logger.setLevel(logging.INFO)
_qh = QueueHandler()
_qh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(_qh)

# ═══════════════════════════════════════════════════════════════════════════
#  GUI
# ═══════════════════════════════════════════════════════════════════════════


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("카카오TV 아카이브")
        self.geometry("820x700")
        self.configure(bg="#f5f5f5")
        self.stop_event = threading.Event()
        self.worker_thread = None
        self.cookies = {}
        self._build_ui()
        self._poll_log()

    # ── UI 빌드 ──────────────────────────────────────────────────────────

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Yellow.TButton", background="#fae100", foreground="#1e1e1e",
                        font=("sans-serif", 11, "bold"))
        style.configure("TLabel", background="#f5f5f5", font=("sans-serif", 10))
        style.configure("Header.TLabel", font=("sans-serif", 16, "bold"), background="#f5f5f5")

        # ── 헤더 ──
        hdr = ttk.Frame(self, padding=10)
        hdr.pack(fill="x")
        ttk.Label(hdr, text="kakao tv 아카이브", style="Header.TLabel").pack(side="left")

        # ── URL 입력 ──
        frm_input = ttk.LabelFrame(self, text="  URL / 검색어 입력  ", padding=8)
        frm_input.pack(fill="x", padx=10, pady=(0, 5))

        self.txt_urls = scrolledtext.ScrolledText(
            frm_input, height=5, font=("Consolas", 11),
            wrap="word", bg="#fff", fg="#1e1e1e", insertbackground="#1e1e1e",
            relief="solid", borderwidth=1,
        )
        self.txt_urls.pack(fill="x")
        self.txt_urls.insert("1.0",
            "# 한 줄에 하나씩 입력 (영상/채널 URL, 검색어, 카페 URL)\n"
            "# 예: https://tv.kakao.com/channel/12345\n"
            "# 예: 더빙\n"
            "# 예: https://cafe.daum.net/mycafe (쿠키 필요)\n")

        # ── 쿠키 ──
        frm_cookie = ttk.Frame(self, padding=(10, 0))
        frm_cookie.pack(fill="x")
        ttk.Label(frm_cookie, text="쿠키 (카페용):").pack(side="left")
        self.var_cookie = tk.StringVar(
            value=str(ROOT / "cookies.txt") if (ROOT / "cookies.txt").exists() else "")
        ent = ttk.Entry(frm_cookie, textvariable=self.var_cookie, width=50)
        ent.pack(side="left", padx=5)
        ttk.Button(frm_cookie, text="찾아보기", command=self._browse_cookie).pack(side="left")
        ttk.Button(frm_cookie, text="붙여넣기", command=self._paste_cookie).pack(side="left", padx=5)

        # ── 버튼 ──
        frm_btn = ttk.Frame(self, padding=10)
        frm_btn.pack(fill="x")
        self.btn_start = ttk.Button(frm_btn, text=" 시작 ", style="Yellow.TButton",
                                    command=self._start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(frm_btn, text=" 중지 ", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=5)
        self.lbl_status = ttk.Label(frm_btn, text="대기 중")
        self.lbl_status.pack(side="left", padx=10)

        # ── 작업 큐 ──
        frm_queue = ttk.LabelFrame(self, text="  작업 큐  ", padding=5)
        frm_queue.pack(fill="both", expand=True, padx=10, pady=5)

        cols = ("type", "input", "status")
        self.tree = ttk.Treeview(frm_queue, columns=cols, show="headings", height=6)
        self.tree.heading("type", text="유형")
        self.tree.heading("input", text="입력")
        self.tree.heading("status", text="상태")
        self.tree.column("type", width=70, stretch=False)
        self.tree.column("input", width=450)
        self.tree.column("status", width=200)
        sb = ttk.Scrollbar(frm_queue, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # ── 로그 ──
        frm_log = ttk.LabelFrame(self, text="  로그  ", padding=5)
        frm_log.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.txt_log = scrolledtext.ScrolledText(
            frm_log, height=10, font=("Consolas", 10),
            bg="#1e1e1e", fg="#e0e0e0", insertbackground="#e0e0e0",
            wrap="word", state="disabled", relief="flat",
        )
        self.txt_log.pack(fill="both", expand=True)

    # ── 이벤트 핸들러 ────────────────────────────────────────────────────

    def _browse_cookie(self):
        path = filedialog.askopenfilename(
            title="쿠키 파일 선택",
            filetypes=[("Text/JSON", "*.txt *.json"), ("All", "*.*")])
        if path:
            self.var_cookie.set(path)

    def _paste_cookie(self):
        """클립보드 내용을 cookies.txt로 저장."""
        try:
            text = self.clipboard_get()
        except Exception:
            messagebox.showwarning("붙여넣기", "클립보드가 비어있습니다")
            return
        if not text.strip():
            return
        cookies = {}
        for line in text.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = re.split(r'\s{2,}', line)
            if len(parts) >= 2 and not parts[0].startswith("."):
                cookies[parts[0]] = parts[1]
            elif "=" in line:
                k, v = line.split("=", 1)
                cookies[k.strip()] = v.strip()
        if cookies:
            path = ROOT / "cookies.txt"
            path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
            self.var_cookie.set(str(path))
            logger.info(f"쿠키 저장: {len(cookies)}개 → {path}")
        else:
            messagebox.showwarning("붙여넣기", "쿠키를 파싱할 수 없습니다")

    def _start(self):
        text = self.txt_urls.get("1.0", "end").strip()
        lines = [l.strip() for l in text.splitlines()
                 if l.strip() and not l.strip().startswith("#")]
        if not lines:
            messagebox.showinfo("알림", "URL 또는 검색어를 입력하세요")
            return

        # 쿠키 로드
        cookie_path = self.var_cookie.get().strip()
        if cookie_path:
            self.cookies = load_cookies(cookie_path)

        # 작업 큐 구성
        self.tree.delete(*self.tree.get_children())
        tasks = []
        for line in lines:
            c = classify_input(line)
            if not c:
                continue
            label_map = {"video": "영상", "channel": "채널", "search": "검색", "cafe": "카페"}
            display = c.get("url") or c.get("query", "")
            iid = self.tree.insert("", "end",
                                   values=(label_map.get(c["type"], "?"), display[:60], "대기"))
            tasks.append((c, iid))

        if not tasks:
            return

        self.stop_event.clear()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.lbl_status.configure(text="작업 중...")

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        self.worker_thread = threading.Thread(target=self._worker, args=(tasks,), daemon=True)
        self.worker_thread.start()

    def _stop(self):
        self.stop_event.set()
        self.lbl_status.configure(text="중지 요청...")
        logger.info("중지 요청됨")

    def _worker(self, tasks):
        total = len(tasks)
        for idx, (task, iid) in enumerate(tasks):
            if self.stop_event.is_set():
                self._update_tree(iid, "중지됨")
                continue

            self.lbl_status.configure(text=f"작업 중... ({idx + 1}/{total})")

            def update(s, _iid=iid):
                self._update_tree(_iid, s)

            try:
                if task["type"] == "video":
                    update("다운로드 중...")
                    download_single_video(task["url"], update, self.stop_event)
                elif task["type"] == "channel":
                    channel_download(task["url"], update, self.stop_event)
                elif task["type"] == "search":
                    search_and_download(task["query"], update, self.stop_event)
                elif task["type"] == "cafe":
                    if not self.cookies:
                        update("실패: 쿠키 없음")
                        logger.error("카페 다운로드에 쿠키가 필요합니다")
                    else:
                        grpid = task.get("grpid", "")
                        if not grpid:
                            update("실패: grpid 없음")
                        else:
                            cafe_download_all(grpid, self.cookies, update, self.stop_event)
            except Exception as e:
                logger.error(f"에러: {e}")
                update(f"에러: {str(e)[:50]}")

        self.after(0, self._work_done)

    def _work_done(self):
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        status = "완료" if not self.stop_event.is_set() else "중지됨"
        self.lbl_status.configure(text=status)
        logger.info(f"=== 전체 작업 {status} ===")

    def _update_tree(self, iid, status):
        try:
            self.tree.set(iid, "status", status)
        except Exception:
            pass

    # ── 로그 폴링 ────────────────────────────────────────────────────────

    def _poll_log(self):
        while not log_queue.empty():
            try:
                msg = log_queue.get_nowait()
                self.txt_log.configure(state="normal")
                self.txt_log.insert("end", msg + "\n")
                self.txt_log.see("end")
                self.txt_log.configure(state="disabled")
            except queue.Empty:
                break
        self.after(100, self._poll_log)


# ═══════════════════════════════════════════════════════════════════════════

def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
