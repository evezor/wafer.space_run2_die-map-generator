import struct, json
from collections import namedtuple
'''
potential message packing
[data][header][len]
'''

Functor = namedtuple('Functor', ('pid', 'arg'))


# ---------------------------------------------------------------------------
# datatypes — the ONE datatype-name -> wire-code map.
#
# This used to be duplicated in parameter.py and namespace.py, and the copies
# disagreed (rgb was '3B' in one and '3b' in the other). Both import it from
# here now; message.py owns it because serialization is what a wire code means.
#
# A wire code is either a struct format ('f', '?', '3B') or one of floe's own
# pseudo-codes handled by _PACK/_UNPACK below:
#
#   'e'  raw buffer, no transform
#   'u'  utf8 text
#   'j'  JSON
#   'J'  joystick  -> (x, y, button)
#
# Pseudo-codes are single characters on purpose: the code is what rides in an
# add_sub frame (see nwk.add_sub), so it has to stay small.
#
# Anything NOT listed here is passed to struct verbatim, so a Parameter may
# still declare a raw format ('3h', '2f') as its serdes. Note that MicroPython's
# struct has no 'e' (half-float) typecode — a serdes of '2e' packs fine under
# CPython and dies with "bad typecode" on an ESP32.
# ---------------------------------------------------------------------------
datatypes = {
    'bool': '?',
    'byte': 'b',
    'unbyte': 'B',
    'int16': 'h',
    'unint16': 'H',
    'int': 'i',
    'unint': 'I',
    'int64': 'q',
    'unint64': 'Q',
    'float': 'f',
    'double': 'd',
    'buf': 'e',
    'bytes': 'e',
    'utf8': 'u',
    'string': 'u',
    'nibble': 'u',
    'rgb': '3B',
    'JSON': 'j',
    'json': 'j',
    'joystick': 'J',
}


# --- joystick ('J') --------------------------------------------------------
# One stick — (x, y, button) — in ONE classic 8-byte CAN frame.
#
#   x, y   int16, Q15 fixed point:  -1.0 .. +1.0  <->  -32767 .. +32767
#   button int16, 0 / 1
#
# '3h' is 6 bytes. Three float32s would be 12 (two frames), and three
# half-floats ('3e') do not exist in MicroPython's struct at all. int16 gives
# ~3e-5 resolution on a stick that a physical ADC + deadzone can't come close
# to, so the fixed point costs nothing real.
_JOY_FMT = '3h'
_JOY_SCALE = 32767


def _q15(f):
    """float -1..+1 -> int16, clamped."""
    n = int(round(float(f) * _JOY_SCALE))
    if n > _JOY_SCALE:
        return _JOY_SCALE
    if n < -_JOY_SCALE:
        return -_JOY_SCALE
    return n


def _joy_pack(v):
    """(x, y[, button]) -> 6 bytes. A bare (x, y) pair packs with button=0 so a
    2-tuple producer still serializes."""
    b = v[2] if len(v) > 2 else 0
    return struct.pack(_JOY_FMT, _q15(v[0]), _q15(v[1]), 1 if b else 0)


def _joy_unpack(load):
    """6 bytes -> (x: float, y: float, button: bool) — the shape a
    JoystickSplitter decouples."""
    x, y, b = struct.unpack(_JOY_FMT, load)
    return (x / _JOY_SCALE, y / _JOY_SCALE, bool(b))


# Codec tables. Hoisted to module level (they used to be rebuilt inside
# bundle()/unbundle() on every single frame) — a Parameter broadcasting at
# 50 Hz was allocating a 15-entry dict per emit.
_PACK = {
    'b': lambda l: struct.pack('b', l),   # signed char
    'B': lambda l: struct.pack('B', l),   # unsigned char
    '?': lambda l: struct.pack('b', l),   # bool
    'h': lambda l: struct.pack('h', l),   # short int16
    'H': lambda l: struct.pack('H', l),   # unsigned short uint16
    'i': lambda l: struct.pack('i', l),   # int
    'I': lambda l: struct.pack('I', l),   # unsigned int
    'q': lambda l: struct.pack('q', l),   # long
    'Q': lambda l: struct.pack('Q', l),   # unsigned long
    'f': lambda l: struct.pack('f', l),   # float
    'd': lambda l: struct.pack('d', l),   # double
    'e': lambda l: l,                     # pass the buffer through
    'a': lambda l: l.encode(),            # encode string
    'u': lambda l: l.encode(),            # encode string
    'j': lambda l: json.dumps(l).encode(),  # JSON as utf8
    'J': _joy_pack,                       # (x, y, button)
}

