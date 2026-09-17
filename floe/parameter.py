from .core import FP, Stater, Channel
from .iris import Iris
from .message import datatypes

import os, json

PID = int

# blob constants
ACTIVE = 1
SND2OB = 2
SND2IIB = 4
DBG_SRL = 8
HOT = 16
PARTIAL = 32
ALIAS = 64
LOGGING = 128
RENDER_GUI = 256


class HotEdge:
    """Wraps a hot subscription so the callee receives the upstream caller.

    Stored in a Parameter's `self.hot` slot in place of the bare callee ref
    when the callee opts into caller-awareness via `wants_caller = True`.
    Fan-out at emit() calls `edge(state)`, which forwards to
    `callee(state, caller=upstream)` transparently.
    """
    __slots__ = ('callee', 'caller')

    def __init__(self, callee, caller):
        self.callee = callee
        self.caller = caller

    def __call__(self, state):
        return self.callee(state, caller=self.caller)


def _hot_callee(h):
    return h.callee if isinstance(h, HotEdge) else h


class Parameter:  # Abstract class
    wants_caller = False  # opt-in marker; set True on class or instance to receive caller=
    # The name -> wire-code map lives in message.py (one copy; this used to be a
    # near-duplicate that disagreed with namespace.py's). Still exposed here as
    # Parameter.datatypes because Parameters and Zorg read it off the class.
    datatypes = datatypes


    # __slots__ = ('pid', 'state', 'serdes', 'p', 'iris', 'blob', 'hot')
    def __init__(self, *, pid: int=0, iris: Iris, state: any = None, name=None, active=False, debug=False, bcast=False, render_gui=False, **k):
        self.pid = int(pid)
        self.state = state

        self.p = iris.p
        self.iris = iris

        # blob package: [hot, debug, broadcast(self), broadcast(bus), active] >>>LSB
        self.blob = 0
        if active:
            self.blob |= ACTIVE
        if debug:
            self.blob |= DBG_SRL
        if bcast:
            self.blob |= SND2OB
        # render_gui DEFAULTS FALSE: the transposer always passes render_gui
        # explicitly (derived from GUI layout membership — see
        # transposer/param_types.py _derive_render_gui, planning/gui_visibility_harmonization).
        # So the default only applies to Parameters built OUTSIDE the transposer
        # — internal helper instances (e.g. KiCad.__init__ creating its own
        # GRBL). Those must NOT compose themselves into the runtime GUI; a True
        # default made every such helper render a phantom widget via get_gui().
        if render_gui:
            self.blob |= RENDER_GUI

        self.hot = None
        # self.partial = None
        # self.alias = None

        iris.p[self.pid] = self


        self.name = name
        if name is not None and name != 'no_name':
            iris.locals[name] = self

        print(f"{self.name}:\n    {self.__class__.__name__}, pid:{self.pid}")

    # ------------------------------------------------------------------------

    def __call__(self, state) -> None:
        self.state = state
        # print(f'current state is {self.state}')
        self.emit()

    def receive_from_gui(self, data):
        """Called when a message arrives from the browser GUI via WebSocket.

        Override to handle GUI input separately from programmatic input (__call__).
        Default: delegates to __call__, but logs a warning to encourage explicit overrides.
        """
        print(f"[DEPRECATION] {self.name}: receive_from_gui not overridden, routing to __call__. Consider adding an explicit override.")
        self(data)

    def update(self):
        for attr, val in self.__dict__.items():
            if isinstance(val, FP):
                target = self.iris.p[val.pid]
                # FP(pid) -> the Parameter; FP(pid, 'x') -> its named local Channel.
                setattr(self, attr, getattr(target, val.attr) if val.attr else target)
            elif isinstance(val, Channel):
                val.resolve(self.iris.p)

    def _load_save_data(self):
        # helper function for loading save data
        if 'savedata' not in os.listdir():
            return
        if f'{self.pid}.json' in os.listdir('savedata'):
            with open(f'savedata/{self.pid}.json', 'r') as savedata:
                return json.load(savedata)


    def save(self):
        # still not sure how I want to implement saves.
        # should there be a global method or should they be param specific
        pass

    def _save(self, data: dict):
        import os, json
        if 'savedata' not in os.listdir():
            os.mkdir('savedata')
        with open(f'savedata/{self.pid}.json', 'w') as f:
            json.dump(data, f)

        # this could be another way
        # ignore = {'iris', 'pid', 'p', 'funcs'}
        # assets = {}
        # for name, attr in self.__dict__.items():
        #     if name not in ignore:
        #         if isinstance(attr, Parameter):
        #             continue
        #         if isinstance(attr, Stater):
        #             assets[name] = attr.state
        #         else:
        #             assets[name] = attr
        # return assets

    def add_hot(self, hot: any):  # int | str | callable
        """add internal subscription, usually called by the subscriber"""
        self.blob |= HOT
        if isinstance(hot, str):
            hot = self.p[int(hot)]
        elif isinstance(hot, int):
            hot = self.p[hot]

        edge = HotEdge(hot, self) if getattr(hot, 'wants_caller', False) else hot

        if self.hot:
            if isinstance(self.hot, tuple):
                _hot = list(self.hot)
                _hot.append(edge)
                self.hot = tuple(_hot)
            else:
                self.hot = (self.hot, edge)
        else:
            self.hot = edge

    def remove_hot(self, param):
        print('param', param, self.hot)
        if _hot_callee(self.hot) is param:  # param is only hot route
            self.hot = None
            self.blob ^= HOT
            return
        if isinstance(self.hot, tuple):
            kept = tuple(h for h in self.hot if _hot_callee(h) is not param)
            if len(kept) != len(self.hot):
                self.hot = kept if kept else None
                if not kept:
                    self.blob ^= HOT

    @property
    def active(self):
        return self.blob & ACTIVE

    @property
    def renders_gui(self):
        """True iff this Parameter is composed into the GUI (RENDER_GUI bit set).

        The browser only registers a hermes handler for pids it composed via
        iris.get_gui()/_get_gui(). A Parameter that streams live updates
        (_push_gui) for an uncomposed pid spams 'No handler registered for
        pid ...'. Gate such pushes on this so the push set never outruns the
        compose set.
        """
        return bool(self.blob & RENDER_GUI)


    def _get_gui(self):
        return None

    def safe_state(self):
        """Override on Parameters that own physical hardware.

        Called by argus.critical() to bring the hardware to a known
        safe state on critical fault. Default no-op — Parameters with
        nothing physical to make safe (Gui*, CodeBlock, etc.) inherit
        the default and do nothing.
        """
        pass

    # ------------------------------------------------------------------------

    def emit(self, is_write=False, pid=None, adr=None) -> None:
        if self.blob & ACTIVE:  # ACTIVE
            if self.blob & SND2OB:  # SEND TO OUTBOX
                if pid is None:
                    pid = self.pid
                self.iris.send(pid=pid,
                               load=self.iris.msg.bundle(self.state, self.serdes),
                               is_write=is_write,
                               adr=adr)
            # if self.blob & SND2IIB:  # YIELD
            #     self.iris.send_i((self.pid, self.state))
            if self.blob & DBG_SRL:  # DEBUG SERIAL
                self.iris.argus.post(f'DEBUG: pid: {self.pid}, state: {self.state}')

            if self.blob & HOT:  # CALL param
                if isinstance(self.hot, tuple):
                    for h in self.hot:
                        h(self.state)  # h = Parameter
                else:
                    self.hot(self.state)

            # if self.blob & LOGGING:
            #     print(f'MAKE LOGGER: pid: {self.pid}, state: {self.state}')
