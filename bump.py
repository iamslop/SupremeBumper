# language: Python, file: bump.py, target: Windows/Linux, Python 3.10+
# deps: pip install aiohttp
# /bump is an application (slash) command, so this fires an interaction directly
# instead of sending the text "/bump" (which Discord would treat as a plain message).

import asyncio
import json
import re
import sys
import time
from datetime import datetime, timezone

import aiohttp

sys.stdout.reconfigure(line_buffering=True)

TOKEN = "USER_TOKEN" # not bot token
GUILD_ID = "SERVER_ID"
CHANNEL_ID = "CHANNEL_ID" # where to send the bump command

APP_ID = "302050872383242240"  # Disboard
BOT_ID = "302050872383242240"  # Disboard user id (same app)
CMD_NAME = "bump"
RETRY_GUARD = 20.0
STATUS_INTERVAL = 1.0

API = "https://discord.com/api/v10"
GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) discord/1.0.9025 Chrome/120.0.6099.291 "
    "Electron/28.2.10 Safari/537.36"
)

HEADERS = {
    "Authorization": TOKEN,
    "Content-Type": "application/json",
    "User-Agent": UA,
    "X-Discord-Locale": "en-US",
}

state = {
    "session_id": None,
    "command": None,
    "last_bot_ts": None,
    "attempts": 0,
    "last_attempt": None,
    "cooldown_until": None,
    "last_ephemeral": None,
}


def snowflake() -> str:
    return str((int(time.time() * 1000) - 1420070400000) << 22)


def log(tag: str, message: str) -> None:
    print(f"[{tag:<8}] {message}")


def parse_time(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def clock(ts: str, full: bool = False) -> str:
    fmt = "%Y-%m-%d %H:%M:%S" if full else "%H:%M:%S"
    return parse_time(ts).astimezone().strftime(fmt)


def age_text(ts: str) -> str:
    seconds = max(
        0.0,
        (datetime.now(timezone.utc) - parse_time(ts)).total_seconds(),
    )
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds / 60:.1f} min ago"
    return f"{seconds / 3600:.1f} h ago"


def message_summary(message: dict) -> str:
    parts = [message.get("content") or ""]
    for embed in message.get("embeds", []):
        parts.append(embed.get("title") or "")
        parts.append(embed.get("description") or "")
    return " ".join(" ".join(parts).split())


def last_message_text(full: bool = False) -> str:
    ts = state["last_bot_ts"]
    if not ts:
        return "none"
    return f"{clock(ts, full=full)} ({age_text(ts)})"


def cooldown_text() -> str:
    until = state["cooldown_until"]
    if not until:
        return "ready now"
    left = until - time.time()
    if left <= 0:
        return "ready now"
    return f"in {int(left // 60)}m {int(left % 60):02d}s"


def print_status() -> None:
    log(
        "STATUS",
        f"last bump {last_message_text()}"
        f"  |  next bump {cooldown_text()}"
        f"  |  attempts {state['attempts']}",
    )


def banner() -> None:
    print("=" * 64)
    print("  DISBOARD bumper")
    print(f"  guild    : {GUILD_ID}")
    print(f"  channel  : {CHANNEL_ID}")
    print("  mode     : bump as soon as the cooldown clears")
    print("=" * 64)


async def report_last_bot_message(session: aiohttp.ClientSession) -> None:
    before = None
    for _ in range(10):
        params = {"limit": 100}
        if before:
            params["before"] = before
        async with session.get(
            f"{API}/channels/{CHANNEL_ID}/messages",
            headers=HEADERS,
            params=params,
        ) as r:
            if r.status == 429:
                body = await r.json()
                await asyncio.sleep(float(body.get("retry_after", 1)))
                continue
            if r.status != 200:
                log("ERROR", f"history {r.status}: {(await r.text())[:120]}")
                return
            messages = await r.json()
        for message in messages:
            if message.get("author", {}).get("id") == BOT_ID:
                state["last_bot_ts"] = message["timestamp"]
                state["cooldown_until"] = (
                    parse_time(message["timestamp"]).timestamp() + 2 * 3600
                )
                log("DISBOARD", f"last message {last_message_text(full=True)}")
                return
        if len(messages) < 100:
            break
        before = messages[-1]["id"]
    log("DISBOARD", "no message found in recent history")


