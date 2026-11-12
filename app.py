#!/usr/bin/env python3
"""Command Center v2.1 (Windows): launcher, tasks, focus timer, Google Calendar. Neo-brutalist UI.
Setup: pip install pywebview cryptography keyboard google-auth-oauthlib requests   |   Run: python app.py
Global hotkey: Ctrl+Alt+M shows/hides the window. Esc hides it.
"""
import base64, json, os, shutil, subprocess, sys, time, uuid, webbrowser
from datetime import datetime, timedelta
from pathlib import Path
import webview
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

try:
    import keyboard
except Exception:  # hotkey is optional; the app still works without it
    keyboard = None

TITLE, HUD_TITLE = "Command Center", "CC HUD"
HOME = Path.home() / ".command_center"
SALT, DATA = HOME / "salt.bin", HOME / "data.enc"
HOME.mkdir(exist_ok=True)
S = {"f": None, "d": None, "fails": 0, "win": None, "hud": None,
     "vis": True, "quit": False, "hk": False, "hudvis": False, "hs": ("", "", False)}
DEFAULTS = {"shortcuts": [], "tasks": []}
GSCOPE = ["https://www.googleapis.com/auth/calendar.readonly"]

# ---------- Windows helpers (window focus, title bar colours) ----------
IS_WIN = sys.platform.startswith("win")
if IS_WIN:
    import ctypes
    from ctypes import wintypes
    U = ctypes.windll.user32
    U.FindWindowW.argtypes, U.FindWindowW.restype = [wintypes.LPCWSTR, wintypes.LPCWSTR], wintypes.HWND
    U.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    U.SetForegroundWindow.argtypes = [wintypes.HWND]
    U.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, wintypes.UINT]
    DW = ctypes.windll.dwmapi
    DW.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]


def hwnd(title):
    return U.FindWindowW(None, title) if IS_WIN else None


def style_titlebar():
    """Cream title bar with black text and border to match the UI (Windows 11)."""
    try:
        h = hwnd(TITLE)
        for attr, val in ((20, 0), (35, 0xD6F4FF), (36, 0x111111), (34, 0x111111)):
            DW.DwmSetWindowAttribute(h, attr, ctypes.byref(ctypes.c_int(val)), 4)
    except Exception:
        pass


def show_main():
    h = hwnd(TITLE)
    if h:
        U.ShowWindow(h, 9)                       # SW_RESTORE
        U.SetWindowPos(h, -1, 0, 0, 0, 0, 0x43)  # topmost, no move/size, show
        U.keybd_event(0x12, 0, 0, 0); U.keybd_event(0x12, 0, 2, 0)  # lets us take foreground
        U.SetForegroundWindow(h)
    else:
        S["win"].show()
    S["vis"] = True
    refresh_hud()
    try:
        S["win"].evaluate_js("focusIn()")
    except Exception:
        pass


def hide_main():
    h = hwnd(TITLE)
    if h:
        U.ShowWindow(h, 0)
    else:
        S["win"].hide()
    S["vis"] = False
    refresh_hud()


def toggle():
    hide_main() if S["vis"] else show_main()


def refresh_hud():
    """Show the floating micro-HUD only while a timer is running and the main window is hidden."""
    w = S["hud"]
    if not w:
        return
    label, text, running = S["hs"]
    want = running and not S["vis"]
    try:
        if want:
            w.evaluate_js(f"setHud({json.dumps(label)},{json.dumps(text)})")
        if want != S["hudvis"]:
            w.show() if want else w.hide()
            S["hudvis"] = want
    except Exception:
        pass


def start_hotkey():
    if keyboard:
        try:
            keyboard.add_hotkey("ctrl+alt+m", toggle)
            S["hk"] = True
        except Exception:
            S["hk"] = False


def on_closing():
    # With the hotkey active, the X button hides the app; use /quit to exit completely.
    if S["quit"] or not S["hk"]:
        return True
    hide_main()
    return False


def wire_drop():
    """Forward files dropped on the window to the page (needs pywebview 5+)."""
    try:
        from webview.dom import DOMEventHandler

        def on_drop(e):
            files = e.get("dataTransfer", {}).get("files", [])
            paths = [f["pywebviewFullPath"] for f in files if f.get("pywebviewFullPath")]
            if paths:
                S["win"].evaluate_js(f"onDrop({json.dumps(paths)})")

        S["win"].dom.document.events.drop += DOMEventHandler(on_drop, True, True)
    except Exception:
        pass


def on_loaded():
    style_titlebar()
    wire_drop()


# ---------- Encryption + storage ----------
def key(pw, salt):
    k = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=480_000)
    return base64.urlsafe_b64encode(k.derive(pw.encode()))


def save():
    t = DATA.with_suffix(".tmp")
    t.write_bytes(S["f"].encrypt(json.dumps(S["d"]).encode()))
    t.replace(DATA)


def launch_target(kind, t):
    if kind == "link":
        webbrowser.open(t if "://" in t else "https://" + t)
    elif IS_WIN:
        os.startfile(t)  # noqa
    else:
        subprocess.Popen([t] if shutil.which(t) else ["xdg-open", t])


def dlg(kind, **kw):
    D = getattr(webview, "FileDialog", None)
    t = getattr(D, kind) if D else getattr(webview, kind + "_DIALOG")
    return S["win"].create_file_dialog(t, **kw)


def task(tid):
    return next(t for t in S["d"]["tasks"] if t["id"] == tid)


def cleanup():
    S["f"] = S["d"] = None