_UNPACK = {
    'b': lambda l: struct.unpack('b', l)[0],
    'B': lambda l: struct.unpack('B', l)[0],
    '?': lambda l: bool(struct.unpack('b', l)[0]),
    'h': lambda l: struct.unpack('h', l)[0],
    'H': lambda l: struct.unpack('H', l)[0],
    'i': lambda l: struct.unpack('i', l)[0],
    'I': lambda l: struct.unpack('I', l)[0],
    'q': lambda l: struct.unpack('q', l)[0],
    'Q': lambda l: struct.unpack('Q', l)[0],
    'f': lambda l: struct.unpack('f', l)[0],
    'd': lambda l: struct.unpack('d', l)[0],
    'e': lambda l: l,                       # return the buffer
    'a': lambda l: l.decode('ascii'),
    'u': lambda l: l.decode('utf8'),
    'j': lambda l: json.loads(l.decode('utf8')),
    'J': _joy_unpack,
}


def wire_code(type):
    """Resolve a bundle spec to its wire code.

    `type` may already BE a code ('f', 'J', '3B') or it may be a datatype NAME
    ('float', 'joystick', 'rgb'). Both reach the codecs: a Parameter's `serdes`
    is written either way, and a subscription's bundle is always a name (see
    Subscription.export in the transposer, which stores the port's manifest
    datatype). Before this existed, `unbundle(type='joystick')` fell straight
    through to struct and raised "bad typecode" on the consumer board.
    """
    return datatypes.get(type, type)


def is_wire_code(code):
    """True if `code` is something bundle()/unbundle() can actually serialize:
    one of floe's pseudo-codes ('e', 'u', 'j', 'J') or a format struct
    understands ('f', '?', '3B').

    A datatype NAME that never resolved through wire_code() is neither. That
    happens when the map above predates the type — most often a stale exported
    floe/message.py — and the name then rides an add_sub frame verbatim, where
    it fails as an oversize payload far from the real cause. Callers that put a
    code on the wire check here first (see Zorg.create_sub).
    """
    if not code or not isinstance(code, str):
        return False
    if code in _PACK:
        return True
    try:
        struct.calcsize(code)
        return True
    except Exception:
        return False


