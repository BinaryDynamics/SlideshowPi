import importlib.util
import json
from pathlib import Path


def load_module():
    spec = importlib.util.spec_from_file_location('first_boot', Path(__file__).parents[1] / 'deploy' / 'first_boot.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stage_copies_app_writes_private_config_and_queues_service(tmp_path, monkeypatch):
    module = load_module()
    source = tmp_path / 'boot' / 'pi-slideshow'
    for folder in ('slideshow', 'templates', 'static', 'deploy'):
        (source / folder).mkdir(parents=True)
        (source / folder / 'sample').write_text('payload')
    for name in ('run.py', 'player.py', 'VERSION', 'deploy/install.sh', 'deploy/pi-slideshow-install.service'):
        (source / name).write_text('payload')
    setup = {'country': 'ZA', 'ssid': 'test-hotspot', 'password': 'test-only-password'}
    (source / 'setup.json').write_text(json.dumps(setup))
    app = tmp_path / 'opt' / 'pi-slideshow'
    config = tmp_path / 'etc' / 'pi-slideshow' / 'setup.json'
    units = tmp_path / 'etc' / 'systemd' / 'system'
    units.mkdir(parents=True)
    monkeypatch.setattr(module, '__file__', str(source / 'deploy' / 'first_boot.py'))
    monkeypatch.setattr(module, 'APP', app)
    monkeypatch.setattr(module, 'CONFIG', config)
    monkeypatch.setattr(module, 'DONE', tmp_path / 'not-completed')
    # Redirect just the service destination; all other Paths are real temp files.
    real_path = module.Path
    monkeypatch.setattr(module, 'Path', lambda value: units if value == '/etc/systemd/system' else real_path(value))
    calls = []
    monkeypatch.setattr(module, 'run', lambda *args: calls.append(args))
    monkeypatch.setattr(module, 'status', lambda message: None)
    module.stage()
    assert json.loads(config.read_text()) == setup
    assert (app / 'slideshow' / 'sample').read_text() == 'payload'
    assert (units / module.UNIT).exists()
    assert calls == [('systemctl', 'daemon-reload'), ('systemctl', 'enable', module.UNIT),
                     ('systemctl', 'start', '--no-block', module.UNIT)]
