from wordmatch import match_words


def test_script_spelling_wins_and_times_come_from_the_audio():
    heard = [("hello", 0.1, 0.4), (" There.", 0.5, 0.8)]
    assert match_words("Hello, there!", heard, 1.0) == [
        ("Hello,", 0.1, 0.4),
        ("there!", 0.5, 0.8),
    ]


def test_a_word_not_heard_is_placed_between_its_neighbours():
    heard = [("the", 0.0, 0.2), ("hedgehog", 0.3, 0.9), ("sleeps", 1.4, 1.8)]
    assert match_words("The hedgehog quietly sleeps", heard, 2.0) == [
        ("The", 0.0, 0.2),
        ("hedgehog", 0.3, 0.9),
        ("quietly", 0.9, 1.4),
        ("sleeps", 1.4, 1.8),
    ]


def test_unheard_words_at_the_edges_share_the_audio_up_to_its_start_and_end():
    assert match_words("Good night", [], 1.5) == [("Good", 0.0, 0.75), ("night", 0.75, 1.5)]


def test_two_words_heard_as_one_split_its_span():
    heard = [("Goodnight,", 0.0, 0.5), ("little", 0.5, 0.7)]
    assert match_words("Good night, little", heard, 1.0) == [
        ("Good", 0.0, 0.25),
        ("night,", 0.25, 0.5),
        ("little", 0.5, 0.7),
    ]
