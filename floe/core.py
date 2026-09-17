import json
try:
    import uasyncio as asyncio
except:
    import asyncio
import struct, gc, sys

PID = int

class Stater:
    def __init__(self, state: any):
        """ stored constant for use when a remote parameter is not wanted """    
        self.state = state
        self.hot = None
        
    def __call__(self, *args, **kwargs) -> None:
        if args:
            self.state = args[0]
        if self.hot:
            if type(self.hot) is tuple:
                for h in self.hot:
                    h(self.state)
            else:
                self.hot(self.state)

    def add_hot(self, hot: callable):
        if self.hot:
            if self.hot is tuple:
                _hot = list(self.hot)
                _hot.append(hot)
                self.hot = tuple(_hot)
            else:
                self.hot = (self.hot, hot)
        else:
            self.hot = hot

    def remove_hot(self, hot: callable):
        if self.hot is hot:
            self.hot = None
        elif isinstance(self.hot, tuple) and hot in self.hot:
            hots = [h for h in self.hot if h is not hot]
            self.hot = tuple(hots) if hots else None
    
class FP:
    '''Future Param, is a holder until all params created then updated with references with update method

    With an optional attr, FP(pid, 'x') resolves to a named attribute of the target
    Parameter (a local Channel) instead of the Parameter itself — see Channel below.
    '''
    def __init__(self, pid, attr=None) -> None:
        self.pid = pid
        self.attr = attr
        
def make_var(item: any) -> Stater | FP:
    """
    Parameter will expect to get value by requesting item.state
    """
    if isinstance(item, FP):
        return item
    return Stater(item)


class Channel(Stater):
    """A named, local-only output stream owned by a Parameter — the output dual of
    make_var/Stater.

    No pid, no serdes, no bus identity -> it is never serialized; only the event
    channel (send()) crosses the wire. A Channel holds the wire's downstream
    subscribers; the producer pushes a value with ``self.x(value)``, which fans out
    to every subscriber exactly like Parameter.emit()'s hot loop. ``state`` is the
    last value pushed, so a consumer holding a reference can pull it (``channel.state``)
    just as it reads an input Parameter's state.

    Subscribers arrive as constructor FPs (one, a list, or None) and are swapped for
    the live Parameters they point at in Parameter.update() via resolve().
    """

    def __init__(self, subs=None):
        super().__init__(None)            # state=None, hot=None
        if subs is None:
            self._subs = []
        elif isinstance(subs, (list, tuple)):
            self._subs = list(subs)
        else:
            self._subs = [subs]

    def resolve(self, p):
        """Swap pending subscriber FPs for the live Parameters they point at."""
        for fp in self._subs:
            self.add_hot(p[fp.pid] if isinstance(fp, FP) else fp)
        self._subs = []

    def add_hot(self, hot):
        # Deliberately NOT inherited from Stater: Stater.add_hot tests
        # ``self.hot is tuple`` (always False), which nests the fan-out list for the
        # 3rd+ subscriber. Channels routinely have several, so use the correct
        # isinstance test (mirrors Parameter.add_hot). Stater.__call__'s own fan-out
        # (``type(self.hot) is tuple``) is already correct, so we reuse it.
        if self.hot:
            if isinstance(self.hot, tuple):
                self.hot = self.hot + (hot,)
            else:
                self.hot = (self.hot, hot)
        else:
            self.hot = hot


def make_channel(item) -> Channel:
    """Output dual of make_var: wrap a Parameter's channel kwarg (subscriber FPs)
    into a Channel the producer pushes to. Idempotent for an existing Channel."""
    if isinstance(item, Channel):
        return item
    return Channel(item)

class Bifrost:
    """ bifrost is the bridge for the gods. busses and other things are shuffled behind the scenes to the websocket"""
    def __init__(self) -> None:
        self.bifrost = []
        self._checked = [] # to be injected once known to be true
        self.funcs = {}
    
    def active(self):
        if self._checked != []:
            return True
        return False
    
    def send(self, pid: int, msg: str | dict):
        if self.active():
            if isinstance(msg, dict):
                msg = json.dumps(msg)
            self.bifrost.append(f'{pid},{msg}')
        else:
            print(f"{msg}")

    def post(self, msg: str):
        self.send('term', msg)

    def write(self, msg:str):
        # method for when std_out is redirected
        if msg == "\n" or msg == "":
            return
        self.send('term', f"print: {msg}")
    
    def any(self) -> bool:
        if self.bifrost != []:
            return True
        return False
    
    def pop(self) -> str: 
        return self.bifrost.pop(0)
        
    # methods below are for cpython, in upython bifrost is handled in server.process_all
    def add_socket(self, manager):
        self.manager = manager
        self._checked = manager.active_connections

    async def chk(self):
        while True:
            if self.any():
                await self.manager.broadcast(self.pop())
            await asyncio.sleep(.01)
            # await asyncio.sleep(.02)
    
    # method for pyscript
    async def pyscript_chk(self, callback, iris, core_type: str):
        import sys
        if core_type == 'py': # pyscript python core type
            # micropython has stdout hardcoded and cannot be changed yet?
            sys.stdout = iris.bifrost
        self._checked = True
        while True:
            if self.any():
                callback(self.pop())
            await asyncio.sleep(.005)

