
from contextlib import asynccontextmanager
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, HTMLResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect
from starlette.templating import Jinja2Templates
from starlette.staticfiles import StaticFiles

from broadcaster import Broadcast
broadcast = Broadcast("memory://")

from pathlib import Path
import uvicorn

import asyncio
import io, sys, json, os
import traceback
import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


from floe import Iris, implementation
iris = Iris()

import config


BASE_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "htmldirectory"))


#######################
# webrepl
#######################

def do_repl(code: str, iris):
    print('repling', code)
    code = code.replace('<br>', '\n')
    code = code.replace('&nbsp;', ' ')
    try:
        _return = str(eval(code, globals(), iris.locals)).strip('<>')
        return f">>> {code}\n{_return}"

    except SyntaxError:
        try:
            exec(compile(code, 'input', 'single'), globals(), iris.locals)

            return f">>> {code}"
        except Exception as e:
            trace = traceback.format_exc()
            return f">>> {code}\n{trace}"

    except Exception as e:
        trace = traceback.format_exc()
        return f">>> {code}\n{trace}"

def get_parameter_names():
    """Scan static/Parameters/ for available .js files that also have .html."""
    names = []
    params_dir = Path('static/Parameters')
    if params_dir.exists():
        for f in params_dir.iterdir():
            if f.suffix == '.js' and (params_dir / f'{f.stem}.html').exists():
                names.append(f.stem)
    return sorted(names)


async def get_parameter_js(request):
    """Serve a single stitched Parameter JS file (js.js + html.html + constructor registration)."""
    from starlette.responses import Response
    name = request.path_params['name']
    folder = name.rsplit('.', 1)[0]  # "GRBL.js" → "GRBL"

    js_path = Path('static/Parameters') / f'{folder}.js'
    html_path = Path('static/Parameters') / f'{folder}.html'

    if not js_path.exists() or not html_path.exists():
        return Response("// Parameter not found", status_code=404, media_type="application/javascript")

    with open(js_path, 'r') as f:
        js = f.read()

    if not js:
        return Response("", media_type="application/javascript")

    with open(html_path, 'r') as f:
        html = f.read()

    content = js.replace('{{ html }}', html)
    content += f"\nconstructors['{folder}'] = {folder};"
    return Response(content, media_type="application/javascript")


async def terminal(request):
    return TEMPLATES.TemplateResponse(
        request, "terminal.html",
        context={"parameter_names": get_parameter_names()}
    )

file = None


def unescape(msg):
    """Reverse frameOut()'s escaping (core/utils.js): '\\n' -> newline,
    '\\\\' -> backslash.

    One left-to-right pass, deliberately. Two chained replaces get this wrong:
    a literal backslash-then-n in the payload is escaped to three characters,
    and either replace order decodes those three back to the wrong thing.
    """
    if '\\' not in msg:
        return msg  # fast path — most messages carry no backslash at all
    out = []
    i = 0
    n = len(msg)
    while i < n:
        c = msg[i]
        if c == '\\' and i + 1 < n:
            nxt = msg[i + 1]
            if nxt == 'n':
                out.append('\n')
                i += 2
                continue
            if nxt == '\\':
                out.append('\\')
                i += 2
                continue
        out.append(c)
        i += 1
    return ''.join(out)


