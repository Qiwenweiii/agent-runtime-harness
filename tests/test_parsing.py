from parsing import parse_tool_args


class TestValidJSON:
    def test_plain_object(self):
        args, err = parse_tool_args('{"service_name": "api", "lines": 20}')
        assert args == {"service_name": "api", "lines": 20}
        assert err is None

    def test_empty_object(self):
        args, err = parse_tool_args("{}")
        assert args == {}
        assert err is None

    # 空串/None 走源码里的 `raw or "{}"` 兜底，视为合法空参数
    def test_empty_string_falls_back_to_dict(self):
        args, err = parse_tool_args("")
        assert args == {}
        assert err is None

    def test_none_falls_back_to_dict(self):
        args, err = parse_tool_args(None)
        assert args == {}
        assert err is None


class TestInvalidJSON:
    def test_broken_json_returns_error_not_exception(self):
        args, err = parse_tool_args('{"service_name": ')
        assert args is None
        assert err is not None and "不是合法 JSON" in err

    def test_plain_text(self):
        args, err = parse_tool_args("not json at all")
        assert args is None
        assert "不是合法 JSON" in err


class TestNonDictJSON:
    def test_json_list(self):
        args, err = parse_tool_args("[1, 2, 3]")
        assert args is None
        assert err is not None and "JSON object" in err and "list" in err

    def test_json_number(self):
        args, err = parse_tool_args("42")
        assert args is None
        assert "int" in err

    def test_json_string(self):
        args, err = parse_tool_args('"hello"')
        assert args is None
        assert "str" in err

    def test_json_null(self):
        args, err = parse_tool_args("null")
        assert args is None
        assert "NoneType" in err
