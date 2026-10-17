"""Pre-write OS failures must not look like a partially completed migration."""
from contextlib import redirect_stdout, redirect_stderr
import errno
from io import StringIO
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config import cutover as cutover_config
from claude_config.core.transaction import FilePlan
from test_framework_cutover import CutoverFixture


class CutoverReportingTests(CutoverFixture):
    def test_lock_failure_reports_errno_and_no_configuration_writes(self):
        manager = self.coordinator()
        plan = manager.plan()
        output, progress = StringIO(), StringIO()
        with patch.object(cutover_config, 'Cutover', return_value=manager), patch.object(manager, 'plan', return_value=plan), \
                patch.object(FilePlan, 'apply', side_effect=OSError(errno.EMFILE, 'PRIVATE_EXCEPTION_TEXT')), \
                redirect_stdout(output), redirect_stderr(progress):
            status = cutover_config.main(['apply', '--spec', str(self.spec_path), '--format', 'json'])
        report = json.loads(output.getvalue())
        self.assertEqual(status, 2)
        self.assertEqual(report['errors'][0]['errno'], errno.EMFILE)
        self.assertEqual(report['errors'][0]['phase'], 'execution')
        self.assertFalse(report['execution']['started'])
        self.assertEqual(report['execution']['status'], 'failed')
        self.assertEqual(report['summary']['applied'], 0)
        self.assertIsNone(report['recovery']['journal'])
        self.assertNotIn('PRIVATE_EXCEPTION_TEXT', output.getvalue())
        self.assertFalse(self.tool.exists())
        self.assertFalse(self.target_data.exists())


if __name__ == '__main__':
    unittest.main()