def process(msg):
    global iris
    global file

    # print(msg)
    if msg == 'get_webstuff':
        webstuff = f'compose_page,{iris.get_gui()}'
        return webstuff

    ##PID##,$$DATA$$ <- message format
    comma = msg.find(',')
    if comma == -1:
        return f'unknwn thing from socket {msg}'
    pid = msg[:comma]
    data = msg[comma+1:]


    if pid == 'term':
        # code = data.decode("utf-8")
        return f'term,{do_repl(data, iris)}'

    # -------
    #  Filesender Stuff
    # -------
    elif pid == 'get_file':
        if isinstance(data, bytes):
            data = data.decode()
        with open(data, 'r') as f:
            try:
                return f'to_file_editor,{data},{f.read()}'
            except UnicodeError:
                return f'to_file_editor,UnicodeError,UnicodeError'

    elif pid == 'save_file':
        if isinstance(data, str):
            data = data.encode()
        if data[:9] == b'newsingle':
            second_comma = data.find(b',', 10)
            filename = data[10:second_comma].decode()
            global file
            file = open(filename, 'wb')
            print('isnewsingle')
            file.write(data[second_comma+1:])
            file.close()
            file = None
            return 'term,File saved'
        elif data[:3] == b'new':
            second_comma = data.find(b',', 4)
            filename = data[4:second_comma].decode()
            file = open(filename, 'wb')
            print('isnew')
            file.write(data[second_comma+1:])
            return f'send_next_chunk, '
        elif data[:3] == b'end':
            print('end')
            file.write(data[4:])
            file.close()
            file = None
            return 'term,File saved'
        else:
            # assumed tag is 'chunk'
            print('chunk')
            file.write(data[6:])
            return f'send_next_chunk, '

    elif pid == 'listdir':

        def is_dir(path):
            """Check if path is a directory in MicroPython."""
            try:
                return (os.stat(path)[0] & 0x4000) != 0  # stat.S_IFDIR bit
            except OSError:
                return False

        def get_directory_structure(root_dir):
            dir_structure = {}
            try:
                for entry in os.listdir(root_dir):
                    path = root_dir + "/" + entry if root_dir != "/" else "/" + entry
                    if is_dir(path):
                        dir_structure[entry] = get_directory_structure(path)
                    else:
                        dir_structure[entry] = None  # or path if you prefer full paths
            except OSError:
                pass  # directory may not exist / inaccessible
            return dir_structure
        root = "/" if implementation.name == "micropython" else "."
        return f'listdir,{json.dumps(get_directory_structure(root))}'

    # -------
    #  <end> Filesender Stuff
    # -------

    try:
        pid = int(pid)
    except:
        ValueError('pid not int')
        return

    if data == 'true':
        data = True
    if data == 'false':
        data = False

    try:
        # print('processing', pid, data)
        iris.p[pid].receive_from_gui(data)
    except Exception as e:
        trace = traceback.format_exc()
        return f'term,{trace}'


async def chatroom_ws(websocket):
    await websocket.accept()

    receiver_task = asyncio.create_task(chatroom_ws_receiver(websocket))
    sender_task = asyncio.create_task(chatroom_ws_sender(websocket))

    done, pending = await asyncio.wait(
        {receiver_task, sender_task}, return_when=asyncio.FIRST_COMPLETED
    )

    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    for task in done:
        try:
            task.result()
        except WebSocketDisconnect:
            pass
        except Exception:
            raise

async def chatroom_ws_receiver(websocket):
    async for message in websocket.iter_text():
        # Messages are newline-framed and escaped by frameOut() (core/utils.js)
        # because the ESP32 runtime can't trust frame boundaries. Starlette
        # hands us exactly one frame per iteration, so no cross-iteration
        # buffer is needed here — splitting is enough, and it keeps an
        # unframed (legacy) message working as a single element.
        for line in message.split('\n'):
            if not line:
                continue
            processed = process(unescape(line))
            if processed:
                await broadcast.publish(channel="chatroom", message=processed)


async def chatroom_ws_sender(websocket):
    async with broadcast.subscribe(channel="chatroom") as subscriber:
        async for event in subscriber:
            await websocket.send_text(event.message)


#######################
# sync — pull from IDE on demand (DEV-ONLY)
#######################

def _touch_reload():
    """Touch the uvicorn _reload sentinel to trigger a process restart —
    this platform's reboot mechanism (re-runs _init_iris/config.setup).
    Registered as iris.reboot() via iris.set_reboot (ACT-LEAF reboot matrix)."""
    os.makedirs('_reload', exist_ok=True)
    with open('_reload/reload.py', 'w') as f:
        f.write('# touched to trigger uvicorn reload\n')


