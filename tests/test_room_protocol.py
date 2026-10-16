#!/usr/bin/env python3
"""Exercise a real room server over the ENet room protocol with generated clients.

  python3 tests/test_room_protocol.py --image IMAGE    run the image, as CI does
  python3 tests/test_room_protocol.py --binary PATH    run a locally built eden-room
  python3 tests/test_room_protocol.py --binary PATH --stress SECONDS
                                                       the live relay settings under churn

The clients need the ENet shared library (libenet7 on Debian and Ubuntu). No game or
account is involved. The checks follow the patches: admission and rejection, join rate
limiting, local moderator rights, packet validation, relay (byte for byte, including to
unknown addresses, and the delivery each kind of packet gets), the console log labels,
DIAG (including what it sees of a member whose packets to the room are delayed, lost and
paused), and a clean SIGTERM stop. A public room is also stopped while it unregisters from a
stand-in lobby, also one that is slow to answer, and must save its ban list first; the image
reaches that lobby through host.docker.internal. With --binary, a room whose log can't be
written is stopped too. --stress runs unsequenced relay with fragmenting packets
while members join, leave, vanish and time out, then stops the room under load; build the
room with a sanitizer to use it.
"""

import argparse
import base64
import ctypes as C
import ctypes.util
import json
import os
import re
import resource
import select
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
    "EDEN_ROOM_RELAY_MODE": "unsequenced",  # as the image sets
    "EDEN_ROOM_MOD_USERNAME": MODERATOR,
    "EDEN_ROOM_DIAG_INTERVAL_SEC": "1",
}

# RoomMessageTypes in src/network/room.h
JOIN_REQUEST, JOIN_SUCCESS, ROOM_INFORMATION, SET_GAME_INFO, PROXY_PACKET = 1, 2, 3, 4, 5
LDN_PACKET, CHAT_MESSAGE, NAME_COLLISION, VERSION_MISMATCH, WRONG_PASSWORD = 6, 7, 8, 10, 11
ROOM_IS_FULL = 13
MOD_KICK, MOD_PERMISSION_DENIED, JOIN_SUCCESS_AS_MOD = 17, 22, 24
NETWORK_VERSION = 1
# ENetPacketFlag of a received packet: how the room sent it
RELIABLE, UNSEQUENCED, UNRELIABLE_FRAGMENT = 1, 2, 8
# Network::Protocol in src/common/socket_types.h
TCP, UDP = 3, 4


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


