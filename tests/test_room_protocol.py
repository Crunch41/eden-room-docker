#!/usr/bin/env python3
"""Exercise a real room server over the ENet room protocol with generated clients.

  python3 tests/test_room_protocol.py --image IMAGE    run the image, as CI does
  python3 tests/test_room_protocol.py --binary PATH    run a locally built eden-room
  python3 tests/test_room_protocol.py --binary PATH --stress SECONDS
                                                       the live relay settings under churn

The clients need the ENet shared library (libenet7 on Debian and Ubuntu). No game or
account is involved. The checks follow the patches: admission and rejection, join rate
limiting, local moderator rights, packet validation, relay (byte for byte, including to
unknown addresses), the console log labels, DIAG, and a clean SIGTERM stop. With
--binary, a public room is also stopped while it unregisters from a stand-in lobby.
--stress runs sequenced relay with fragmenting packets while members join, leave, vanish
and time out, then stops the room under load; build the room with a sanitizer to use it.
"""

import argparse
import base64
import ctypes as C
import ctypes.util
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

PASSWORD = "generated-only"
MODERATOR = "generated-second"
ROOM_ENV = {
    "EDEN_ROOM_RELAY_MODE": "reliable",
    "EDEN_ROOM_MOD_USERNAME": MODERATOR,
    "EDEN_ROOM_DIAG_INTERVAL_SEC": "1",
}

# RoomMessageTypes in src/network/room.h
JOIN_REQUEST, JOIN_SUCCESS, ROOM_INFORMATION, SET_GAME_INFO, PROXY_PACKET = 1, 2, 3, 4, 5
LDN_PACKET, CHAT_MESSAGE, NAME_COLLISION, VERSION_MISMATCH, WRONG_PASSWORD = 6, 7, 8, 10, 11
ROOM_IS_FULL = 13
MOD_KICK, MOD_PERMISSION_DENIED, JOIN_SUCCESS_AS_MOD = 17, 22, 24
NETWORK_VERSION = 1


class Address(C.Structure):
    _fields_ = [("host", C.c_uint32), ("port", C.c_uint16)]


class Packet(C.Structure):
    _fields_ = [("references", C.c_size_t), ("flags", C.c_uint32),
                ("data", C.c_void_p), ("length", C.c_size_t)]


class Event(C.Structure):
    _fields_ = [("type", C.c_int), ("peer", C.c_void_p), ("channel", C.c_uint8),
                ("data", C.c_uint32), ("packet", C.POINTER(Packet))]


def load_enet():
    name = ctypes.util.find_library("enet")
    if not name:
        sys.exit("The ENet shared library is missing (apt install libenet7).")
    lib = C.CDLL(name)
    for function, args, result in [
        ("enet_initialize", [], C.c_int),
        ("enet_host_create", [C.c_void_p, C.c_size_t, C.c_size_t, C.c_uint32, C.c_uint32], C.c_void_p),
        ("enet_host_connect", [C.c_void_p, C.POINTER(Address), C.c_size_t, C.c_uint32], C.c_void_p),
        ("enet_host_service", [C.c_void_p, C.POINTER(Event), C.c_uint32], C.c_int),
        ("enet_host_flush", [C.c_void_p], None),
        ("enet_host_destroy", [C.c_void_p], None),
        ("enet_address_set_host_ip", [C.POINTER(Address), C.c_char_p], C.c_int),
        ("enet_packet_create", [C.c_void_p, C.c_size_t, C.c_uint32], C.POINTER(Packet)),
        ("enet_packet_destroy", [C.POINTER(Packet)], None),
        ("enet_peer_send", [C.c_void_p, C.c_uint8, C.POINTER(Packet)], C.c_int),
        ("enet_peer_disconnect", [C.c_void_p, C.c_uint32], None),
        ("enet_peer_reset", [C.c_void_p], None),
    ]:
        getattr(lib, function).argtypes = args
        getattr(lib, function).restype = result
    assert lib.enet_initialize() == 0
    return lib


def string(value):
    value = value.encode()
    return struct.pack("!I", len(value)) + value


def ldn(remote, broadcast, payload):
    """An LDN packet: type, LAN packet type, local IP, remote IP, broadcast flag, payload."""
    return bytes([LDN_PACKET, 0, 10, 0, 0, 1]) + bytes(remote) + bytes([broadcast]) + payload


