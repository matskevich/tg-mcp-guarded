"""Local shared budget for verified ongoing human conversations, not a Telegram limit."""
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import fcntl


class LocalQuotaExceeded(Exception):
    """An application budget blocked a call before sending it to Telegram."""


class ReplyBudget:
    def __init__(self, directory, *, total=None, per_target=None, interval=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'conversation_reply_budget.json'
        self.lock = self.directory / 'conversation_reply_budget.lock'
        self.total = int(os.getenv('MAX_CONVERSATION_REPLIES_PER_DAY', '100') if total is None else total)
        self.per_target = int(os.getenv('MAX_CONVERSATION_REPLIES_PER_TARGET', '20') if per_target is None else per_target)
        self.interval = float(os.getenv('CONVERSATION_REPLY_MIN_INTERVAL_SEC', '10') if interval is None else interval)
        if self.total < 0 or self.per_target < 0 or self.interval < 0:
            raise ValueError('conversation budget values must be nonnegative')

    def _load(self, now):
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        if not self.path.exists():
            return {'date': day, 'used': 0, 'targets': {}}
        try:
            data = json.loads(self.path.read_text())
            if not (isinstance(data['date'], str)
                    and type(data['used']) is int and data['used'] >= 0
                    and isinstance(data['targets'], dict)):
                raise ValueError('invalid counters')
            for target in data['targets'].values():
                if not (type(target['used']) is int and target['used'] >= 0
                        and isinstance(target['last'], (int, float))):
                    raise ValueError('invalid target counter')
        except (ValueError, KeyError, TypeError) as exc:
            raise LocalQuotaExceeded('conversation_reply: invalid budget state; inspect before retry') from exc
        if data['date'] != day:
            return {'date': day, 'used': 0, 'targets': {}}
        return data

    def reserve(self, target_id, *, now=None):
        if type(target_id) is not int or target_id <= 0:
            raise LocalQuotaExceeded('conversation_reply: positive resolved user id required')
        now = time.time() if now is None else now
        target = hashlib.sha256(str(target_id).encode()).hexdigest()
        fd = os.open(self.lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = self._load(now)
            row = data['targets'].get(target, {'used': 0, 'last': 0})
            if data['used'] >= self.total:
                raise LocalQuotaExceeded(f'conversation_reply: daily budget {data["used"]}/{self.total}; resets at 00:00 UTC')
            if row['used'] >= self.per_target:
                raise LocalQuotaExceeded(f'conversation_reply: per_target budget {row["used"]}/{self.per_target}; resets at 00:00 UTC')
            wait = self.interval - (now - row['last'])
            if row['used'] and wait > 0:
                raise LocalQuotaExceeded(f'conversation_reply: interval; retry after {wait:.1f}s')
            data['used'] += 1
            data['targets'][target] = {'used': row['used'] + 1, 'last': now}
            tmp_fd, tmp_path = tempfile.mkstemp(dir=self.directory, prefix='.reply-budget-')
            try:
                with os.fdopen(tmp_fd, 'w') as stream:
                    json.dump(data, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp_path, self.path)
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def stats(self, *, now=None):
        data = self._load(time.time() if now is None else now)
        return {'used': data['used'], 'limit': self.total, 'per_target_limit': self.per_target,
                'min_interval_sec': self.interval, 'date': data['date'], 'reset_timezone': 'UTC',
                'reservation_semantics': 'attempts; no refund on uncertain outcomes'}
