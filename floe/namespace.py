# The name -> wire-code map is owned by message.py — a namespace proxy and a
# Parameter must agree on what 'rgb' or 'joystick' means on the wire. The local
# copy this replaces had rgb as '3b' (signed) where parameter.py had '3B', so a
# blue channel over 127 packed one way and blew up the other.
from .message import datatypes


namespace = {}

class Board:
    def __init__(self, name, adr):
        self.name = name
        self.adr = adr
        namespace[name] = self
    
    # def send(self, state):
    #     self.iris.send(
    #         pid=self.blob[1],
    #         load=self.iris.msg.bundle(self.state, self.serdes),
    #         is_write=True,
    #         adr=self.blob[0]
    #         )
    
    

class Namespace:
    def __init__(self, adr, pid, serdes, iris) -> None:
        self.pid = pid
        self.serdes = datatypes[serdes] if serdes in datatypes else serdes
        self.adr = adr
        self.iris = iris

    def set(self, state):
        self._send(state, self.pid, self.serdes)

    def _send(self, state, pid, serdes):
        print(state, pid, self.adr, serdes)
        self.iris.send(
            pid=pid,
            load=self.iris.msg.bundle(state, self.serdes),
            is_write=True,
            adr=self.adr
        )
        
    def __call__(self, state):
        self._send(state, self.pid, self.serdes)

class Bool(Namespace):
    def __init__(self, adr, pid, iris) -> None:
        super().__init__(adr, pid, '?', iris)

    def on(self):
        self.set(True)

    def off(self):
        self.set(False)


# ---------------------------------------------------------------------------
# ACT-NS (planning/flotilla_manifest, P3): the namespace ships as DATA, not
# generated constructor-code. This one builder turns a roster into the live
# Board / Namespace / Bool proxies — and the SAME code path serves both boot
# (build_namespace, full roster) and live updates (patch_namespace, a delta,
# mutating in place so held references stay valid). No reboot to apply a
# namespace change. A roster is {board_name: {'adr': int, 'ports':
# {port_name: [serdes_or_datatype, pid]}}}; serdes may be a datatype name
# ('bool', 'float') or the wire char ('?', 'f') — Namespace normalizes both.
# ---------------------------------------------------------------------------

def _is_bool(serdes):
    return serdes == 'bool' or serdes == '?'


def _bind_ports(board, adr, ports, iris):
    """(Re)bind a board's port proxies from data. Replaces the proxy
    objects but never the Board, so `rb = relay_board; rb.relay1` re-reads
    the current proxy."""
    for port_name, spec in ports.items():
        serdes, pid = spec[0], spec[1]
        if _is_bool(serdes):
            setattr(board, port_name, Bool(adr, pid, iris))
        else:
            setattr(board, port_name, Namespace(adr, pid, serdes, iris))


def build_board(name, adr, ports, iris):
    """Construct one Board + its port proxies and register it (Board.__init__
    writes into the module `namespace` dict)."""
    board = Board(name, adr)
    _bind_ports(board, adr, ports or {}, iris)
    return board


def build_namespace(roster, iris):
    """Boot path: turn a full roster into live Board/proxy objects in the
    module `namespace` dict AND iris.locals, so plain-Python sends
    (relay_board.relay1.on()) resolve through iris.locals (the live
    eval/REPL/Gene namespace). Replaces the transposer's generated
    constructor-code. Empty / falsy roster is a clean no-op — a bare Zorg
    comes up with an empty namespace, it does not crash."""
    if not roster:
        return namespace
    for name, data in roster.items():
        build_board(name, data.get('adr'), data.get('ports', {}), iris)
    iris.locals.update(namespace)
    return namespace


def patch_namespace(roster, iris):
    """Live-patch path: apply a roster DELTA without replacing existing
    Board objects (held refs like `rb = relay_board` must stay valid).
    Per board: mutate adr + rebind port proxies on the existing Board, or
    construct + insert if new. Boards absent from `roster` are untouched —
    this is a delta, not a full replace. Free + local, no bus, no reboot."""
    if not roster:
        return namespace
    for name, data in roster.items():
        adr = data.get('adr')
        ports = data.get('ports', {})
        board = namespace.get(name)
        if board is None:
            build_board(name, adr, ports, iris)
        else:
            board.adr = adr                 # mutate in place — keep the object
            _bind_ports(board, adr, ports, iris)
    iris.locals.update(namespace)
    return namespace


def drop_namespace(name, iris):
    """Remove a board that left the flotilla, from both the module
    `namespace` dict and iris.locals. Safe if the board isn't present."""
    namespace.pop(name, None)
    iris.locals.pop(name, None)






        