class Client:
    def __init__(self, lib, address, source=None):
        self.lib = lib
        bind = None
        if source:  # a loopback source address of its own, for the per-address join rate limit
            bind = Address(port=0)
            assert lib.enet_address_set_host_ip(C.byref(bind), source.encode()) == 0
        self.host = lib.enet_host_create(C.byref(bind) if bind else None, 1, 1, 0, 0)
        assert self.host
        self.peer = lib.enet_host_connect(self.host, C.byref(address), 1, 0)
        assert self.peer
        self.messages = []
        self.connected = self.disconnected = False

    def pump(self):
        event = Event()
        while self.lib.enet_host_service(self.host, C.byref(event), 0) > 0:
            if event.type == 1:
                self.connected = True
            elif event.type == 2:
                self.disconnected = True
            elif event.type == 3:
                packet = event.packet.contents
                self.messages.append(C.string_at(packet.data, packet.length))
                self.lib.enet_packet_destroy(event.packet)

    def send(self, payload):
        packet = self.lib.enet_packet_create(payload, len(payload), 1)  # reliable
        assert self.lib.enet_peer_send(self.peer, 0, packet) == 0
        self.lib.enet_host_flush(self.host)

    def join(self, nickname, password=PASSWORD, version=NETWORK_VERSION):
        self.send(bytes([JOIN_REQUEST]) + string(nickname) + b"\xff" * 4 + struct.pack("!I", version)
                  + string(password) + string(""))

    def has(self, message_id, content=b""):
        return any(m and m[0] == message_id and content in m for m in self.messages)


class Room:
    """The room under test: start(), address, output(), stop() -> clean exit."""

    def __init__(self, args, temp, env=ROOM_ENV, max_members=2, extra_args=()):
        self.args = args
        self.temp = Path(temp)
        self.env = env
        self.max_members = max_members
        self.extra_args = list(extra_args)
        self.name = "eden-protocol-" + uuid.uuid4().hex[:12]
        self.process = None

    def start(self):
        if self.args.image:
            env = [f"--env={key}={value}" for key, value in self.env.items()]
            subprocess.run(["docker", "run", "-d", "--name", self.name, "--cpus", "1", "--memory", "512m",
                            f"--env=PASSWORD={PASSWORD}", f"--env=MAX_MEMBERS={self.max_members}",
                            "--env=ROOM_NAME=Generated acceptance", *env, self.args.image],
                           check=True, stdout=subprocess.DEVNULL, timeout=60)
            networks = self.inspect()["NetworkSettings"]["Networks"]
            return next(iter(networks.values()))["IPAddress"], 24872
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        ban_list = self.temp / "ban_list.txt"
        ban_list.write_text("YuzuRoom-BanList-1\n\n")
        self.log = open(self.temp / "room.log", "wb")
        self.process = subprocess.Popen(
            [self.args.binary, "--room-name", "Generated acceptance", "--port", str(port),
             "--max-members", str(self.max_members), "--bind-address", "127.0.0.1", "--password", PASSWORD,
             "--preferred-game", "Any Game", "--preferred-game-id", "0", "--ban-list-file", str(ban_list),
             *self.extra_args],
            env={**os.environ, **self.env, "HOME": str(self.temp)},
            stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT)
        return "127.0.0.1", port

    def inspect(self):
        return json.loads(subprocess.check_output(["docker", "inspect", self.name], timeout=30))[0]

    def output(self):
        if self.args.image:
            return subprocess.run(["docker", "logs", self.name], capture_output=True, timeout=30).stdout.decode()
        return (self.temp / "room.log").read_bytes().decode(errors="replace")

    def stop(self):
        """Stop with SIGTERM; True when the room shut down by itself within 10 seconds."""
        if self.args.image:
            subprocess.run(["docker", "stop", "--time", "10", self.name], check=True,
                           stdout=subprocess.DEVNULL, timeout=60)
            return self.inspect()["State"]["ExitCode"] != 137
        self.process.send_signal(signal.SIGTERM)
        try:
            return self.process.wait(timeout=10) == 0
        except subprocess.TimeoutExpired:
            return False

    def running(self):
        if self.args.image:
            return self.inspect()["State"]["Running"]
        return self.process.poll() is None

    def remove(self):
        if self.args.image:
            subprocess.run(["docker", "rm", "-fv", self.name], capture_output=True, timeout=30)
        elif self.process:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait()
            self.log.close()


