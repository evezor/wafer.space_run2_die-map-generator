"""CPython entry point: `python main.py`.

The runtime lives in runtime.py; this file is a boot guard around it. A
mistake in the canvas used to raise out of startup, kill the uvicorn worker,
and take Sync down with it (uvicorn's reloader keeps holding :8000, so nothing
answers). Here that error is caught instead: the terminal and Sync stay up so
the fix can be pulled. Sync touches the _reload sentinel, uvicorn restarts the
worker, and the real config.py gets another try.

Guarded, in boot order:
  import config       a Parameter import raising (e.g. a missing pip package)
  config.setup(iris)  a constructor raising, a CodeBlock or Module failing to load
  iris.boot()         a Parameter's update(), bus connect, the on_startup CodeBlock

Errors a Parameter *contains* and reports through argus while the canvas starts
(e.g. an on_startup CodeBlock whose function raises) would otherwise only reach
the console: bifrost isn't live yet and no browser is connected. They're
recorded and replayed on every page load, alongside any boot failure.
"""
import json
import sys
import types
import traceback

boot_error = None   # the stage that raised; the canvas is not running normally
boot_errors = []    # argus error/critical reports from before bifrost went live

_FAILED_PAGE = 'compose_page,' + json.dumps([{
    'type': '_canvas_info', 'name': 'BOOT FAILED', 'canvas_id': '', 'id': '',
    'layout': [], 'decorations': [],
}])


def _fail(stage):
    global boot_error
    boot_error = (f'BOOT FAILED at {stage}. The canvas is not running normally; '
                  f'fix it in the IDE, then Sync.\n\n{traceback.format_exc()}')
    print(boot_error)


def _guard_stage(stage, fn, critical):
    def guarded(*args, **kwargs):
        if boot_error:
            return  # an earlier stage failed; don't build on a half-made canvas
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            _fail(stage)
            try:
                # what runtime._init_iris did with a boot failure before re-raising
                critical(f'boot failed at {stage}: {e}')
            except Exception:
                traceback.print_exc()
    return guarded


def _record_boot_errors(iris):
    def recording(level, report):
        def wrapped(msg):
            if not iris.bifrost.active():
                boot_errors.append(f'{level}: {msg}')
            return report(msg)
        return wrapped
    iris.argus.error = recording('ERROR', iris.argus.error)
    iris.argus.critical = recording('CRITICAL', iris.argus.critical)


def _guard_page(process, iris):
    def guarded(msg):
        if msg != 'get_webstuff':
            return process(msg)
        # on every page load, so a browser opened later still sees why
        if boot_errors:
            iris.bifrost.post('BOOT ERRORS: reported while the canvas started. It is running, '
                              'but may not be working correctly.\n'
                              + '\n'.join(f'  {e}' for e in boot_errors))
            iris.bifrost.send('toast', {'level': 'warning',
                                        'msg': f'{len(boot_errors)} error(s) while starting - see terminal'})
        if not boot_error:
            return process(msg)
        iris.bifrost.post(boot_error)
        iris.bifrost.send('toast', {'level': 'error', 'msg': 'BOOT FAILED - see terminal'})
        try:
            return process(msg)
        except Exception:
            return _FAILED_PAGE  # e.g. setup failed before iris.set_info, so get_gui() can't render
    return guarded


def _load():
    try:
        import config
    except Exception:
        _fail('import config')
        config = types.ModuleType('config')
        config.setup = lambda iris: None
        sys.modules['config'] = config  # runtime.py's own `import config` gets this stand-in

    import runtime
    iris = runtime.iris
    critical = iris.argus.critical  # unrecorded: a stage failure is already reported as boot_error
    _record_boot_errors(iris)
    config.setup = _guard_stage('config.setup', config.setup, critical)
    iris.boot = _guard_stage('iris.boot', iris.boot, critical)
    runtime.process = _guard_page(runtime.process, iris)
    return runtime.app


def __getattr__(name):
    # uvicorn resolves "main:app" with getattr, so the canvas is loaded and
    # guarded once, in the worker. At module level it would also run in the
    # reloader parent, and again when the spawned worker re-runs this script
    # as __mp_main__.
    global app
    if name == 'app':
        app = _load()
        return app
    raise AttributeError(f"module 'main' has no attribute '{name}'")


if __name__ == "__main__":
    import os
    import uvicorn
    os.makedirs('_reload', exist_ok=True)
    # Seed the sentinel so uvicorn has something to watch
    with open('_reload/reload.py', 'a') as f:
        pass
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True, reload_dirs=["_reload"])