def _load_sync_settings():
    """Load sync_settings.json if it exists."""
    try:
        with open('sync_settings.json', 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

sync_settings = _load_sync_settings()


def _pull_floe(req, ide_url, updated, errors):
    """Refresh floe/ from the IDE alongside the Parameters.

    Parameters are written against the IDE's current floe. Syncing them onto
    this build's export-time floe once left SerialTTLBus calling a CanHeader
    method that didn't exist yet, which killed its receive loop. floe/_env.py
    is baked for this build at export and is never replaced.
    """
    resp = req.get(f'{ide_url}/sync/floe', timeout=5)
    if resp.status_code != 200:
        errors.append(f'floe/: HTTP {resp.status_code} - framework not refreshed')
        return
    os.makedirs('floe', exist_ok=True)
    for name, text in resp.json().get('files', {}).items():
        if name == '_env.py' or '/' in name or '\\' in name or not name.endswith('.py'):
            continue
        with open(f'floe/{name}', 'w', encoding='utf-8') as f:
            f.write(text)
        updated.append(f'floe/{name}')


def _pull_subs(req, ide_url, canvas_id, updated, errors):
    """Refresh subscriptions.json from the flotilla canvas.

    The canvas is the source of truth for this board's cross-board wiring, so
    an on-bus flotilla board's file is replaced, even with {}. Boot replays
    it into iris when Sync restarts the runtime (cpython touches the reload
    sentinel; docker's uvicorn --reload fires on config.py). A standalone or
    off-bus board gets null back and keeps whatever Zorg's Save subs wrote.
    Failures are recorded, never raised, so they can't abort the rest of Sync.
    """
    try:
        resp = req.get(f'{ide_url}/sync/subscriptions/{canvas_id}', timeout=10)
    except Exception as e:
        errors.append(f'subscriptions.json: {e} - not refreshed')
        return
    if resp.status_code != 200:
        errors.append(f'subscriptions.json: HTTP {resp.status_code} - not refreshed')
        return
    subs = resp.json().get('subscriptions')
    if subs is None:
        return
    with open('subscriptions.json', 'w') as f:
        json.dump(subs, f)
    updated.append('subscriptions.json')


async def sync_pull(request):
    """DEV-ONLY: Pull latest config.py, the floe framework, parameters and
    subscriptions.json from the IDE."""
    from starlette.responses import JSONResponse
    import requests as req

    ide_url = sync_settings.get('ide_url')
    canvas_id = sync_settings.get('canvas_id')
    if not ide_url or not canvas_id:
        return JSONResponse({'error': 'No sync_settings.json or missing ide_url/canvas_id'}, status_code=400)

    updated = []
    errors = []

    try:
        # First, before any .py lands: on docker, uvicorn --reload restarts
        # the worker the moment config.py changes, and boot must find this.
        _pull_subs(req, ide_url, canvas_id, updated, errors)

        # Pull config.py
        resp = req.get(f'{ide_url}/sync/config/{canvas_id}', timeout=5)
        if resp.status_code == 200:
            with open('config.py', 'w', encoding='utf-8') as f:
                f.write(resp.text)
            updated.append('config.py')
        else:
            errors.append(f'config.py: HTTP {resp.status_code}')

        _pull_floe(req, ide_url, updated, errors)

        # Pull parameter list from dependencies
        dep_resp = req.get(f'{ide_url}/sync/dependencies/{canvas_id}', timeout=5)
        if dep_resp.status_code == 200:
            dependencies = dep_resp.json().get('dependencies', [])
            os.makedirs('parameters', exist_ok=True)
            os.makedirs('static/Parameters', exist_ok=True)
            logger.info(f'sync: dependencies = {dependencies}')
            for dep in dependencies:
                if dep.startswith('Module:'):
                    # Module dependency format: "Module:name:canvas_id"
                    parts = dep.split(':')
                    logger.info(f'sync: module dep parts = {parts}')
                    if len(parts) == 3:
                        mod_name, mod_canvas_id = parts[1], parts[2]
                        mod_resp = req.get(f'{ide_url}/sync/module/{mod_canvas_id}/{mod_name}', timeout=5)
                        logger.info(f'sync: module {mod_name} response = {mod_resp.status_code}')
                        if mod_resp.status_code == 200:
                            with open(f'{mod_name}.py', 'w', encoding='utf-8') as f:
                                f.write(mod_resp.text)
                            updated.append(f'{mod_name}.py')
                        else:
                            errors.append(f'{mod_name}.py: HTTP {mod_resp.status_code}')
                    continue
                # Pull .py into parameters/
                param_resp = req.get(f'{ide_url}/sync/parameter/{dep}', timeout=5)
                if param_resp.status_code == 200:
                    with open(f'parameters/{dep}.py', 'w', encoding='utf-8') as f:
                        f.write(param_resp.text)
                    updated.append(f'parameters/{dep}.py')
                else:
                    errors.append(f'{dep}.py: HTTP {param_resp.status_code}')

                # Pull .js and .html into static/Parameters/
                for file_type, ext in [('js', 'js'), ('html', 'html')]:
                    resp = req.get(f'{ide_url}/sync/parameter/{dep}?file_type={file_type}', timeout=5)
                    if resp.status_code == 200:
                        with open(f'static/Parameters/{dep}.{ext}', 'w', encoding='utf-8') as f:
                            f.write(resp.text)
                        updated.append(f'static/Parameters/{dep}.{ext}')
                    elif resp.status_code != 404:
                        errors.append(f'{dep}.{ext}: HTTP {resp.status_code}')

        # Update local last_modified
        check_resp = req.get(f'{ide_url}/sync/check/{canvas_id}', timeout=5)
        if check_resp.status_code == 200:
            sync_settings['last_modified'] = check_resp.json().get('last_modified', 0)
            with open('sync_settings.json', 'w') as f:
                json.dump(sync_settings, f, indent=2)

    except Exception as e:
        errors.append(str(e))

    # Touch sentinel so uvicorn restarts the runtime
    if updated:
        _touch_reload()

    logger.info(f'sync: pulled {len(updated)} files, {len(errors)} errors')
    if errors:
        for e in errors:
            logger.warning(f'sync error: {e}')
    return JSONResponse({'updated': updated, 'errors': errors})


async def reset_reload(request):
    """Touch the reload sentinel to trigger a uvicorn restart."""
    _touch_reload()
    logger.info('reset: touched reload sentinel')
    return JSONResponse({'status': 'ok'})


routes = [
    WebSocketRoute("/ws", chatroom_ws, name="chatroom_ws"),
    Route("/", terminal),
    Route("/parameters/get/{name}", get_parameter_js),
    Route("/sync/pull", sync_pull, methods=["POST"]),
    Route("/reset", reset_reload, methods=["POST"]),
    Mount("/static", app=StaticFiles(directory="static"), name="static"),
]


async def chk_bifrost():
    while True:
        if iris.bifrost.any():
            msg = iris.bifrost.pop()
            await broadcast.publish(channel="chatroom", message=msg)
        await asyncio.sleep(0)

def _init_iris():
    # Argus is callable from the moment Iris() is constructed —
    # bifrost.post falls back to print before iris.bifrost._checked
    # is set, so boot-time critical events still surface in server
    # logs. Wrap Parameter construction so a __init__ raising goes
    # through Argus and is visible in the same channels as runtime
    # faults.
    try:
        config.setup(iris)
        iris.boot(start_mailboxes=False)
    except Exception as e:
        iris.argus.critical(f"boot failed: {e}")
        raise
    iris.bifrost._checked = True
    # ACT-LEAF: this platform reboots by touching the uvicorn _reload
    # sentinel (re-runs config.setup). Register it so iris.reboot() works.
    iris.set_reboot(_touch_reload)
    if 'subscriptions.json' in os.listdir():
        with open('subscriptions.json', 'r') as f:
            subs = json.load(f)
            for header, pid_struct in subs.items():
                iris.subscribe(int(header), pid_struct[0], pid_struct[1])


@asynccontextmanager
async def lifespan(app):
    _init_iris()
    await broadcast.connect()
    asyncio.create_task(chk_bifrost())
    asyncio.create_task(iris.cib())
    asyncio.create_task(iris.cob())
    yield
    await broadcast.disconnect()


app = Starlette(
    routes=routes,
    lifespan=lifespan,
)


if __name__ == "__main__":
    os.makedirs('_reload', exist_ok=True)
    # Seed the sentinel so uvicorn has something to watch
    with open('_reload/reload.py', 'a') as f:
        pass
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True, reload_dirs=["_reload"])