# ---------- Google Calendar (read-only; token + client kept inside the encrypted data file) ----------
def gcal_creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    tok = S["d"].get("gcal_token")
    if not tok:
        return None
    c = Credentials.from_authorized_user_info(json.loads(tok), GSCOPE)
    if not c.valid:
        c.refresh(Request())
        S["d"]["gcal_token"] = c.to_json()
        save()
    return c


def gcal_fetch():
    from google.auth.transport.requests import AuthorizedSession
    c = gcal_creds()
    if not c:
        raise RuntimeError("Not connected. Reconnect Google.")
    start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    r = AuthorizedSession(c).get(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events",
        params={"timeMin": start.isoformat(), "timeMax": (start + timedelta(days=7)).isoformat(),
                "singleEvents": "true", "orderBy": "startTime", "maxResults": 60}, timeout=15)
    r.raise_for_status()
    out = []
    for e in r.json().get("items", []):
        if e.get("status") == "cancelled":
            continue
        s, en = e.get("start", {}), e.get("end", {})
        out.append({"id": e.get("id", ""), "title": e.get("summary", "(No title)"),
                    "start": s.get("dateTime") or s.get("date"), "end": en.get("dateTime") or en.get("date"),
                    "allDay": "dateTime" not in s, "loc": e.get("location", "")})
    return out


class Api:
    def state(self):
        return {"setup": not (SALT.exists() and DATA.exists())}

    def unlock(self, pw, create=False):
        if create:
            if len(pw) < 8:
                return {"ok": False, "err": "Use at least 8 characters."}
            salt = os.urandom(16)
            SALT.write_bytes(salt)
            S["f"], S["d"] = Fernet(key(pw, salt)), {"shortcuts": [], "tasks": []}
            save()
            return {"ok": True}
        if S["fails"] >= 5:
            return {"ok": False, "err": "Too many attempts. Restart the app."}
        try:
            f = Fernet(key(pw, SALT.read_bytes()))
            S["d"] = json.loads(f.decrypt(DATA.read_bytes()))
        except InvalidToken:
            S["fails"] += 1
            time.sleep(1)
            return {"ok": False, "err": "Wrong password."}
        for k, v in DEFAULTS.items():  # upgrade older data in place
            S["d"].setdefault(k, list(v))
        S["f"], S["fails"] = f, 0
        return {"ok": True}

    def lock(self):
        cleanup()

    def data(self):
        return {k: S["d"].get(k, []) for k in DEFAULTS}

    # window control
    def hide(self):
        hide_main()

    def quit(self):
        S["quit"] = True
        for w in list(webview.windows):
            w.destroy()

    def attention(self):
        if not S["vis"]:
            show_main()

    def hud(self, label, text, running):
        S["hs"] = (label, text, bool(running))
        refresh_hud()

    # shortcuts
    def add_shortcut(self, name, kind, target):
        S["d"]["shortcuts"].append({"name": name, "kind": kind, "target": target})
        save()
        return self.data()

    def remove_shortcut(self, i):
        del S["d"]["shortcuts"][i]
        save()
        return self.data()

    def launch(self, i):
        s = S["d"]["shortcuts"][i]
        try:
            launch_target(s["kind"], s["target"])
            return ""
        except Exception as ex:
            return str(ex)

    def pick(self):
        r = dlg("OPEN")
        return r[0] if r else ""

    # tasks
    def add_task(self, title, pri="med", due=""):
        S["d"]["tasks"].append({"id": uuid.uuid4().hex, "title": title, "pri": pri, "due": due, "done": False})
        save()
        return self.data()

    def toggle_task(self, tid):
        t = task(tid)
        t["done"] = not t["done"]
        save()
        return self.data()

    def delete_task(self, tid):
        S["d"]["tasks"].remove(task(tid))
        save()
        return self.data()

    # google calendar
    def gcal_status(self):
        return {"client": bool(S["d"].get("gcal_client")), "linked": bool(S["d"].get("gcal_token"))}

    def gcal_set_client(self):
        r = dlg("OPEN", file_types=("JSON files (*.json)",))
        if not r:
            return {"ok": False, "err": ""}
        try:
            cfg = json.loads(Path(r[0]).read_text("utf-8"))
        except Exception:
            return {"ok": False, "err": "That file isn't valid JSON."}
        if "installed" not in cfg:
            return {"ok": False, "err": "Wrong credential type. Create the OAuth client as “Desktop app”."}
        S["d"]["gcal_client"] = cfg
        save()
        return {"ok": True}

    def gcal_connect(self):
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow = InstalledAppFlow.from_client_config(S["d"]["gcal_client"], GSCOPE)
            c = flow.run_local_server(port=0, timeout_seconds=180,
                                      success_message="Connected! You can close this tab and go back to Command Center.")
            S["d"]["gcal_token"] = c.to_json()
            save()
            return {"ok": True}
        except Exception as ex:
            return {"ok": False, "err": str(ex)[:220] or "Sign-in was cancelled."}

    def gcal_disconnect(self):
        S["d"].pop("gcal_token", None)
        save()
        return {"ok": True}

    def gcal_events(self):
        try:
            return {"ok": True, "events": gcal_fetch()}
        except Exception as ex:
            msg = str(ex)
            if "invalid_grant" in msg or "Reconnect" in msg:
                S["d"].pop("gcal_token", None)
                save()
                return {"ok": False, "err": "Google sign-in expired. Reconnect."}
            return {"ok": False, "err": msg[:220]}


