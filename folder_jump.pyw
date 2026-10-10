"""단축키(기본 Alt+`)로 즐겨찾기 폴더 창을 띄워 바로 이동하는 런처."""
import ctypes
import json
import os
import queue
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import urllib.request
import winreg
from ctypes import wintypes
from pathlib import Path
from tkinter import filedialog, messagebox

APP = "폴더 바로가기"
FROZEN = getattr(sys, "frozen", False)  # PyInstaller로 만든 exe로 실행 중인지
HERE = Path(sys.executable if FROZEN else __file__).resolve().parent
# exe는 실행할 때마다 임시 폴더에 풀리므로 설정·폰트를 %APPDATA%에 둔다. 스크립트는 파일 옆에 둔다.
DATA_DIR = Path(os.environ["APPDATA"]) / "FolderJumper" if FROZEN else HERE
CONFIG_FILE = DATA_DIR / "settings.json"
FONT_FILE = DATA_DIR / "fonts" / "Pretendard-Bold.otf"
FONT_URLS = (  # 공식 저장소(v1.3.9) 먼저, 안 되면 jsDelivr
    "https://raw.githubusercontent.com/orioncactus/pretendard/v1.3.9/packages/pretendard/dist/public/static/Pretendard-Bold.otf",
    "https://cdn.jsdelivr.net/npm/pretendard@1.3.9/dist/public/static/Pretendard-Bold.otf",
)
OLD_FOLDERS_FILE = DATA_DIR / "folders.json"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "FolderJumper"

MOD_ALT, MOD_CTRL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_APP_SETKEY = 0x0312, 0x8001
# 단축키로 쓸 수 없는 키: Shift/Ctrl/Alt/Win, Caps Lock, 한/영, 한자
NOT_HOTKEY = {0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x14, 0x15, 0x19}
EXTENDED_VK = set(range(0x21, 0x2F))  # PgUp ~ Delete: 이름 조회 시 확장키 비트 필요
MAX_ROWS = 7

# 디자인: Graphite & Amber (캔버스 시안 기준)
FONT, FALLBACK_FONT = "Pretendard", "맑은 고딕"  # Pretendard: https://github.com/orioncactus/pretendard
I_SEARCH, I_FOLDER, I_FOLDER_OPEN, I_STAR, I_GEAR = "\uE721", "\uE8B7", "\uE838", "\uE734", "\uE713"
I_ADD, I_UP, I_DOWN, I_DELETE, I_ENTER = "\uE710", "\uE70E", "\uE70D", "\uE74D", "\uE751"
THEMES = {
    "dark": dict(bg="#141416", raised="#1C1C20", footer="#18181B", nav="#18181B", caption="#101012",
                 line="#2A2A30", line2="#222227", keyline="#34343B", text="#EDEDEF", text2="#C9C9CF",
                 navtext="#B4B4BC", sub="#B0B0B8", sub2="#A2A2AB", accent="#F2B248", accent_fill="#F2B248",
                 accent_ring="#F2B248", on_accent="#1A1405", sel="#26231D", sel_hover="#332F27",
                 tile="#1F1F24", tile_sel="#3A3020", key_sel_fill="#2E2A22", key_sel_line="#4A4232",
                 hover="#202025", error="#FF8A80", switch_off="#3A3A42", knob="#EDEDEF"),
    "light": dict(bg="#FBFBFA", raised="#FFFFFF", footer="#F3F3F4", nav="#F3F3F4", caption="#F3F3F4",
                  line="#E2E2E6", line2="#E6E6EA", keyline="#D9D9DE", text="#18181B", text2="#3F3F46",
                  navtext="#3F3F46", sub="#4B4B54", sub2="#56565F", accent="#8A5409", accent_fill="#F2B248",
                  accent_ring="#D99A2B", on_accent="#1A1405", sel="#FFF1D6", sel_hover="#FBE3B8",
                  tile="#EFEFF1", tile_sel="#FADFAA", key_sel_fill="#FFF8EA", key_sel_line="#E9C98B",
                  hover="#EEEEF0", error="#C62828", switch_off="#C9C9CF", knob="#FFFFFF"),
}
# 테마 고르기 카드의 미리보기 색: (배경, 테두리, 막대1, 강조 막대, 막대3)
PREVIEWS = {"dark": ("#141416", "#34343B", "#4A4A52", "#3A3020", "#2E2E34"),
            "light": ("#FBFBFA", "#DEDEE3", "#C9C9CF", "#FADFAA", "#E4E4E7")}
DEFAULTS = {"hotkey": [MOD_ALT, 0xC0], "theme": "dark", "folders": []}

user32 = ctypes.windll.user32
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


# ---------- 설정 파일 ----------

def load_config():
    if CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    else:
        if OLD_FOLDERS_FILE.exists():
            paths = json.loads(OLD_FOLDERS_FILE.read_text(encoding="utf-8"))
        else:
            paths = [p for p in default_folders() if os.path.isdir(p)]
        cfg = {"folders": [{"name": Path(p).name or p, "path": p} for p in paths]}
    return {**DEFAULTS, **cfg}


def default_folders():
    """처음 쓰는 사람의 기본 즐겨찾기: 바탕 화면, 다운로드, 문서 (OneDrive로 옮겨진 경우도 찾는다)."""
    def shell_folder(csidl):
        buf = ctypes.create_unicode_buffer(260)
        ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
        return buf.value
    return [shell_folder(0x10), str(Path.home() / "Downloads"), shell_folder(0x05)]  # 0x10 바탕 화면, 0x05 문서


def save_config(cfg):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def filter_folders(folders, text):
    words = text.lower().split()
    return [f for f in folders if all(w in f"{f['name']} {f['path']}".lower() for w in words)]


