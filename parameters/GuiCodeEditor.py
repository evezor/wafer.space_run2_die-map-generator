import json
from floe import Parameter

class GuiCodeEditor(Parameter):
    serdes = 'e'  # bool

    def __init__(self, *, name: str = "", initial_value: str = "", **k):
        super().__init__(name=name, **k)
        self.name = name
        self.state = initial_value

    def __call__(self, state):
        super().__call__(state)
        self.iris.bifrost.send(self.pid, json.dumps({'cmd': 'set', 'text': self.state}))

    def receive_from_gui(self, data):
        # `sync` keeps self.state fresh as the user types but does NOT wake hot
        # subscribers - only an explicit `submit` propagates downstream.
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                pass
        if isinstance(data, dict) and 'cmd' in data:
            cmd = data.get('cmd')
            text = data.get('text', '')
            if cmd == 'sync':
                self.state = text
                self.iris.bifrost.send(self.pid, json.dumps({'cmd': 'synced'}))
                return
            if cmd == 'submit':
                super().__call__(text)
                self.iris.bifrost.send(self.pid, json.dumps({'cmd': 'synced'}))
                return
        super().__call__(data)

    def update(self):
        super().update()
        if not isinstance(self.state, str):
            self.state = self.state.state

    def _get_gui(self):
        return {"name": self.name, "pid": self.pid, "state": self.state, "type": "GuiCodeEditor"}
    