class Message:
    def __init__(self, iris):
        self.s = iris.s  # subscription list
        self.p = iris.p  # p
        self.send = iris.send
        self.iris = iris
        self.uuid = None
        # single items, len(bytearray) >= 8

        self.encodings = ('utf8', 'ascii')

    # ------------------------------------------------------------------------
    """
    Addresses:
        0: FAULT        : We are dangerous
        1,254: NWK      : Administrative functions zorg -> devices
        2,255: ZORG     : Zorg loves to listen     zorg <- devices
        3: Open channels: Devices are assigned these

    Types (CAN type bits):
        0: BROADCAST    : pubsub default
        1: WRITE        : directed write to target Parameter

    (Bit layout changes in Phase 3 of big-payload work; see
    planning/big_payload/plan.md for the final layout.)

        can     [priority][adr][pid][type]
        mqtt    [address]/[parameter id]/[type]
        rabbit  [address].[parameter id].[type]
        kafka   [address].[parameter id].[type]
    """

    # Channel addresses, not devices - never a proof-of-life source.
    RESERVED_ADRS = (0, 1, 2, 254, 255)

    def want(self, msg_adr, pid, is_write, h, self_adr):
        # Proof of life. On a BROADCAST the header's address field is the
        # *producer*, so any frame a device publishes - one we subscribe to
        # or not - proves that device is alive. A directed WRITE carries
        # only the destination, so it says nothing about who sent it, and
        # Zorg-channel traffic is credited by its own handlers, which
        # recover the sender from the pid (see do_zorg / Zorg.terminal).
        #
        # This is the bus's hottest path, so it does the cheapest possible
        # thing: one set add, no clock read, no lookup. Zorg drains the set
        # in its chk() loop and stamps last_seen once per adr per pass.
        # iris.zorg is False on every leaf, so leaves pay one attribute
        # read. See planning/zorg_c2 - liveness is ambient; pings are
        # operator-driven.
        if (self.iris.zorg and not is_write
                and msg_adr != self_adr
                and msg_adr not in self.RESERVED_ADRS):
            self.iris.zorg.seen_adrs.add(msg_adr)

        if msg_adr == 0:                                # FAULT
            return self.do_flt, pid

        elif msg_adr == 1 or msg_adr == 254:            # NWK
            return self.do_nwk, pid

        elif msg_adr == 2 or msg_adr == 255:            # ZORG
            return self.do_zorg, pid

        elif msg_adr == self_adr and is_write:          # directed WRITE
            return self.do_write, pid

        elif h in self.s and not is_write:              # BROADCAST subscription
            return self.do_sub, self.s[h]

        return False, None


    def do_flt(self, load: bytearray, pid: int) -> None:  # adr 0
        """Process FAULT frame from a remote device.

        Hands off to argus.fault_received() so the local cascade goes
        through the same path as a locally-originated critical event.
        Argus deliberately does not re-broadcast on this path.
        """
        try:
            reason = bytes(load).decode('utf8') if load else ''
        except (UnicodeDecodeError, AttributeError):
            reason = repr(load)
        self.iris.argus.fault_received(pid, reason)

    def do_nwk(self, load: bytearray, pid: int) -> None:  # adr 1
        """ process nwk message"""
        print('doing nwk stuff', pid, load)
        self.iris.n[pid](load, self.iris)
        

    def do_zorg(self, load: bytearray, pid: int):  # adr 2
        if self.iris.zorg:  #IAMZORG
            if pid < 1000: # this is a ping
                self.iris.zorg.ping_from(load, pid)
            elif pid < 2000:
                self.iris.zorg.terminal(load, pid - 1000)
            else:
                self.iris.n[pid](load, self.iris)

    def do_write(self, load: bytearray, pid) -> None:
        print(load, pid)
        if pid < 1000:  
            self.iris.n[pid](load, self.iris)
        else:
            try:
                self.p[pid](self.unbundle(load=load, type=self.p[pid].serdes))
            except Exception as e:
                print(e)  # TODO create error oover bus stuff


    def do_sub(self, load: bytearray, sub: list[str|int, str]):
        # print('sub', h)
        data = self.unbundle(type=sub[1], load=load)
        # print('parsing', sub, data)

        # subscription contains extra arguments
        # sub: (pid, serdes)
        # sub: ((pid, pid), serdes)

        if not isinstance(sub[0], (tuple, list)):
            # single subscription
            if len(sub) > 2:
                self.p[sub[0]](data, *sub)
                return

            else:  # regular subscription
                self.p[sub[0]](data)
                return

        for s in sub[0]:  # multiple sub
            if s not in self.p:
                continue
            if len(sub) > 2:
                self.p[s](data, s, *sub[1:])
            # regular subscription
            else:
                self.p[s](data)

    # ------------------------------------------------------------------------

    @staticmethod
    def unbundle(load: bytearray, type: str) -> any:
        """Wire bytes -> a Python value, per the bundle spec `type` (a datatype
        name or a wire code — see wire_code)."""
        fmt = wire_code(type)
        fn = _UNPACK.get(fmt)
        if fn is not None:
            return fn(load)
        if not fmt:
            raise ValueError('unbundle: no bundle type for this subscription')
        # Not a known codec — a raw struct collection format like '3B'.
        return struct.unpack(fmt, load)

    # ------------------------------------------------------------------------

    @staticmethod
    def bundle(load: any, type: str) -> bytes:
        """A Python value -> wire bytes, per the bundle spec `type` (a datatype
        name or a wire code — see wire_code)."""
        fmt = wire_code(type)
        fn = _PACK.get(fmt)
        if fn is not None:
            return fn(load)
        if not fmt:
            raise ValueError('bundle: no serdes on this Parameter')
        # Not a known codec — a raw struct collection format like '3B'.
        return struct.pack(fmt, *load)