def identify_payload() -> dict:
    return {
        "op": 2,
        "d": {
            "token": TOKEN,
            "capabilities": 125,
            "properties": {
                "os": "Windows",
                "browser": "Chrome",
                "device": "",
                "system_locale": "en-US",
                "browser_user_agent": UA,
                "browser_version": "120.0.6099.291",
                "os_version": "10",
                "referrer": "",
                "referring_domain": "",
                "referrer_current": "",
                "referring_domain_current": "",
                "release_channel": "stable",
                "client_build_number": 240000,
            },
            "presence": {
                "status": "online",
                "since": 0,
                "activities": [],
                "afk": False,
            },
            "compress": False,
        },
    }


async def gateway(session: aiohttp.ClientSession) -> None:
    seq = None
    while True:
        try:
            async with session.ws_connect(GATEWAY, max_msg_size=0) as ws:
                hello = await ws.receive_json()
                interval = hello["d"]["heartbeat_interval"] / 1000.0

                async def beat() -> None:
                    while not ws.closed:
                        await asyncio.sleep(interval)
                        try:
                            await ws.send_json({"op": 1, "d": seq})
                        except Exception:
                            return

                hb = asyncio.create_task(beat())
                await ws.send_json(identify_payload())

                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    op = data.get("op")
                    t = data.get("t")
                    if op == 0:
                        seq = data.get("s", seq)
                        if t == "READY":
                            state["session_id"] = data["d"]["session_id"]
                            user = data["d"]["user"]
                            log(
                                "SESSION",
                                f"connected as {user['username']} ({user['id']})"
                                f"  |  id {state['session_id'][:8]}...",
                            )
                        elif t == "RESUMED":
                            log("GATEWAY", "resumed")
                        elif t == "MESSAGE_CREATE":
                            message = data["d"]
                            if (
                                message.get("channel_id") == CHANNEL_ID
                                and message.get("author", {}).get("id") == BOT_ID
                            ):
                                if message.get("flags", 0) & 64:
                                    summary = message_summary(message)
                                    minute_match = re.search(
                                        r"another\s+(\d+)\s+minute",
                                        summary,
                                        re.IGNORECASE,
                                    )
                                    second_match = re.search(
                                        r"wait\s+(?:another\s+)?(\d+)\s+second",
                                        summary,
                                        re.IGNORECASE,
                                    )
                                    if minute_match:
                                        minutes = int(minute_match.group(1))
                                        state["cooldown_until"] = (
                                            time.time() + minutes * 60
                                        )
                                        ready = datetime.fromtimestamp(
                                            state["cooldown_until"]
                                        ).strftime("%H:%M")
                                        if summary != state["last_ephemeral"]:
                                            state["last_ephemeral"] = summary
                                            log(
                                                "COOLDOWN",
                                                f"{minutes} min left"
                                                f"  |  ready at {ready}",
                                            )
                                    elif second_match:
                                        seconds = int(second_match.group(1))
                                        state["cooldown_until"] = (
                                            time.time() + seconds
                                        )
                                        if summary != state["last_ephemeral"]:
                                            state["last_ephemeral"] = summary
                                            log(
                                                "COOLDOWN",
                                                f"{seconds}s before retry",
                                            )
                                    elif summary != state["last_ephemeral"]:
                                        state["last_ephemeral"] = summary
                                        log(
                                            "DISBOARD",
                                            summary[:200] or "(empty reply)",
                                        )
                                else:
                                    state["last_bot_ts"] = message["timestamp"]
                                    state["cooldown_until"] = time.time() + 2 * 3600
                                    state["last_ephemeral"] = None
                                    log(
                                        "DISBOARD",
                                        f"new message {clock(message['timestamp'])}"
                                        f"  ({age_text(message['timestamp'])})",
                                    )
                    elif op == 1:
                        await ws.send_json({"op": 1, "d": seq})
                    elif op == 7:
                        break
                    elif op == 9:
                        await asyncio.sleep(2)
                        await ws.send_json(identify_payload())

                hb.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log("ERROR", f"gateway: {exc}  |  reconnecting in 5s")
        state["session_id"] = None
        await asyncio.sleep(5)