class Argus:
    """Unified diagnostic broadcast (severity-driven).

    Single API: the call site picks a level, Argus picks the sinks. For
    now all non-debug levels route through bifrost.post (which already
    falls back to print when no bifrost connection exists), so the
    levels differ only by prefix. The methods diverge in behavior over
    later phases:

      - error    → forward to Zorg
      - critical → safe_state() fan-out, FAULT frame on bus (adr=0),
                   toast in the runtime GUI

    debug uses raw print so verbose output stays on serial and out of
    the runtime terminal by default.
    """

    def __init__(self, iris):
        self.iris = iris
        self._in_critical = False

    def _toast(self, level, msg):
        # Hermes' 'toast' pid is special-cased into IdeToast.show().
        # IdeToast supports info / success / warning / error styles.
        self.iris.bifrost.send('toast', {'level': level, 'msg': msg})

    def _broadcast_fault(self, msg):
        # Outbound FAULT (adr=0) so neighbors on the bus know we're
        # in trouble. pid=0 means "the device itself" rather than
        # any specific Parameter — refine with a real source pid
        # later if a use case shows up.
        self.iris.send(
            pid=0,
            load=self.iris.msg.bundle(msg, 'u'),
            adr=0,
        )

    def _local_fan_out(self):
        # Reentrancy guard handles two cases:
        #   - safe_state() raises → calls argus.critical → would loop
        #   - a duplicate FAULT arrives mid-cascade → would re-fan-out
        if self._in_critical:
            return
        self._in_critical = True
        try:
            # Snapshot so a safe_state() that mutates iris.p (unlikely
            # but cheap to defend against) doesn't trip iteration.
            for p in list(self.iris.p.values()):
                # Some things in iris.p (e.g. animation handlers like
                # RgbArrayColorSwirl) don't subclass Parameter and
                # have no safe_state. Skip them silently rather than
                # logging an AttributeError per fault.
                fn = getattr(p, 'safe_state', None)
                if fn is None:
                    continue
                try:
                    fn()
                except Exception as e:
                    # Bypass self.error here — it's another level
                    # method whose behavior may grow (Zorg, etc.) and
                    # we don't want richer paths firing from inside
                    # an already-failing cascade.
                    name = getattr(p, 'name', None) or getattr(p, 'pid', '?')
                    self.iris.bifrost.post(f"safe_state failed on {name}: {e}")
        finally:
            self._in_critical = False

    def debug(self, msg):
        print(f"DEBUG: {msg}")

    def info(self, msg):
        self.iris.bifrost.post(f"INFO: {msg}")
        self._toast('info', msg)

    def post(self, msg):
        self.iris.bifrost.post(msg)

    def error(self, msg):
        self.iris.bifrost.post(f"ERROR: {msg}")
        self._toast('warning', msg)

    def critical(self, msg):
        """Local critical event: log, toast, broadcast FAULT, fan out."""
        self.iris.bifrost.post(f"CRITICAL: {msg}")
        self._toast('error', msg)
        # Skip the broadcast and second fan-out if we're already
        # inside a cascade (a safe_state() called critical()). The
        # secondary terminal+toast still surface so the operator sees
        # the chained failure.
        if self._in_critical:
            return
        self._broadcast_fault(msg)
        self._local_fan_out()

    def fault_received(self, source_pid, reason):
        """Remote FAULT arrived: log, toast, fan out — but DO NOT
        re-broadcast or two devices would ping-pong forever.
        """
        msg = f"FAULT from remote pid={source_pid}: {reason}"
        self.iris.bifrost.post(f"CRITICAL: {msg}")
        self._toast('error', msg)
        self._local_fan_out()


# floe.implementation — the runtime environment descriptor (see
# planning/floe_implementation). One place to ask *what* (interpreter) and
# *where* (location) a canvas runs, plus capability flags derived from `where`.
# `where` is authoritative when baked at export by create_zip (floe/_env.py →
# WHERE); absent that file (PyScript JIT, host/pytest) it is auto-detected from
# sys.implementation + sys.platform. Prefer asking a capability
# (`if implementation.sockets:`) over a location.

