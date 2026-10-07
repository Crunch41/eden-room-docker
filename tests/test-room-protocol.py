"""Exercise the real built room with generated ENet clients; no game assets/account."""
import ctypes as C
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import uuid


class Address(C.Structure):
    _fields_ = [('host', C.c_uint32), ('port', C.c_uint16)]


class Packet(C.Structure):
    _fields_ = [('references', C.c_size_t), ('flags', C.c_uint32),
                ('data', C.c_void_p), ('length', C.c_size_t)]


class Event(C.Structure):
    _fields_ = [('type', C.c_int), ('peer', C.c_void_p), ('channel', C.c_uint8),
                ('data', C.c_uint32), ('packet', C.POINTER(Packet))]


def command(*args):
    return subprocess.check_output(args, stderr=subprocess.PIPE, timeout=60).decode().strip()


def string(value):
    value = value.encode()
    return struct.pack('!I', len(value)) + value


class Client:
    def __init__(self, lib, address):
        self.lib = lib
        self.host = lib.enet_host_create(None, 1, 1, 0, 0)
        assert self.host
        self.peer = lib.enet_host_connect(self.host, C.byref(address), 1, 0)
        assert self.peer
        self.messages = []
        self.connected = False
        self.disconnected = False

    def pump(self):
        event = Event()
        while self.lib.enet_host_service(self.host, C.byref(event), 0) > 0:
            if event.type == 1: self.connected = True
            elif event.type == 2: self.disconnected = True
            elif event.type == 3:
                packet = event.packet.contents
                self.messages.append(C.string_at(packet.data, packet.length))
                self.lib.enet_packet_destroy(event.packet)

    def send(self, payload):
        packet = self.lib.enet_packet_create(payload, len(payload), 1)
        assert self.lib.enet_peer_send(self.peer, 0, packet) == 0
        self.lib.enet_host_flush(self.host)

    def join(self, nickname, password='generated-only', version=1):
        self.send(b'\x01' + string(nickname) + b'\xff' * 4 + struct.pack('!I', version)
                  + string(password) + string(''))

    def has(self, message_id, content=b''):
        return any(p and p[0] == message_id and content in p for p in self.messages)


def main():
    image = sys.argv[1]
    name = 'eden-protocol-' + uuid.uuid4().hex[:12]
    clients = []
    results = {}
    with tempfile.TemporaryDirectory(prefix='eden-protocol-') as temp:
        try:
            command('docker', 'run', '-d', '--name', name, '--cpus', '1', '--memory', '512m',
                    '-e', 'PASSWORD=generated-only', '-e', 'MAX_MEMBERS=2', '-e', 'ROOM_NAME=Generated acceptance',
                    '-e', 'EDEN_ROOM_RELAY_MODE=reliable', image)
            command('docker', 'cp', '-L', name + ':/lib/x86_64-linux-gnu/libenet.so.7', temp + '/libenet.so.7')
            lib = C.CDLL(str(Path(temp) / 'libenet.so.7'))
            signatures = {
                'enet_host_create': ([C.c_void_p, C.c_size_t, C.c_size_t, C.c_uint32, C.c_uint32], C.c_void_p),
                'enet_host_connect': ([C.c_void_p, C.POINTER(Address), C.c_size_t, C.c_uint32], C.c_void_p),
                'enet_host_service': ([C.c_void_p, C.POINTER(Event), C.c_uint32], C.c_int),
                'enet_host_flush': ([C.c_void_p], None),
                'enet_host_destroy': ([C.c_void_p], None),
                'enet_address_set_host_ip': ([C.POINTER(Address), C.c_char_p], C.c_int),
                'enet_packet_create': ([C.c_void_p, C.c_size_t, C.c_uint32], C.POINTER(Packet)),
                'enet_packet_destroy': ([C.POINTER(Packet)], None),
                'enet_peer_send': ([C.c_void_p, C.c_uint8, C.POINTER(Packet)], C.c_int),
                'enet_peer_disconnect': ([C.c_void_p, C.c_uint32], None),
            }
            for key, (args, result) in signatures.items():
                getattr(lib, key).argtypes = args
                getattr(lib, key).restype = result
            assert lib.enet_initialize() == 0
            address = Address(port=24872)
            ip = json.loads(command('docker', 'inspect', name))[0]['NetworkSettings']['Networks']['bridge']['IPAddress']
            assert lib.enet_address_set_host_ip(C.byref(address), ip.encode()) == 0

            def wait(predicate, seconds=5):
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    for client in clients: client.pump()
                    if predicate(): return True
                    time.sleep(.01)
                return False

            def new():
                client = Client(lib, address); clients.append(client)
                assert wait(lambda: client.connected), 'ENet connection failed'
                return client

            time.sleep(2)
            bad = new(); bad.join('wrong-password', password='wrong')
            assert wait(lambda: bad.has(11) and bad.disconnected), 'Wrong password did not reject and release peer'
            results['wrong_password_rejected_and_disconnected'] = True
            time.sleep(1.1)
            bad_version = new(); bad_version.join('wrong-version', version=99)
            assert wait(lambda: bad_version.has(10) and bad_version.disconnected), 'Version mismatch not rejected'
            results['version_mismatch_rejected'] = True
            time.sleep(1.1)
            first = new(); first.join('generated-first')
            assert wait(lambda: first.has(2)), 'First room admission failed'
            time.sleep(1.1)
            second = new(); second.join('generated-second')
            assert wait(lambda: second.has(2)), 'Second room admission failed'
            first.send(b'\x07' + string('generated-chat'))
            assert wait(lambda: second.has(7, b'generated-chat')), 'Chat did not reach second member'
            first.send(b'\x04' + string('Generated game') + struct.pack('!Q', 1) + string('fixture'))
            assert wait(lambda: second.has(3, b'Generated game')), 'Game information was not broadcast'
            results['join_chat_game_info'] = True
            relay = b'\x06\x00' + b'\x0a\x00\x00\x01' + b'\xff' * 4 + b'\x01' + struct.pack('!I', 16) + b'generated-relay!'
            first.send(relay)
            assert wait(lambda: relay in second.messages), 'Admitted LDN payload was changed or not relayed'
            second.messages.clear()
            outsider = new(); outsider.send(relay)
            assert not wait(lambda: second.has(6), 1), 'Unadmitted peer bypassed room membership'
            first.send(b'\x06'); first.send(relay + b'x' * 1600)
            assert not wait(lambda: second.has(6), 1), 'Malformed/oversized payload was relayed'
            first.send(relay)
            assert wait(lambda: relay in second.messages), 'Malformed input killed normal relay'
            results['relay_membership_size_and_malformed_guards'] = True
            lib.enet_peer_disconnect(first.peer, 0); lib.enet_host_flush(first.host)
            assert wait(lambda: first.disconnected), 'Graceful disconnect failed'
            time.sleep(1.1)
            rejoined = new(); rejoined.join('generated-first')
            assert wait(lambda: rejoined.has(2)), 'Nickname/slot not reusable after disconnect'
            results['disconnect_reconnect'] = True
            command('docker', 'stop', '--time', '10', name)
            state = json.loads(command('docker', 'inspect', name))[0]['State']
            assert state['ExitCode'] != 137, 'Room required forced shutdown'
            results['graceful_shutdown'] = True
            print(json.dumps(results, indent=2))
        finally:
            for client in clients: client.lib.enet_host_destroy(client.host)
            subprocess.run(['docker', 'rm', '-fv', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)


if __name__ == '__main__': main()
