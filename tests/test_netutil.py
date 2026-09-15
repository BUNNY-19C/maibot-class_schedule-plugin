"""URL 安全校验（SSRF 防护）与 ICS 抓取测试。

拦截用例全部使用 IP 字面量，不依赖 DNS / 网络，可离线运行。
"""

import unittest

import _bootstrap  # noqa: F401  —— 注册插件包

from class_schedule.ics_parser import decode_ics_bytes
from class_schedule.netutil import (
    UnsafeUrlError,
    _GuardedRedirectHandler,
    fetch_ics,
    validate_url,
)

#: 一个公网 IP 字面量，用于验证"合法地址放行"（不发起真实请求）
PUBLIC_IP_URL = "http://93.184.216.34/calendar.ics"


class TestBlockedUrls(unittest.TestCase):
    def test_non_http_schemes_rejected(self):
        for url in (
            "file:///etc/passwd",
            "ftp://example.com/a.ics",
            "gopher://example.com/",
            "data:text/plain,hello",
            "javascript:alert(1)",
        ):
            with self.subTest(url=url):
                with self.assertRaises(UnsafeUrlError):
                    validate_url(url)

    def test_loopback_rejected(self):
        for url in ("http://127.0.0.1/a.ics", "http://[::1]/a.ics", "http://127.1.2.3/"):
            with self.subTest(url=url):
                with self.assertRaises(UnsafeUrlError):
                    validate_url(url)

    def test_private_ranges_rejected(self):
        for url in (
            "http://10.0.0.5/a.ics",
            "http://172.16.3.4/a.ics",
            "http://192.168.1.10/a.ics",
        ):
            with self.subTest(url=url):
                with self.assertRaises(UnsafeUrlError):
                    validate_url(url)

    def test_cloud_metadata_endpoint_rejected(self):
        """169.254.169.254 是云环境凭据泄露的经典入口。"""
        with self.assertRaises(UnsafeUrlError):
            validate_url("http://169.254.169.254/latest/meta-data/")

    def test_unspecified_and_cgnat_rejected(self):
        with self.assertRaises(UnsafeUrlError):
            validate_url("http://0.0.0.0/a.ics")
        with self.assertRaises(UnsafeUrlError):
            validate_url("http://100.64.0.1/a.ics")

    def test_ipv4_mapped_ipv6_rejected(self):
        """::ffff:127.0.0.1 不能绕过回环检查。"""
        with self.assertRaises(UnsafeUrlError):
            validate_url("http://[::ffff:127.0.0.1]/a.ics")

    def test_localhost_names_rejected(self):
        for url in (
            "http://localhost/a.ics",
            "http://LOCALHOST/a.ics",
            "http://foo.localhost/a.ics",
            "http://printer.local/a.ics",
            "http://wiki.internal/a.ics",
        ):
            with self.subTest(url=url):
                with self.assertRaises(UnsafeUrlError):
                    validate_url(url)

    def test_empty_and_hostless_rejected(self):
        for url in ("", "   ", "http:///path", "https://"):
            with self.subTest(url=url):
                with self.assertRaises(UnsafeUrlError):
                    validate_url(url)


class TestAllowedUrls(unittest.TestCase):
    def test_public_ip_literal_passes(self):
        self.assertEqual(validate_url(PUBLIC_IP_URL), PUBLIC_IP_URL)

    def test_https_public_ip_passes(self):
        self.assertEqual(validate_url("https://1.1.1.1/cal.ics"), "https://1.1.1.1/cal.ics")


class TestRedirectGuard(unittest.TestCase):
    def test_redirect_to_private_address_rejected(self):
        """公网地址 302 到内网时必须被拦下。"""
        handler = _GuardedRedirectHandler()
        with self.assertRaises(UnsafeUrlError):
            handler.redirect_request(
                None, None, 302, "Found", {}, "http://192.168.0.1/secret.ics"
            )

    def test_redirect_to_file_scheme_rejected(self):
        handler = _GuardedRedirectHandler()
        with self.assertRaises(UnsafeUrlError):
            handler.redirect_request(None, None, 302, "Found", {}, "file:///etc/passwd")

    def test_redirect_count_is_capped(self):
        self.assertLessEqual(_GuardedRedirectHandler.max_redirections, 3)


class TestDecode(unittest.TestCase):
    def test_gbk_content_decoded(self):
        """国内教务系统常见 GBK 编码，不能解成乱码。"""
        raw = "高等数学".encode("gb18030")
        self.assertEqual(decode_ics_bytes(raw, ""), "高等数学")

    def test_wrong_charset_header_falls_back(self):
        raw = "高等数学".encode("gb18030")
        self.assertEqual(decode_ics_bytes(raw, "utf-8"), "高等数学")

    def test_utf8_preferred(self):
        raw = "课程 ICS".encode("utf-8")
        self.assertEqual(decode_ics_bytes(raw, "utf-8"), "课程 ICS")


class TestFetchGuards(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_rejects_private_url_without_network(self):
        with self.assertRaises(UnsafeUrlError):
            await fetch_ics("http://127.0.0.1/cal.ics")

    async def test_fetch_rejects_bad_scheme(self):
        with self.assertRaises(UnsafeUrlError):
            await fetch_ics("file:///etc/passwd")


if __name__ == "__main__":
    unittest.main()
