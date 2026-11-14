"""Exercise the bundled server, PDF helper, and launcher shutdown pipe."""

import base64
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from urllib.request import Request, urlopen

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / 'runtime' / 'research-server' / 'research-server.exe'


def pdf_fixture():
    pdf = PdfWriter()
    page = pdf.add_blank_page(width=612, height=792)
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({
        NameObject('/F1'): DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                            NameObject('/Subtype'): NameObject('/Type1'),
                                            NameObject('/BaseFont'): NameObject('/Helvetica')})})})
    contents = DecodedStreamObject()
    contents.set_data(b'BT /F1 12 Tf 20 700 Td (Bundled PDF extraction works) Tj ET')
    page[NameObject('/Contents')] = contents
    buffer = BytesIO()
    pdf.write(buffer)
    return buffer.getvalue()


def call(port, path, payload=None):
    body = json.dumps(payload).encode('utf-8') if payload is not None else None
    with urlopen(Request(f'http://127.0.0.1:{port}{path}', body,
                         headers={'Content-Type': 'application/json'}), timeout=2) as response:
        return response.read()


def check_uploaded_pdf(port, raw):
    result = json.loads(call(port, '/api/jobs', {
        'mode': 'research', 'pdf': {'name': 'fixture.pdf', 'data': base64.b64encode(raw).decode('ascii')},
    }))
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = json.loads(call(port, '/api/jobs/' + result['id']))
        if job['status'] in {'completed', 'failed'}:
            paper = json.loads(call(port, '/api/papers/' + job['paper_id']))
            profile = paper['profile']
            assert profile and profile['coverage']['read_pages'] == 1, job
            return
        time.sleep(.1)
    raise RuntimeError('Uploaded PDF was not processed')


def main():
    env = {key: value for key, value in os.environ.items()
           if key.upper() not in {'OPENAI_API_KEY', 'PYTHONHOME', 'PYTHONPATH'}}
    env['RESEARCH_AGENT_INSTANCE'] = 'windows-smoke-local'
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32')
    test_data = ROOT / '.test-data'
    test_data.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='windows-bundle-', dir=test_data) as temp:
        raw = pdf_fixture()
        for command in ('extract', 'extract-full'):
            result = subprocess.run([str(SERVER), command], input=raw, capture_output=True,
                                    check=True, cwd=temp, env=env, timeout=20)
            document = json.loads(result.stdout)
            assert 'Bundled PDF extraction works' in str(document['pages']), document

        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]

        for attempt in range(2):
            with subprocess.Popen([str(SERVER), '--stop-on-stdin', '--port', str(port),
                                   '--data-dir', 'data', '--materials-dir', 'Materials'],
                                  cwd=temp, env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW) as process:
                try:
                    deadline = time.monotonic() + 25
                    while time.monotonic() < deadline and process.poll() is None:
                        try:
                            health = json.loads(call(port, '/api/health'))
                            break
                        except OSError:
                            time.sleep(.1)
                    else:
                        raise RuntimeError('Bundled server did not start')

                    assert health['instance'] == env['RESEARCH_AGENT_INSTANCE']
                    assert health['worker_alive'] and health['pdf_configured'] and not health['api_configured']
                    assert b'Research Agent' in call(port, '/')
                    assert (Path(temp) / 'Materials').is_dir()
                    if attempt == 0:
                        check_uploaded_pdf(port, raw)
                        job = json.loads(call(port, '/api/jobs', {
                            'source': '2409.10975', 'mode': 'demo', 'settings': {'stage_seconds': 10},
                        }))['id']
                    else:
                        assert json.loads(call(port, '/api/jobs/' + job))['status'] == 'interrupted'
                    process.stdin.close()
                    assert process.wait(timeout=15) == 0
                except Exception:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                    print(process.stderr.read().decode('utf-8', errors='backslashreplace'))
                    raise

    print('Windows bundle passed PDF extraction, offline startup, shutdown, and restart with saved data.')


if __name__ == '__main__':
    main()
