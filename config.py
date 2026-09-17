
# --------------------------------------
__floe__ = {'adr': None, 'canvas_id': '6a956ff2eab3a963canv', 'flotilla_id': '6a956ff2eab3a963canv', 'flotilla_name': 'ws_helper_g802', 'name': 'ws_helper_g802', 'version': 'ad5b1b260eb8f92354b04609e9aa6d86297c1db6fca8c616b79a44bb5cc8c47b'}
# --------------------------------------
from floe import FP

from parameters.Variable import Variable
from parameters.WsHelper import WsHelper
from parameters.GuiCodeEditor import GuiCodeEditor
def setup(iris):

  GuiCodeEditor(name='csv_output', pid=35097, initial_value='', datatype="any", debug=False, active=True, bcast=False, render_gui=True, iris=iris)
  Variable(name='no_name', pid=52270, state={'die_widths': [4, 4, 4, 4, 4, 4, 2, 2, 2, 0.124], 'die_heights': [5.182, 5.182, 5.182, 2.591, 2.591, 2.591, 0.439], 'kerf': 0, 'wafer_size': 200, 'exclusion_zone': 2.9, 'center_offset_x': 0.0627, 'center_offset_y': 11.879, 'rotation': 90, 'ignore_reticles': [[-4, -2], [-4, -1], [-4, 0], [-3, -4], [-3, 2], [-2, -5], [-2, 3], [-1, -5], [-1, 3], [0, -5], [0, 3], [1, -5], [1, 3], [2, -4], [2, 2], [3, -2], [3, -1], [3, 0]], 'ignore_first_die_col': False, 'ignore_last_die_col': True, 'ignore_first_die_row': False, 'ignore_last_die_row': True}, datatype="json", constant=False, debug=False, active=True, bcast=False, iris=iris)
  WsHelper(name='wsh', pid=36866, config=FP(52270), datatype="string", debug=False, active=True, bcast=False, render_gui=True, iris=iris)
  iris.add_hots({"35097": [36866]})
  iris.set_info(__floe__)
  iris.set_layout([(36866,1,1,12,5),(35097,1,7,12,2)])

