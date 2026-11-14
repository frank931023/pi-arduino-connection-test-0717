"""
Firmware command catalog — the single source of truth for every control UI.

Both the local page on this Pi and the PC Motion Control Center draw their buttons from
GET /api/commands, so a firmware change only needs an edit here.

Which catalog a Pi serves depends on the board its Arduino is: BOARD in the Pi's .env
(Ansible writes it from the `board` host variable). The Mega runs the full ceiling rig
firmware; the Uno has far fewer pins, so it only gets the axes its firmware drives.

`timeout` (seconds) is how long the Pi waits for DONE/ERR. None means "use the Pi's
COMMAND_TIMEOUT": calibrate / home / move can take well over 30 s on the real rig at
low speed, so they follow the one configurable default instead of a guessed number.
"""

X_AXIS = {
    'title': 'X Axis', 'range': '0 – 1000 mm',
    'actions': [{'cmd': 'SXC', 'label': 'Calibrate'}, {'cmd': 'SXH', 'label': 'Home'},
                {'cmd': 'SXP', 'label': 'Position', 'timeout': 10}],
    'move': {'prefix': 'SXM', 'label': 'Move', 'unit': 'mm'}}
Y_AXIS = {
    'title': 'Y Axis', 'range': '0 – 1000 mm',
    'actions': [{'cmd': 'SYC', 'label': 'Calibrate'}, {'cmd': 'SYH', 'label': 'Home'},
                {'cmd': 'SYP', 'label': 'Position', 'timeout': 10}],
    'move': {'prefix': 'SYM', 'label': 'Move', 'unit': 'mm'}}
Z_AXIS = {
    'title': 'Z Axis', 'range': '0 – 300 mm',
    'actions': [{'cmd': 'SZC', 'label': 'Calibrate'}, {'cmd': 'SZH', 'label': 'Home'},
                {'cmd': 'SZP', 'label': 'Position', 'timeout': 10}],
    'move': {'prefix': 'SZM', 'label': 'Move', 'unit': 'mm'}}
ROTATION = {
    'title': 'Rotation', 'range': '-180° – 180°',
    'actions': [{'cmd': 'RC', 'label': 'Calibrate'}, {'cmd': 'RH', 'label': 'Home'},
                {'cmd': 'RP', 'label': 'Angle', 'timeout': 10}],
    'move': {'prefix': 'RM', 'label': 'Rotate', 'unit': '°'}}
TILT = {
    'title': 'Tilt', 'range': '0° – 90°',
    'actions': [{'cmd': 'TP', 'label': 'Angle', 'timeout': 10}],
    'move': {'prefix': 'TM', 'label': 'Tilt', 'unit': '°'}}
LED_TEST = {
    'title': 'LED Test', 'range': '',
    'actions': [{'cmd': 'L0', 'label': 'All off', 'timeout': 5}, {'cmd': 'L1', 'label': 'Action', 'timeout': 5},
                {'cmd': 'L2', 'label': 'Status', 'timeout': 5}, {'cmd': 'L3', 'label': 'All on', 'timeout': 5}]}

# `home_sequence`: what "Home all devices" on the PC runs, one after another, on a Pi with this board.
BOARDS = {
    'mega': {
        'label': 'Arduino Mega 2560',
        'groups': [X_AXIS, Y_AXIS, Z_AXIS, ROTATION, TILT, LED_TEST],
        'home_sequence': ['SXH', 'SYH', 'SZH', 'RH'],
    },
    # TODO: placeholder until the Uno firmware exists — list the axes it really drives.
    'uno': {
        'label': 'Arduino Uno',
        'groups': [X_AXIS, Y_AXIS, LED_TEST],
        'home_sequence': ['SXH', 'SYH'],
    },
}


def catalog(board, default_timeout):
    """The board's groups with every timeout filled in, so clients never have to guess one."""
    def resolve(entry):
        return {**entry, 'timeout': entry.get('timeout') or default_timeout}

    spec = BOARDS[board]
    groups = []
    for group in spec['groups']:
        resolved = {**group, 'actions': [resolve(a) for a in group['actions']]}
        if 'move' in group:
            resolved['move'] = resolve(group['move'])
        groups.append(resolved)
    return {'board': board, 'board_label': spec['label'], 'groups': groups,
            'home_sequence': spec['home_sequence'], 'default_timeout': default_timeout}
