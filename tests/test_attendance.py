import unittest

import _stubs  # noqa: F401  (installs the browser-stack stand-ins)

from ns_signer.attendance import is_attendance_completed, is_session_expired


class AttendanceResponseTests(unittest.TestCase):
    def test_already_checked_in_counts_as_completed(self):
        self.assertTrue(
            is_attendance_completed(
                {"success": False, "message": "今天已完成签到，请勿重复操作"}
            )
        )

    def test_unknown_failure_is_not_completed(self):
        self.assertFalse(
            is_attendance_completed({"success": False, "message": "服务器开小差了"})
        )


class SessionExpiryTests(unittest.TestCase):
    """A dead session and a failed check-in need different answers, so they are told apart."""

    def test_unauthorized_status_means_the_session_is_gone(self):
        self.assertTrue(is_session_expired({"status": 403, "body": None}))

    def test_not_logged_in_message_means_the_session_is_gone(self):
        self.assertTrue(
            is_session_expired({"status": 200, "body": {"success": False, "message": "未登录"}})
        )

    def test_ordinary_failure_does_not_look_like_an_expired_session(self):
        self.assertFalse(
            is_session_expired({"status": 200, "body": {"success": False, "message": "服务器开小差了"}})
        )

    def test_a_successful_check_in_is_never_an_expired_session(self):
        self.assertFalse(is_session_expired({"status": 200, "body": {"success": True}}))


if __name__ == "__main__":
    unittest.main()