async def fetch_command(session: aiohttp.ClientSession):
    url = f"{API}/guilds/{GUILD_ID}/application-command-index"
    async with session.get(url, headers=HEADERS) as r:
        if r.status == 429:
            body = await r.json()
            await asyncio.sleep(float(body.get("retry_after", 1)))
            return None
        if r.status != 200:
            log("ERROR", f"command index {r.status}: {(await r.text())[:120]}")
            return None
        data = await r.json()
    for cmd in data.get("application_commands", []):
        if cmd.get("application_id") == APP_ID and cmd.get("name") == CMD_NAME:
            return cmd
    return None


async def sender(session: aiohttp.ClientSession) -> None:
    while state["command"] is None:
        cmd = await fetch_command(session)
        if cmd:
            state["command"] = cmd
            log("COMMAND", f"/{cmd['name']} resolved  |  id {cmd['id']}")
        else:
            log("WARN", "command lookup failed  |  retrying in 5s")
            await asyncio.sleep(5)

    while True:
        if state["session_id"] is None:
            await asyncio.sleep(0.25)
            continue

        now = time.time()
        until = state["cooldown_until"]
        if until and now < until:
            await asyncio.sleep(min(until - now, 1.0))
            continue

        state["cooldown_until"] = now + RETRY_GUARD
        cmd = state["command"]
        payload = {
            "type": 2,
            "application_id": APP_ID,
            "guild_id": GUILD_ID,
            "channel_id": CHANNEL_ID,
            "session_id": state["session_id"],
            "data": {
                "version": cmd.get("version", "1"),
                "id": cmd["id"],
                "name": CMD_NAME,
                "type": 1,
                "options": [],
                "attachments": [],
            },
            "nonce": snowflake(),
        }

        try:
            async with session.post(
                f"{API}/interactions", headers=HEADERS, json=payload
            ) as r:
                if r.status == 204:
                    state["attempts"] += 1
                    state["last_attempt"] = datetime.now(timezone.utc).isoformat()
                    log(
                        "BUMP",
                        f"sent {clock(state['last_attempt'])}"
                        f"  |  attempt #{state['attempts']}",
                    )
                elif r.status == 429:
                    body = await r.json()
                    wait = float(body.get("retry_after", 1))
                    state["cooldown_until"] = time.time() + wait
                    log("LIMIT", f"rate limited  |  waiting {wait:.1f}s")
                else:
                    text = await r.text()
                    log("ERROR", f"{r.status}: {text[:160]}")
        except Exception as exc:
            log("ERROR", f"send: {exc}")


async def status_reporter() -> None:
    while True:
        await asyncio.sleep(STATUS_INTERVAL)
        print_status()


async def main() -> None:
    banner()
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{API}/users/@me", headers=HEADERS) as r:
            if r.status != 200:
                log("ERROR", f"token rejected ({r.status}): {(await r.text())[:160]}")
                return
            me = await r.json()
            log("ACCOUNT", f"{me.get('username')} ({me['id']})")

        await report_last_bot_message(session)
        print_status()
        await asyncio.gather(
            gateway(session),
            sender(session),
            status_reporter(),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print()
        log("EXIT", "stopped")