HUD_HTML = r"""<!doctype html><meta charset=utf-8><style>
*{margin:0;box-sizing:border-box}
body{height:100vh;padding:5px 9px 9px 5px;background:#FFF4D6;font-family:"Arial Black","Segoe UI Black",Impact,sans-serif;color:#111;user-select:none;overflow:hidden}
.c{height:100%;border:3px solid #111;background:#FFD93D;box-shadow:4px 4px 0 #111;display:grid;place-content:center;text-align:center}
small{display:block;font:800 11px "Segoe UI",Arial,sans-serif;text-transform:uppercase;letter-spacing:.08em}
b{font:900 30px Consolas,"Courier New",monospace}
</style><div class=c><small id=l></small><b id=t></b></div>
<script>function setHud(l,t){document.getElementById('l').textContent=l;document.getElementById('t').textContent=t}</script>"""

HTML = r"""<!doctype html><meta charset=utf-8><style>
*{box-sizing:border-box;margin:0}
:root{--bg:#FFF4D6;--ink:#111;--y:#FFD93D;--p:#FF6B9D;--c:#5EC8FF;--g:#5CE08A;--o:#FF9F43;--v:#B692FF;--r:#FF4D4D;--mut:#4a4a4a;
--hd:"Arial Black","Segoe UI Black",Impact,sans-serif;--mono:Consolas,"Courier New",monospace}
html,body{height:100%}
body{font:600 14px "Segoe UI",Arial,sans-serif;color:var(--ink);background-color:var(--bg);background-image:radial-gradient(rgba(17,17,17,.2) 1.4px,transparent 1.7px);background-size:22px 22px;overflow:hidden;user-select:none}
[hidden]{display:none!important}
button{font:inherit;color:inherit;border:3px solid var(--ink);cursor:pointer;border-radius:0}
input,select{font:inherit;color:var(--ink);background:#fff;border:3px solid var(--ink);border-radius:0;outline:0;user-select:text;padding:9px 12px}
:focus-visible{outline:3px solid var(--p);outline-offset:2px}
.box{background:#fff;border:3px solid var(--ink);box-shadow:6px 6px 0 var(--ink)}
.btn{background:var(--y);padding:9px 16px;font-weight:800;text-transform:uppercase;letter-spacing:.04em;font-size:12.5px;box-shadow:4px 4px 0 var(--ink);transition:transform .08s,box-shadow .08s}
.btn:hover{transform:translate(-2px,-2px);box-shadow:6px 6px 0 var(--ink)}
.btn:active{transform:translate(4px,4px);box-shadow:0 0 0 var(--ink)}
.btn.g{background:#fff}.btn.p{background:var(--p)}.btn.c{background:var(--c)}
body.drag:after{content:"DROP TO ADD";position:fixed;inset:10px;border:4px dashed var(--ink);background:rgba(255,217,61,.7);display:grid;place-items:center;font:900 30px var(--hd);z-index:50;pointer-events:none}
#lock{height:100%;display:grid;place-items:center}
.panel{width:360px;padding:30px;background:#fff;border:3px solid var(--ink);box-shadow:10px 10px 0 var(--ink);animation:pop .25s steps(4)}
.panel svg{width:44px;height:44px;stroke:var(--ink);fill:var(--y);stroke-width:2.4;margin-bottom:10px}
.panel h1{font:900 26px var(--hd);text-transform:uppercase;line-height:1}
.panel p{color:var(--mut);margin:8px 0 18px}
.panel input{width:100%;margin-bottom:12px}
.panel .btn{width:100%;padding:12px}
#err{color:var(--r);height:18px;margin-top:12px;font-weight:800;font-size:13px}
.shake{animation:shake .3s}
@keyframes shake{25%{transform:translateX(-10px)}75%{transform:translateX(10px)}}
@keyframes pop{from{transform:translate(8px,8px);box-shadow:2px 2px 0 var(--ink)}}
#app{height:100%;display:flex;flex-direction:column;padding:18px 22px 16px;gap:16px}
header{display:flex;gap:16px;align-items:stretch}
.clock{background:var(--y);padding:8px 20px;min-width:250px}
#tm{font:900 38px var(--mono);letter-spacing:-.02em;line-height:1.1}
#dt{font-weight:800;font-size:12px;text-transform:uppercase;letter-spacing:.05em;margin-top:2px}
.bar{flex:1;display:flex;align-items:center;gap:10px;padding:0 16px;background:#fff}
.bar svg{width:20px;stroke:var(--ink);fill:none;stroke-width:3;flex:none}
.bar input{flex:1;border:0;padding:8px 0;font-size:16px;background:none}
.bar input:focus-visible{outline:0}
#lb{align-self:stretch;padding:0 22px;background:var(--p)}
nav{display:flex;gap:10px;flex-wrap:wrap}
nav button{background:#fff;padding:7px 20px;font-weight:800;text-transform:uppercase;letter-spacing:.04em;font-size:12.5px;box-shadow:3px 3px 0 var(--ink);transition:transform .08s,box-shadow .08s}
nav button:hover{transform:translate(-1px,-1px);box-shadow:4px 4px 0 var(--ink)}
nav .on{background:var(--ink);color:#fff;transform:translate(3px,3px);box-shadow:0 0 0 var(--ink)}
main{flex:1;overflow:auto;padding:4px 10px 12px 4px}
main::-webkit-scrollbar{width:14px}main::-webkit-scrollbar-track{background:#fff;border-left:3px solid var(--ink)}main::-webkit-scrollbar-thumb{background:var(--ink);border:3px solid #fff}
#banner{display:flex;align-items:center;gap:14px;background:var(--o);padding:10px 16px;font-weight:800;font-size:15px}
#banner span{flex:1}
#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(138px,1fr));gap:18px}
.tile{position:relative;padding:16px 10px 14px;text-align:center;background:#fff;border:3px solid var(--ink);box-shadow:5px 5px 0 var(--ink);cursor:pointer;transition:transform .08s,box-shadow .08s;display:flex;flex-direction:column;align-items:center;gap:6px}
.tile:hover{transform:translate(-2px,-2px);box-shadow:8px 8px 0 var(--ink)}
.tile:active{transform:translate(4px,4px);box-shadow:0 0 0 var(--ink)}
.ico{width:56px;height:56px;border:3px solid var(--ink);display:grid;place-items:center;font:900 26px var(--hd);box-shadow:3px 3px 0 var(--ink)}
.plus{background:#fff;border-style:dashed;box-shadow:none}
.tile b{font-weight:800;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tile small{color:var(--mut);font-size:11px;font-weight:700}
.x{position:absolute;top:2px;right:7px;opacity:0;font-style:normal;font-weight:900;font-size:18px}
.tile:hover .x{opacity:1}.x:hover{color:var(--r)}
.row{display:flex;align-items:center;gap:12px;padding:10px 14px;background:#fff;border:3px solid var(--ink);box-shadow:4px 4px 0 var(--ink);margin-bottom:14px}
.row .n{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:800;font-size:15px}
.row small{color:var(--mut);font-weight:700}
.row button{background:#fff;padding:4px 11px;font-weight:800;font-size:12.5px;box-shadow:2px 2px 0 var(--ink)}
.row button:hover{background:var(--y)}.row button:active{transform:translate(2px,2px);box-shadow:none}
.add{display:flex;gap:10px;padding:12px;margin-bottom:18px;align-items:center}
.add #tt{flex:1}
.add input[type=datetime-local]{font-family:inherit}
.chk{width:28px;height:28px;background:#fff!important;padding:0!important;display:grid;place-items:center;font-size:17px;font-weight:900;flex:none;box-shadow:none!important}
.dn .chk{background:var(--g)!important}
.dn .n{text-decoration:line-through;opacity:.5}
.b{padding:2px 9px;border:2px solid var(--ink);font-size:11.5px;font-weight:800;text-transform:uppercase;letter-spacing:.03em;white-space:nowrap;background:#fff}
.b.over{background:var(--r);color:#fff}.b.soon{background:var(--o)}.b.later{background:var(--c)}.b.now{background:var(--g)}
.fwrap{display:flex;flex-direction:column;align-items:center;gap:22px;padding-top:6px}
.fm{display:flex;gap:10px}
.fm button{background:#fff;padding:7px 18px;font-weight:800;text-transform:uppercase;font-size:12.5px;box-shadow:3px 3px 0 var(--ink)}
.fm .on{background:var(--ink);color:#fff;transform:translate(3px,3px);box-shadow:none}
.ring{position:relative;width:280px;height:280px;border-radius:50%;display:grid;place-items:center;box-shadow:9px 9px 0 var(--ink);background:var(--y)}
.ring svg{position:absolute;inset:16px;width:calc(100% - 32px);transform:rotate(-90deg)}
.ring circle{fill:none;stroke-width:9}
.ring .tr{stroke:#fff;stroke-width:9}
#arc{stroke:var(--p);stroke-dasharray:339.3;transition:stroke-dashoffset .25s linear}
.mid{text-align:center;position:relative}
#ft{font:900 56px var(--mono);letter-spacing:-.03em}
#fl{font-weight:800;text-transform:uppercase;letter-spacing:.08em}
#fk{font-size:12px;font-weight:700;max-width:190px;min-height:16px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fb{display:flex;gap:14px;align-items:center}
.fb input{width:78px;text-align:center}
.cal-h{display:flex;gap:12px;align-items:center;margin-bottom:6px}
.cal-h h2,.setup h2{font:900 22px var(--hd);text-transform:uppercase;flex:1}
.day{display:inline-block;background:var(--ink);color:#fff;padding:4px 12px;margin:14px 0 12px;font-weight:800;text-transform:uppercase;letter-spacing:.05em;font-size:12.5px}
.tm{width:128px;flex:none;font:900 13px var(--mono)}
.setup{padding:24px;max-width:620px}
.setup ol{margin:14px 0 18px 22px;line-height:1.75;font-weight:700}
.setup .acts{display:flex;gap:12px;align-items:center}
.cerr{color:var(--r);font-weight:800;margin-top:12px}
#sheet{position:fixed;inset:0;background:rgba(17,17,17,.55);display:grid;place-items:center;z-index:20}
#sheet .panel{width:400px}
#sheet input{margin-bottom:12px}
.kind{display:flex;gap:10px;margin:14px 0}
.kind button{background:#fff;padding:6px 16px;font-weight:800;text-transform:uppercase;font-size:12.5px;box-shadow:3px 3px 0 var(--ink)}
.kind .on{background:var(--ink);color:#fff;transform:translate(3px,3px);box-shadow:none}
.acts{display:flex;gap:10px;justify-content:flex-end}.acts .btn{width:auto}
.empty{text-align:center;padding:40px;font-weight:800;background:#fff;border:3px dashed var(--ink)}
#toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);padding:10px 20px;background:var(--ink);color:#fff;border:3px solid var(--ink);box-shadow:5px 5px 0 var(--y);font-weight:800;opacity:0;pointer-events:none;z-index:30}
#toast.on{opacity:1}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>
<div id=lock><div class=panel>
<svg viewBox="0 0 24 24"><rect x=4 y=10 width=16 height=11 /><path d="M8 10V7a4 4 0 018 0v3"/></svg>
<h1>Command Center</h1><p id=sub></p>
<input id=pw type=password placeholder=Password><input id=pw2 type=password placeholder="Confirm password" hidden>
<button class=btn id=go>Unlock</button><div id=err></div></div></div>
<div id=app hidden>
<header><div class="clock box"><div id=tm></div><div id=dt></div></div>
<div class="bar box"><svg viewBox="0 0 24 24"><circle cx=11 cy=11 r=7 /><path d="M20 20l-4-4"/></svg><input id=q placeholder="Search apps, or type /task  /add  /cal  /lock  /quit"></div><button class="btn p box" id=lb>Lock</button></header>
<div id=banner class=box hidden><span id=bt></span><button class="btn g" id=bd>Dismiss</button></div>
<nav id=nav><button class=on data-t=launch>Launcher</button><button data-t=tasks>Tasks</button><button data-t=focus>Focus</button><button data-t=cal>Calendar</button></nav>
<main id=launch><div id=grid></div></main>
<main id=tasks hidden>
 <div class="add box"><input id=tt placeholder="New task"><select id=tp><option value=high>High</option><option value=med selected>Medium</option><option value=low>Low</option></select><input id=td type=datetime-local><button class=btn id=ta>Add</button></div>
 <div id=tl></div></main>
<main id=focus hidden><div class=fwrap>
 <div class=fm id=fm><button class=on data-m=pomo>Pomodoro</button><button data-m=count>Timer</button><button data-m=sw>Stopwatch</button></div>
 <div class=ring><svg viewBox="0 0 120 120"><circle class=tr cx=60 cy=60 r=54 /><circle id=arc cx=60 cy=60 r=54 /></svg>
 <div class=mid><div id=fl>Focus</div><div id=ft>25:00</div><div id=fk></div></div></div>
 <div class=fb><button class="btn p" id=fgo>Start</button><button class="btn g" id=frs>Reset</button><button class="btn g" id=fskip>Skip</button><input id=fmin type=number min=1 max=600 value=25 title="Minutes" hidden></div>
</div></main>
<main id=cal hidden></main>
<div id=sheet hidden><div class=panel><h1 style="font-size:21px">New Shortcut</h1>
<div class=kind id=kind><button class=on data-k=app>App</button><button data-k=link>Link</button></div>
<input id=sn placeholder=Name><input id=st placeholder="App path or URL">
<div class=acts><button class="btn g" id=br>Browse…</button><button class="btn g" id=sc>Cancel</button><button class=btn id=ss>Save</button></div></div></div>
<div id=toast></div></div>
<script>
const $=s=>document.querySelector(s),A=()=>pywebview.api,MOD='Ctrl+';
let D={shortcuts:[],tasks:[]},vis=[],setup=false,kind='app',idle,curTab='launch';
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const COL=['#FFD93D','#FF6B9D','#5EC8FF','#5CE08A','#FF9F43','#B692FF'];
const pick=s=>COL[[...s].reduce((a,c)=>a+c.charCodeAt(0),0)%COL.length];
const p2=n=>String(n).padStart(2,'0');
const toast=m=>{const t=$('#toast');t.textContent=m;t.classList.add('on');setTimeout(()=>t.classList.remove('on'),2800)};
const TABS=['launch','tasks','focus','cal'];
const tab=t=>{curTab=t;document.querySelectorAll('#nav button').forEach(b=>b.classList.toggle('on',b.dataset.t===t));TABS.forEach(x=>$('#'+x).hidden=x!==t);render();if(t==='cal'&&CAL.st.linked&&Date.now()-CAL.last>6e4)calLoad()};
const bump=()=>{clearTimeout(idle);idle=setTimeout(()=>{if(!$('#app').hidden)lock()},9e5)};
const focusIn=()=>($('#app').hidden?$('#pw'):$('#q')).focus();
async function boot(){setup=(await A().state()).setup;$('#sub').textContent=setup?'Create a password to protect everything here.':'Enter your password to continue.';$('#pw2').hidden=!setup;$('#go').textContent=setup?'Create Password':'Unlock';$('#pw').focus()}
function fail(m){$('#err').textContent=m;const p=$('#lock .panel');p.classList.remove('shake');void p.offsetWidth;p.classList.add('shake')}
async function go(){const p=$('#pw').value;$('#err').textContent='';
 if(setup&&p!==$('#pw2').value)return fail('Passwords don’t match.');
 const r=await A().unlock(p,setup);if(!r.ok)return fail(r.err);
 D=await A().data();$('#pw').value=$('#pw2').value='';$('#lock').hidden=true;$('#app').hidden=false;tab('launch');$('#q').focus();bump();
 CAL.st=await A().gcal_status();calLoad()}
async function lock(){await A().lock();D={shortcuts:[],tasks:[]};CAL.ev=[];CAL.st={client:false,linked:false};$('#q').value='';$('#banner').hidden=true;$('#app').hidden=true;$('#lock').hidden=false;boot()}

/* ---------- clock ---------- */
function clock(){const n=new Date(),t=p2(n.getHours())+':'+p2(n.getMinutes())+':'+p2(n.getSeconds());
 if($('#tm').textContent!==t){$('#tm').textContent=t;$('#dt').textContent=n.toLocaleDateString(undefined,{weekday:'long',year:'numeric',month:'long',day:'numeric'})}}

/* ---------- render ---------- */
function render(){if(curTab==='launch')renderLaunch();else if(curTab==='tasks')renderTasks();else if(curTab==='cal')renderCal();else renderFocus()}
function renderLaunch(){
 const q=$('#q').value.trim().toLowerCase(),f=q[0]==='/'?'':q;
 vis=D.shortcuts.map((s,i)=>({...s,i})).filter(s=>!f||s.name.toLowerCase().includes(f)).sort((a,b)=>a.name.localeCompare(b.name));
 $('#grid').innerHTML=vis.map((s,n)=>`<div class=tile data-i=${s.i}><div class=ico style="background:${pick(s.name)}">${esc(s.name[0].toUpperCase())}</div><b>${esc(s.name)}</b><small>${s.kind==='link'?'Link':'App'}${n<9?' · '+MOD+(n+1):''}</small><i class=x data-x=${s.i}>×</i></div>`).join('')+`<div class=tile id=addtile><div class="ico plus">+</div><b>Add</b></div>`}

const PRI={high:['High','#FF6B9D'],med:['Medium','#FFD93D'],low:['Low','#5CE08A']};
function dueInfo(t){if(t.done||!t.due)return['',''];const ms=new Date(t.due)-Date.now();
 const when=new Date(t.due).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
 return ms<0?['over','Overdue · '+when]:ms<864e5?['soon','Due soon · '+when]:['later',when]}
function renderTasks(){
 const ts=[...D.tasks].sort((a,b)=>(a.done-b.done)||((a.due||'z').localeCompare(b.due||'z')));
 $('#tl').innerHTML=ts.length?ts.map(t=>{const[c,l]=dueInfo(t),p=PRI[t.pri]||PRI.med;
  return `<div class="row ${t.done?'dn':''}"><button class=chk data-c="${t.id}" aria-label="Toggle done">${t.done?'✓':''}</button><div class=n>${esc(t.title)}</div>${l?`<span class="b ${c}">${esc(l)}</span>`:''}<span class=b style="background:${p[1]}">${p[0]}</span>${t.done?'':`<button data-f="${t.id}">Focus</button>`}<button data-x="${t.id}" aria-label="Delete task">×</button></div>`}).join(''):'<div class=empty>No tasks yet. Add one above, or type /task and a title in the search bar.</div>'}

/* ---------- focus engine ---------- */
const PH={focus:[25,'Focus'],short:[5,'Short break'],long:[15,'Long break']};
const T={mode:'pomo',phase:'focus',dur:25*6e4,left:25*6e4,end:0,run:false,n:0,t0:0,acc:0,task:''};
const setPhase=p=>{T.phase=p;T.dur=T.left=PH[p][0]*6e4;T.run=false};
const cur=()=>T.mode==='sw'?(T.run?T.acc+Date.now()-T.t0:T.acc):(T.run?Math.max(0,T.end-Date.now()):T.left);
function fmt(ms,up){const s=up?Math.floor(ms/1e3):Math.ceil(ms/1e3),h=Math.floor(s/3600),m=Math.floor(s%3600/60);return (h?h+':'+p2(m):p2(m))+':'+p2(s%60)}
function startPause(){
 if(T.run){const c=cur();T.run=false;if(T.mode==='sw')T.acc=c;else T.left=c}
 else{if(T.mode!=='sw'&&T.left<=0)T.left=T.dur;if(T.mode==='sw')T.t0=Date.now();else T.end=Date.now()+T.left;T.run=true}
 renderFocus()}
function reset(){T.run=false;if(T.mode==='sw')T.acc=0;else T.left=T.dur;renderFocus()}
const advance=()=>{if(T.phase==='focus'){T.n++;setPhase(T.n%4?'short':'long')}else setPhase('focus')};
function chime(){try{const c=new AudioContext();[880,1318].forEach((f,i)=>{const o=c.createOscillator(),g=c.createGain();o.type='square';o.frequency.value=f;o.connect(g);g.connect(c.destination);const t=c.currentTime+i*.18;g.gain.setValueAtTime(.0001,t);g.gain.exponentialRampToValueAtTime(.12,t+.02);g.gain.exponentialRampToValueAtTime(.0001,t+.6);o.start(t);o.stop(t+.65)})}catch(e){}}
function done(){T.run=false;chime();A().attention();const was=T.mode==='pomo'?PH[T.phase][1]:'Timer';toast(was+' finished.');if(T.mode==='pomo')advance();else T.left=0;renderFocus()}
function label(){return T.mode==='pomo'?PH[T.phase][1]:T.mode==='count'?'Timer':'Stopwatch'}
function renderFocus(){
 const c=cur(),up=T.mode==='sw';
 $('#fl').textContent=label();$('#ft').textContent=fmt(c,up);
 $('#fk').textContent=T.task?'Working on: '+T.task:(T.mode==='pomo'?'Sessions done: '+T.n:'');
 $('#fgo').textContent=T.run?'Pause':(c>0&&(up?c>0:c<T.dur)?'Resume':'Start');
 $('#fskip').hidden=T.mode!=='pomo';$('#fmin').hidden=T.mode!=='count';
 const frac=up?(c%6e4)/6e4:(T.dur?c/T.dur:0);$('#arc').style.strokeDashoffset=339.3*(1-frac);
 document.querySelectorAll('#fm button').forEach(b=>b.classList.toggle('on',b.dataset.m===T.mode))}
let lastHud='';
function ftick(){
 if(T.run&&T.mode!=='sw'&&cur()<=0)return done();
 const c=cur(),txt=fmt(c,T.mode==='sw');
 if(curTab==='focus'&&!$('#app').hidden)renderFocus();
 const key=T.run+'|'+txt;if(key!==lastHud){lastHud=key;A().hud(label(),txt,T.run)}}

/* ---------- google calendar ---------- */
const CAL={st:{client:false,linked:false},ev:[],err:'',busy:false,last:0,notified:new Set()};
const pd=s=>s.length===10?new Date(s+'T00:00:00'):new Date(s);
const hm=d=>p2(d.getHours())+':'+p2(d.getMinutes());
const localIso=d=>d.getFullYear()+'-'+p2(d.getMonth()+1)+'-'+p2(d.getDate())+'T'+hm(d);
function dayLabel(d){const t=new Date();t.setHours(0,0,0,0);const k=Math.round((d-t)/864e5);return k<=0?'Today':k===1?'Tomorrow':d.toLocaleDateString(undefined,{weekday:'long',month:'short',day:'numeric'})}
async function calLoad(){if(!CAL.st.linked||CAL.busy)return;CAL.busy=true;
 const r=await A().gcal_events();CAL.busy=false;
 if(r.ok){CAL.ev=r.events;CAL.err='';CAL.last=Date.now()}else{CAL.err=r.err;if(/reconnect/i.test(r.err))CAL.st.linked=false}
 if(curTab==='cal')renderCal()}
function renderCal(){
 const el=$('#cal'),s=CAL.st;
 if(!s.client)return el.innerHTML=`<div class="setup box"><h2>Link Google Calendar</h2><ol><li>Go to console.cloud.google.com and create a project.</li><li>Enable the <b>Google Calendar API</b>.</li><li>Set up the OAuth consent screen (External) and add your Gmail as a test user.</li><li>Create credentials → OAuth client ID → <b>Desktop app</b>, then download the JSON.</li><li>Choose that file here.</li></ol><div class=acts><button class="btn c" data-a=set>Choose credentials file</button></div><div class=cerr>${esc(CAL.err)}</div></div>`;
 if(!s.linked)return el.innerHTML=`<div class="setup box"><h2>Almost there</h2><p style="margin-top:10px">Credentials saved. Connect your Google account. A browser tab will open so you can approve read-only access.</p><div class=acts style="margin-top:18px"><button class="btn p" data-a=connect id=cb>Connect Google</button><button class="btn g" data-a=set>Change credentials file</button></div><div class=cerr>${esc(CAL.err)}</div></div>`;
 const g={};for(const e of CAL.ev){const st=pd(e.start),t=new Date();t.setHours(0,0,0,0);const k=dayLabel(st<t?t:st);(g[k]=g[k]||[]).push(e)}
 const now=Date.now();
 let h=`<div class=cal-h><h2>Next 7 days</h2><button class="btn c" data-a=refresh>${CAL.busy?'Loading…':'Refresh'}</button><button class="btn g" data-a=off>Disconnect</button></div>`;
 if(CAL.err)h+=`<div class=cerr>${esc(CAL.err)}</div>`;
 h+=Object.keys(g).length?Object.entries(g).map(([k,es])=>`<div class=day>${esc(k)}</div>`+es.map(e=>{
   const st=pd(e.start),en=pd(e.end),i=CAL.ev.indexOf(e),m=(st-now)/6e4;
   const bd=e.allDay?'':(st<=now&&now<en)?'<span class="b now">Now</span>':(m>0&&m<=60)?`<span class="b soon">In ${Math.ceil(m)} min</span>`:'';
   return `<div class=row><div class=tm>${e.allDay?'ALL DAY':hm(st)+' – '+hm(en)}</div><div class=n>${esc(e.title)}${e.loc?`<br><small>${esc(e.loc)}</small>`:''}</div>${bd}<button data-a=task data-i=${i}>+ Task</button></div>`}).join('')).join(''):'<div class=empty>Nothing on your calendar for the next 7 days.</div>';
 el.innerHTML=h}
$('#cal').onclick=async e=>{const b=e.target.closest('button');if(!b)return;const a=b.dataset.a;
 if(a==='set'){const r=await A().gcal_set_client();if(r.ok){CAL.st=await A().gcal_status();CAL.err=''}else if(r.err)CAL.err=r.err;renderCal()}
 if(a==='connect'){b.textContent='Waiting for browser…';b.disabled=true;const r=await A().gcal_connect();
  if(r.ok){CAL.st=await A().gcal_status();CAL.err='';await calLoad()}else CAL.err=r.err;renderCal()}
 if(a==='refresh')calLoad().then(renderCal),renderCal();
 if(a==='off'){await A().gcal_disconnect();CAL.st.linked=false;CAL.ev=[];renderCal()}
 if(a==='task'){const ev=CAL.ev[+b.dataset.i],st=pd(ev.start);if(ev.allDay)st.setHours(9,0);await addTask(ev.title,'med',localIso(st));toast('Task added with the event time as its deadline.')}};
const banner=m=>{$('#bt').textContent=m;$('#banner').hidden=false};
$('#bd').onclick=()=>$('#banner').hidden=true;
function remind(){if($('#app').hidden)return;const now=Date.now();
 for(const e of CAL.ev){if(e.allDay)continue;const m=(pd(e.start)-now)/6e4,k=e.id+e.start;
  if(m>0&&m<=10&&!CAL.notified.has(k)){CAL.notified.add(k);banner(e.title+' starts in '+Math.ceil(m)+' min');chime();A().attention()}}}

setInterval(()=>{clock();if(window.pywebview&&pywebview.api)ftick()},250);
setInterval(()=>{if(curTab==='tasks'&&!$('#app').hidden)renderTasks()},30000);
setInterval(remind,20000);
setInterval(()=>{if(!$('#app').hidden)calLoad()},3e5);

/* ---------- events ---------- */
const run=async i=>{const r=await A().launch(i);if(r)toast('Couldn’t open: '+r)};
const openSheet=()=>{$('#sheet').hidden=false;$('#sn').value=$('#st').value='';$('#sn').focus()};
const addTask=async(t,p='med',d='')=>{if(!t.trim())return;D=await A().add_task(t.trim(),p,d);if(curTab==='tasks')render()};
$('#go').onclick=go;$('#pw').onkeydown=$('#pw2').onkeydown=e=>{if(e.key==='Enter')go()};$('#lb').onclick=lock;
document.querySelectorAll('#nav button').forEach(b=>b.onclick=()=>tab(b.dataset.t));
$('#grid').onclick=async e=>{const x=e.target.closest('.x'),t=e.target.closest('.tile');if(!t)return;
 if(x){D=await A().remove_shortcut(+x.dataset.x);return render()}
 if(t.id==='addtile')return openSheet();run(+t.dataset.i)};
$('#ta').onclick=async()=>{await addTask($('#tt').value,$('#tp').value,$('#td').value);$('#tt').value=$('#td').value='';$('#tt').focus()};
$('#tt').onkeydown=e=>{if(e.key==='Enter')$('#ta').click()};
$('#tl').onclick=async e=>{const b=e.target.closest('button');if(!b)return;const d=b.dataset;
 if(d.c){D=await A().toggle_task(d.c);render()}
 if(d.x){D=await A().delete_task(d.x);render()}
 if(d.f){T.task=D.tasks.find(t=>t.id===d.f).title;T.mode='pomo';if(!T.run)setPhase('focus');tab('focus')}};
document.querySelectorAll('#fm button').forEach(b=>b.onclick=()=>{T.mode=b.dataset.m;T.run=false;T.acc=0;
 if(T.mode==='pomo')setPhase(T.phase);else if(T.mode==='count'){T.dur=T.left=(+$('#fmin').value||25)*6e4}renderFocus()});
$('#fgo').onclick=startPause;$('#frs').onclick=reset;$('#fskip').onclick=()=>{advance();renderFocus()};
$('#fmin').onchange=()=>{if(!T.run){T.dur=T.left=Math.max(1,+$('#fmin').value||25)*6e4;renderFocus()}};
$('#q').oninput=()=>{if(curTab==='launch')render()};
$('#q').onkeydown=e=>{if(e.key!=='Enter')return;const q=e.target.value.trim();e.target.value='';
 if(q[0]==='/'){const[c,...r]=q.slice(1).split(' '),cmd=c.toLowerCase(),arg=r.join(' ');render();
  if(cmd==='lock')return lock();if(cmd==='tasks')return tab('tasks');if(cmd==='focus')return tab('focus');if(cmd==='cal')return tab('cal');
  if(cmd==='add')return openSheet();if(cmd==='quit')return A().quit();
  if(cmd==='task'){if(!arg)return toast('Type /task followed by the title.');addTask(arg);return toast('Task added.')}
  return toast('Commands: /task  /add  /tasks  /focus  /cal  /lock  /quit')}
 if(curTab==='launch'&&vis[0])run(vis[0].i);render()};
document.querySelectorAll('#kind button').forEach(b=>b.onclick=()=>{kind=b.dataset.k;document.querySelectorAll('#kind button').forEach(x=>x.classList.toggle('on',x===b));$('#br').hidden=kind!=='app'});
$('#br').onclick=async()=>{const p=await A().pick();if(p)$('#st').value=p};
$('#ss').onclick=async()=>{const n=$('#sn').value.trim(),t=$('#st').value.trim();if(!n||!t)return;D=await A().add_shortcut(n,kind,t);$('#sheet').hidden=true;render()};
$('#sc').onclick=()=>$('#sheet').hidden=true;
document.onkeydown=e=>{bump();const m=e.ctrlKey&&!e.altKey;
 if(m&&/^[1-9]$/.test(e.key)&&curTab==='launch'&&vis[e.key-1]){e.preventDefault();run(vis[e.key-1].i)}
 if(m&&e.key==='l'&&!$('#app').hidden){e.preventDefault();lock()}
 if(e.key==='Escape'){if(!$('#sheet').hidden)$('#sheet').hidden=true;else A().hide()}};
onmousemove=bump;

/* ---------- drag & drop (drop an .exe or folder on the Launcher to make a shortcut) ---------- */
document.addEventListener('dragover',e=>{e.preventDefault();document.body.classList.add('drag')});
document.addEventListener('dragleave',e=>{if(!e.relatedTarget)document.body.classList.remove('drag')});
document.addEventListener('drop',e=>{e.preventDefault();document.body.classList.remove('drag')});
async function onDrop(paths){
 if($('#app').hidden)return;
 if(curTab!=='launch')return toast('Open the Launcher tab to drop apps onto it.');
 for(const p of paths)D=await A().add_shortcut(p.split(/[\\/]/).pop().replace(/\.[^.]+$/,'')||p,'app',p);
 render();toast('Shortcut added.')}
window.addEventListener('pywebviewready',boot);
</script>"""

if __name__ == "__main__":
    S["win"] = webview.create_window(TITLE, html=HTML, js_api=Api(), width=1020, height=740,
                                     min_size=(800, 560), on_top=True, background_color="#FFF4D6")
    S["hud"] = webview.create_window(HUD_TITLE, html=HUD_HTML, width=210, height=84, x=40, y=40,
                                     frameless=True, easy_drag=True, on_top=True, hidden=True, resizable=False)
    S["win"].events.closing += on_closing
    S["win"].events.closed += cleanup
    S["win"].events.loaded += on_loaded
    start_hotkey()
    webview.start()
