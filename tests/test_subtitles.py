from backend.app.main import fmt_srt, normalize, srt_text, vtt_text

def test_timestamp_format():
    assert fmt_srt(1.234) == "00:00:01,234"

def test_segment_bounds_clamp_to_duration():
    actual = normalize([{"id":1,"start":-1,"end":90,"text":"hello"}], duration=15)
    assert actual == [{"id":1,"start":0.0,"end":15.0,"text":"hello"}]

def test_srt_roundtrip_shape_and_vtt_header():
    segs = [{"id":1,"start":1.25,"end":2.5,"text":"hello"}]
    assert "00:00:01,250 --> 00:00:02,500" in srt_text(segs)
    assert vtt_text(segs).startswith("WEBVTT")
