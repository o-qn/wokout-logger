import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workout_logger.backups import BackupManager
from workout_logger.database import Database
from workout_logger.main import app_paths, recover_startup_database


class StartupRecoveryTests(unittest.TestCase):
    def test_corrupt_database_opens_recovery_copy_and_persists_selection(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            source = Database(td / 'source' / 'workouts.db')
            source.seed_exercises()
            backup = BackupManager(source).create_backup()
            broken = td / 'broken' / 'workouts.db'
            broken.parent.mkdir()
            broken.write_bytes(b'corrupted database retained for recovery')
            original = broken.read_bytes()
            with patch('workout_logger.main.select', return_value=backup), patch('workout_logger.main.console'):
                recovered = recover_startup_database(broken, 'test corruption')
            self.assertIsNotNone(recovered)
            self.assertEqual(broken.read_bytes(), original)
            self.assertEqual(Database(recovered).fetchone('SELECT COUNT(*) FROM exercises')[0], 11)
            with patch.dict('os.environ', {'WORKOUT_LOGGER_DATA_DIR': str(broken.parent)}):
                self.assertEqual(app_paths()[0], recovered)
            self.assertEqual(json.loads((broken.parent / 'active-recovery.json').read_text())['database'], recovered.name)

    def test_bad_pointer_cannot_select_an_external_or_nonrecovery_database(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            pointer = td / 'active-recovery.json'
            for filename in ('../another.db', 'workouts.db', 'recovered-missing.db'):
                pointer.write_text(json.dumps({'database': filename}))
                with patch.dict('os.environ', {'WORKOUT_LOGGER_DATA_DIR': str(td)}):
                    self.assertEqual(app_paths()[0], td / 'workouts.db')


if __name__ == '__main__':
    unittest.main()
