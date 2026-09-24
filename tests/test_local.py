import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from app.config import Config
from app.local import ChildJob, LocalError, check_token, pg_command


async def test_missing_token_is_clear_and_does_not_connect():
    with pytest.raises(LocalError, match='BOT_TOKEN'):
        await check_token(Config())


def test_pg_ctl_uses_file_not_pipe(tmp_path):
    with patch('app.local.LOCAL',tmp_path), patch('app.local.pg_binary',return_value='pg_ctl'), patch('app.local.subprocess.run') as run:
        run.return_value.returncode = 0
        pg_command('pg_ctl','status')
        assert run.call_args.kwargs['stdout'] != subprocess.PIPE
        assert run.call_args.kwargs['stderr'] == subprocess.STDOUT
        assert 'capture_output' not in run.call_args.kwargs


@pytest.mark.skipif(os.name != 'nt', reason='Windows process lifecycle')
def test_windows_job_stops_child_when_launcher_exits():
    job = ChildJob()
    child = subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        job.attach(child)
        assert child.poll() is None
        job.close()
        child.wait(timeout=10)
        assert child.returncode is not None
    finally:
        job.close()
        if child.poll() is None:
            child.terminate()
            child.wait()
