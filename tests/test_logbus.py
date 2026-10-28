from hammock.logbus import LogBus, strip_ansi


def test_strip_ansi_removes_colors_and_screen_control():
    assert strip_ansi("\x1b[31merror\x1b[0m: boom") == "error: boom"
    assert strip_ansi("\x1b[2J\x1b[3J\x1b[H  VITE v5 ready") == "  VITE v5 ready"
    assert strip_ansi("\x1b]8;;http://x\x07link\x1b]8;;\x07") == "link"


def test_window_is_ordered_across_services():
    bus = LogBus()
    a = bus.publish("api", "one", "stdout")
    bus.publish("pricing", "two", "stderr")
    c = bus.publish("api", "three", "stdout")
    lines = bus.window(a.mono, c.mono)
    assert [l.text for l in lines] == ["one", "two", "three"]


def test_ring_buffer_is_bounded():
    bus = LogBus(maxlen=3)
    for i in range(10):
        bus.publish("api", str(i), "stdout")
    assert [l.text for l in bus.lines("api")] == ["7", "8", "9"]
