"""Windows setup window. Saved settings are protected with Windows DPAPI."""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import queue
import sqlite3
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from notifier import ServiceError, Telegram
from market import PublicMarket, Rules, rank, watch

FOLDER = Path(__file__).resolve().parent
SETTINGS = FOLDER / "settings.dat"


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect(data, decrypt=False):
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = Blob(len(data), buffer)
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        function = crypt.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        function = crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = function(ctypes.byref(source), "Binance P2P Notifier", None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise OSError("Windows could not protect or read the saved settings.")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.busy = False
        self.closing = False
        root.title("Binance P2P · Best Market Offers")
        root.geometry("940x850")
        root.minsize(850, 750)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TLabel", font=("Segoe UI", 10))
        style.configure("TButton", padding=7, font=("Segoe UI", 10))
        outer = ttk.Frame(root, padding=24)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="The best USDT / EUR offers", font=("Segoe UI", 20, "bold")).pack(anchor="w")
        ttk.Label(outer, text="BUY: lowest price first. SELL: highest price first. Payments: SEPA Instant or Wise.").pack(anchor="w", pady=(4, 12))
        ttk.Label(outer, text="Alerts when offers enter the best five, improve in price, or become the new leader.\n"
                  "Public market data: no Binance API key needed. Telegram settings stay encrypted on this PC.",
                  wraplength=880).pack(anchor="w", pady=(0, 12))
        form = ttk.Frame(outer)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        self.values, self.entries = {}, []
        for row, (key, label, hidden) in enumerate([
            ("bot_token", "Telegram bot token", True), ("chat_id", "Telegram chat ID", False),
            ("min_eur", "Minimum trade value (EUR)", False), ("max_eur", "Maximum trade value (EUR)", False),
            ("poll_seconds", "Check interval (seconds)", False)]):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 15), pady=6)
            var = tk.StringVar(value={"poll_seconds": "15", "min_eur": "500", "max_eur": "10000"}.get(key, ""))
            entry = ttk.Entry(form, textvariable=var, show="*" if hidden else "")
            entry.grid(row=row, column=1, sticky="ew", pady=6)
            self.values[key] = var
            self.entries.append(entry)
        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(10, 4))
        self.config_buttons = []
        for label, command in [("Save settings", self.save), ("Find my chat", self.find_chat),
                               ("Test market", self.test_binance), ("Send test alert", self.test_telegram)]:
            button = ttk.Button(actions, text=label, command=command)
            button.pack(side="left", padx=(0, 6))
            self.config_buttons.append(button)
        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=(10, 12))
        self.start_button = ttk.Button(controls, text="Start monitoring", command=self.start)
        self.start_button.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(controls, text="Stop", command=self.stop, state="disabled")
        self.stop_button.pack(side="left")
        ttk.Button(controls, text="View saved history", command=self.history).pack(side="right")
        self.status = tk.StringVar(value="Ready · configure and test your connections")
        ttk.Label(outer, textvariable=self.status, wraplength=650).pack(anchor="w", pady=(0, 8))
        self.market_table = ttk.Treeview(outer, columns=("side", "rank", "price", "value", "seller", "payment"), show="headings", height=10)
        for column, label, width in [("side", "Action", 55), ("rank", "Rank", 45), ("price", "EUR / USDT", 90),
                                     ("value", "Approx usable EUR", 160), ("seller", "Advertiser", 180), ("payment", "Payment", 190)]:
            self.market_table.heading(column, text=label)
            self.market_table.column(column, width=width, minwidth=40)
        self.market_table.pack(fill="x", pady=(0, 8))
        self.log = scrolledtext.ScrolledText(outer, height=6, state="disabled", font=("Consolas", 9), wrap="word")
        self.log.pack(fill="both", expand=True)
        ttk.Label(outer, text="Keep the app open and PC awake. History saves observed offers, including offers outside your range.\n"
                  "Rank covers up to 20 offers per payment method and side. Binance confirms your eligibility and exact limits.",
                  wraplength=880).pack(anchor="w", pady=(10, 0))
        if SETTINGS.exists():
            try:
                config = json.loads(protect(SETTINGS.read_bytes(), decrypt=True))
                for key, var in self.values.items():
                    if key in config:
                        var.set(str(config[key]))
            except Exception:
                messagebox.showwarning("Settings", "Saved settings could not be read. Enter them again on this Windows account.")
        root.after(150, self.drain)
        root.protocol("WM_DELETE_WINDOW", self.close)

    def config(self, required=()):
        config = {key: var.get().strip() for key, var in self.values.items()}
        for key in required:
            if not config[key]:
                raise ServiceError("Complete the " + key.replace("_", " ") + " field first.")
        try:
            config["poll_seconds"] = int(config["poll_seconds"])
            if not 10 <= config["poll_seconds"] <= 3600:
                raise ValueError()
        except ValueError:
            raise ServiceError("Choose a check interval from 10 to 3600 seconds.") from None
        Rules.from_config(config)
        return config

    def report(self, text):
        self.events.put(("log", text))

    def drain(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "done":
                    self.busy = False
                    self.set_running(False)
                elif kind == "chat":
                    self.values["chat_id"].set(value)
                elif kind == "market":
                    self.show_market(value)
                else:
                    self.status.set(value)
                    self.log.configure(state="normal")
                    self.log.insert("end", value + "\n")
                    if int(self.log.index("end-1c").split(".")[0]) > 300:
                        self.log.delete("1.0", "50.0")
                    self.log.see("end")
                    self.log.configure(state="disabled")
        except queue.Empty:
            pass
        if self.closing and not self.busy:
            self.root.destroy()
        else:
            self.root.after(150, self.drain)

    def set_running(self, running):
        for widget in self.entries + self.config_buttons + [self.start_button]:
            widget.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")

    def background(self, operation, required):
        if self.busy:
            return
        try:
            config = self.config(required)
        except ServiceError as error:
            messagebox.showerror("Check settings", str(error))
            return
        self.busy = True
        self.stop_event.clear()
        self.set_running(True)

        def run():
            try:
                operation(config)
            except ServiceError as error:
                self.report(str(error))
            except Exception:
                # Do not expose exception strings that might contain secret URLs.
                self.report("Stopped after an unexpected local error. Check folder access and available disk space.")
            finally:
                self.events.put(("done", None))

        self.worker = threading.Thread(target=run, daemon=False)
        self.worker.start()

    def save(self):
        try:
            config = self.config()
            temp = SETTINGS.with_suffix(".tmp")
            temp.write_bytes(protect(json.dumps(config).encode()))
            temp.replace(SETTINGS)
            self.report("Settings saved, encrypted for your Windows account.")
        except (ServiceError, OSError) as error:
            messagebox.showerror("Save settings", str(error))

    def find_chat(self):
        def operation(config):
            self.report("Looking for your private bot chat…")
            chat = Telegram(config["bot_token"], "").find_private_chat(self.report)
            self.events.put(("chat", chat))
            self.report("Found a private chat. Verify the chat ID, then Save settings.")
        self.background(operation, ("bot_token",))

    def test_binance(self):
        def operation(config):
            self.report("Reading public USDT/EUR offers for Wise and SEPA Instant…")
            snapshot = PublicMarket().scan(self.stop_event.is_set)
            if snapshot is None:
                self.report("Market test stopped.")
                return
            rules = Rules.from_config(config)
            ranked = {side: rank(offers, rules) for side, offers in snapshot.items()}
            self.events.put(("market", ranked))
            self.report(f"Public market test passed. Matching top five: {len(ranked['BUY'])} BUY, {len(ranked['SELL'])} SELL. "
                        "Start monitoring to save history and send alerts.")
        self.background(operation, ())

    def test_telegram(self):
        def operation(config):
            Telegram(config["bot_token"], config["chat_id"]).send(
                "Binance P2P market watcher: test received. Watches BUY + SELL USDT/EUR, SEPA Instant or Wise. "
                "Alerts on top-five entries, improved prices and a new best offer. Start monitoring in the app.")
            self.report("Test alert sent. Check your Telegram bot chat.")
        self.background(operation, ("bot_token", "chat_id"))

    def start(self):
        def operation(config):
            self.report("Starting the public market watcher. The first scan sends the current best five on each side.")
            watch(config, FOLDER, self.stop_event, self.report, lambda ranked: self.events.put(("market", ranked)))
            self.report("Monitoring stopped.")
        self.background(operation, ("bot_token", "chat_id"))

    def show_market(self, ranked):
        self.market_table.delete(*self.market_table.get_children())
        rules = Rules.from_config(self.config())
        for side in ("BUY", "SELL"):
            for position, offer in enumerate(ranked[side], 1):
                low, high = offer.usable(rules)
                self.market_table.insert("", "end", values=(side, position, str(offer.price),
                    f"{low:,.2f} - {high:,.2f}", offer.merchant, ", ".join(offer.methods)))

    def history(self):
        path = FOLDER / "market.sqlite3"
        if not path.exists():
            messagebox.showinfo("History", "Start monitoring first. Observed offers are saved automatically.")
            return
        try:
            with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as database:
                total = database.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
                changes = database.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
                rows = database.execute("SELECT side,id,first_seen,last_seen,snapshot FROM offers ORDER BY last_seen DESC LIMIT 500").fetchall()
        except sqlite3.Error:
            messagebox.showerror("History", "Could not read the market history database.")
            return
        from datetime import datetime
        window = tk.Toplevel(self.root)
        window.title("Observed market offers")
        window.geometry("1100x500")
        ttk.Label(window, text=f"{total} unique offers saved; {changes} recorded versions. Showing up to 500 most recently seen.\n"
                  "Last seen means present in a watched API result, not proof that an offer is still open.", padding=12).pack(anchor="w")
        table = ttk.Treeview(window, columns=("side", "id", "first", "last", "seller", "price", "amount", "pay"), show="headings")
        for name, label, width in [("side", "Side", 50), ("id", "Offer ID", 160), ("first", "First seen", 140),
                                  ("last", "Last seen", 140), ("seller", "Advertiser", 170), ("price", "EUR/USDT", 85),
                                  ("amount", "Available USDT", 115), ("pay", "Payment", 170)]:
            table.heading(name, text=label)
            table.column(name, width=width)
        scrollbar = ttk.Scrollbar(window, orient="vertical", command=table.yview)
        table.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        table.pack(fill="both", expand=True)
        for side, number, first, last, serialized in rows:
            item = json.loads(serialized)
            times = [datetime.fromtimestamp(value / 1000).strftime("%d %b %H:%M:%S") for value in (first, last)]
            table.insert("", "end", values=(side, number, *times, item["merchant"], item["price"],
                                          item["available"], ", ".join(item["methods"])))

    def stop(self):
        self.stop_event.set()
        self.report("Stopping after the current network request finishes…")
        self.stop_button.configure(state="disabled")

    def close(self):
        self.closing = True
        if self.busy:
            self.stop()
        else:
            self.root.destroy()


def main():
    # Only one instance can deliver from this folder at a time.
    import msvcrt
    lock = open(FOLDER / "app.lock", "a+b")
    lock.seek(0)
    if lock.read(1) == b"":
        lock.write(b"0")
        lock.flush()
    lock.seek(0)
    root = tk.Tk()
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        root.withdraw()
        messagebox.showinfo("Already open", "Binance P2P Notifier is already running from this folder.")
        root.destroy()
        lock.close()
        return
    try:
        App(root)
        root.mainloop()
    finally:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        lock.close()


if __name__ == "__main__":
    main()