def proxy(remote, protocol, broadcast, payload, local=b"\x0a\0\0\x01"):
    """A proxy packet: type; domain, IP and port of both ends; protocol; broadcast flag; payload."""
    return (bytes([PROXY_PACKET, 0]) + bytes(local) + b"\x30\x39" + b"\0" + bytes(remote) + b"\x30\x39"
            + bytes([protocol, broadcast]) + payload)


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
        self.flags = {}  # message -> the ENet flags it arrived with
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
                message = C.string_at(packet.data, packet.length)
                self.messages.append(message)
                self.flags[message] = packet.flags
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

    def __init__(self, args, temp, env=ROOM_ENV, max_members=2, extra_args=(), preexec_fn=None,
                 ban_list="YuzuRoom-BanList-1\n\n", lobby=None):
        self.args = args
        self.temp = Path(temp)
        self.env = env
        self.max_members = max_members
        self.extra_args = list(extra_args)
        self.preexec_fn = preexec_fn
        self.ban_list_text = ban_list
        self.lobby = lobby  # (username, token, Lobby) for a public room
        self.name = "eden-protocol-" + uuid.uuid4().hex[:12]
        self.process = None

    def start(self):
        if self.args.image:
            env = [f"--env={key}={value}" for key, value in self.env.items()]
            if self.lobby:
                username, token, lobby = self.lobby
                env += [f"--env=USERNAME={username}", f"--env=TOKEN={token}",
                        f"--env=WEB_API_URL={lobby.container_url}", "--add-host=host.docker.internal:host-gateway"]
            subprocess.run(["docker", "run", "-d", "--name", self.name, "--cpus", "1", "--memory", "512m",
                            f"--env=PASSWORD={PASSWORD}", f"--env=MAX_MEMBERS={self.max_members}",
                            "--env=ROOM_NAME=Generated acceptance", *env, self.args.image],
                           check=True, stdout=subprocess.DEVNULL, timeout=60)
            networks = self.inspect()["NetworkSettings"]["Networks"]
            return next(iter(networks.values()))["IPAddress"], 24872
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        ban_list = self.ban_list = self.temp / "ban_list.txt"
        ban_list.write_text(self.ban_list_text)
        if self.lobby:
            username, token, lobby = self.lobby
            self.extra_args += ["--username", username, "--token", token, "--web-api-url", lobby.url]
        self.log = open(self.temp / "room.log", "wb")
        self.process = subprocess.Popen(
            [self.args.binary, "--room-name", "Generated acceptance", "--port", str(port),
             "--max-members", str(self.max_members), "--bind-address", "127.0.0.1", "--password", PASSWORD,
             "--preferred-game", "Any Game", "--preferred-game-id", "0", "--ban-list-file", str(ban_list),
             *self.extra_args],
            env={**os.environ, **self.env, "HOME": str(self.temp)},
            stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT, preexec_fn=self.preexec_fn)
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

    IMAGE_BAN_LIST = "/home/eden/.local/share/eden-room/ban_list.txt"

    def remove_ban_list(self):
        if self.args.image:
            subprocess.run(["docker", "exec", self.name, "rm", "-f", self.IMAGE_BAN_LIST], check=True, timeout=30)
        else:
            self.ban_list.unlink()

    def has_ban_list(self):
        if self.args.image:  # docker cp also reads a stopped container
            return subprocess.run(["docker", "cp", f"{self.name}:{self.IMAGE_BAN_LIST}", "-"],
                                  capture_output=True, timeout=30).returncode == 0
        return self.ban_list.exists()

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
        check("LDN packets relayed reliably", second.flags[relay] == RELIABLE)
        for name, packet, flags in [
            ("UDP proxy packet relayed unsequenced", proxy(b"\xff" * 4, UDP, 1, b"generated-game"), UNSEQUENCED),
            ("large UDP proxy packet relayed as unreliable fragments",
             proxy(b"\xff" * 4, UDP, 1, os.urandom(1400)), UNRELIABLE_FRAGMENT),
            ("TCP proxy packet relayed reliably", proxy(b"\xff" * 4, TCP, 1, b"generated-stream"), RELIABLE),
        ]:
            first.send(packet)
            check(name, wait(lambda: packet in second.messages) and second.flags[packet] == flags)
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
                            ("PING", "PING  | "), ("STAT", "STAT  | "), ("STAT uplink", " up_holds "),
                            ("DIAG boot", "DIAG  | boot mode=unsequenced ldn=reliable"),
                            ("DIAG report", "DIAG  | mode=unsequenced members="),
                            ("DIAG member", "DIAG  | member ["), ("DIAG advice", "DIAG  | advice: ")]:
            check(f"log shows {label}", text in log)
        holds = [int(n) for n in re.findall(r"DIAG  \| mode=.* up_holds=(\d+) ", log)]
        check("no holds on a clean path (a fragmented packet is not one)", holds and not any(holds))
        check("DIAG does not call the transport clean", "looks clean" not in log)
        check("SIGTERM stops the room cleanly", room.stop())
        return results
    finally:
        for client in clients:
            client.lib.enet_host_destroy(client.host)
        room.remove()


