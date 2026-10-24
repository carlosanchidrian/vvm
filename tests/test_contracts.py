"""Comprueba que el esqueleto del paquete expone los contratos acordados."""

import vvm
from vvm import audit, cli, host_check, images, vm


def test_package_has_version():
    assert isinstance(vvm.__version__, str) and vvm.__version__


def test_contract_symbols_exist():
    assert callable(host_check.check_host)
    assert callable(host_check.has_blocking)
    assert callable(images.create_disk_fd)
    assert callable(images.validate_boot_files)
    assert callable(vm.build_cmd)
    assert callable(vm.run_vm)
    assert callable(audit.take_snapshot)
    assert callable(audit.build_report)
    assert callable(audit.default_session_path)
    assert callable(audit.save_session)
    assert callable(audit.load_session)
    assert callable(cli.main)
