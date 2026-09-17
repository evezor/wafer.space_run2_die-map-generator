from floe import Iris, Parameter, FP, PID

class Variable(Parameter):
    def __init__(self, *, datatype: str, iris: Iris, state: any = None, pid: int = 0, constant: bool=False, **k):
        super().__init__(pid=pid, iris=iris, state=state, **k)
        if datatype == 'code':
            datatype = 'string'
        self.serdes = self.datatypes[datatype]
        self.constant = constant
        if datatype == 'rgb':
            self.state = (self.state['red'], self.state['green'], self.state['blue'])
    
    def __call__(self, state):
        if self.constant:
            self.emit()
            return
        super().__call__(state)

    
    