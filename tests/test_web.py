from app.web import split_lines


def test_split_lines_accepts_lines_and_commas():
    assert split_lines("Review\nDone,Blocked\n\n") == ["Review", "Done", "Blocked"]
