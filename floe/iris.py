
# import struct
try:
    import uasyncio as asyncio
# except (ModuleNotFoundError, ImportError): # TODO: make pyscript mpy have correct exceptions
except:
    import asyncio
from . import message
from . import nwk
import os, collections, json, time
from .core import Bifrost, Argus, implementation
from .canheader import CanHeader
from .manifest import Manifest
# from floe.network import base_parameters


class NullBus():
    """Default bus when no transport is bound (e.g. pyscript canvas with
    no bus Parameter). Subscribe/unsubscribe are no-ops; send prints.
    Carries a CanHeader so callers of `iris.bus.header.pack(...)` and
    `iris.bus.header.unpack(...)` don't crash before a real bus binds."""
    def __init__(self, iris):
        self.msg = iris.msg
        self.header = CanHeader(adr=3, s=iris.s)
    def subscribe(self, *a):
        pass
    def unsubscribe(self, *a):
        # Mirror subscribe(*a): callers pass a header (clear_subs,
        # iris.unsubscribe). Without *a, every unsubscribe on a busless
        # device (NullBus default) would TypeError.
        pass
    def send(self, *a, **k):
        print(a, k)
    def rts(self):
        return True
    def connect(self):
        pass

Canvas = collections.namedtuple('Canvas', ['canvas_id','canvas_name','flotilla_id','flotilla_name','adr','version'])


def classify_update(files_changed, adr_changed):
    """ACT-LEAF (planning/flotilla_manifest) reboot-matrix boundary — the
    single place that says which bucket an update falls in:

      'adr'    -> the bus address changed. Special flow: Zorg pushes the new
                  config at the OLD adr, the leaf reboots onto the NEW adr,
                  Zorg re-discovers it.
      'config' -> files changed (config.py / code / a port-or-serdes
                  signature change — changing a signature re-transposes
                  config.py, so an ns change IMPLIES a files change). Reboot
                  to re-run config.setup.
      'subs'   -> only cross-board wiring (uppie_subs) changed, no files.
                  Hot re-subscribe live, NO reboot.

    Precedence adr > config > subs: a subs-only change is exactly 'no files
    touched and no adr change'."""
    if adr_changed:
        return 'adr'
    if files_changed:
        return 'config'
    return 'subs'


