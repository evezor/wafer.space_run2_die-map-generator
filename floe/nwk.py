
'''
Calls from network channel or to self

'''
import struct, json, os, time


SUBS_REPLY_PID = 2002
HEALTH_REPLY_PID = 2003
WHOAMI_REPLY_PID = 2004
ZORG_CHANNEL = 2


# add_sub / rm_sub share this fixed head: producer_adr, producer_pid,
# consumer_pid. add_sub then carries the bundle's wire code as the REST of the
# payload — no length field, because the frame's own length already bounds it
# (a single CAN frame keeps its DLC; a fragmented one carries a declared
# length). The old format string was 'BHHs', and a bare 's' packs exactly ONE
# byte: every multi-character code — '3B' for rgb, '2f', anything — was
# silently truncated to its first character and then failed to unbundle.
SUB_FMT = 'BHH'
SUB_LEN = struct.calcsize(SUB_FMT)


def add_sub(msg, iris):
    adr, pid, self_pid = struct.unpack(SUB_FMT, msg[:SUB_LEN])
    bundle = bytes(msg[SUB_LEN:]).decode('utf8')
    header = iris.bus.header.pack(is_write=False, pid=pid, adr=adr)
    print('added sub')
    iris.subscribe(header, self_pid, bundle)


def rm_sub(msg, iris):
    """Remove one consumer's binding to a producer.

    Payload `BHH`: producer_adr, producer_pid, consumer_pid. The bus
    header's adr is the consumer (this device), so by the time we get
    here we know we own the subscription being removed.
    """
    adr, pid, self_pid = struct.unpack(SUB_FMT, msg[:SUB_LEN])
    header = iris.bus.header.pack(is_write=False, pid=pid, adr=adr)
    iris.unsubscribe(header, self_pid)


def respond_health(iris):
    """Phase 10 b'health' responder. Gathers richer-than-ping data and
    replies on pid 2003. Best-effort — fields that can't be gathered
    on the current platform are simply omitted."""
    health = {}
    try:
        import gc as _gc
        health['ram_free'] = _gc.mem_free()
        health['ram_alloc'] = _gc.mem_alloc()
    except Exception:
        pass
    try:
        health['uptime_s'] = int(time.ticks_ms() // 1000)
    except (AttributeError, NameError):
        try:
            health['uptime_s'] = int(time.time())
        except NameError:
            pass
    # Walk loaded parameters for a small inventory blob.
    try:
        params = {}
        for pid_n, p in iris.p.items():
            params[str(pid_n)] = {
                'name': getattr(p, 'name', None),
                'cls': type(p).__name__,
            }
        health['params'] = params
    except Exception:
        pass
    envelope = json.dumps({'machine_id': iris.id, 'health': health}).encode('utf8')
    iris.send(pid=HEALTH_REPLY_PID, load=envelope, is_write=True, adr=ZORG_CHANNEL)


def respond_subs(iris):
    """Pack this device's iris.s as JSON and reply to Zorg on pid 2002.

    Normalizes the (int header, (pid|tuple, bundle)) shape into a list
    of {src: {adr, pid, is_write}, dst_pids: [...], bundle: '...'}
    rows that JSON-serialize cleanly.
    """
    out = []
    for header, payload in iris.s.items():
        try:
            adr, p_pid, is_write, _is_multi, _counter = iris.bus.header.unpack(header)
        except Exception:
            continue
        sub_pids, bundle = payload[0], payload[1]
        if isinstance(sub_pids, (tuple, list)):
            dst_pids = list(sub_pids)
        else:
            dst_pids = [sub_pids]
        out.append({
            'src': {'adr': adr, 'pid': p_pid, 'is_write': bool(is_write)},
            'dst_pids': dst_pids,
            'bundle': bundle,
        })
    envelope = json.dumps({'machine_id': iris.id, 'subs': out}).encode('utf8')
    iris.send(pid=SUBS_REPLY_PID, load=envelope, is_write=True, adr=ZORG_CHANNEL)


def respond_whoami(iris):
    """ACT-WHOAMI b'whoami' responder: identity (__floe__) + derived ports,
    assembled live from iris.p (see Iris.whoami). Replies to Zorg on pid
    2004 — the desired/observed *identity* counterpart to the file
    manifest. Nothing is stored on the leaf."""
    envelope = json.dumps(iris.whoami()).encode('utf8')
    iris.send(pid=WHOAMI_REPLY_PID, load=envelope, is_write=True, adr=ZORG_CHANNEL)


def narrowband(msg, iris):
    if msg == b'ping':
        iris.bus.ping()
    elif msg == b'savesubs':
        print('saving subs')
        with open('subscriptions.json', 'w') as f:
            json.dump(iris.s, f)
    elif msg == b'clrsubs':
        print('clearing subs')
        iris.clear_subs()
        if 'subscriptions.json' in os.listdir():
            os.remove('subscriptions.json')
    elif msg == b'manifest':
        # iris.manifest is set by Iris.__init__ on every device that
        # has floe; if it's missing we're talking to something older.
        m = getattr(iris, 'manifest', None)
        if m is not None:
            m.respond()
    elif msg == b'subs':
        respond_subs(iris)
    elif msg == b'health':
        respond_health(iris)
    elif msg == b'whoami':
        respond_whoami(iris)


def handle_manifest_reply(msg, iris):
    """Receive a manifest reply (sent by Manifest.respond on the device
    side, addressed to Zorg). Decodes the JSON envelope and installs
    the manifest into FleetState. No-op on boards that aren't Zorg."""
    if iris.zorg is None:
        return  # stray reply on a non-Zorg board; ignore
    try:
        data = json.loads(bytes(msg).decode('utf8'))
    except (ValueError, UnicodeDecodeError) as e:
        print('manifest reply parse error:', e)
        return
    iris.zorg.handle_manifest_reply(data)


def handle_subs_reply(msg, iris):
    """Receive a subs reply (sent by respond_subs on the device side,
    addressed to Zorg). No-op on boards that aren't Zorg."""
    if iris.zorg is None:
        return
    try:
        data = json.loads(bytes(msg).decode('utf8'))
    except (ValueError, UnicodeDecodeError) as e:
        print('subs reply parse error:', e)
        return
    iris.zorg.handle_subs_reply(data)


def handle_health_reply(msg, iris):
    """Receive a b'health' reply. No-op on non-Zorg boards."""
    if iris.zorg is None:
        return
    try:
        data = json.loads(bytes(msg).decode('utf8'))
    except (ValueError, UnicodeDecodeError) as e:
        print('health reply parse error:', e)
        return
    iris.zorg.handle_health_reply(data)


def handle_whoami_reply(msg, iris):
    """Receive a b'whoami' reply (identity + derived ports), sent by
    respond_whoami on the device side, addressed to Zorg. No-op on boards
    that aren't Zorg."""
    if iris.zorg is None:
        return
    try:
        data = json.loads(bytes(msg).decode('utf8'))
    except (ValueError, UnicodeDecodeError) as e:
        print('whoami reply parse error:', e)
        return
    iris.zorg.handle_whoami_reply(data)


nwk = {
    # there are technically 2**16 ~65k possible values here.
    100: add_sub,
    101: rm_sub,
    111: narrowband, # these functions to not require args
    # esp specific #500 level | See: ESP32Core.py for list of commands
    # zorg 700 level  | See : Zorg.py for list of commands
    # DEBUG 1000-1255 each device has it's own
    2001: handle_manifest_reply,
    2002: handle_subs_reply,
    2003: handle_health_reply,
    2004: handle_whoami_reply,
}
