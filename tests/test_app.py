"""
Tests for the Pi API with a fake serial port standing in for the Arduino firmware.

Run:  pip install -r requirements.txt pytest && python -m pytest
"""
import threading

import pytest

import app as pi


class FakeSerial:
    """
    Minimal pyserial stand-in. `script` maps a command to the lines the firmware answers.
    Lines in `pending` are returned before anything else (e.g. a late DONE).
    """
    is_open = True

    def __init__(self, script=None, pending=None, release=None):
        self.script = script or {}
        self.buffer = list(pending or [])
        self.written = []
        self.release = release      # threading.Event: block readline() until set

    def write(self, data):
        cmd = data.decode().rstrip('\n')
        self.written.append(cmd)
        self.buffer.extend(self.script.get(cmd, [f'ERR: {cmd}']))

    def readline(self):
        if self.release is not None:
            self.release.wait(5)
        return (self.buffer.pop(0) + '\n').encode() if self.buffer else b''

    def close(self):
        pass


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(pi, '_desynced', False)
    monkeypatch.setattr(pi, '_job', None)
    pi.app.config['TESTING'] = True
    return pi.app.test_client()


def use_serial(monkeypatch, fake):
    monkeypatch.setattr(pi, 'ser', fake)
    return fake


# ----- protocol -----

def test_command_filters_heartbeats(client, monkeypatch):
    use_serial(monkeypatch, FakeSerial({'SXM,500': ['Calling move x', 'HB,1200,BUSY', 'moving', 'DONE']}))
    body = client.post('/api/command', json={'command': 'SXM,500'}).get_json()
    assert body['arduino_result'] == 'DONE'
    assert body['arduino_response'] == ['Calling move x', 'moving', 'DONE']
    assert pi._last_heartbeat['arduino_state'] == 'BUSY'


def test_firmware_err_is_still_a_success(client, monkeypatch):
    use_serial(monkeypatch, FakeSerial())
    resp = client.post('/api/command', json={'command': 'NOPE'})
    assert resp.status_code == 200
    assert resp.get_json()['arduino_result'] == 'ERR'


def test_timeout_marks_desync_and_next_command_skips_the_late_done(client, monkeypatch):
    fake = use_serial(monkeypatch, FakeSerial({'SXC': [], 'SXP': ['Position = 5', 'DONE']}))
    resp = client.post('/api/command', json={'command': 'SXC', 'timeout': 0.05})
    assert resp.status_code == 503
    assert resp.get_json()['error'] == 'TIMEOUT_NO_TERMINAL'
    assert pi._desynced

    fake.buffer.append('DONE')      # the late terminal of SXC finally arrives
    body = client.post('/api/command', json={'command': 'SXP'}).get_json()
    assert body['arduino_response'] == ['Position = 5', 'DONE']
    assert not pi._desynced


def test_no_arduino_does_not_crash(client, monkeypatch):
    monkeypatch.setattr(pi, 'ser', None)
    monkeypatch.setattr(pi, '_last_connect_attempt', 0.0)
    monkeypatch.setattr(pi, 'PORT', '/dev/does-not-exist')
    status = client.get('/api/status').get_json()
    assert status['arduino_status'] == 'disconnected'
    assert status['arduino_error'] == 'SERIAL_DISCONNECTED'
    assert client.get('/api/health').status_code == 200


# ----- validation -----

@pytest.mark.parametrize('payload, message', [
    ({'command': ''}, 'Missing command'),
    ({'command': 'SXH\nSYH'}, 'one line'),
    ({'command': 'X' * 65}, 'one line'),
    ({'command': 'SXH', 'timeout': 'soon'}, 'Invalid timeout'),
    ({'command': 'SXH', 'timeout': 0}, 'at most'),
    ({'command': 'SXH', 'timeout': 301}, 'at most'),
])
def test_bad_requests(client, monkeypatch, payload, message):
    fake = use_serial(monkeypatch, FakeSerial())
    resp = client.post('/api/command', json=payload)
    assert resp.status_code == 400
    assert message in resp.get_json()['error']
    assert fake.written == []


def test_missing_body(client):
    assert client.post('/api/command', data='not json', content_type='text/plain').status_code == 400


# ----- one command at a time -----

def test_busy_while_a_command_runs(client, monkeypatch):
    release = threading.Event()
    use_serial(monkeypatch, FakeSerial({'SXC': ['DONE']}, release=release))
    worker = threading.Thread(target=lambda: pi.app.test_client().post('/api/command', json={'command': 'SXC'}))
    worker.start()
    try:
        for _ in range(100):            # wait until the command has claimed the job
            if pi.current_job():
                break
            threading.Event().wait(0.01)

        status = client.get('/api/status').get_json()
        assert status['arduino_status'] == 'busy'
        assert status['running']['command'] == 'SXC'

        second = client.post('/api/command', json={'command': 'SYC'})
        assert second.status_code == 409
        assert second.get_json()['running']['command'] == 'SXC'
        assert client.get('/api/ping').status_code == 409
    finally:
        release.set()
        worker.join(5)
    assert pi.current_job() is None


# ----- discovery -----

def test_status_reports_timeouts_and_version(client, monkeypatch):
    use_serial(monkeypatch, FakeSerial({'?': ['Ceiling Rig Front', 'DONE']}))
    body = client.get('/api/status').get_json()
    assert body['arduino_status'] == 'connected'
    assert body['rig_name'] == 'Ceiling Rig Front'
    assert body['command_timeout'] == pi.COMMAND_TIMEOUT
    assert body['max_command_timeout'] == pi.MAX_COMMAND_TIMEOUT
    assert body['app_version'] == pi.APP_VERSION


def test_command_catalog_has_every_timeout_filled_in(client):
    body = client.get('/api/commands').get_json()
    entries = [a for g in body['groups'] for a in g['actions']] + [g['move'] for g in body['groups'] if 'move' in g]
    assert entries and all(e['timeout'] > 0 for e in entries)
    calibrate = next(a for a in body['groups'][0]['actions'] if a['cmd'] == 'SXC')
    assert calibrate['timeout'] == pi.COMMAND_TIMEOUT
    assert body['home_sequence'] == ['SXH', 'SYH', 'SZH', 'RH']