class Iris:
    def __init__(self):
        print('Iris initializing')
        self.id = ""
        # self.globals = {'iris': self} # the globals to be used by eval functions
        self.p = {}                 # Parameter Table with no adr
        self.s = {}                 # Subscription List {header: (pid, bundle)}

        self.n = nwk.nwk            # these are nwk and zorg functions, putting them here to keep p smaller
        
        # these are temporary until I figure out something better
        self.zorg = False
        self.locals = {'iris': self}  # namespace for repl, gene and eval functions
        
        self.ob = []                # Outbox
        self.obg = []               # Outbox generators
        
        self.bifrost: Bifrost = Bifrost()
        
        self.core = None            # core element
        self.msg = message.Message(iris=self)
        
        self.bus = NullBus(self)             # default until a real bus binds
        self.buss = {}            # moving to multiple busses soon
        
        self.argus = Argus(self)
        self.manifest = Manifest(self)   # device-side file manifest responder

        self.layout = []            # GUI layout: list of (pid, col, row, w, h) tuples
        self.decorations = []       # GUI decorations: list of dicts (text notes, etc.)
        self.info = None
        self.floe = None            # raw __floe__ identity dict (set by set_info)
        self._reboot = None         # platform reboot hook (set by set_reboot)
        try:
            self.ib = collections.deque((), 40, True)  # micopython with max len and overflow protection
        except TypeError:
            self.ib = collections.deque([])
        
        self.startup_events = 0  # are we awaiting startup events?
        
    def get_gui(self):
        # D1 (planning/gui_visibility_harmonization): GUI visibility has a single
        # source of truth — the RENDER_GUI bit, which the transposer derives from
        # layout membership (or, for layout-less legacy canvases, the type's
        # gui[2] default). One path, no layout-vs-flag rivalry. Widget POSITIONS
        # still travel separately via `layout` in the _canvas_info below.
        from .parameter import RENDER_GUI

        def iter():
            for param in self.p.values():
                # Anything in iris.p that isn't a proper Parameter has no blob;
                # it can't carry the RENDER_GUI bit, so it never renders — skip it.
                if not (getattr(param, 'blob', 0) & RENDER_GUI):
                    continue
                ws = param._get_gui()
                if ws:
                    yield json.dumps(ws)
        parts = list(iter())
        info = {"name": self.info.canvas_name, "canvas_id": self.info.canvas_id, "id": self.id, "type": "_canvas_info", "layout": self.layout, "decorations": self.decorations}
        parts.append(json.dumps(info))
        return "[" + ",".join(parts) + "]"
    
    def on_startup(self, add_remove):
        if add_remove == 'add':
            self.startup_events += 1
        elif add_remove == 'remove':
            self.startup_events -= 1
            print(f'iris.py on_startup {self.startup_events}')
        else:
            pass
        
    async def wait_for_startup(self):
        while self.startup_events:
            await asyncio.sleep(.2)
            print('waiting on startup')
        return True
       
    def report(self):
        report = []
        report.append('**** Parameter Table ****')
        for pid, v in sorted(self.p.items()):
            report.append(f'{v.name if hasattr(v, "name") else "no_name"}: {v.__class__.__name__}, {pid: 6}')
        report.append('\n\n**** Subscriptions ****')
        for header, (sub_pid, bundle) in self.s.items():
            adr, pid, is_write, is_multi, counter = self.bus.header.unpack(header)
            report.append(f'header: {header}, adr: {adr}, pid: {pid}, w: {int(is_write)} m: {int(is_multi)} c: {counter} -> sub_pid: {sub_pid}, bundle: {bundle}')
        report.append('\n\n**** OUTBOX ****')
        report.append('\n'.join(f'{a}: {b}' for a, b in self.ob))
        report.append('\n\n ********************** \n')

        return '\n'.join(report)
    
    def list_locals(self):
        return '\n'.join(f'{k}: {v}' for k,v in self.locals.items())

    def whoami(self):
        """ACT-WHOAMI (planning/flotilla_manifest): derive-and-report. Walk
        the live parameter table and assemble this board's published ports
        {name: [serdes, pid]} on the fly — nothing stored on the leaf
        (protects device memory). The runtime *observed* twin of the
        transposer's desired roster, and the identity companion to
        manifest (which reports files).

        serdes is the struct-format char the device actually serializes
        with ('f', '?', 'e', '2e', 'u', ...) — the canonical wire form.
        Parameters with no serdes (CodeBlock / logic / event-only buttons,
        and local-only Channels which never cross the bus) are not ports,
        so they're skipped. self.floe is the __floe__ identity dict set by
        set_info (None if this board never ran a transposed config)."""
        ports = {}
        for pid, param in self.p.items():
            name = getattr(param, 'name', None)
            if not name or name == 'no_name':
                continue
            serdes = getattr(param, 'serdes', None)
            if serdes is None:
                continue
            ports[name] = [serdes, pid]
        # `implementation` lets Zorg surface each node's runtime (where +
        # capability flags) in the Fleet table — the floe.implementation
        # adoption (planning/zorg_control_plane P2). Derived, not stored.
        return {'machine_id': self.id, 'floe': self.floe, 'ports': ports,
                'implementation': implementation.as_dict()}
    
    def set_layout(self, layout):
        """Store GUI layout tuples: [(pid, col, row, w, h), ...]"""
        self.layout = layout

    def set_decorations(self, decorations):
        """Store GUI decorations: [{id, kind, layer, col, row, w, h, content, style}, ...]"""
        self.decorations = decorations

    def set_info(self, info, canvas_name=None):
        """Store board identity. Accepts either the new `__floe__` dict
        (name, canvas_id, flotilla_id, flotilla_name, adr, version) or the
        legacy positional (canvas_id, canvas_name) pair. Always builds a
        Canvas namedtuple that exposes canvas_id/canvas_name plus the
        richer flotilla identity (None when absent). The raw dict is kept
        on self.floe for structural readers (Zorg)."""
        if isinstance(info, dict):
            floe = info
        else:
            floe = {'canvas_id': info, 'name': canvas_name}
        self.floe = floe
        self.info = Canvas(
            floe.get('canvas_id'),
            floe.get('name'),
            floe.get('flotilla_id'),
            floe.get('flotilla_name'),
            floe.get('adr'),
            floe.get('version'),
        )
            
    def save(self):
        p = {}
        for pid, param in self.p.items():
            p[pid] = param.save()
        return p

    def send(self, *, pid, load, is_write=False, adr=None, is_generator=False) -> None:
        """ add to outbox """
        if adr is not None:
            h = self.bus.header.pack(is_write=is_write, pid=pid, adr=adr)
        else:
            h = self.bus.header.pack(is_write=is_write, pid=pid, adr=self.bus.header.adr)
        # print('sending message: {} to outbox'.format(m))

        # put message in sorted order min lowest
        if is_generator:
            self.obg.append((h, load))
            return
        if len(self.ob) < 20:
            # TODO there is an overflow condition when bus is not working, fix this
            self.ob.append((h, load))

    async def cob(self):
        """ check outbox """
        while True:
            if self.ob:
                if self.bus.rts():
                    # print('sending message')
                    h, load = self.ob.pop(0)
                    self.bus.send(load, h)
            elif self.obg:  #outbox generators
                if self.bus.rts():
                    try:
                        load = next(self.obg[0][1])
                        h = self.obg[0][0]
                        self.bus.send(load, h)
                    except StopIteration:
                        print('done sending')
                        self.obg.pop(0)            
            await asyncio.sleep(.02)

    async def cib(self):
        """ check inbox """
        while True:
            if self.ib:
                msg_func, sub, load = self.ib.popleft()
                # print(msg_func, sub, load)
                msg_func(load, sub)
            await asyncio.sleep(0)

    def add_bus(self, label, bus):
        self.bus = bus
        self.buss[label] = bus

    def subscribe(self, header, pid, bundle):
        self.bus.subscribe(header, pid)  # busses like MQTT require subbing from broker
        # One consumer is stored as a bare pid, several as a tuple. A fan-out
        # read back from subscriptions.json arrives as a JSON list, so flatten
        # both sides: a later add_sub on the same header must extend it, not
        # nest it as ([p1, p2], p3), which do_sub can't look up.
        new = tuple(pid) if isinstance(pid, (tuple, list)) else (pid,)
        if header in self.s:
            current, bundle = self.s[header]      # the first subscriber's bundle wins
            pids = list(current) if isinstance(current, (tuple, list)) else [current]
        else:
            pids = []
        known = len(pids)
        for p in new:
            if p not in pids:
                pids.append(p)
        if not pids or (known and len(pids) == known):
            return                                # nothing new
        self.s[header] = (pids[0] if len(pids) == 1 else tuple(pids), bundle)
    
    def unsubscribe(self, header, pid=None):
        """Remove a subscription.

        - unsubscribe(header)           — drop the whole entry (legacy).
        - unsubscribe(header, pid)      — drop one consumer pid; if it
                                          was the last, drop the entry.
        """
        if pid is None:
            self.bus.unsubscribe(header)
            self.s.pop(header, None)
            return
        entry = self.s.get(header)
        if entry is None:
            return
        current, bundle = entry
        if isinstance(current, (tuple, list)):
            remaining = [p for p in current if p != pid]
            if not remaining:
                self.bus.unsubscribe(header)
                self.s.pop(header, None)
            elif len(remaining) == 1:
                self.s[header] = (remaining[0], bundle)
            else:
                self.s[header] = (tuple(remaining), bundle)
        else:
            if current == pid:
                self.bus.unsubscribe(header)
                self.s.pop(header, None)

    def clear_subs(self):
        subs = list(self.s.keys())
        for sub in subs:
            print(f'unsubscribing: {sub}')
            self.bus.unsubscribe(sub)
            self.s.pop(sub)
    
    def add_hots(self, all_hots: dict[str, list[int]]):
        for pid, hots in all_hots.items():
            for hot in hots:
                self.p[int(hot)].add_hot(pid)

    # ---- ACT-LEAF: the per-change update path (the reboot matrix) ----------
    # "Leaves may reboot; Zorg must not." A leaf applies an update by the
    # cheapest correct path: a cross-board wiring (uppie_subs) change is hot
    # re-subscribed live; a config/code/signature/adr change reboots to
    # re-run config.setup. classify_update() (module level) is the boundary.

    def set_reboot(self, fn):
        """Register the platform's reboot mechanism. cpython touches the
        uvicorn _reload sentinel; esp32 calls machine.reset. The matrix
        calls iris.reboot() without knowing the platform."""
        self._reboot = fn

    def reboot(self):
        """Platform-abstracted reboot — re-runs config.setup from scratch.
        Returns True if a reboot was triggered, False if no platform hook is
        registered (so callers/tests can tell)."""
        if self._reboot is not None:
            self._reboot()
            return True
        print('iris.reboot(): no platform reboot hook registered')
        return False

    def resubscribe(self, subs):
        """Apply this leaf's FULL desired bus-subscription set live, leaving
        NO stale subscriptions behind — the no-reboot path for a uppie_subs
        (cross-board wiring) change. Rebuilds iris.s from scratch (clear +
        re-add) so removed wires don't linger. Each entry is
        (producer_adr, producer_pid, consumer_pid, bundle) — the same field
        order as the add_sub wire command."""
        self.clear_subs()
        for producer_adr, producer_pid, consumer_pid, bundle in subs:
            header = self.bus.header.pack(is_write=False, pid=producer_pid, adr=producer_adr)
            self.subscribe(header, consumer_pid, bundle)

    def apply_update(self, update):
        """Leaf-side dispatch of the reboot matrix. `update` carries 'kind'
        ('subs' | 'config' | 'adr', as classified by classify_update on
        Zorg) and, for 'subs', the full desired 'subs' set. subs -> hot
        re-subscribe (no reboot, returns False); config/adr -> reboot
        (returns True). For config/adr the new files are written before this
        call (FileSender); the reboot re-runs config.setup so the leaf comes
        back matching desired."""
        kind = update.get('kind')
        if kind == 'subs':
            self.resubscribe(update.get('subs', []))
            return False
        return self.reboot()

        
    def boot(self, start_mailboxes=False):
        pids = list(self.p.keys()) # p changes during iteration
                    
        for pid in pids:
            self.p[pid].update()
        if start_mailboxes:
            loop = asyncio.get_event_loop()
            loop.create_task(self.cib())
            loop.create_task(self.cob())
        
        if self.core:
            # start wifi and/or other device specific hardware
            self.core.boot()
            
        for bus in self.buss:
            bus.connect()
            
        self.bus.connect()

        if 'on_startup' in self.locals:
            # this is a CodeBlock with name on_startup
            startup = self.locals.pop('on_startup')
            startup("None")  # event cannot be None 
                 
            
if __name__ == '__main__':
    print('iris test')
    # iris = Iris(adr=36, fault_bits=8, header_bits=29, ad_bits=10, priority_bits=3)
    # print(test := iris.msg.unpack(472186874))
    # print(iris.msg.pack(**test))