def download_font():
    """Pretendard Bold를 fonts 폴더에 받는다. 실패하면 False (그때는 맑은 고딕으로 보인다)."""
    for url in FONT_URLS:
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                data = r.read()
        except OSError:
            continue
        if data[:4] == b"OTTO":  # OTF 파일이 맞는지 확인 (오류 페이지를 저장하지 않게)
            FONT_FILE.parent.mkdir(parents=True, exist_ok=True)
            FONT_FILE.write_bytes(data)
            return True
    return False


# ---------- 자동 실행 (HKCU\...\Run) ----------

def autostart_command():
    if FROZEN:
        return f'"{sys.executable}"'
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return f'"{pythonw if pythonw.exists() else exe}" "{Path(__file__).resolve()}"'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_NAME)
        return True
    except OSError:
        return False


def set_autostart(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass


# ---------- 단축키 ----------

def key_name(mod, vk):
    parts = [n for bit, n in ((MOD_CTRL, "Ctrl"), (MOD_ALT, "Alt"), (MOD_SHIFT, "Shift"), (MOD_WIN, "Win")) if mod & bit]
    if 0x70 <= vk <= 0x87:
        key = f"F{vk - 0x6F}"
    elif 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        key = chr(vk)
    elif vk in (0x13, 0x91, 0x2C):
        key = {0x13: "Pause", 0x91: "Scroll Lock", 0x2C: "PrtSc"}[vk]
    else:
        buf = ctypes.create_unicode_buffer(32)
        lparam = user32.MapVirtualKeyW(vk, 0) << 16 | (1 << 24 if vk in EXTENDED_VK else 0)
        key = buf.value if user32.GetKeyNameTextW(lparam, buf, 32) else f"0x{vk:02X}"
    return " + ".join(parts + [key])


class Hotkey:
    """RegisterHotKey 메시지는 등록한 스레드로 오므로, 등록·해제·수신을 전용 스레드 하나에서 한다."""

    def __init__(self, events):
        self.events, self.replies = events, queue.Queue()
        ready = threading.Event()
        threading.Thread(target=self._run, args=(ready,), daemon=True).start()
        ready.wait()

    def _run(self, ready):
        msg = wintypes.MSG()
        self.tid = kernel32.GetCurrentThreadId()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # 메시지 큐 생성
        ready.set()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self.events.put(None)
            elif msg.message == WM_APP_SETKEY:
                user32.UnregisterHotKey(None, 1)
                ok = not msg.lParam or bool(user32.RegisterHotKey(None, 1, msg.wParam | MOD_NOREPEAT, msg.lParam))
                self.replies.put(ok)

    def set(self, mod, vk):
        """단축키를 바꾼다. vk=0이면 해제만 한다. 등록 성공 여부를 돌려준다."""
        user32.PostThreadMessageW(self.tid, WM_APP_SETKEY, mod, vk)
        return self.replies.get(timeout=2)


# ---------- 그리기 도구 ----------

def colorref(hex_color):
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return b << 16 | g << 8 | r


def dwm_set(widget, attr, value):
    """Windows 11 창 속성(둥근 모서리, 테두리·제목 표시줄 색). 지원 안 되면 무시."""
    v = ctypes.c_int(value)
    try:
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            user32.GetParent(widget.winfo_id()), attr, ctypes.byref(v), ctypes.sizeof(v))
    except OSError:
        pass


def ellipsize(font, text, width):
    if font.measure(text) <= width:
        return text
    while text and font.measure(text + "…") > width:
        text = text[:-1]
    return text + "…"


class Surface(tk.Canvas):
    """매번 통째로 다시 그리는 캔버스. 클릭·호버는 그릴 때 등록한 영역(hit)으로 처리한다."""

    def __init__(self, master, draw, **kw):
        super().__init__(master, highlightthickness=0, borderwidth=0, **kw)
        self.draw, self.hits, self.hover = draw, [], None
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self.set_hover(None))
        self.bind("<Button-1>", self._click)

    def redraw(self):
        self.delete("d")  # 직접 그린 것만 지우고 Entry 같은 위젯 창("keep")은 그대로 둔다
        self.hits = []
        self.draw(self)

    def hit(self, key, box, action):
        self.hits.append((key, box, action))

    def rrect(self, x1, y1, x2, y2, r, **kw):
        r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
        pts = (x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y1 + r, x2, y2 - r,
               x2, y2 - r, x2, y2, x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2, x1, y2, x1, y2 - r,
               x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1)
        kw.setdefault("outline", "")
        return self.create_polygon(pts, smooth=True, tags="d", **kw)

    def text(self, x, y, text, **kw):
        return self.create_text(x, y, text=text, tags="d", **kw)

    def line(self, *pts, **kw):
        return self.create_line(*pts, tags="d", **kw)

    def box(self, x1, y1, x2, y2, **kw):
        return self.create_rectangle(x1, y1, x2, y2, width=0, tags="d", **kw)

    def oval(self, x1, y1, x2, y2, **kw):
        return self.create_oval(x1, y1, x2, y2, width=0, tags="d", **kw)

    def _find(self, x, y):
        for key, (x1, y1, x2, y2), action in reversed(self.hits):
            if x1 <= x < x2 and y1 <= y < y2:
                return key, action
        return None, None

    def _motion(self, e):
        key, _ = self._find(e.x, e.y)
        self.configure(cursor="hand2" if key else "")
        self.set_hover(key)

    def set_hover(self, key):
        if key != self.hover:
            self.hover = key
            self.redraw()

    def _click(self, e):
        _, action = self._find(e.x, e.y)
        if action:
            action()


# ---------- 앱 ----------

