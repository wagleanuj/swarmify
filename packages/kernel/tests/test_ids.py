from swarmify_kernel import new_id


def test_new_id_has_prefix_and_26_char_ulid() -> None:
    value = new_id("run")
    prefix, _, ulid = value.partition("_")
    assert prefix == "run"
    assert len(ulid) == 26
    assert set(ulid) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def test_new_ids_are_unique_and_time_ordered() -> None:
    ids = [new_id("x") for _ in range(100)]
    assert len(set(ids)) == 100
    assert ids[0][:12] <= ids[-1][:12]
