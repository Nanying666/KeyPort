"""上游错误分类：人机验证挑战（3007）应识别为 CAPTCHA 而非 BAD_REQUEST。"""

from __future__ import annotations

from keyport.upstream.base import ErrorKind, UpstreamError, classify_status

# 真实上游响应体（zcode.z.ai Coding Plan 网关）
CAPTCHA_BODY = '{"code":3007,"msg":"captcha verify failed","logid":"20260928002028e1991f398fc7e5005c57"}'


def test_classify_captcha_challenge():
    assert classify_status(400, CAPTCHA_BODY) == ErrorKind.CAPTCHA


def test_classify_captcha_case_insensitive():
    assert classify_status(400, '{"code":3007,"msg":"Captcha Verify Failed"}') == ErrorKind.CAPTCHA


def test_classify_captcha_by_code_plus_word():
    assert classify_status(400, '{"code":3007,"msg":"captcha required"}') == ErrorKind.CAPTCHA


def test_plain_400_stays_bad_request():
    """普通 400 不应被误判为验证码。"""
    assert classify_status(400, '{"error":{"message":"invalid model"}}') == ErrorKind.BAD_REQUEST
    assert classify_status(400, "") == ErrorKind.BAD_REQUEST


def test_3007_in_logid_only_is_not_captcha():
    """仅 logid 里含 3007、无 captcha 字样时不应误判。"""
    body = '{"code":400,"msg":"bad request","logid":"3007abcdef"}'
    assert classify_status(400, body) == ErrorKind.BAD_REQUEST


def test_captcha_does_not_failover():
    """换号也无用（同域共用端点），故不触发故障转移。"""
    err = UpstreamError(ErrorKind.CAPTCHA, 400, "captcha verify failed")
    assert err.should_failover is False


def test_bad_request_still_does_not_failover():
    assert UpstreamError(ErrorKind.BAD_REQUEST, 400).should_failover is False


def test_rate_limit_still_failovers():
    assert UpstreamError(ErrorKind.RATE_LIMIT, 429).should_failover is True
