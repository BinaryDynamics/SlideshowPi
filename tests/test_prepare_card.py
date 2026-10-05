import importlib.util
from pathlib import Path

import pytest
import yaml

spec = importlib.util.spec_from_file_location('prepare_card', Path(__file__).parents[1] / 'tools' / 'prepare_card.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_cloud_init_preserves_existing_settings():
    text = '#cloud-config\nhostname: test-pi\nuser:\n  name: admin\n  passwd: "hash-placeholder"\nruncmd:\n  - [ systemctl, enable, --now, ssh ]\ntimezone: UTC\n'
    new = module.add_hook(text)
    before, after = yaml.safe_load(text), yaml.safe_load(new)
    assert after['runcmd'] == before['runcmd'] + [module.HOOK]
    assert after['user'] == before['user']
    assert after['timezone'] == before['timezone']
    assert module.add_hook(new) == new


def test_cloud_init_hook_added_to_empty_config():
    assert yaml.safe_load(module.add_hook('#cloud-config\nhostname: test\n'))['runcmd'] == [module.HOOK]


def test_setup_validation():
    expected = {'ssid': 'test-slideshow', 'password': 'test-password-123', 'country': 'ZA'}
    assert {k: module.validate_setup(expected)[k] for k in expected} == expected
    for key, value in [('ssid', 'invalid\nssid'), ('password', 'short'), ('country', 'invalid')]:
        with pytest.raises(ValueError):
            module.validate_setup(dict(expected, **{key: value}))


def test_rejects_invalid_cloud_init():
    for value in ['not cloud config', '#cloud-config\nruncmd: wrong\n']:
        with pytest.raises(ValueError):
            module.add_hook(value)