class DelayLink:
    """A UDP hop between one client and the room, like a long uplink: what the client sends
    reaches the room `delay` seconds later, and with drop_next set, the next datagram that
    carries data (not just acknowledgements) is lost. What the room sends passes straight back."""

    def __init__(self, room_ip, room_port, delay):
        import collections
        import threading
        self.delay = delay
        self.drop_next = False
        self.dropped = 0
        self.client_side = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.client_side.bind(("127.0.0.1", 0))
        self.port = self.client_side.getsockname()[1]
        self.room_side = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.room_side.connect((room_ip, room_port))
        self.client = None
        self.queue = collections.deque()  # (due, datagram), in order
        self.running = True
        self.thread = threading.Thread(target=self.forward, daemon=True)
        self.thread.start()

    def forward(self):
        while self.running:
            timeout = max(0.0, self.queue[0][0] - time.monotonic()) if self.queue else 0.05
            readable, _, _ = select.select([self.client_side, self.room_side], [], [], min(timeout, 0.05))
            for sock in readable:
                if sock is self.client_side:
                    data, self.client = sock.recvfrom(65536)
                    if self.drop_next and len(data) > 150:
                        self.drop_next = False
                        self.dropped += 1
                    else:
                        self.queue.append((time.monotonic() + self.delay, data))
                else:
                    data = sock.recv(65536)
                    if self.client:
                        self.client_side.sendto(data, self.client)
            while self.queue and self.queue[0][0] <= time.monotonic():
                self.room_side.send(self.queue.popleft()[1])

    def close(self):
        self.running = False
        self.thread.join()
        self.client_side.close()
        self.room_side.close()


def uplink(args, temp):
    """What DIAG and STAT see of a member whose packets reach the room 30 ms late, when one of
    them is lost and when the member then stops sending for a while: the loss has to show as a
    hold on the member's stream, the pause as a gap no hold explains."""
    room = Room(args, temp)
    link = client = None
    results = []
    try:
        ip, port = room.start()
        lib = load_enet()
        time.sleep(2)
        link = DelayLink(ip, port, 0.03)
        address = Address(port=link.port)
        assert lib.enet_address_set_host_ip(C.byref(address), b"127.0.0.1") == 0
        client = Client(lib, address)

        def play(seconds, send=True):  # a game packet every frame
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                client.pump()
                if send:
                    client.send(proxy(b"\xff" * 4, UDP, 1, os.urandom(200)))
                time.sleep(1 / 60)

        def check(name, ok):
            if not ok:
                print(room.output()[-6000:], file=sys.stderr)
                raise AssertionError(name)
            results.append(name)

        end = time.monotonic() + 10
        while time.monotonic() < end and not client.connected:
            client.pump()
            time.sleep(0.01)
        client.join("generated-uplink")
        play(1, send=False)
        check("member joined over the delaying link", client.has(JOIN_SUCCESS))
        play(2)  # long enough for the client's RTT estimate to settle
        link.drop_next = True
        play(1)
        check("link lost one datagram", link.dropped == 1)
        play(0.4, send=False)
        play(1.5)  # and a DIAG report
        lib.enet_peer_disconnect(client.peer, 0)
        lib.enet_host_flush(client.host)
        end = time.monotonic() + 5
        while time.monotonic() < end and not client.disconnected:
            client.pump()
            time.sleep(0.01)
        time.sleep(0.5)

        lines = room.output().splitlines()
        member = [line for line in lines if "DIAG  | member [" in line and "generated-uplink" in line]
        stat = next((line for line in lines if "STAT  | " in line and "generated-uplink" in line), "")
        count = lambda pattern, text: int(re.search(pattern, text).group(1)) if re.search(pattern, text) else 0
        check("DIAG shows the member", member)
        check("DIAG shows the lost datagram as a hold", sum(count(r" up_holds (\d+) ", line) for line in member) >= 1)
        check("DIAG shows the pause as a gap no hold explains",
              sum(count(r", (\d+) unheld\)", line) for line in member) >= 1)
        check("STAT shows the hold and the pause",
              count(r" up_holds (\d+) ", stat) >= 1 and count(r", (\d+) unheld\)", stat) >= 1)
        check("each DIAG report counts its own window: together they make up the session",
              all(sum(count(pattern, line) for line in member) == count(pattern, stat)
                  for pattern in (r" up_holds (\d+) ", r", (\d+) unheld\)")))
        advice = "\n".join(line for line in lines if "DIAG  | advice: " in line)
        check("DIAG advice names the member's uplink", "generated-uplink's packets to the room were held" in advice)
        check("DIAG advice names the member's stall", "generated-uplink sent no game packets" in advice)
        check("SIGTERM stops the room cleanly", room.stop())
        return results
    finally:
        if client:
            client.lib.enet_host_destroy(client.host)
        if link:
            link.close()
        room.remove()


