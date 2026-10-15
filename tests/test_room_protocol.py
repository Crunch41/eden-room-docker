#!/usr/bin/env python3
"""Exercise a real room server over the ENet room protocol with generated clients.

  python3 tests/test_room_protocol.py --image IMAGE    run the image, as CI does
  python3 tests/test_room_protocol.py --binary PATH    run a locally built eden-room

The clients need the ENet shared library (libenet7 on Debian and Ubuntu). No game,
account or lobby is involved. The checks follow the patches: admission and rejection,
join rate limiting, local moderator rights, packet validation, relay (byte for byte,
including to unknown addresses), the console log labels, DIAG, and a clean SIGTERM stop.
"""

import argparse
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
JOIN_REQUEST, JOIN_SUCCESS, ROOM_INFORMATION, SET_GAME_INFO = 1, 2, 3, 4
LDN_PACKET, CHAT_MESSAGE, VERSION_MISMATCH, WRONG_PASSWORD = 6, 7, 10, 11
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
    def __init__(self, lib, address):
        self.lib = lib
        self.host = lib.enet_host_create(None, 1, 1, 0, 0)
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

    def __init__(self, args, temp):
        self.args = args
        self.temp = Path(temp)
        self.name = "eden-protocol-" + uuid.uuid4().hex[:12]
        self.process = None

    def start(self):
        if self.args.image:
            env = [f"--env={key}={value}" for key, value in ROOM_ENV.items()]
            subprocess.run(["docker", "run", "-d", "--name", self.name, "--cpus", "1", "--memory", "512m",
                            f"--env=PASSWORD={PASSWORD}", "--env=MAX_MEMBERS=2",
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
             "--max-members", "2", "--bind-address", "127.0.0.1", "--password", PASSWORD,
             "--preferred-game", "Any Game", "--preferred-game-id", "0", "--ban-list-file", str(ban_list)],
            env={**os.environ, **ROOM_ENV, "HOME": str(self.temp)},
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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--image", help="Docker image to run")
    target.add_argument("--binary", help="eden-room executable to run")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="eden-protocol-") as temp:
        for name in run(args, temp):
            print("ok:", name)


if __name__ == "__main__":
    main()
