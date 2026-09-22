import unittest

import requests

import bili_api


class FakeResponse:
    def __init__(self, body=None, status_code=200):
        self._body = body if body is not None else {"code": 0, "data": 0}
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self._body


class FakeHttpSession:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse()
        self.error = error
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.error:
            raise self.error
        return self.response


def make_session(http=None):
    cookie = bili_api.CookieInfo("sess", "csrf", "123")
    session = bili_api.BiliSession(cookie)
    session.session = http or FakeHttpSession()
    session.write_interval = 0
    session._write_log = lambda *args, **kwargs: None
    return session


class BatchApiTests(unittest.TestCase):
    def test_resource_limit_and_deduplication(self):
        clean, encoded = bili_api.BiliSession._batch_resources([1, 2, 1])
        self.assertEqual(clean, [1, 2])
        self.assertEqual(encoded, "1:2,2:2")
        with self.assertRaises(bili_api.BiliApiError):
            bili_api.BiliSession._batch_resources(list(range(1, 1002)))

    def test_move_batch_payload(self):
        http = FakeHttpSession()
        session = make_session(http)
        count = session.move_batch("10", "20", [101, 102], mid="123")
        self.assertEqual(count, 2)
        method, url, kwargs = http.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/x/v3/fav/resource/move"))
        self.assertEqual(kwargs["data"]["src_media_id"], "10")
        self.assertEqual(kwargs["data"]["tar_media_id"], "20")
        self.assertEqual(kwargs["data"]["resources"], "101:2,102:2")

    def test_batch_delete_payload(self):
        http = FakeHttpSession()
        session = make_session(http)
        count = session.batch_delete("10", [101, 102])
        self.assertEqual(count, 2)
        _, url, kwargs = http.calls[0]
        self.assertTrue(url.endswith("/x/v3/fav/resource/batch-del"))
        self.assertEqual(kwargs["data"]["media_id"], "10")

    def test_rename_and_delete_folder_payloads(self):
        http = FakeHttpSession()
        session = make_session(http)
        session.rename_folder("10", "新名称")
        session.delete_folder("11")
        session.clean_invalid_folder("12")
        self.assertTrue(http.calls[0][1].endswith("/x/v3/fav/folder/edit"))
        self.assertEqual(http.calls[0][2]["data"]["title"], "新名称")
        self.assertTrue(http.calls[1][1].endswith("/x/v3/fav/folder/del"))
        self.assertEqual(http.calls[1][2]["data"]["media_ids"], "11")
        self.assertTrue(http.calls[2][1].endswith("/x/v3/fav/resource/clean"))
        self.assertEqual(http.calls[2][2]["data"]["media_id"], "12")

    def test_business_error_is_definite_failure(self):
        http = FakeHttpSession(FakeResponse({"code": -400, "message": "bad"}))
        session = make_session(http)
        with self.assertRaises(bili_api.BiliApiError) as ctx:
            session.move_batch("10", "20", [101], mid="123")
        self.assertNotIsInstance(ctx.exception, bili_api.WriteUncertainError)

    def test_rate_limit_is_not_unknown(self):
        http = FakeHttpSession(FakeResponse(status_code=412))
        session = make_session(http)
        with self.assertRaises(bili_api.RateLimitedError):
            session.move_batch("10", "20", [101], mid="123")

    def test_timeout_is_unknown(self):
        http = FakeHttpSession(error=requests.Timeout("timeout"))
        session = make_session(http)
        with self.assertRaises(bili_api.WriteUncertainError):
            session.move_batch("10", "20", [101], mid="123")


if __name__ == "__main__":
    unittest.main()