def run(args, temp):
    room = Room(args, temp)
    clients = []
    results = []
    try:
        ip, port = room.start()
        lib = load_enet()
        address = Address(port=port)
        assert lib.enet_address_set_host_ip(C.byref(address), ip.encode()) == 0

        def wait(predicate, seconds=5):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                for client in clients:
                    client.pump()
                if predicate():
                    return True
                time.sleep(0.01)
            return False

        def check(name, ok):
            if not ok:
                print(room.output()[-6000:], file=sys.stderr)
                raise AssertionError(name)
            results.append(name)

        def connect():
            client = Client(lib, address)
            clients.append(client)
            assert wait(lambda: client.connected, 10), "ENet connection failed"
            return client

        def next_join_allowed():
            time.sleep(1.1)  # one join attempt per address per second

        time.sleep(2)
        bad = connect()
        bad.join("wrong-password", password="wrong")
        check("wrong password rejected, peer released", wait(lambda: bad.has(WRONG_PASSWORD) and bad.disconnected))
        next_join_allowed()
        old = connect()
        old.join("wrong-version", version=NETWORK_VERSION + 98)
        check("version mismatch rejected, peer released", wait(lambda: old.has(VERSION_MISMATCH) and old.disconnected))
        next_join_allowed()

        first = connect()
        first.join("generated-first")
        check("member admitted", wait(lambda: first.has(JOIN_SUCCESS)))
        hasty = connect()
        hasty.join("generated-hasty")
        check("second join within a second refused",
              wait(lambda: hasty.disconnected) and not hasty.has(JOIN_SUCCESS))
        next_join_allowed()
        second = connect()
        second.join(MODERATOR)
        check("EDEN_ROOM_MOD_USERNAME on a local address is moderator", wait(lambda: second.has(JOIN_SUCCESS_AS_MOD)))
        first.send(bytes([MOD_KICK]) + string(MODERATOR))
        check("kick from a non-moderator denied", wait(lambda: first.has(MOD_PERMISSION_DENIED)))

        first.send(bytes([CHAT_MESSAGE]) + string("generated-chat"))
        check("chat relayed", wait(lambda: second.has(CHAT_MESSAGE, b"generated-chat")))
        first.send(bytes([SET_GAME_INFO]) + string("Generated game") + struct.pack("!Q", 1) + string("fixture"))
        check("game info broadcast", wait(lambda: second.has(ROOM_INFORMATION, b"Generated game")))

        relay = ldn(b"\xff" * 4, 1, struct.pack("!I", 16) + b"generated-relay!")
        first.send(relay)
        check("LDN broadcast relayed byte for byte", wait(lambda: relay in second.messages))
        unknown = ldn(b"\x01\x02\x03\x04", 0, b"generated-unknown-address")
        first.send(unknown)
        check("LDN packet to an unknown address broadcast", wait(lambda: unknown in second.messages))

        second.messages.clear()
        outsider = connect()
        outsider.send(relay)
        check("peer that never joined cannot relay", not wait(lambda: second.has(LDN_PACKET), 1))
        for bad_packet in (bytes([LDN_PACKET]), relay[:8], relay + b"x" * 1600, bytes([CHAT_MESSAGE, 0, 0])):
            first.send(bad_packet)
        check("truncated and oversized packets not relayed", not wait(lambda: second.has(LDN_PACKET), 1))
        first.send(relay)
        check("relay still works after bad packets", wait(lambda: relay in second.messages))

        lib.enet_peer_disconnect(first.peer, 0)
        lib.enet_host_flush(first.host)
        check("graceful disconnect", wait(lambda: first.disconnected))
        next_join_allowed()
        rejoined = connect()
        rejoined.join("generated-first")
        check("nickname and slot reusable after disconnect", wait(lambda: rejoined.has(JOIN_SUCCESS)))
        time.sleep(1.5)  # one DIAG report

        log = room.output()
        for label, text in [("JOIN", "JOIN  | "), ("player count", "has joined. (1/2)"), ("LEAVE", "LEAVE | "),
                            ("CHAT", "CHAT  | generated-first: generated-chat"), ("GAME", "GAME  | "),
                            ("PING", "PING  | "), ("STAT", "STAT  | "), ("DIAG boot", "DIAG  | boot mode=reliable"),
                            ("DIAG report", "DIAG  | mode=reliable members=")]:
            check(f"log shows {label}", text in log)
        check("SIGTERM stops the room cleanly", room.stop())
        return results
    finally:
        for client in clients:
            client.lib.enet_host_destroy(client.host)
        room.remove()


