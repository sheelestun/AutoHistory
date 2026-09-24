"""Retention maintenance. Run daily after verifying independent backup success."""
import time
from datetime import date, timedelta
from pathlib import Path

from app.config import Config


def main():
    config=Config.load()
    for path in Path(config.pdf_tmp_dir).glob('report-*.pdf'):
        if path.stat().st_mtime < time.time()-3600:
            path.unlink(missing_ok=True)
    cutoff=date.today()-timedelta(days=7)
    for path in Path(config.deletion_journal_dir).glob('*.jsonl'):
        try:
            day=date.fromisoformat(path.stem)
        except ValueError:
            continue
        if day < cutoff:
            path.unlink(missing_ok=True)


if __name__=='__main__':
    main()
