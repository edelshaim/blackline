from blackline_tool.cli import parse_args


def test_parse_args_accepts_strict_legal_flag() -> None:
    args = parse_args(["a.txt", "b.txt", "--strict-legal"])
    assert args.strict_legal is True


def test_parse_args_accepts_legacy_strict_aliases() -> None:
    underscored = parse_args(["a.txt", "b.txt", "--strict_legal"])
    mode_alias = parse_args(["a.txt", "b.txt", "--strict-legal-mode"])
    assert underscored.strict_legal is True
    assert mode_alias.strict_legal is True

