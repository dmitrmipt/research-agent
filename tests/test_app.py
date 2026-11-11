import base64
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import unittest
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


class AppTest(unittest.TestCase):
    def setUp(self):
        self.temp = ROOT / '.test-data' / uuid.uuid4().hex
        self.temp.mkdir(parents=True)
        self.process = None
        self.addCleanup(self.cleanup)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.url = f'http://127.0.0.1:{self.port}'
        self.start()

    def start(self):
        self.process = subprocess.Popen([sys.executable, str(ROOT / 'app.py'), '--port', str(self.port), '--data-dir', str(self.temp)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail(self.process.stderr.read().decode(errors='replace'))
            try:
                if self.call('/api/health')['worker_alive']:
                    return
            except OSError:
                time.sleep(.05)
        self.fail('Server failed to start')

    def kill(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=5)
        if self.process and self.process.stderr:
            self.process.stderr.close()

    def cleanup(self):
        self.kill()
        if self.temp.resolve().parent == (ROOT / '.test-data').resolve():
            shutil.rmtree(self.temp)

    def call(self, path, data=None, headers=None):
        body = json.dumps(data).encode() if data is not None else None
        request = Request(self.url + path, data=body, headers=headers or {'Content-Type': 'application/json'})
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def settings(self, **changes):
        value = self.call('/api/settings')
        value.update(changes)
        return self.call('/api/settings', value)

    def create(self, **changes):
        value = {'source': '2409.10975v2'}
        value.update(changes)
        return self.call('/api/jobs', value)

    def wait_status(self, job_id, states, timeout=9):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.call('/api/jobs/' + job_id)
            if job['status'] in states:
                return job
            time.sleep(.08)
        self.fail(f'Expected {states}; got {job["status"]}')

    def test_complete_snapshot_and_library(self):
        self.settings(stage_seconds=1, interests='Quantum optics')
        first = self.create(questions='<script>literal user text</script>')
        self.settings(interests='Changed profile', stage_seconds=2)
        second = self.create(source='https://arxiv.org/abs/2409.10975v2')
        self.call('/api/jobs/' + second['id'] + '/cancel', {})
        done = self.wait_status(first['id'], ['completed'])
        self.assertEqual(done['settings']['interests'], 'Quantum optics')
        self.assertEqual(done['progress'], 100)
        self.assertIn('Научный анализ не выполнялся', done['report'])
        self.assertIn('Вызовы API: 0', done['report'])
        self.assertIn('Текст статьи не загружался', done['report'])
        self.assertNotIn('Загруженный PDF сохранён', done['report'])
        self.assertEqual(len(self.call('/api/papers')), 1)
        self.assertEqual(self.call('/api/papers')[0]['job_count'], 2)
        with urlopen(self.url + '/api/jobs/' + first['id'] + '/report') as response:
            self.assertIn('attachment', response.headers['Content-Disposition'])
            self.assertEqual(response.read().decode(), done['report'])

    def test_queue_stop_and_restart(self):
        first = self.create()
        self.wait_status(first['id'], ['running'])
        second = self.create(source='2409.10975v1')
        self.assertEqual(second['status'], 'queued')
        stopped = self.call('/api/jobs/' + first['id'] + '/cancel', {})
        self.assertEqual(stopped['status'], 'cancelled')
        self.wait_status(second['id'], ['running'])
        third = self.create(source='2501.00001')
        self.kill()
        self.start()
        self.assertEqual(self.call('/api/jobs/' + first['id'])['status'], 'cancelled')
        for job in (second, third):
            saved = self.call('/api/jobs/' + job['id'])
            self.assertEqual(saved['status'], 'interrupted')
            self.assertIsNone(saved['report'])
        time.sleep(.3)
        self.assertFalse(any(j['status'] in ('queued', 'running') for j in self.call('/api/jobs')))

    def test_failure_and_time_limit(self):
        self.settings(stage_seconds=1)
        job = self.create(scenario='error')
        failed = self.wait_status(job['id'], ['failed'])
        self.assertIn('Имитирована', failed['error'])
        self.assertIsNone(failed['report'])
        self.settings(stage_seconds=2, time_limit_seconds=5)
        job = self.create()
        failed = self.wait_status(job['id'], ['failed'])
        self.assertIn('лимиту времени', failed['error'])

    def test_pdf_validation_and_local_access(self):
        raw = b'%PDF-1.4\n% Demo upload fixture, not a scientific document\n%%EOF'
        upload = {'name': '../example.pdf', 'data': base64.b64encode(raw).decode()}
        job = self.create(pdf=upload)
        self.assertEqual(job['title'], 'example.pdf')
        files = list((self.temp / 'uploads').glob('*.pdf'))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), raw)
        self.create(pdf=upload)
        self.assertEqual(len(self.call('/api/papers')), 1)
        for data in ({'source': 'https://example.com/a'}, {'source': 'bad'}, {'pdf': {'name': 'x.pdf', 'data': '!!!!'}}):
            with self.assertRaises(HTTPError) as error:
                self.call('/api/jobs', data)
            self.assertEqual(error.exception.code, 400)
        with self.assertRaises(HTTPError) as error:
            self.call('/api/jobs', {'source': '2409.10975'}, {'Content-Type': 'application/json', 'Origin': 'https://example.com'})
        self.assertEqual(error.exception.code, 403)
        with urlopen(self.url) as response:
            self.assertIn('Новый разбор', response.read().decode())
            self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])

    def test_second_instance_cannot_interrupt_jobs(self):
        job = self.create()
        self.wait_status(job['id'], ['running'])
        process = subprocess.run([sys.executable, str(ROOT / 'app.py'), '--port', str(self.port), '--data-dir', str(self.temp)], capture_output=True, timeout=4)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(self.call('/api/jobs/' + job['id'])['status'], 'running')


if __name__ == '__main__':
    unittest.main()