class Lobby:
    """A stand-in for the lobby web service, enough for a public room to register, announce and
    delete itself. DELETE answers after delete_delay seconds, so the room is still waiting for it
    while it shuts down."""

    def __init__(self, delete_delay=1, for_container=False):
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
                        time.sleep(delete_delay)
                    self.reply("application/json", "{}")

            do_GET = do_POST = do_DELETE = handle_one

        # A container reaches the runner through host.docker.internal, so listen beyond loopback then.
        self.server = http.server.ThreadingHTTPServer(("0.0.0.0" if for_container else "127.0.0.1", 0), Handler)
        port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{port}"
        self.container_url = f"http://host.docker.internal:{port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def public_room(args, temp, delete_delay=1):
    """A room announced to a lobby unregisters itself when it stops; stopping must still be clean
    while that request is in flight. With a lobby slower than docker stop's 10 seconds, the room
    must give up on it in time, having saved its ban list first."""
    lobby = Lobby(delete_delay, for_container=bool(args.image))
    token = base64.b64encode(b"generated:generated-token").decode()
    room = Room(args, temp, lobby=("generated", token, lobby))
    slow = " (slow lobby)" if delete_delay > 10 else ""
    try:
        room.start()
        end = time.monotonic() + 10
        while time.monotonic() < end and ("POST", "/lobby/generated-room-id-0123456789abcdef") not in lobby.requests:
            time.sleep(0.05)
        if ("POST", "/lobby/generated-room-id-0123456789abcdef") not in lobby.requests:
            print(room.output()[-6000:], file=sys.stderr)
            raise AssertionError("public room registered and announced")
        room.remove_ban_list()  # the room writes it again when it stops
        stopped = room.stop()
        log = room.output()
        if not stopped or "Sanitizer" in log or "runtime error" in log:
            print(log[-20000:], file=sys.stderr)
            raise AssertionError(f"public room stops cleanly within 10 seconds while unregistering{slow}")
        if ("DELETE", "/lobby/generated-room-id-0123456789abcdef") not in lobby.requests:
            raise AssertionError("public room unregisters from the lobby when it stops")
        if not room.has_ban_list():
            raise AssertionError("public room saves its ban list when it stops")
        return [f"public room registered and announced{slow}",
                f"public room stops cleanly within 10 seconds while unregistering{slow}",
                f"public room saves its ban list when it stops{slow}"]
    finally:
        room.remove()
        lobby.close()


def unwritable_log(args, temp):
    """A room whose log file can't be written (as on a full disk) must still stop. An error is
    flushed to the log file at once; when that flush fails, the failure is itself logged, which
    used to deadlock the logger. The invalid ban list makes the room log an error at startup."""
    def no_file_writes():
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)  # fail writes (EFBIG) instead of killing
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    room = Room(args, temp, preexec_fn=no_file_writes, ban_list="not a ban list\n")
    # Eden creates its log directory but not the ones above it (the image's entrypoint does)
    (Path(temp) / ".local" / "share" / "eden").mkdir(parents=True)
    try:
        room.start()
        time.sleep(2)
        if not room.running():
            raise AssertionError("room with an unwritable log keeps running")
        if not room.stop():
            raise AssertionError("room with an unwritable log stops cleanly within 10 seconds")
        return ["room with an unwritable log keeps running",
                "room with an unwritable log stops cleanly within 10 seconds"]
    finally:
        room.remove()


STRESS_ENV = {  # the live room's settings
    "EDEN_ROOM_RELAY_MODE": "unsequenced",
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
    with tempfile.TemporaryDirectory(prefix="eden-protocol-") as temp:
        for name in uplink(args, temp):
            print("ok:", name)
    for delete_delay in (1, 25):
        with tempfile.TemporaryDirectory(prefix="eden-protocol-") as temp:
            for name in public_room(args, temp, delete_delay):
                print("ok:", name)
    if args.binary:  # needs a resource limit set on the room's own process
        with tempfile.TemporaryDirectory(prefix="eden-protocol-") as temp:
            for name in unwritable_log(args, temp):
                print("ok:", name)


if __name__ == "__main__":
    main()
