from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

from watch_with_me.transcript import (
    choose_segments,
    dialogue_segments,
    extract_with_command,
    parse_srt,
    render_argv,
    render_segment,
    usable_chapters,
)

SRT = """1
00:00:01,000 --> 00:00:03,000
Hello there.

2
00:00:04,500 --> 00:00:06,000
<i>General Kenobi.</i>

3
00:01:50,000 --> 00:01:52,000
A long way later.
"""


def test_parse_srt_accepts_commas_and_strips_tags():
    cues = parse_srt(SRT)
    assert [c["text"] for c in cues] == ["Hello there.", "General Kenobi.", "A long way later."]
    assert cues[0]["start_ms"] == 1000
    assert cues[2]["start_ms"] == 110_000


def test_dialogue_segments_split_on_a_long_silence():
    segments = dialogue_segments(parse_srt(SRT), target_ms=10_000, hard_break_ms=45_000)
    assert len(segments) == 2
    assert segments[0]["lines"][0]["text"] == "Hello there."


def test_segments_do_not_cross_a_hard_break():
    segments = dialogue_segments(parse_srt(SRT), hard_break_ms=30_000)
    assert segments[0]["end_ms"] < 110_000


def test_usable_chapters_requires_several_sane_spans():
    assert not usable_chapters([{"startTimeOffset": 0, "endTimeOffset": 600_000}], 600_000)
    many = [{"startTimeOffset": i * 60_000, "endTimeOffset": (i + 1) * 60_000}
            for i in range(6)]
    assert usable_chapters(many, 360_000)


def test_choose_segments_prefers_chapters_then_dialogue():
    cues = parse_srt(SRT)
    chapters = [{"startTimeOffset": i * 60_000, "endTimeOffset": (i + 1) * 60_000}
                for i in range(5)]
    _, method = choose_segments({"Chapter": chapters, "duration": 300_000}, cues)
    assert method == "chapters"
    _, method = choose_segments({"Chapter": [], "duration": 300_000}, cues)
    assert method == "dialogue_gaps"


def test_render_segment_never_includes_later_lines():
    segment = {"start_ms": 0, "end_ms": 3000, "lines": parse_srt(SRT)}
    rendered = render_segment(segment)
    assert "Hello there." in rendered
    assert "A long way later." not in rendered


def test_extract_with_command_runs_without_a_shell(tmp_path):
    source = tmp_path / "source.srt"
    source.write_text(SRT, encoding="utf-8")
    output = tmp_path / "out.srt"
    ok = extract_with_command("cp {media_path} {output}",
                              media_path=str(source), stream_id="1", output=output)
    assert ok
    assert "Hello there." in output.read_text(encoding="utf-8")


REMOTE_TEMPLATE = 'ssh host "printf %s {media_path}"'


def _remote_sees(media_path: str) -> str:
    """What the remote shell prints, using a real /bin/sh as the simulator.

    shlex.split is not a shell: it leaves backslash escapes that a shell strips,
    so it cannot prove the remote parse is safe. printf is harmless, which keeps
    the test from executing anything if the quoting is wrong.
    """
    argv = render_argv(REMOTE_TEMPLATE, media_path=media_path, stream_id="",
                       output=Path("/tmp/out.srt"))
    assert argv[:2] == ["ssh", "host"]
    assert len(argv) == 3, argv
    return subprocess.run(["sh", "-c", argv[2]], capture_output=True, text=True).stdout


def test_ssh_template_keeps_a_spaced_path_in_one_remote_argument():
    assert _remote_sees("/m/a b/c d.mkv") == "/m/a b/c d.mkv"


def test_ssh_template_survives_an_apostrophe():
    assert _remote_sees("/m/Don't Breathe (2016)/Don't Breathe.mkv") == \
        "/m/Don't Breathe (2016)/Don't Breathe.mkv"


def test_ssh_template_does_not_execute_command_substitution():
    # A real shell would run `whoami` here if the quoting were wrong; the output
    # must be the literal path, not a username.
    assert _remote_sees("/m/A$B `whoami`/C.mkv") == "/m/A$B `whoami`/C.mkv"


def test_extract_command_accepts_a_command_that_streams_to_stdout(tmp_path):
    out = tmp_path / "streamed.srt"
    script = "print('1'); print('00:00:01,000 --> 00:00:02,000'); print('hello')"
    template = "{python} -c \"{script}\"".format(python=sys.executable, script=script)
    ok = extract_with_command(template, media_path="x.mkv", stream_id="1", output=out)
    assert ok
    assert "hello" in out.read_text()


def test_extract_with_empty_template_does_nothing(tmp_path):
    assert not extract_with_command("", media_path="x", stream_id="1",
                                    output=Path(tmp_path / "nope.srt"))


def test_extract_command_treats_a_spaced_path_as_one_token(tmp_path):
    source_dir = tmp_path / "media with spaces"
    source_dir.mkdir()
    source = source_dir / "movie file.mkv"
    source.write_text(SRT, encoding="utf-8")
    output = tmp_path / "out file.srt"
    ok = extract_with_command("cp {media_path} {output}",
                              media_path=str(source), stream_id="1", output=output)
    assert ok
    assert "Hello there." in output.read_text(encoding="utf-8")


def test_extract_command_treats_an_apostrophe_path_as_one_token(tmp_path):
    source = tmp_path / "Don't Breathe.mkv"
    source.write_text(SRT, encoding="utf-8")
    output = tmp_path / "out.srt"
    # Before the quoting fix this raised ValueError: No closing quotation.
    ok = extract_with_command("cp {media_path} {output}",
                              media_path=str(source), stream_id="1", output=output)
    assert ok
    assert "Hello there." in output.read_text(encoding="utf-8")


def test_ssh_style_template_round_trips_a_nasty_path_through_a_shell(tmp_path):
    # ssh concatenates its argv with spaces and the remote login shell re-parses
    # the line; a local sh -c with positional args reproduces that behaviour.
    source_dir = tmp_path / "Don't Breathe (2016)"
    source_dir.mkdir()
    source = source_dir / "movie file.mkv"
    source.write_text(SRT, encoding="utf-8")
    output = tmp_path / "out.srt"
    template = "sh -c 'cat \"$1\"' sh {media_path}"
    ok = extract_with_command(template, media_path=str(source), stream_id="1", output=output)
    assert ok
    assert "Hello there." in output.read_text(encoding="utf-8")
