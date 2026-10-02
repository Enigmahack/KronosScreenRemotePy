"""BTN / BTN_DOWN / BTN_UP on the wire (docs/api.md section 7): a fake control port captures what CtrlClient sends."""
import os, socket, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import Core.ctrl_client as CC
from Core.button_codes import BUTTON_CODES, btn

fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


lines, stamps = [], []
srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen(1)
port = srv.getsockname()[1]


def serve():
    conn, _ = srv.accept()
    buf = b""
    while True:
        try:
            d = conn.recv(4096)
        except OSError:
            return
        if not d:
            return
        buf += d
        while b"\n" in buf:
            ln, buf = buf.split(b"\n", 1)
            lines.append(ln.decode())
            stamps.append(time.monotonic())


threading.Thread(target=serve, daemon=True).start()

c = CC.CtrlClient()
c.send("127.0.0.1", port, btn("COMBI"))
c.send_chord("127.0.0.1", port, ["BANK_UA", "BANK_IA"])
c.send_chord("127.0.0.1", port, ["MIX_KNOBS", "RESET", "ENTER", "NUM5"], 250)
c.send("127.0.0.1", port, btn("EXIT"))
deadline = time.time() + 5
while len(lines) < 12 and time.time() < deadline:
    time.sleep(0.05)

body = [l for l in lines if l != "CTRL_PERSIST"]
check("press+release is BTN <code>", body[0] == "BTN 1")
check("chord is DOWN in order then UP reversed", body[1:5] == ["BTN_DOWN 31", "BTN_DOWN 24", "BTN_UP 24", "BTN_UP 31"])
check("held chord", body[5:13] == ["BTN_DOWN 74", "BTN_DOWN 75", "BTN_DOWN 23", "BTN_DOWN 16",
                                    "BTN_UP 16", "BTN_UP 23", "BTN_UP 75", "BTN_UP 74"])
check("command after a held chord is not reordered", body[13:14] == ["BTN 8"] if len(body) > 13 else False)
offs = [i for i, l in enumerate(lines) if l == "BTN_DOWN 16"]
if offs:
    i = offs[0]
    gap = stamps[i + 1] - stamps[i]
    check("hold of ~250 ms between last DOWN and first UP", 0.2 <= gap <= 0.6)
check("no deprecated BUTTON/CHORD on the wire", not any(l.startswith(("BUTTON", "CHORD")) for l in lines))
check("every table code is a valid 0-127 integer", all(isinstance(v, int) and 0 <= v <= 127 for v in BUTTON_CODES.values()))

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