class App:
    def __init__(self, root, hotkey):
        self.root, self.hk = root, hotkey
        self.cfg = load_config()
        self.c = THEMES[self.cfg["theme"]]
        self.scale = root.winfo_fpixels("1i") / 96
        self.visible = False
        self.busy = False  # 대화상자가 떠 있는 동안은 포커스를 잃어도 숨기지 않는다
        self.capturing = False
        self.hk_error = None
        self.settings = None
        families = set(tkfont.families(root))
        icon = "Segoe Fluent Icons" if "Segoe Fluent Icons" in families else "Segoe MDL2 Assets"
        mono = "Cascadia Mono" if "Cascadia Mono" in families else "Consolas"
        ui = FONT if FONT in families else FALLBACK_FONT
        F = lambda family, px, bold=False: tkfont.Font(
            root, family=family, size=-self.px(px), weight="bold" if bold else "normal")
        # 작은 글씨도 Bold: GDI가 OTF를 가늘게 그려서 Regular~SemiBold는 흐려 보인다
        self.f = dict(sm=F(ui, 13, True), md=F(ui, 14, True), mdb=F(ui, 14, True), lg=F(ui, 15, True),
                      lgb=F(ui, 15, True), name=F(ui, 16, True), title=F(ui, 21, True),
                      mono_sm=F(mono, 13), mono_md=F(mono, 14),
                      icon12=F(icon, 12), icon14=F(icon, 14), icon16=F(icon, 16), icon18=F(icon, 18))
        root.title(APP)
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        self.build_popup()

    def px(self, n):
        return round(n * self.scale)

    def save(self):
        save_config(self.cfg)

    def dialog(self, fn, *args, **kwargs):
        self.busy = True
        try:
            return fn(*args, **kwargs)
        finally:
            self.busy = False

    def paint_widgets(self):
        """테마 색을 캔버스 밖의 실제 위젯(입력칸 등)에 칠한다."""
        c = self.c
        self.cv.configure(bg=c["bg"])
        if self.settings:
            self.settings.configure(bg=c["bg"])
            self.nav.configure(bg=c["nav"])
            self.main.configure(bg=c["bg"])
            self.fav.configure(bg=c["bg"])
            self.name_entry.configure(bg=c["raised"], fg=c["text"], insertbackground=c["text"])

    # ----- 공통 부품 -----

    def keycap(self, cv, x, cy, text, font=None, fg=None, fill=None, outline=None, h=22, r=6, right=True):
        """키보드 키 모양 표시. right=True면 x가 오른쪽 끝. 반대쪽 끝 x를 돌려준다."""
        c, p = self.c, self.px
        font = font or (self.f["mono_sm"] if text == "`" else self.f["sm"])
        w = font.measure(text) + p(14)
        x1 = x - w if right else x
        cv.rrect(x1, cy - p(h) / 2, x1 + w, cy + p(h) / 2, p(r), fill=fill or c["raised"], outline=outline or c["keyline"])
        cv.text(x1 + w / 2, cy, text, font=font, fill=fg or c["sub"])
        return x1 if right else x1 + w

    def button(self, cv, key, x, y, text, action, primary=False, icon=None, h=38, right=False):
        c, f, p = self.c, self.f, self.px
        font = f["mdb"] if primary else f["md"]
        icon_w = f["icon14"].measure(icon) + p(6) if icon else 0
        w = font.measure(text) + icon_w + p(28)
        x1 = x - w if right else x
        if primary:
            fill, fg, outline = c["accent_fill"], c["on_accent"], ""
        else:
            fill, fg, outline = (c["hover"] if cv.hover == key else c["tile"]), c["text"], c["keyline"]
        cv.rrect(x1, y, x1 + w, y + p(h), p(9), fill=fill, outline=outline)
        tx = x1 + p(14)
        if icon:
            cv.text(tx, y + p(h) / 2, icon, anchor="w", font=f["icon14"], fill=fg)
            tx += icon_w
        cv.text(tx, y + p(h) / 2, text, anchor="w", font=font, fill=fg)
        cv.hit(key, (x1, y, x1 + w, y + p(h)), action)
        return x1

    # ----- 검색 팝업 -----

    def build_popup(self):
        r = self.root
        self.cv = Surface(r, self.draw_popup)
        self.cv.pack(fill="both", expand=True)
        self.query = tk.StringVar()
        self.renaming = None  # 이름을 고치는 중인 줄 번호

        # 검색창 없이 팝업에서 바로 입력받는다. 입력한 글자는 목록 머리 줄에 보인다.
        e = self.cv
        e.bind("<Key>", self.on_type)
        e.bind("<BackSpace>", lambda ev: self.query.set(self.query.get()[:-1]))
        e.bind("<Control-BackSpace>", lambda ev: self.query.set(""))
        e.bind("<Control-v>", lambda ev: self.paste())
        e.bind("<Return>", lambda ev: self.open_selected())
        e.bind("<Escape>", lambda ev: self.query.set("") if self.query.get() else self.hide())
        e.bind("<Down>", lambda ev: self.move(1))
        e.bind("<Up>", lambda ev: self.move(-1))
        e.bind("<Control-d>", lambda ev: self.quick_add())
        e.bind("<F2>", lambda ev: self.start_rename())
        e.bind("<Control-comma>", lambda ev: self.open_settings())
        e.bind("<Control-q>", lambda ev: r.destroy())
        r.bind("<MouseWheel>", lambda ev: self.move(-1 if ev.delta > 0 else 1))
        r.bind("<FocusOut>", lambda ev: r.after(150, self.maybe_hide))
        self.query.trace_add("write", lambda *_: self.on_query())
        self.paint_widgets()
        self.on_query()

    def on_type(self, e):
        if e.state & 0x4 or not e.char or not e.char.isprintable():  # Ctrl 조합·특수키는 제외
            return
        self.query.set(self.query.get() + e.char)

    def paste(self):
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            return
        self.query.set(self.query.get() + " ".join(text.split("\n")).strip())

    def typed_dir(self):
        typed = self.query.get().strip().strip('"')
        return typed if typed and os.path.isdir(typed) else None

    def on_query(self):
        q = self.query.get()
        self.typed = self.typed_dir()
        if self.typed:
            self.items = [dict(kind="open", title="이 폴더 열기", sub="탐색기에서 바로 엽니다"),
                          dict(kind="add", title="즐겨찾기에 추가", sub="다음부터 이름으로 찾을 수 있어요")]
            self.header, self.count = "입력한 경로", ""
        else:
            self.items = [dict(kind="folder", title=f["name"], sub=f["path"])
                          for f in filter_folders(self.cfg["folders"], q)]
            self.header = "즐겨찾기"
            self.count = f"{len(self.items)}개" if q.strip() else f"입력해서 검색 · {len(self.items)}개"
        self.sel = self.offset = 0
        self.cv.hover = None
        self.cv.redraw()

    def draw_popup(self, cv):
        c, f, p = self.c, self.f, self.px
        if self.renaming is None and cv.hover and cv.hover.startswith("row:"):  # 마우스를 올린 줄이 선택된다
            self.sel = self.offset + int(cv.hover[4:])
        W = p(680)
        n = min(len(self.items), MAX_ROWS)
        list_h = n * p(56) + max(n - 1, 0) * p(2) + p(10) if n else p(64)
        H = p(44) + list_h + p(44)
        cv.configure(width=W, height=H)

        # 머리 줄: 입력 중이면 입력한 글자, 아니면 "즐겨찾기"
        q = self.query.get()
        cv.text(W - p(20), p(24), self.count, anchor="e", font=f["sm"], fill=c["sub"])
        if q:
            font = f["mono_md"] if self.typed else f["lgb"]
            avail = W - p(40) - p(20) - f["sm"].measure(self.count) - p(16)
            cv.text(p(20), p(24), I_SEARCH, anchor="w", font=f["icon14"], fill=c["accent"])
            cv.text(p(42), p(24), ellipsize(font, q, avail), anchor="w", font=font, fill=c["text"])
        else:
            cv.text(p(20), p(24), self.header, anchor="w", font=f["sm"], fill=c["sub"])

        # 목록
        y0 = p(44)
        for i in range(n):
            k = self.offset + i
            item, sel = self.items[k], k == self.sel
            y = y0 + i * (p(56) + p(2))
            if sel:
                cv.rrect(p(8), y, W - p(8), y + p(56), p(10), fill=c["sel"])
            cv.rrect(p(20), y + p(10), p(56), y + p(46), p(9), fill=c["tile_sel"] if sel else c["tile"])
            glyph = {"folder": I_FOLDER, "open": I_FOLDER_OPEN, "add": I_STAR}[item["kind"]]
            cv.text(p(38), y + p(28), glyph, font=f["icon18"], fill=c["accent"] if sel else c["sub"])
            right = W - p(20)
            if sel and item["kind"] == "add":
                right = self.keycap(cv, right, y + p(28), "Ctrl D", fg=c["text2"]) - p(12)
            elif sel:
                right = self.keycap(cv, right, y + p(28), I_ENTER, font=f["icon12"], fg=c["accent"],
                                    fill=c["key_sel_fill"], outline=c["key_sel_line"])
                if item["kind"] == "folder":
                    label = "저장" if k == self.renaming else "열기"
                    right -= p(8)
                    cv.text(right, y + p(28), label, anchor="e", font=f["sm"], fill=c["text2"])
                    right -= f["sm"].measure(label)
                right -= p(12)
            sub_font = f["mono_md"] if item["kind"] == "folder" else f["md"]
            avail = right - p(70)
            if k == self.renaming:
                cv.rrect(p(64), y + p(5), right, y + p(31), p(6), fill=c["raised"], outline=c["accent_ring"])
                cv.coords(self.rename_win, p(70), y + p(18))
                cv.itemconfigure(self.rename_win, width=right - p(76))
            else:
                cv.text(p(70), y + p(19), ellipsize(f["name"], item["title"], avail), anchor="w", font=f["name"], fill=c["text"])
            cv.text(p(70), y + p(38), ellipsize(sub_font, item["sub"], avail), anchor="w", font=sub_font, fill=c["sub"])
            cv.hit(f"row:{i}", (p(8), y, W - p(8), y + p(56)), lambda k=k: self.activate(k))
        if not n:
            msg = ("즐겨찾기가 비어 있어요 · Ctrl+D로 폴더를 추가하세요" if not self.cfg["folders"]
                   else "일치하는 폴더가 없어요")
            cv.text(W / 2, y0 + p(24), msg, font=f["md"], fill=c["sub"])

        # 아래 안내 줄
        fy = H - p(44)
        cv.box(0, fy, W, H, fill=c["footer"])
        cv.line(0, fy, W, fy, fill=c["line"])
        x = p(20)
        for key, label in (("↑↓", "이동"), ("F2", "이름 변경"), ("Ctrl D", "추가"), ("Esc", "닫기")):
            x = self.keycap(cv, x, fy + p(22), key, fg=c["text2"], fill=c["footer"], h=22, r=5, right=False) + p(6)
            cv.text(x, fy + p(22), label, anchor="w", font=f["sm"], fill=c["sub"])
            x += f["sm"].measure(label) + p(16)
        parts = ((I_GEAR, f["icon14"], c["text2"]), ("설정", f["sm"], c["text2"]), ("Ctrl ,", f["sm"], c["sub2"]))
        bw = sum(font.measure(t) for t, font, _ in parts) + p(6) * 2 + p(20)
        bx1, bx2 = W - p(10) - bw, W - p(10)
        if cv.hover == "settings":
            cv.rrect(bx1, fy + p(6), bx2, fy + p(38), p(8), fill=c["hover"])
        x = bx1 + p(10)
        for t, font, color in parts:
            cv.text(x, fy + p(22), t, anchor="w", font=font, fill=color)
            x += font.measure(t) + p(6)
        cv.hit("settings", (bx1, fy + p(6), bx2, fy + p(38)), self.open_settings)

        if self.visible:
            self.fit(W, H)

    def fit(self, w, h):
        r = self.root
        r.geometry(f"{w}x{h}+{(r.winfo_screenwidth() - w) // 2}+{r.winfo_screenheight() // 4}")

    def start_rename(self):
        """선택한 즐겨찾기의 이름 자리를 입력칸으로 바꾼다. Enter 저장, Esc 취소."""
        if self.renaming is not None or not self.items or self.items[self.sel]["kind"] != "folder":
            return "break"
        self.renaming = self.sel
        c = self.c
        self.rename_var = tk.StringVar(value=self.items[self.sel]["title"])
        e = self.rename_entry = tk.Entry(self.cv, textvariable=self.rename_var, font=self.f["name"], relief="flat",
                                         borderwidth=0, highlightthickness=0, bg=c["raised"], fg=c["text"],
                                         insertbackground=c["text"], selectbackground=c["accent_fill"],
                                         selectforeground=c["on_accent"])
        self.rename_win = self.cv.create_window(0, 0, window=e, anchor="w", tags="keep")
        e.bind("<Return>", lambda ev: self.finish_rename(True))
        e.bind("<Escape>", lambda ev: self.finish_rename(False) or "break")
        e.bind("<FocusOut>", lambda ev: self.finish_rename(True))
        self.cv.redraw()
        e.select_range(0, "end")
        e.icursor("end")
        e.focus_set()
        return "break"

    def finish_rename(self, save):
        if self.renaming is None:
            return
        path, name = self.items[self.renaming]["sub"], self.rename_var.get().strip()
        self.renaming = None
        self.cv.delete(self.rename_win)
        self.rename_entry.destroy()
        if save and name:
            for folder in self.cfg["folders"]:
                if folder["path"] == path:
                    folder["name"] = name
            self.save()
            if self.settings:
                self.sync_name()
                self.fav.redraw()
        self.on_query()
        for k, item in enumerate(self.items):  # 이름이 바뀐 줄을 계속 선택해 둔다
            if item["sub"] == path:
                self.sel, self.offset = k, max(0, k - MAX_ROWS + 1)
                break
        self.cv.redraw()
        self.cv.focus_set()

    def move(self, step):
        if self.renaming is not None:
            return "break"
        if self.items:
            self.sel = max(0, min(len(self.items) - 1, self.sel + step))
            if self.sel < self.offset:
                self.offset = self.sel
            elif self.sel >= self.offset + MAX_ROWS:
                self.offset = self.sel - MAX_ROWS + 1
            self.cv.hover = None
            self.cv.redraw()
        return "break"

    def activate(self, k):
        if self.renaming is not None:  # 이름을 고치는 중에 목록을 누르면 저장만 한다
            self.finish_rename(True)
            return
        self.sel = k
        self.open_selected()

    def open_selected(self):
        if not self.items:
            return
        item = self.items[self.sel]
        if item["kind"] == "add":
            self.quick_add()
            return
        path = self.typed if item["kind"] == "open" else item["sub"]
        if not os.path.isdir(path):
            self.dialog(messagebox.showerror, "폴더 없음", f"폴더를 찾을 수 없어요:\n{path}", parent=self.root)
            self.focus_popup()
            return
        os.startfile(path)
        self.hide()

    def add_folder(self, path):
        path = os.path.normpath(path)
        folders = self.cfg["folders"]
        for i, f in enumerate(folders):
            if f["path"].lower() == path.lower():
                return i
        folders.append({"name": Path(path).name or path, "path": path})
        self.save()
        return len(folders) - 1

    def quick_add(self):
        path = self.typed_dir() or self.dialog(filedialog.askdirectory, parent=self.root)
        if path:
            self.add_folder(path)
            self.query.set("")
        self.focus_popup()
        return "break"

    def toggle(self):
        if self.visible:
            self.hide()
        else:
            self.show()

    def show(self):
        self.visible = True
        self.query.set("")
        self.root.deiconify()
        self.cv.redraw()
        dwm_set(self.root, 33, 2)  # 둥근 모서리
        dwm_set(self.root, 34, colorref(self.c["line"]))  # 테두리 색
        self.focus_popup()

    def focus_popup(self):
        self.root.lift()
        user32.SetForegroundWindow(user32.GetParent(self.root.winfo_id()))
        self.root.focus_force()
        self.cv.focus_set()

    def hide(self):
        self.finish_rename(True)
        self.visible = False
        self.root.withdraw()

    def maybe_hide(self):
        if not self.visible or self.busy:
            return
        try:
            focused = self.root.focus_get()
        except KeyError:
            focused = None
        if focused is None:
            self.hide()

    # ----- 설정 창 -----

    def open_settings(self, page=None):
        self.hide()
        if self.settings:
            if page:
                self.page = page
                self.redraw_settings()
            self.settings.deiconify()
            self.settings.lift()
            self.settings.focus_force()
            return
        p = self.px
        s = self.settings = tk.Toplevel(self.root)
        s.withdraw()
        s.title(f"{APP} 설정")
        s.resizable(False, False)
        self.page = page or "fav"
        self.fav_sel = 0 if self.cfg["folders"] else None
        self.fav_scroll = 0
        self.autostart = autostart_enabled()

        W, H = p(760), p(500)
        self.nav = Surface(s, self.draw_nav, width=p(184), height=H)
        self.nav.pack(side="left", fill="y")
        self.main = Surface(s, self.draw_main, width=W - p(184), height=H)
        self.main.pack(side="left", fill="both", expand=True)
        self.fav_w, self.fav_h = W - p(184) - p(56) - p(12), p(308) - p(12)
        self.fav = Surface(self.main, self.draw_fav_list, width=self.fav_w, height=self.fav_h)
        self.fav.bind("<MouseWheel>", lambda e: self.scroll_fav(-1 if e.delta > 0 else 1))
        self.fav_win = self.main.create_window(p(34), p(94), window=self.fav, anchor="nw", tags="keep")
        self.name_var = tk.StringVar()
        self.name_entry = tk.Entry(self.main, textvariable=self.name_var, relief="flat", borderwidth=0,
                                   highlightthickness=0, font=self.f["lg"])
        self.name_entry.bind("<Return>", lambda e: self.fav_rename())
        self.name_entry.bind("<FocusIn>", lambda e: self.main.redraw())
        self.name_entry.bind("<FocusOut>", lambda e: self.main.redraw())
        self.name_win = self.main.create_window(p(42), p(456), window=self.name_entry, anchor="w", tags="keep")

        s.bind("<Escape>", lambda e: self.cancel_capture() if self.capturing else self.close_settings())
        s.protocol("WM_DELETE_WINDOW", self.close_settings)
        self.paint_widgets()
        self.sync_name()
        self.redraw_settings()
        s.geometry(f"{W}x{H}+{(s.winfo_screenwidth() - W) // 2}+{(s.winfo_screenheight() - H) // 3}")
        self.paint_settings_frame()
        s.deiconify()
        s.lift()
        s.focus_force()

    def paint_settings_frame(self):
        dark = self.cfg["theme"] == "dark"
        dwm_set(self.settings, 20, dark)  # 다크 제목 표시줄
        dwm_set(self.settings, 35, colorref(self.c["caption"]))  # 제목 표시줄 색
        dwm_set(self.settings, 36, colorref(self.c["sub"]))  # 제목 글자 색

    def close_settings(self):
        if self.capturing:
            self.cancel_capture()
        self.settings.destroy()
        self.settings = None

    def redraw_settings(self):
        self.nav.redraw()
        self.main.redraw()

    def set_page(self, page):
        self.page = page
        self.redraw_settings()

    def draw_nav(self, cv):
        c, f, p = self.c, self.f, self.px
        W, H = p(184), p(500)
        cv.line(W - 1, 0, W - 1, H, fill=c["line2"])
        for i, (key, label, icon) in enumerate((("fav", "즐겨찾기", I_FOLDER), ("general", "일반", I_GEAR))):
            y = p(16) + i * p(44)
            active = self.page == key
            if active or cv.hover == key:
                cv.rrect(p(10), y, W - p(10), y + p(40), p(9), fill=c["sel"] if active else c["hover"])
            cv.text(p(24), y + p(20), icon, anchor="w", font=f["icon16"], fill=c["accent"] if active else c["navtext"])
            cv.text(p(50), y + p(20), label, anchor="w", font=f["lgb"] if active else f["lg"],
                    fill=c["text"] if active else c["navtext"])
            cv.hit(key, (p(10), y, W - p(10), y + p(40)), lambda k=key: self.set_page(k))
        cv.text(p(22), H - p(18), "변경 사항은\n바로 저장돼요", anchor="sw", font=f["sm"], fill=c["sub2"], justify="left")

    def draw_main(self, cv):
        on_fav = self.page == "fav"
        for win in (self.fav_win, self.name_win):
            cv.itemconfigure(win, state="normal" if on_fav else "hidden")
        if on_fav:
            self.draw_fav_page(cv)
            self.fav.redraw()
        else:
            self.draw_general_page(cv)

    def card(self, cv, x1, y1, x2, y2, title, desc, desc_color=None):
        c, f, p = self.c, self.f, self.px
        cv.rrect(x1, y1, x2, y2, p(12), fill=c["bg"], outline=c["line"])
        cv.text(x1 + p(18), y1 + p(24), title, anchor="w", font=f["lgb"], fill=c["text"])
        cv.text(x1 + p(18), y1 + p(48), desc, anchor="w", font=f["sm"], fill=desc_color or c["sub"])

    # 즐겨찾기 화면

    def draw_fav_page(self, cv):
        c, f, p = self.c, self.f, self.px
        x1, x2 = p(28), p(576) - p(28)
        cv.text(x1, p(26), "즐겨찾기", anchor="nw", font=f["title"], fill=c["text"])
        cv.text(x1, p(60), f"{key_name(*self.cfg['hotkey'])} 팝업에 이 순서대로 보여요", anchor="nw",
                font=f["md"], fill=c["sub"])
        self.button(cv, "add", x2, p(26), "폴더 추가", self.fav_add, primary=True, icon=I_ADD, right=True)
        cv.rrect(x1, p(88), x2, p(396), p(12), fill=c["bg"], outline=c["line"])

        cv.text(x1, p(416), "선택한 폴더의 표시 이름", anchor="w", font=f["sm"], fill=c["sub"])
        try:
            focused = self.settings.focus_get() is self.name_entry
        except KeyError:
            focused = False
        ax = self.button(cv, "apply", x2, p(436), "적용", self.fav_rename, h=40, right=True)
        cv.rrect(x1, p(436), ax - p(8), p(476), p(9), fill=c["raised"],
                 outline=c["accent_ring"] if focused else c["line"])
        cv.itemconfigure(self.name_win, width=ax - p(8) - p(12) - p(42))

    def draw_fav_list(self, cv):
        c, f, p = self.c, self.f, self.px
        W, H = self.fav_w, self.fav_h
        folders = self.cfg["folders"]
        if not folders:
            cv.text(W / 2, H / 2, "아직 즐겨찾기가 없어요\n‘폴더 추가’를 눌러 시작하세요", font=f["md"],
                    fill=c["sub"], justify="center")
            return
        rh, gap = p(56), p(4)
        for i, folder in enumerate(folders):
            y = i * (rh + gap) - self.fav_scroll
            if y + rh < 0 or y > H:
                continue
            sel = i == self.fav_sel
            if sel or cv.hover == f"row:{i}":
                cv.rrect(0, y, W, y + rh, p(10), fill=c["sel"] if sel else c["hover"])
            cv.rrect(p(12), y + p(12), p(44), y + p(44), p(8), fill=c["tile_sel"] if sel else c["tile"])
            cv.text(p(28), y + rh / 2, I_FOLDER, font=f["icon16"], fill=c["accent"] if sel else c["sub"])
            cv.hit(f"row:{i}", (0, y, W, y + rh), lambda i=i: self.fav_select(i))
            right = W - p(10)
            if sel:
                for key, icon, action, color in (("del", I_DELETE, self.fav_remove, c["error"]),
                                                 ("down", I_DOWN, lambda: self.fav_move(1), c["text2"]),
                                                 ("up", I_UP, lambda: self.fav_move(-1), c["text2"])):
                    bx1 = right - p(34)
                    if cv.hover == key:
                        cv.rrect(bx1, y + p(11), right, y + p(45), p(8), fill=c["sel_hover"])
                    cv.text((bx1 + right) / 2, y + rh / 2, icon, font=f["icon14"], fill=color)
                    cv.hit(key, (bx1, y + p(11), right, y + p(45)), action)
                    right = bx1 - p(2)
            avail = right - p(58) - p(8)
            cv.text(p(58), y + p(19), ellipsize(f["lgb"], folder["name"], avail), anchor="w",
                    font=f["lgb"], fill=c["text"])
            cv.text(p(58), y + p(38), ellipsize(f["mono_sm"], folder["path"], avail), anchor="w",
                    font=f["mono_sm"], fill=c["sub"])

    def scroll_fav(self, step):
        rh = self.px(60)
        top = max(0, len(self.cfg["folders"]) * rh - self.px(4) - self.fav_h)
        self.fav_scroll = max(0, min(top, self.fav_scroll + step * rh))
        self.fav.redraw()

    def show_fav(self, i):
        """i번째 줄이 보이도록 스크롤한다."""
        rh = self.px(60)
        if i * rh < self.fav_scroll:
            self.fav_scroll = i * rh
        elif i * rh + rh - self.px(4) > self.fav_scroll + self.fav_h:
            self.fav_scroll = i * rh + rh - self.px(4) - self.fav_h

    def sync_name(self):
        self.name_var.set(self.cfg["folders"][self.fav_sel]["name"] if self.fav_sel is not None else "")

    def fav_select(self, i):
        self.fav_sel = i
        self.sync_name()
        self.fav.redraw()

    def fav_add(self):
        path = self.dialog(filedialog.askdirectory, parent=self.settings)
        if path:
            self.fav_sel = self.add_folder(path)
            self.show_fav(self.fav_sel)
            self.sync_name()
            self.fav.redraw()

    def fav_remove(self):
        i = self.fav_sel
        if i is None:
            return
        folder = self.cfg["folders"][i]
        if self.dialog(messagebox.askyesno, "삭제", f"즐겨찾기에서 뺄까요?\n{folder['path']}\n\n(실제 폴더는 지워지지 않아요)",
                       parent=self.settings):
            del self.cfg["folders"][i]
            self.save()
            self.fav_sel = min(i, len(self.cfg["folders"]) - 1) if self.cfg["folders"] else None
            self.scroll_fav(0)
            self.sync_name()
            self.fav.redraw()

    def fav_move(self, step):
        i, folders = self.fav_sel, self.cfg["folders"]
        if i is None or not 0 <= i + step < len(folders):
            return
        folders[i], folders[i + step] = folders[i + step], folders[i]
        self.save()
        self.fav_sel = i + step
        self.show_fav(self.fav_sel)
        self.fav.redraw()

    def fav_rename(self):
        i, name = self.fav_sel, self.name_var.get().strip()
        if i is None or not name:
            return
        self.cfg["folders"][i]["name"] = name
        self.save()
        self.fav.redraw()

    # 일반 화면

    def draw_general_page(self, cv):
        c, f, p = self.c, self.f, self.px
        x1, x2 = p(28), p(576) - p(28)
        cv.text(x1, p(26), "일반", anchor="nw", font=f["title"], fill=c["text"])

        # 단축키
        y, h = p(72), p(80)
        self.card(cv, x1, y, x2, y + h, "단축키", self.hk_error or "어디서든 이 키로 팝업을 열고 닫아요",
                  c["error"] if self.hk_error else None)
        cy = y + h / 2
        right = self.button(cv, "hk", x2 - p(18), cy - p(18), "취소" if self.capturing else "변경",
                            self.cancel_capture if self.capturing else self.capture_hotkey, h=36, right=True) - p(12)
        if self.capturing:
            text = "키 조합을 누르세요…"
            w = f["md"].measure(text) + p(24)
            cv.rrect(right - w, cy - p(17), right, cy + p(17), p(8), fill=c["raised"], outline=c["accent_ring"], width=2)
            cv.text(right - w / 2, cy, text, font=f["md"], fill=c["accent"])
        else:
            for j, part in enumerate(reversed(key_name(*self.cfg["hotkey"]).split(" + "))):
                if j:
                    cv.text(right - p(4), cy, "+", anchor="e", font=f["sm"], fill=c["sub2"])
                    right -= f["sm"].measure("+") + p(8)
                font = f["mono_md"] if part == "`" else f["mdb"]
                w = max(font.measure(part) + p(18), p(30))
                cv.rrect(right - w, cy - p(14), right, cy + p(17), p(7), fill=c["keyline"])  # 키 아랫면
                cv.rrect(right - w, cy - p(15), right, cy + p(14), p(7), fill=c["raised"], outline=c["keyline"])
                cv.text(right - w / 2, cy, part, font=font, fill=c["text"])
                right -= w

        # 테마
        y, h = y + h + p(14), p(150)
        self.card(cv, x1, y, x2, y + h, "테마", "팝업과 설정 창에 함께 적용돼요")
        ow = (x2 - x1 - p(36) - p(10)) / 2
        oy, oh = y + p(72), p(62)
        for j, (key, label) in enumerate((("dark", "다크"), ("light", "라이트"))):
            ox = x1 + p(18) + j * (ow + p(10))
            active, hov = self.cfg["theme"] == key, cv.hover == f"theme:{key}"
            cv.rrect(ox, oy, ox + ow, oy + oh, p(10), fill=c["raised"] if active else (c["hover"] if hov else c["bg"]),
                     outline=c["accent_ring"] if active else c["line"], width=2)
            bg, border, bar1, bar2, bar3 = PREVIEWS[key]
            px1, py1 = ox + p(11), oy + p(11)
            cv.rrect(px1, py1, px1 + p(64), py1 + p(40), p(6), fill=bg, outline=border)
            cv.rrect(px1 + p(7), py1 + p(7), px1 + p(43), py1 + p(12), p(2), fill=bar1)
            cv.rrect(px1 + p(7), py1 + p(16), px1 + p(57), py1 + p(24), p(2), fill=bar2)
            cv.rrect(px1 + p(7), py1 + p(28), px1 + p(35), py1 + p(33), p(2), fill=bar3)
            cv.text(px1 + p(78), oy + oh / 2, label, anchor="w", font=f["mdb"] if active else f["md"],
                    fill=c["text"] if active else c["text2"])
            cv.hit(f"theme:{key}", (ox, oy, ox + ow, oy + oh), lambda k=key: self.set_theme(k))

        # 자동 실행
        y, h = y + h + p(14), p(80)
        self.card(cv, x1, y, x2, y + h, "윈도우 시작 시 자동 실행", "로그인하면 백그라운드에서 조용히 켜져요")
        cy = y + h / 2
        sx2, sx1 = x2 - p(18), x2 - p(18) - p(46)
        cv.rrect(sx1, cy - p(13), sx2, cy + p(13), p(13), fill=c["accent_fill"] if self.autostart else c["switch_off"])
        kx = sx2 - p(13) if self.autostart else sx1 + p(13)
        cv.oval(kx - p(10), cy - p(10), kx + p(10), cy + p(10), fill=c["knob"])
        cv.hit("auto", (x1, y, x2, y + h), self.toggle_autostart)

    def capture_hotkey(self):
        self.capturing = True
        self.hk_error = None
        self.hk.set(0, 0)  # 지금 단축키도 다시 누를 수 있게 잠시 해제
        self.settings.bind("<KeyPress>", self.on_capture_key)
        self.main.focus_set()
        self.main.redraw()

    def cancel_capture(self):
        self.settings.unbind("<KeyPress>")
        self.capturing = False
        self.hk.set(*self.cfg["hotkey"])
        self.main.redraw()

    def on_capture_key(self, e):
        vk = e.keycode
        if vk in NOT_HOTKEY:
            return "break"
        held = lambda *keys: any(user32.GetAsyncKeyState(k) & 0x8000 for k in keys)
        mod = ((MOD_CTRL if held(0x11) else 0) | (MOD_ALT if held(0x12) else 0)
               | (MOD_SHIFT if held(0x10) else 0) | (MOD_WIN if held(0x5B, 0x5C) else 0))
        self.settings.unbind("<KeyPress>")
        self.capturing = False
        new = [mod, vk]
        if self.hk.set(*new):
            self.cfg["hotkey"] = new
            self.save()
        else:
            self.hk.set(*self.cfg["hotkey"])
            self.hk_error = f"{key_name(*new)}는 다른 프로그램이 쓰고 있어요"
        self.main.redraw()
        return "break"

    def set_theme(self, theme):
        if theme == self.cfg["theme"]:
            return
        self.cfg["theme"] = theme
        self.save()
        self.c = THEMES[theme]
        self.paint_widgets()
        self.cv.redraw()
        self.paint_settings_frame()
        self.redraw_settings()

    def toggle_autostart(self):
        set_autostart(not self.autostart)
        self.autostart = autostart_enabled()
        self.main.redraw()


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    mutex = kernel32.CreateMutexW(None, False, "FolderJumper.SingleInstance")  # noqa: F841 (프로세스 끝까지 유지)
    already_running = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS

    root = tk.Tk()
    root.withdraw()
    icon = Path(getattr(sys, "_MEIPASS", HERE)) / "icon.ico"  # exe 안에 함께 묶인 아이콘
    if icon.exists():
        root.iconbitmap(default=str(icon))
    if already_running:
        messagebox.showinfo(APP, "이미 실행 중이에요.\n설정한 단축키로 창을 열 수 있어요.")
        return

    # Pretendard가 설치돼 있지 않으면 fonts 폴더에 한 번 받아 두고, 이 프로그램 안에서만 쓰도록 불러온다
    if FONT not in tkfont.families(root) and not FONT_FILE.exists():
        download_font()
    if FONT_FILE.exists():
        ctypes.windll.gdi32.AddFontResourceExW(str(FONT_FILE), 0x10, 0)  # FR_PRIVATE

    first_run = not CONFIG_FILE.exists()
    events = queue.Queue()
    hotkey = Hotkey(events)
    app = App(root, hotkey)
    if not hotkey.set(*app.cfg["hotkey"]):
        app.hk_error = f"{key_name(*app.cfg['hotkey'])} 단축키를 등록하지 못했어요. 다른 키로 바꿔주세요."
        app.open_settings("general")
    elif first_run:  # 실행해도 화면에 아무것도 안 뜨므로 처음 한 번은 사용법을 알려준다
        app.save()
        messagebox.showinfo(APP, f"폴더 바로가기가 실행됐어요.\n\n"
                                 f"{key_name(*app.cfg['hotkey'])} 를 누르면 어디서든 즐겨찾기 창이 열려요.\n"
                                 f"창에서 Ctrl+, 로 설정을, Ctrl+Q 로 종료할 수 있어요.")
        app.show()

    def poll():
        try:
            while True:
                events.get_nowait()
                app.toggle()
        except queue.Empty:
            pass
        root.after(50, poll)

    poll()
    root.mainloop()


if __name__ == "__main__":
    main()