class Lobby:
    """A stand-in for the lobby web service, enough for a public room to register, announce and
    delete itself. DELETE answers after a delay, so the room is still waiting for it while it
    shuts down."""

    def __init__(self):
        import http.server
        import threading
        lobby = self
        self.requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, content_type, body):
                body = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def handle_one(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                lobby.requests.append((self.command, self.path))
                if self.path == "/jwt/internal":
                    self.reply("text/html", "generated.jwt.token")
                elif self.path == "/jwt/external/key.pem":
                    self.reply("text/plain", "")
                elif self.command == "POST" and self.path == "/lobby":
                    self.reply("application/json", json.dumps({
                        "id": "generated-room-id-0123456789abcdef", "externalGuid": "generated-guid",
                        "address": "127.0.0.1", "name": "Generated acceptance", "owner": "generated",
                        "port": 24872, "preferredGameName": "Any Game", "preferredGameId": 0,
                        "maxPlayers": 2, "netVersion": NETWORK_VERSION, "hasPassword": True}))
                else:
                    if self.command == "DELETE":
                        time.sleep(1)
                    self.reply("application/json", "{}")

            do_GET = do_POST = do_DELETE = handle_one

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def public_room(args, temp):
    """A room announced to a lobby unregisters itself when it stops; stopping must still be clean
    while that request is in flight."""
    lobby = Lobby()
    token = base64.b64encode(b"generated:generated-token").decode()
    room = Room(args, temp, extra_args=["--username", "generated", "--token", token, "--web-api-url", lobby.url])
    try:
        room.start()
        end = time.monotonic() + 10
        while time.monotonic() < end and ("POST", "/lobby/generated-room-id-0123456789abcdef") not in lobby.requests:
            time.sleep(0.05)
        if ("POST", "/lobby/generated-room-id-0123456789abcdef") not in lobby.requests:
            print(room.output()[-6000:], file=sys.stderr)
            raise AssertionError("public room registered and announced")
        stopped = room.stop()
        log = room.output()
        if not stopped or "Sanitizer" in log or "runtime error" in log:
            print(log[-20000:], file=sys.stderr)
            raise AssertionError("public room stops cleanly while unregistering from the lobby")
        if ("DELETE", "/lobby/generated-room-id-0123456789abcdef") not in lobby.requests:
            raise AssertionError("public room unregisters from the lobby when it stops")
        return ["public room registered and announced",
                "public room stops cleanly while unregistering from the lobby"]
    finally:
        room.remove()
        lobby.close()


STRESS_ENV = {  # the live room's settings
    "EDEN_ROOM_RELAY_MODE": "sequenced",
    "EDEN_ROOM_PEER_TIMEOUT_MIN": "8000",
    "EDEN_ROOM_PEER_TIMEOUT_MAX": "25000",
    "EDEN_ROOM_PING_INTERVAL": "100",
    "EDEN_ROOM_UNKNOWN_IP_FALLBACK": "drop",
    "EDEN_ROOM_DIAG_INTERVAL_SEC": "5",
}


def stress(args, temp, seconds):
    """Members relay LDN and proxy packets, most of them above ENet's fragment size, while others
    join, leave, vanish, stop answering and get rejected. The room has to stay up throughout and
    stop cleanly on SIGTERM under load. Returns counts of what happened."""
    import random
    rng = random.Random(args.seed)
    room = Room(args, temp, env=STRESS_ENV, max_members=8)
    clients = {}  # Client -> its state
    leaving = []  # (Client, time to destroy it): graceful disconnects still being sent
    counts = dict(joins=0, rejected=0, graceful=0, vanished=0, reset=0, frozen=0, sent=0, received=0)
    lib = None
    try:
        ip, port = room.start()
        lib = load_enet()
        address = Address(port=port)
        assert lib.enet_address_set_host_ip(C.byref(address), ip.encode()) == 0
        sources = [f"127.0.{a}.{b}" for a in range(1, 4) for b in range(2, 255)]
        last_join = {}
        time.sleep(2)

        def new_client():
            now = time.monotonic()
            source = next(s for s in rng.sample(sources, len(sources)) if now - last_join.get(s, 0) > 1.2)
            last_join[source] = now
            kind = rng.choices(["member", "wrong", "duplicate"], [8, 1, 1])[0]
            clients[Client(lib, address, source)] = dict(
                kind=kind, joined=False, sent_join=False, fake_ip=None, frozen_until=0,
                nickname=f"stress-{rng.randrange(10**6):06d}", born=now)

        def drop(client, how):
            clients.pop(client)
            counts[how] += 1
            if how == "graceful":
                lib.enet_peer_disconnect(client.peer, 0)
                lib.enet_host_flush(client.host)
                leaving.append((client, time.monotonic() + 1))
            elif how == "reset":  # the room is not told; it has to time the peer out
                lib.enet_peer_reset(client.peer)
                lib.enet_host_destroy(client.host)
            else:
                lib.enet_host_destroy(client.host)

        def relay_packet(sender, members):
            size = rng.choice([rng.randrange(40, 600), rng.randrange(1300, 1537), rng.randrange(1365, 1537)])
            target = rng.choice([b"\xff" * 4, b"\x01\x02\x03\x04"] + [m["fake_ip"] for m in members])
            broadcast = int(target == b"\xff" * 4)
            if rng.random() < 0.5:
                head = ldn(target, broadcast, b"")
            else:  # proxy: type, domain, local IP, port, domain, remote IP, port, protocol, broadcast
                head = (bytes([PROXY_PACKET, 0]) + sender["fake_ip"] + b"\x30\x39"
                        + b"\0" + target + b"\x30\x39" + b"\x01" + bytes([broadcast]))
            return head + rng.randbytes(size - len(head))

        end = time.monotonic() + seconds
        while time.monotonic() < end:
            now = time.monotonic()
            if not room.running():
                print(room.output()[-20000:], file=sys.stderr)
                raise AssertionError("the room died under load")
            while len(clients) < 12:
                new_client()
            for client, until in list(leaving):
                if now > until:
                    leaving.remove((client, until))
                    lib.enet_host_destroy(client.host)
                else:
                    client.pump()
            members = [state for state in clients.values() if state["joined"]]
            for client, state in list(clients.items()):
                if state["frozen_until"]:  # no acks, no pings: the room times the peer out
                    if now > state["frozen_until"]:
                        drop(client, "vanished")
                    continue
                client.pump()
                if client.disconnected:
                    clients.pop(client)
                    lib.enet_host_destroy(client.host)
                    continue
                for message in client.messages:
                    if message[:1] in (bytes([LDN_PACKET]), bytes([PROXY_PACKET])):
                        counts["received"] += 1
                    elif message[:1] in (bytes([JOIN_SUCCESS]), bytes([JOIN_SUCCESS_AS_MOD])):
                        state["joined"], state["fake_ip"] = True, message[1:5]
                        counts["joins"] += 1
                    elif message[:1] in (bytes([WRONG_PASSWORD]), bytes([ROOM_IS_FULL]), bytes([NAME_COLLISION])):
                        counts["rejected"] += 1
                client.messages.clear()
                if client.connected and not state["sent_join"]:
                    state["sent_join"] = True
                    if state["kind"] == "wrong":
                        client.join(state["nickname"], password="wrong")
                    elif state["kind"] == "duplicate" and members:
                        client.join(rng.choice(members)["nickname"])
                    else:
                        client.join(state["nickname"])
                if not state["joined"]:
                    if now - state["born"] > 6:
                        drop(client, "vanished")
                    continue
                for _ in range(rng.choice([0, 1, 1, 2, 4])):
                    client.send(relay_packet(state, members))
                    counts["sent"] += 1
                roll = rng.random()
                if roll < 0.002:
                    drop(client, "graceful")
                elif roll < 0.004:
                    drop(client, "vanished")
                elif roll < 0.005:
                    drop(client, "reset")
                elif roll < 0.006:
                    state["frozen_until"] = now + rng.uniform(3, 30)
                    counts["frozen"] += 1
                elif roll < 0.008:
                    client.send(bytes([CHAT_MESSAGE]) + string("stress chat"))
            time.sleep(0.002)

        # SIGTERM while members are still sending.
        members = [state for state in clients.values() if state["joined"]]
        for client, state in clients.items():
            if state["joined"] and not state["frozen_until"]:
                for _ in range(20):
                    client.send(relay_packet(state, members))
        stopped = room.stop()
        log = room.output()
        reports = [line for line in log.splitlines() if "Sanitizer" in line or "runtime error" in line]
        if reports or not stopped:
            print(log[-20000:], file=sys.stderr)
            raise AssertionError("sanitizer report" if reports else "SIGTERM did not stop the room cleanly")
        return counts
    finally:
        if lib:
            for client in list(clients) + [client for client, _ in leaving]:
                lib.enet_host_destroy(client.host)
        room.remove()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--image", help="Docker image to run")
    target.add_argument("--binary", help="eden-room executable to run")
    parser.add_argument("--stress", type=float, metavar="SECONDS",
                        help="instead of the checks, load the room with the live settings and member churn")
    parser.add_argument("--seed", type=int, default=1, help="random seed for --stress")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="eden-protocol-") as temp:
        if args.stress:
            print("stress ok:", stress(args, temp, args.stress))
            return
        for name in run(args, temp):
            print("ok:", name)
    if args.binary:  # the image test cannot reach a lobby on the runner
        with tempfile.TemporaryDirectory(prefix="eden-protocol-") as temp:
            for name in public_room(args, temp):
                print("ok:", name)


if __name__ == "__main__":
    main()
