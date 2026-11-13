"""
Firmware command catalog — the single source of truth for every control UI.

Both the local page on this Pi and the PC Motion Control Center draw their buttons from
GET /api/commands, so a firmware change only needs an edit here.

`timeout` (seconds) is how long the Pi waits for DONE/ERR. None means "use the Pi's
COMMAND_TIMEOUT": calibrate / home / move can take well over 30 s on the real rig at
low speed, so they follow the one configurable default instead of a guessed number.
"""

GROUPS = [
    {'title': 'X Axis', 'range': '0 – 1000 mm',
     'actions': [{'cmd': 'SXC', 'label': 'Calibrate'}, {'cmd': 'SXH', 'label': 'Home'},
                 {'cmd': 'SXP', 'label': 'Position', 'timeout': 10}],
     'move': {'prefix': 'SXM', 'label': 'Move', 'unit': 'mm'}},
    {'title': 'Y Axis', 'range': '0 – 1000 mm',
     'actions': [{'cmd': 'SYC', 'label': 'Calibrate'}, {'cmd': 'SYH', 'label': 'Home'},
                 {'cmd': 'SYP', 'label': 'Position', 'timeout': 10}],
     'move': {'prefix': 'SYM', 'label': 'Move', 'unit': 'mm'}},
    {'title': 'Z Axis', 'range': '0 – 300 mm',
     'actions': [{'cmd': 'SZC', 'label': 'Calibrate'}, {'cmd': 'SZH', 'label': 'Home'},
                 {'cmd': 'SZP', 'label': 'Position', 'timeout': 10}],
     'move': {'prefix': 'SZM', 'label': 'Move', 'unit': 'mm'}},
    {'title': 'Rotation', 'range': '-180° – 180°',
     'actions': [{'cmd': 'RC', 'label': 'Calibrate'}, {'cmd': 'RH', 'label': 'Home'},
                 {'cmd': 'RP', 'label': 'Angle', 'timeout': 10}],
     'move': {'prefix': 'RM', 'label': 'Rotate', 'unit': '°'}},
    {'title': 'Tilt', 'range': '0° – 90°',
     'actions': [{'cmd': 'TP', 'label': 'Angle', 'timeout': 10}],
     'move': {'prefix': 'TM', 'label': 'Tilt', 'unit': '°'}},
    {'title': 'LED Test', 'range': '',
     'actions': [{'cmd': 'L0', 'label': 'All off', 'timeout': 5}, {'cmd': 'L1', 'label': 'Action', 'timeout': 5},
                 {'cmd': 'L2', 'label': 'Status', 'timeout': 5}, {'cmd': 'L3', 'label': 'All on', 'timeout': 5}]},
]

# "Home all devices" on the PC runs these one after another on every Pi.
HOME_SEQUENCE = ['SXH', 'SYH', 'SZH', 'RH']


def catalog(default_timeout):
    """GROUPS with every timeout filled in, so clients never have to guess one."""
    def resolve(entry):
        return {**entry, 'timeout': entry.get('timeout') or default_timeout}

    groups = []
    for group in GROUPS:
        resolved = {**group, 'actions': [resolve(a) for a in group['actions']]}
        if 'move' in group:
            resolved['move'] = resolve(group['move'])
        groups.append(resolved)
    return {'groups': groups, 'home_sequence': HOME_SEQUENCE, 'default_timeout': default_timeout}