# where -> capabilities. docker/host (and the cpython export, which bakes
# 'host') share one row: no GPIO, real sockets, persistent fs. pyscript =
# browser: no sockets, has fetch, ephemeral fs. esp32 = the only target with
# real hardware and tight memory.
_CAPS = {
    'esp32':    {'hardware': True,  'sockets': True,  'fetch': False,
                 'browser': False, 'persistent_fs': True,  'constrained': True},
    'pyscript': {'hardware': False, 'sockets': False, 'fetch': True,
                 'browser': True,  'persistent_fs': False, 'constrained': False},
    'docker':   {'hardware': False, 'sockets': True,  'fetch': False,
                 'browser': False, 'persistent_fs': True,  'constrained': False},
    'host':     {'hardware': False, 'sockets': True,  'fetch': False,
                 'browser': False, 'persistent_fs': True,  'constrained': False},
}

_CAP_KEYS = ('hardware', 'sockets', 'fetch', 'browser', 'persistent_fs', 'constrained')


class Implementation:
    """Runtime descriptor: `.name` (interpreter), `.where` (location), and the
    capability flags derived from `where`. `.wasm` is kept as a back-compat
    alias for `not .hardware` (its historical 'no GPIO' meaning — True in the
    browser AND on docker/host) so existing hardware guards don't move; the
    accurate browser test is `.browser`."""

    def __init__(self):
        imp = sys.implementation
        self.name = imp.name
        # Kept for back-compat (was set only on micropython before).
        self._machine = getattr(imp, '_machine', None)
        baked = self._baked_where()
        self.baked = baked is not None
        self.where = baked if self.baked else self._detect_where()
        caps = _CAPS.get(self.where, _CAPS['host'])
        for k in _CAP_KEYS:
            setattr(self, k, caps[k])
        # `.wasm` == "no GPIO hardware" (browser + docker/host). Preserves the
        # exact behavior the ~dozen `if not implementation.wasm:` guards rely on.
        self.wasm = not self.hardware

    def _baked_where(self):
        """`where` baked into the bundle at export (floe/_env.py); present only
        in exported builds. Returns None for JIT/dev → auto-detect."""
        try:
            from . import _env
            return getattr(_env, 'WHERE', None)
        except Exception:
            return None

    def _detect_where(self):
        """Auto-detect from sys.implementation + sys.platform. esp32/pyscript
        are unambiguous; docker only ever arrives via the baked _env, so an
        undetected cpython falls back to the most-capable non-wasm default,
        'host'."""
        plat = sys.platform
        if self.name == 'micropython':
            machine = (self._machine or '').lower()
            if 'emscripten' in machine or plat == 'webassembly':
                return 'pyscript'
            if plat == 'esp32':
                return 'esp32'
            return 'host'
        multiarch = getattr(sys.implementation, '_multiarch', '') or ''
        if plat == 'emscripten' or 'wasm' in multiarch or 'emscripten' in multiarch:
            return 'pyscript'
        return 'host'

    def as_dict(self):
        """Flat snapshot for logging / iris.whoami()."""
        d = {'name': self.name, 'where': self.where,
             'baked': self.baked, 'wasm': self.wasm}
        for k in _CAP_KEYS:
            d[k] = getattr(self, k)
        return d


implementation = Implementation()


### WIP
import io
class OrderReceiver:
    serdes = 'e'
    
    def __init__(self, pid, iris):
        self.pid = pid
        self.state = None # ACK
        self.num_bytes = 0
        self.channel = 0  # this is the channel we are receiving on
        self.return_adr = ('adr', 'pid')
        self.len_order = 0 # this will be the length of the order
        self.recving = False
        self.msg_type = 0 # 0 bytes|1 str|2 order
        
        
    def __call__(self, state, *args, **kwargs):
        """
        First message packing
        B return adr
        H return pid
        H len order
        B msg_type:
            0: bytes
            1: uft8 str
            2: order utf8 json
        """
        if not self.recving:
            # begin transmission
            return_adr, return_pid, len_order, msg_type = struct.unpack('BHHB', state)
            self.return_adr = (return_adr, return_pid)
            self.len_order = len_order
            self.recving = True
            gc.collect()
            self.state = io.BytesIO(b"")  # bytearray(len_order)
            self.ack()
        
        elif self.recving:
            self.num_bytes += len(state)
            
            if self.cur_byte == len(self.state):
                # we are done
                self.process()
            else:
                self.ack()    
    
    def process(self):
        val = self.state.getvalue()
        if self.msg_type == 0:
            # bytes
            print(val)
        elif self.msg_type == 1:
            # utf8 string
            print(val.decode())
        elif self.msg_type == 2:
            # utf8 string
            print(json.loads(val.getvalue().decode()))
        
        self.reset()

    def ack(self):
        self.iris.bus.send(adr=self.return_adr[0], 
                            pid=self.return_adr[1],
                            load=b'\x06' #ack
                            )
    
    def reset(self):
        self.state = None
        self.num_bytes = 0
        self.channel = 0  # this is the channel we are receiving on
        self.return_adr = (None, None)
        self.len_order = 0
        self.recving = False
        gc.collect()