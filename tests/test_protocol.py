from driver_autoterm.protocol import FrameParser, build_frame, crc16_modbus, parse_status

# F1: the one real AIR 2D capture (status 3.35, fan only), CRC-valid.
F1 = bytes.fromhex("aa0413000f0323001c7f0084012d08004646000000000062f369")
# F7: stop request from the upstream README.
F7_STOP = bytes.fromhex("aa030000035d7c")
# F12: 4D 10-byte status, must be rejected.
F12 = bytes.fromhex("aa040a000f00011e187f007c0142003421")


def test_builds_documented_request_frames():
    assert build_frame(0x03) == F7_STOP
    assert build_frame(0x23, bytes.fromhex("ffff08ff")) == bytes.fromhex("aa03040023ffff08ffe10b")


def test_crc_matches_real_capture():
    assert crc16_modbus(F1[:-2]).to_bytes(2, "big") == F1[-2:]


def test_parser_reassembles_split_reads_and_resyncs_after_garbage():
    parser = FrameParser()
    out = parser.feed(b"\x00\x13" + F1[:7]) + parser.feed(F1[7:15]) + parser.feed(F1[15:])
    assert len(out) == 1 and out[0].msg == 0x0F
    status = parse_status(out[0].payload)
    assert (status.status_code, status.voltage_v, status.fan_rpm) == ("3.35", 13.2, 4200)
    assert status.heater_temperature_c == 30 and status.external_temperature_c is None


def test_four_d_ten_byte_status_is_not_parsed():
    frames = FrameParser().feed(F12)
    assert len(frames) == 1 and parse_status(frames[0].payload) is None
