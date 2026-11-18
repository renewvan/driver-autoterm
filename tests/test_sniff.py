from driver_autoterm.protocol import (
    MSG_SETTINGS,
    MSG_STATUS,
    MSG_UNBLOCK,
    Frame,
    FrameParser,
    build_frame,
)
from driver_autoterm.sniff import describe, frame_bytes, is_routine, summarize

POLL = Frame(device=0x03, msg=MSG_STATUS)
POLL_REPLY = Frame(device=0x04, msg=MSG_STATUS, payload=bytes(19))
SETTINGS_READ = Frame(device=0x03, msg=MSG_SETTINGS)
SETTINGS_REPLY = Frame(device=0x04, msg=MSG_SETTINGS, payload=bytes([1, 0, 1, 6, 1, 4]))
SETTINGS_WRITE = Frame(device=0x03, msg=MSG_SETTINGS, payload=bytes([1, 0, 1, 6, 1, 4]))
UNBLOCK = Frame(device=0x03, msg=MSG_UNBLOCK)


def test_polls_and_settings_reads_are_routine_and_everything_else_is_marked():
    for frame in (POLL, POLL_REPLY, SETTINGS_READ, SETTINGS_REPLY):
        assert is_routine(frame)
        assert "not a poll" not in describe(frame)
    for frame in (SETTINGS_WRITE, UNBLOCK, Frame(device=0x03, msg=0x7A)):
        assert not is_routine(frame)
        assert describe(frame).endswith("<-- not a poll")


def test_describe_names_the_sender_message_and_payload():
    assert describe(UNBLOCK).startswith("request msg 0d unblock")
    assert "payload [01 00 01 06 01 04]" in describe(SETTINGS_REPLY)
    assert describe(Frame(device=0x00, msg=0x7A)).startswith("ack     msg 7a unknown")


def test_summary_puts_the_one_off_frame_first():
    frames = [POLL, POLL_REPLY] * 20 + [UNBLOCK] + [POLL, POLL_REPLY] * 20
    lines = summarize(frames)
    assert lines[0].startswith("    1 x  request msg 0d unblock")
    assert len(lines) == 3


def test_a_captured_stream_parses_back_to_its_frames():
    stream = b"".join(
        build_frame(f.msg, f.payload, device=f.device) for f in (POLL, POLL_REPLY, UNBLOCK)
    )
    frames = FrameParser().feed(stream)
    assert frames == [POLL, POLL_REPLY, UNBLOCK]
    assert sum(frame_bytes(f) for f in frames) == len(stream)
