import sys
import types
import unittest


fetchers = types.ModuleType("scrapling.fetchers")
fetchers.DynamicSession = object
fetchers.StealthySession = object
scrapling = types.ModuleType("scrapling")
scrapling.fetchers = fetchers
sys.modules.setdefault("scrapling", scrapling)
sys.modules.setdefault("scrapling.fetchers", fetchers)

playwright = types.ModuleType("playwright")
sync_api = types.ModuleType("playwright.sync_api")
sync_api.Page = object
playwright.sync_api = sync_api
sys.modules.setdefault("playwright", playwright)
sys.modules.setdefault("playwright.sync_api", sync_api)

from ns_signer.attendance import is_attendance_completed


class AttendanceResponseTests(unittest.TestCase):
    def test_already_checked_in_counts_as_completed(self):
        self.assertTrue(
            is_attendance_completed(
                {"success": False, "message": "今天已完成签到，请勿重复操作"}
            )
        )

    def test_unknown_failure_is_not_completed(self):
        self.assertFalse(
            is_attendance_completed({"success": False, "message": "未登录或会话过期"})
        )


if __name__ == "__main__":
    unittest.main()
